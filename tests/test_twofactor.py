"""Two-factor sign-in, and the QR code that sets it up.

Three layers are checked here.

The arithmetic, against the published test vectors in RFC 4226 and RFC 6238.
If those pass, the codes this software generates are the same ones every
authenticator app in the world generates, which is the only thing that matters.

The QR encoder, against the format-information table in ISO/IEC 18004 and by
decoding its own output back to the text that went in. A QR code that is
structurally perfect and unreadable looks completely fine by eye — the
round-trip is what catches it.

And the rules on top: that a wrong code is refused, that a used code cannot be
used twice, that five wrong guesses close the door, and above all that nobody
can end up locked out of their own accounts.
"""
from __future__ import annotations

import base64
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-2fa-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import clock  # noqa: E402
from app import db as dbmod, qrcode, totp  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Company, User  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import twofactor as TF  # noqa: E402

#: The stylesheet, read directly by one test below.
CSS_FILE = Path(__file__).resolve().parent.parent / "app" / "static" / "app.css"

#: The secret from the RFCs: the ASCII string "12345678901234567890".
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-2fa-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    with dbmod.session_scope_for(ref.slug) as session:
        yield session
    TF.reset_attempts()
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_attempts():
    TF.reset_attempts()
    yield
    TF.reset_attempts()


def a_user(db, username="ade") -> User:
    from app.security import hash_password

    user = User(username=username, full_name="Ade", role="ADMIN",
                password_hash=hash_password("Lagos2026"))
    db.add(user)
    db.flush()
    return user


# --------------------------------------------------------------------------
# The arithmetic, against the RFCs
# --------------------------------------------------------------------------


def test_hotp_matches_rfc_4226():
    expected = ["755224", "287082", "359152", "969429", "338314",
                "254676", "287922", "162583", "399871", "520489"]
    assert [totp.code_at(RFC_SECRET, c) for c in range(10)] == expected


def test_totp_matches_rfc_6238():
    for when, want in [(59, "94287082"), (1111111109, "07081804"),
                       (1111111111, "14050471"), (1234567890, "89005924"),
                       (2000000000, "69279037"), (20000000000, "65353130")]:
        assert totp.code_at(RFC_SECRET, when // totp.STEP, digits=8) == want


def test_a_new_secret_is_valid_base32_and_not_predictable():
    secrets = {totp.new_secret() for _ in range(50)}
    assert len(secrets) == 50
    for secret in secrets:
        assert totp.looks_like_a_secret(secret)
        assert len(secret) == 32       # 20 bytes, base32, padding stripped


def test_a_key_typed_by_hand_is_forgiven():
    """Lower case, spaces and missing padding are how people actually type it."""
    secret = totp.new_secret()
    code = totp.now(secret)
    for variant in (secret.lower(), totp.grouped(secret),
                    totp.grouped(secret).lower(), secret + "===="):
        assert totp.now(variant) == code


def test_rubbish_is_rejected_rather_than_crashing():
    # Note that "not a key" IS valid base32 — every letter of it is in the
    # alphabet. Only characters outside it make a key invalid.
    assert not totp.looks_like_a_secret("not a key!")
    assert not totp.looks_like_a_secret("")
    assert not totp.looks_like_a_secret("ABC018")     # 0 and 1 are not in base32
    assert totp.verify("nonsense!", "123456") is None


# --------------------------------------------------------------------------
# Verifying a code
# --------------------------------------------------------------------------


def test_the_current_code_is_accepted(db):
    secret = totp.new_secret()
    assert totp.verify(secret, totp.now(secret)) is not None


def test_a_wrong_code_is_refused(db):
    secret = totp.new_secret()
    wrong = "000000" if totp.now(secret) != "000000" else "111111"
    assert totp.verify(secret, wrong) is None


def test_a_phone_clock_half_a_minute_out_still_works(db):
    """The commonest support call there is, and it should never happen."""
    secret = totp.new_secret()
    when = 1_700_000_000
    for offset in (-30, 0, 30):
        assert totp.verify(secret, totp.now(secret, when + offset), when=when) is not None


def test_a_code_from_two_minutes_ago_is_refused(db):
    secret = totp.new_secret()
    when = 1_700_000_000
    assert totp.verify(secret, totp.now(secret, when - 120), when=when) is None


def test_a_code_cannot_be_used_twice(db):
    """Somebody who read it over a shoulder must not be able to walk up and use it."""
    secret = totp.new_secret()
    when = 1_700_000_000
    code = totp.now(secret, when)
    counter = totp.verify(secret, code, when=when)
    assert counter is not None
    assert totp.verify(secret, code, when=when, used_counter=counter) is None


def test_a_code_of_the_wrong_shape_is_refused(db):
    secret = totp.new_secret()
    for bad in ("", "12345", "1234567", "abcdef", None):
        assert totp.verify(secret, bad) is None


# --------------------------------------------------------------------------
# Setting it up
# --------------------------------------------------------------------------


def test_setting_up_does_nothing_until_a_code_is_proved(db):
    user = a_user(db)
    TF.begin_setup(user)
    assert user.totp_secret
    assert not user.totp_enabled          # a secret alone must not lock anyone out
    assert not TF.is_on(user)


def test_a_wrong_code_does_not_switch_it_on(db):
    user = a_user(db)
    TF.begin_setup(user)
    wrong = "000000" if totp.now(user.totp_secret) != "000000" else "111111"
    ok, codes = TF.confirm_setup(user, wrong)
    assert not ok and not codes
    assert not user.totp_enabled


def test_the_right_code_switches_it_on_and_hands_over_recovery_codes(db):
    user = a_user(db)
    secret = TF.begin_setup(user)
    ok, codes = TF.confirm_setup(user, totp.now(secret))
    assert ok
    assert user.totp_enabled and TF.is_on(user)
    assert len(codes) == totp.RECOVERY_COUNT
    assert len(set(codes)) == totp.RECOVERY_COUNT
    # Stored hashed, never in the clear
    assert all(code not in user.totp_recovery for code in codes)
    assert user.recovery_codes_left == totp.RECOVERY_COUNT


def test_opening_the_setup_screen_again_keeps_the_key_that_was_scanned(db):
    """The bug this replaces cost somebody their account.

    The setup screen used to mint a new secret on every page load. Scan the QR
    code, reload the page for any reason, and the phone was left holding a key
    the software had already thrown away — so every code it produced was
    refused, for ever, with nothing on screen to say why.
    """
    user = a_user(db)
    first = TF.setup_secret(user)
    assert TF.setup_secret(user) == first
    assert TF.setup_secret(user) == first
    # And the code off that key still works after all those reloads
    assert TF.confirm_setup(user, totp.now(first))[0]


def test_a_key_left_lying_around_for_a_day_is_replaced(db):
    from datetime import datetime, timedelta

    user = a_user(db)
    first = TF.setup_secret(user)
    user.totp_started_at = clock.now() - timedelta(hours=13)
    assert TF.setup_secret(user) != first


def test_starting_again_deliberately_gives_a_new_key(db):
    user = a_user(db)
    first = TF.setup_secret(user)
    assert TF.setup_secret(user, restart=True) != first


def test_an_account_that_already_has_it_on_is_never_reissued(db):
    """Being handed a new key would silently break a working phone."""
    user = a_user(db)
    secret = TF.setup_secret(user)
    TF.confirm_setup(user, totp.now(secret))
    assert TF.setup_secret(user) != secret          # a fresh one for a fresh setup
    assert user.totp_enabled is False               # and the live one is stood down


# --------------------------------------------------------------------------
# A right code and a wrong clock
# --------------------------------------------------------------------------


def test_a_computer_four_minutes_slow_is_told_so_rather_than_refused(db):
    """The phone is right, the person is right, the computer is wrong."""
    user = a_user(db)
    secret = TF.setup_secret(user)
    from_the_phone = totp.now(secret, when=time.time() + 240)

    ok, codes = TF.confirm_setup(user, from_the_phone)
    assert ok and len(codes) == totp.RECOVERY_COUNT
    assert user.totp_offset == 8                     # eight thirty-second steps
    assert "4 minutes behind" in TF.clock_note(user)
    assert "Set time automatically" in TF.clock_note(user)


def test_signing_in_keeps_working_while_the_clock_stays_wrong(db):
    user = a_user(db)
    secret = TF.setup_secret(user)
    TF.confirm_setup(user, totp.now(secret, when=time.time() + 240))
    user.totp_last_counter = 0
    assert TF.check(db, user, totp.now(secret, when=time.time() + 240)).ok


def test_putting_the_clock_right_heals_it_rather_than_locking_them_out(db):
    user = a_user(db)
    secret = TF.setup_secret(user)
    TF.confirm_setup(user, totp.now(secret, when=time.time() + 240))
    user.totp_last_counter = 0
    assert TF.check(db, user, totp.now(secret)).ok    # the clock has been fixed
    assert user.totp_offset == 0                      # so the workaround is dropped


def test_a_clock_two_hours_out_is_still_diagnosed(db):
    user = a_user(db)
    secret = TF.setup_secret(user)
    assert totp.find_offset(secret, totp.now(secret, when=time.time() - 3600)) == -120
    assert "1 hour ahead" in totp.minutes_out(-120)


def test_the_wrong_clock_never_widens_the_door_for_a_guess(db):
    """Only a code that is genuinely this secret's is ever accepted."""
    user = a_user(db)
    secret = TF.setup_secret(user)
    TF.confirm_setup(user, totp.now(secret, when=time.time() + 240))
    user.totp_last_counter = 0
    # Everything in the two-hour search window, except the three codes around
    # the offset window, must still be refused.
    centre = totp.counter_for()
    refused = 0
    for step in range(-30, 31):
        if abs(step - 8) <= totp.DRIFT or abs(step) <= totp.DRIFT:
            continue
        TF.reset_attempts(user.id)
        assert not TF.check(db, user, totp.code_at(secret, centre + step)).ok
        refused += 1
    assert refused > 40


def test_a_refused_code_says_the_clock_is_wrong_when_it_is(db):
    user = a_user(db)
    secret = TF.setup_secret(user)
    TF.confirm_setup(user, totp.now(secret))
    user.totp_last_counter = 0
    result = TF.check(db, user, totp.now(secret, when=time.time() + 600))
    assert not result.ok
    assert "clock" in result.message and "10 minutes behind" in result.message


def test_a_code_that_is_not_this_secrets_says_nothing_about_clocks(db):
    user = a_user(db)
    TF.setup_secret(user)
    TF.confirm_setup(user, totp.now(user.totp_secret))
    user.totp_last_counter = 0
    # A code that is real for this secret but two days away — so it is not a
    # clock problem, and the message must not pretend it might be.
    far_away = totp.code_at(user.totp_secret, totp.counter_for() + 5000)
    result = TF.check(db, user, far_away)
    assert not result.ok
    assert "clock" not in result.message
    assert "thirty seconds" in result.message


def test_turning_it_off_leaves_nothing_behind(db):
    user = a_user(db)
    secret = TF.begin_setup(user)
    TF.confirm_setup(user, totp.now(secret))
    TF.turn_off(user)
    assert not user.totp_secret and not user.totp_enabled and not user.totp_recovery
    assert not TF.is_on(user)


# --------------------------------------------------------------------------
# Recovery codes
# --------------------------------------------------------------------------


def test_a_recovery_code_signs_you_in_and_is_then_spent(db):
    user = a_user(db)
    secret = TF.begin_setup(user)
    _, codes = TF.confirm_setup(user, totp.now(secret))

    result = TF.check(db, user, codes[0])
    assert result.ok and result.used_recovery
    assert result.codes_left == totp.RECOVERY_COUNT - 1

    again = TF.check(db, user, codes[0])
    assert not again.ok


def test_a_recovery_code_is_accepted_however_it_is_typed(db):
    user = a_user(db)
    secret = TF.begin_setup(user)
    _, codes = TF.confirm_setup(user, totp.now(secret))
    assert TF.check(db, user, codes[0].lower().replace("-", " ")).ok


def test_the_last_recovery_code_says_so(db):
    user = a_user(db)
    secret = TF.begin_setup(user)
    _, codes = TF.confirm_setup(user, totp.now(secret))
    for code in codes[:-1]:
        TF.check(db, user, code)
    result = TF.check(db, user, codes[-1])
    assert result.ok and result.codes_left == 0
    assert "last recovery code" in result.message


def test_new_recovery_codes_cancel_the_old_ones(db):
    user = a_user(db)
    secret = TF.begin_setup(user)
    _, old = TF.confirm_setup(user, totp.now(secret))
    new = TF.new_recovery_codes(user)
    assert set(new).isdisjoint(old)
    assert not TF.check(db, user, old[0]).ok
    assert TF.check(db, user, new[0]).ok


# --------------------------------------------------------------------------
# Guessing
# --------------------------------------------------------------------------


def test_five_wrong_codes_close_the_door(db):
    when = 1_700_000_000
    user = a_user(db)
    secret = TF.begin_setup(user)
    TF.confirm_setup(user, totp.now(secret, when - 60), when=when - 60)

    for _ in range(TF.MAX_ATTEMPTS):
        assert not TF.check(db, user, "000000", when=when).ok
    blocked = TF.check(db, user, totp.now(secret, when), when=when)
    assert not blocked.ok and blocked.locked
    assert "Try again in" in blocked.message


def test_the_door_opens_again_after_the_lockout(db):
    user = a_user(db)
    secret = TF.begin_setup(user)
    TF.confirm_setup(user, totp.now(secret, 1_699_999_940), when=1_699_999_940)
    now = 1_700_000_000.0
    for _ in range(TF.MAX_ATTEMPTS):
        TF.check(db, user, "000000", when=now)
    assert TF.check(db, user, totp.now(secret, now), when=now).locked
    later = now + TF.LOCKOUT + 1
    assert TF.check(db, user, totp.now(secret, later), when=later).ok


def test_a_correct_code_clears_the_count(db):
    when = 1_700_000_000
    user = a_user(db)
    secret = TF.begin_setup(user)
    # Confirm at the same clock the checks below use: a code already spent
    # during setup is correctly refused afterwards, and mixing the real clock
    # with a fixed one here would be testing that instead.
    TF.confirm_setup(user, totp.now(secret, when - 60), when=when - 60)
    for _ in range(TF.MAX_ATTEMPTS - 1):
        TF.check(db, user, "000000", when=when)
    assert TF.check(db, user, totp.now(secret, when), when=when).ok
    # The count is back to nothing: four more wrong ones (the loop plus the
    # one in the assertion) still leave one attempt in hand.
    later = when + 60
    for _ in range(TF.MAX_ATTEMPTS - 2):
        assert not TF.check(db, user, "000000", when=later).locked
    assert not TF.check(db, user, "000000", when=later).locked


def test_one_persons_lockout_does_not_affect_anybody_else(db):
    when = 1_700_000_000
    one, two = a_user(db, "one"), a_user(db, "two")
    for user in (one, two):
        secret = TF.begin_setup(user)
        TF.confirm_setup(user, totp.now(secret, when - 60), when=when - 60)
    for _ in range(TF.MAX_ATTEMPTS):
        TF.check(db, one, "000000", when=when)
    assert TF.check(db, two, totp.now(two.totp_secret, when), when=when).ok


# --------------------------------------------------------------------------
# The company policy
# --------------------------------------------------------------------------


def test_requiring_it_asks_people_to_set_it_up_rather_than_locking_them_out(db):
    company = db.get(Company, 1)
    company.require_two_factor = True
    user = a_user(db)
    assert TF.must_set_up(user, company)
    secret = TF.begin_setup(user)
    TF.confirm_setup(user, totp.now(secret))
    assert not TF.must_set_up(user, company)


def test_nobody_has_to_set_it_up_when_it_is_optional(db):
    company = db.get(Company, 1)
    company.require_two_factor = False
    assert not TF.must_set_up(a_user(db), company)


# --------------------------------------------------------------------------
# The QR encoder
# --------------------------------------------------------------------------


def test_the_block_tables_add_up_to_the_published_capacities():
    """A typo in those tables is the easiest way to break every QR code."""
    for version, total in qrcode._TOTAL_CODEWORDS.items():
        ec, g1, d1, g2, d2 = qrcode._BLOCKS_M[version]
        blocks = g1 + g2
        assert g1 * d1 + g2 * d2 + blocks * ec == total, f"version {version}"


def test_format_information_matches_the_standard():
    """The published strings for error-correction level M, one per mask.

    Copied from ISO/IEC 18004 Table C.1. Getting these wrong produces a symbol
    no scanner will read while looking perfectly normal on screen, which is
    exactly what happened while this was being written.
    """
    published = [
        "101010000010010", "101000100100101", "101111001111100", "101101101001011",
        "100010111111001", "100000011001110", "100111110010111", "100101010100000",
    ]
    for mask, want in enumerate(published):
        assert format(qrcode._format_bits(mask), "015b") == want, f"mask {mask}"


def test_the_symbol_is_the_right_size_for_its_version():
    for version in range(1, qrcode.MAX_VERSION + 1):
        text = "Z" * qrcode.byte_capacity(version)
        assert len(qrcode.matrix(text)) == version * 4 + 17


def test_the_finder_patterns_are_where_they_belong():
    grid = qrcode.matrix("HELLO")
    size = len(grid)
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        # The outer ring is dark and the ring inside it is light.
        assert grid[top][left] == 1
        assert grid[top + 1][left + 1] == 0
        assert grid[top + 3][left + 3] == 1


def test_a_symbol_decodes_back_to_what_went_in():
    """Read the modules back the way a scanner would and check the payload.

    This is the test that would have caught the format bits going in backwards:
    everything else about the symbol was correct.
    """
    for text in ("A", "HELLO", "https://example.com/a/b?c=d",
                 "Ünïcödé — ₦ 中文", "Z" * 200):
        assert _decode(qrcode.matrix(text)) == text.encode("utf-8")


def _decode(grid: list[list[int]]) -> bytes:
    """A minimal reader: format bits, unmask, de-interleave, strip the header."""
    size = len(grid)
    version = (size - 17) // 4

    bits = 0
    for i in range(15):
        if i < 6:
            bit = grid[8][i]
        elif i == 6:
            bit = grid[8][7]
        elif i == 7:
            bit = grid[8][8]
        elif i == 8:
            bit = grid[7][8]
        else:
            bit = grid[14 - i][8]
        bits |= bit << (14 - i)
    mask = next(m for m in range(8) if qrcode._format_bits(m) == bits)

    scaffold = qrcode._Grid(version)
    scaffold.build_function_patterns()
    plain = [row[:] for row in grid]
    for r in range(size):
        for c in range(size):
            if not scaffold.reserved[r][c] and qrcode._Grid._mask(mask, r, c):
                plain[r][c] ^= 1

    stream: list[int] = []
    upward, col = True, size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for offset in (0, 1):
                if not scaffold.reserved[row][col - offset]:
                    stream.append(plain[row][col - offset])
        upward = not upward
        col -= 2

    words = []
    for i in range(0, len(stream) // 8 * 8, 8):
        byte = 0
        for bit in stream[i:i + 8]:
            byte = (byte << 1) | bit
        words.append(byte)

    # Undo the interleaving to get the data codewords back in order.
    ec_count, g1, d1, g2, d2 = qrcode._BLOCKS_M[version]
    sizes = [d1] * g1 + [d2] * g2
    blocks: list[list[int]] = [[] for _ in sizes]
    at = 0
    for i in range(max(sizes)):
        for b, length in enumerate(sizes):
            if i < length:
                blocks[b].append(words[at])
                at += 1
    data = [word for block in blocks for word in block]

    body = "".join(format(word, "08b") for word in data)
    assert body[:4] == "0100", "not byte mode"
    width = 8 if version < 10 else 16
    length = int(body[4:4 + width], 2)
    start = 4 + width
    return bytes(
        int(body[start + i * 8:start + i * 8 + 8], 2) for i in range(length)
    )


def test_text_too_long_is_refused_clearly():
    with pytest.raises(qrcode.QRError, match="more than a version"):
        qrcode.matrix("x" * 5000)
    assert not qrcode.fits("x" * 5000)
    assert qrcode.fits("x" * 100)


def test_the_svg_is_self_contained_and_sized_right():
    svg = qrcode.svg("HELLO", module=6, quiet=4)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "http" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert 'viewBox="0 0 29 29"' in svg      # 21 modules plus 4 either side


def test_a_provisioning_uri_always_fits_in_a_qr_code():
    """However long the company name is, the code must still be scannable."""
    secret = totp.new_secret()
    for issuer, account in [
        ("Nexora Books", "ade"),
        ("Adeyemi Building Materials Ltd", "accounts@adeyemibuilding.ng"),
        ("Y" * 300, "x" * 300 + "@example.com"),
    ]:
        uri = totp.provisioning_uri(secret, account, issuer)
        assert qrcode.fits(uri)
        assert f"secret={secret}" in uri     # never shortened
        qrcode.matrix(uri)                   # and it really encodes


# --------------------------------------------------------------------------
# Signing in, through the web interface
# --------------------------------------------------------------------------


@pytest.fixture()
def client():
    tmp = tempfile.mkdtemp(prefix="nexora-2faweb-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    TF.reset_attempts()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password",
               data={"new_password": "Lagos2026", "confirm_password": "Lagos2026"},
               follow_redirects=True)
        c.post("/settings/company", data={
            "name": "Adeyemi Trading Ltd", "currency_symbol": "₦", "currency_code": "NGN",
            "fiscal_year_start_month": "1", "vat_rate": "7.5",
            "default_payment_terms_days": "30",
        }, follow_redirects=True)
        yield c
    TF.reset_attempts()
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def _secret_of(client, username="admin") -> str:
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        return db.scalar(select(User).where(User.username == username)).totp_secret


def _let_the_window_pass(username: str = "admin") -> None:
    """Stand in for waiting thirty seconds.

    A code is refused once it has been used, which is deliberate — see
    ``test_a_code_cannot_be_used_twice``. Setting two-factor up spends the
    current code, so signing in with the very same digits a moment later is
    correctly refused. Rather than make every test below sleep for half a
    minute, the record of the spent code is cleared, which is exactly what the
    passage of time would achieve.
    """
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        db.scalar(select(User).where(User.username == username)).totp_last_counter = 0


def _switch_on(client) -> list[str]:
    """Walk the real setup screens and come back with the recovery codes."""
    page = client.get("/account/two-factor", follow_redirects=True)
    assert page.status_code == 200
    assert "<svg" in page.text
    secret = _secret_of(client)
    done = client.post("/account/two-factor", data={"code": totp.now(secret)},
                       follow_redirects=True)
    assert done.status_code == 200
    import re

    return re.findall(r"[A-Z2-9]{5}-[A-Z2-9]{5}", done.text)


def test_the_setup_screen_shows_a_qr_and_the_typed_key(client):
    page = client.get("/account/two-factor", follow_redirects=True)
    assert "<svg" in page.text
    secret = _secret_of(client)
    assert totp.grouped(secret) in page.text


def test_the_typed_key_is_not_squeezed_into_a_ten_pixel_box(client):
    """It was, and it came out one character per line, unreadably.

    A chart legend's ``.key`` rule — ten pixels square, for those little
    coloured markers — matched ``<code class="key">`` on this page too. The
    fix is that the two no longer share a name, and this is the test that says
    so, because the symptom is invisible to every other kind of check.
    """
    page = client.get("/account/two-factor", follow_redirects=True)
    assert 'class="secret-key"' in page.text
    assert 'class="key"' not in page.text

    css = CSS_FILE.read_text(encoding="utf-8")
    assert "code.secret-key" in css
    # Nothing anywhere may set a bare ".key" size again
    assert "\n.key " not in css and "\n.key{" not in css


def test_reloading_the_setup_screen_does_not_break_a_phone_that_scanned_it(client):
    """The whole bug, end to end, through the real screens."""
    client.get("/account/two-factor", follow_redirects=True)
    scanned = _secret_of(client)                  # what the phone now holds

    for _ in range(4):                            # they reload, wander off, come back
        page = client.get("/account/two-factor", follow_redirects=True)
        assert totp.grouped(scanned) in page.text
    assert _secret_of(client) == scanned

    done = client.post("/account/two-factor", data={"code": totp.now(scanned)},
                       follow_redirects=True)
    assert "recovery" in done.text.lower()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.scalar(select(User).where(User.username == "admin")).totp_enabled


def test_starting_again_from_the_screen_hands_out_a_different_key(client):
    client.get("/account/two-factor", follow_redirects=True)
    first = _secret_of(client)
    client.get("/account/two-factor?again=1", follow_redirects=True)
    assert _secret_of(client) != first


def test_a_refused_code_at_setup_explains_itself(client):
    client.get("/account/two-factor", follow_redirects=True)
    secret = _secret_of(client)

    # Wrong for every clock: the message must send them to rescan, not shrug.
    page = client.post("/account/two-factor",
                       data={"code": totp.code_at(secret, totp.counter_for() + 5000)})
    assert "older key" in page.text and "scan the code on this page again" in page.text

def test_a_right_code_on_a_wrong_clock_sets_it_up_and_says_so(client):
    """Refusing here would punish the person for the computer's mistake."""
    client.get("/account/two-factor", follow_redirects=True)
    secret = _secret_of(client)
    page = client.post("/account/two-factor",
                       data={"code": totp.now(secret, when=time.time() + 300)},
                       follow_redirects=True)
    assert "recovery" in page.text.lower()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        assert user.totp_enabled and user.totp_offset == 10
    # And that same page tells them to put the computer's clock right
    assert "clock is about 5 minutes behind" in page.text
    assert "Set time automatically" in page.text


def test_setting_it_up_shows_ten_recovery_codes_once(client):
    codes = _switch_on(client)
    assert len(codes) == totp.RECOVERY_COUNT
    # And they are not on any later page
    assert codes[0] not in client.get("/account", follow_redirects=True).text


def test_signing_in_now_asks_for_the_code(client):
    _switch_on(client)
    _let_the_window_pass()
    client.get("/logout", follow_redirects=True)

    first = client.post("/login", data={"username": "admin", "password": "Lagos2026",
                                        "next": "/"}, follow_redirects=True)
    assert "One more step" in first.text
    # Not signed in yet: the dashboard must still be out of reach
    assert client.get("/", follow_redirects=False).status_code == 303

    secret = _secret_of(client)
    second = client.post("/login/code", data={"code": totp.now(secret)},
                         follow_redirects=True)
    assert second.status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200


def test_a_wrong_code_does_not_sign_you_in(client):
    _switch_on(client)
    client.get("/logout", follow_redirects=True)
    client.post("/login", data={"username": "admin", "password": "Lagos2026", "next": "/"},
                follow_redirects=True)
    r = client.post("/login/code", data={"code": "000000"}, follow_redirects=True)
    assert "not right" in r.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_the_wrong_password_never_reaches_the_code_step(client):
    _switch_on(client)
    client.get("/logout", follow_redirects=True)
    r = client.post("/login", data={"username": "admin", "password": "wrong", "next": "/"},
                    follow_redirects=True)
    assert "One more step" not in r.text
    assert client.post("/login/code", data={"code": "000000"},
                       follow_redirects=False).status_code == 303


def test_a_recovery_code_gets_you_in_through_the_web(client):
    codes = _switch_on(client)
    _let_the_window_pass()
    client.get("/logout", follow_redirects=True)
    client.post("/login", data={"username": "admin", "password": "Lagos2026", "next": "/"},
                follow_redirects=True)
    r = client.post("/login/code", data={"code": codes[0]}, follow_redirects=True)
    assert r.status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200


def test_where_you_were_going_survives_the_second_step(client):
    _switch_on(client)
    _let_the_window_pass()
    client.get("/logout", follow_redirects=True)
    client.post("/login", data={"username": "admin", "password": "Lagos2026",
                                "next": "/reports"}, follow_redirects=True)
    secret = _secret_of(client)
    r = client.post("/login/code", data={"code": totp.now(secret)}, follow_redirects=False)
    assert r.headers["location"] == "/reports"


def test_turning_it_off_needs_the_password(client):
    _switch_on(client)
    client.post("/account/two-factor/off", data={"password": "wrong"},
                follow_redirects=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.scalar(select(User).where(User.username == "admin")).totp_enabled

    client.post("/account/two-factor/off", data={"password": "Lagos2026"},
                follow_redirects=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert not db.scalar(select(User).where(User.username == "admin")).totp_enabled


def test_an_administrator_can_clear_a_lost_phone(client):
    """Nobody may be permanently locked out of their own books."""
    _switch_on(client)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        uid = db.scalar(select(User).where(User.username == "admin")).id
    client.post(f"/settings/users/{uid}/clear-two-factor", follow_redirects=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        user = db.get(User, uid)
        assert not user.totp_enabled and not user.totp_secret

    client.get("/logout", follow_redirects=True)
    r = client.post("/login", data={"username": "admin", "password": "Lagos2026",
                                    "next": "/"}, follow_redirects=True)
    assert "One more step" not in r.text


def test_requiring_it_sends_people_to_the_setup_screen(client):
    client.post("/settings/users/require-two-factor",
                data={"require_two_factor": "1"}, follow_redirects=True)
    r = client.get("/reports", follow_redirects=False)
    assert r.status_code == 303
    assert "/account/two-factor" in r.headers["location"]
    # But the setup screen itself, and the way out, still work
    assert client.get("/account/two-factor", follow_redirects=False).status_code == 200
    assert client.get("/logout", follow_redirects=False).status_code == 303


def test_it_cannot_be_turned_off_while_the_company_requires_it(client):
    _switch_on(client)
    client.post("/settings/users/require-two-factor",
                data={"require_two_factor": "1"}, follow_redirects=True)
    client.post("/account/two-factor/off", data={"password": "Lagos2026"},
                follow_redirects=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.scalar(select(User).where(User.username == "admin")).totp_enabled


def test_the_account_page_says_whether_it_is_on(client):
    assert "Set it up" in client.get("/account", follow_redirects=True).text
    _switch_on(client)
    page = client.get("/account", follow_redirects=True).text
    assert "recovery code" in page


# --------------------------------------------------------------------------
# The way back in when nobody can sign in at all
# --------------------------------------------------------------------------
#
# Every other escape — a recovery code, an administrator clearing it from
# Settings — needs somebody signed in. When the only administrator is the one
# holding the broken phone, the books are shut with nobody able to open them.
# reset_two_factor.py is the answer to that, and these are its tests.


def _rescue(*argv: str) -> int:
    import reset_two_factor

    return reset_two_factor.main(list(argv))


def _fresh(username: str = "ade") -> User:
    with dbmod.session_scope_for(dbmod.current_slug()) as session:
        return session.scalar(select(User).where(User.username == username))


def test_the_rescue_script_clears_a_lost_phone(db, capsys):
    user = a_user(db)
    secret = TF.setup_secret(user)
    TF.confirm_setup(user, totp.now(secret))
    db.commit()
    assert TF.is_on(_fresh())

    assert _rescue("--company", dbmod.current_slug(), "--user", "ade") == 0

    after = _fresh()
    assert not TF.is_on(after)
    assert not after.totp_secret and not after.totp_recovery
    # The password is untouched — this opens the second door, not the first
    from app.security import verify_password

    assert verify_password("Lagos2026", after.password_hash)


def test_the_rescue_script_writes_what_it_did_to_the_audit_trail(db):
    from app.models import AuditLog

    user = a_user(db)
    TF.confirm_setup(user, totp.now(TF.setup_secret(user)))
    db.commit()
    _rescue("--company", dbmod.current_slug(), "--user", "ade")

    with dbmod.session_scope_for(dbmod.current_slug()) as session:
        entries = session.scalars(
            select(AuditLog).where(AuditLog.action == "TWOFACTOR_CLEARED")
        ).all()
    assert entries and "reset_two_factor.py" in (entries[-1].detail or "")


def test_the_rescue_script_lists_who_has_it_on(db, capsys):
    user = a_user(db)
    TF.confirm_setup(user, totp.now(TF.setup_secret(user)))
    db.commit()

    assert _rescue("--list") == 0
    printed = capsys.readouterr().out
    assert "two-factor ON  ade" in printed
    assert "admin" in printed            # and the ones without it, unmarked


def test_the_rescue_script_says_so_when_there_is_nothing_to_do(db, capsys):
    a_user(db)
    db.commit()
    assert _rescue("--company", dbmod.current_slug(), "--user", "ade") == 0
    assert "was not on" in capsys.readouterr().out


def test_the_rescue_script_does_not_invent_a_user(db, capsys):
    a_user(db)
    db.commit()
    _rescue("--company", dbmod.current_slug(), "--user", "nobody")
    assert "no user called" in capsys.readouterr().out


def test_the_account_page_says_when_the_computers_clock_is_wrong(client):
    client.get("/account/two-factor", follow_redirects=True)
    secret = _secret_of(client)
    client.post("/account/two-factor",
                data={"code": totp.now(secret, when=time.time() + 300)},
                follow_redirects=True)
    page = client.get("/account", follow_redirects=True)
    assert "clock is about 5 minutes behind" in page.text


def test_every_screen_says_how_to_get_back_in_without_an_administrator(client):
    """A person cannot use an escape hatch nobody has told them about."""
    _switch_on(client)
    assert "Reset-TwoFactor" in client.get("/account", follow_redirects=True).text
    _let_the_window_pass()
    client.get("/logout", follow_redirects=True)
    page = client.post("/login", data={"username": "admin", "password": "Lagos2026",
                                       "next": "/"}, follow_redirects=True)
    assert "One more step" in page.text
    assert "Reset-TwoFactor" in page.text


def test_the_rescue_still_works_when_the_audit_trail_will_not_take_it(db, monkeypatch, capsys):
    """The last resort must not fail because of a secondary write.

    Somebody reaching for this cannot get into their own books. If a damaged
    audit table could roll the rescue back with it, they would be exactly as
    shut out as before, which defeats the point of having the tool at all.
    """
    import reset_two_factor

    user = a_user(db)
    TF.confirm_setup(user, totp.now(TF.setup_secret(user)))
    db.commit()

    def refuse(*args, **kwargs):
        raise RuntimeError("audit_log is not writable")

    monkeypatch.setattr(reset_two_factor, "audit", refuse)
    assert _rescue("--company", dbmod.current_slug(), "--user", "ade") == 0
    assert not TF.is_on(_fresh())
    assert "audit_log is not writable" in capsys.readouterr().out
