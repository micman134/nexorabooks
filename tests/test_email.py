import email
import os
import shutil
import socket
import socketserver
import tempfile
import threading
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-mail-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry, fileguard  # noqa: E402
from app import clock  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    Company,
    Contact,
    EmailLog,
    Invoice,
    InvoiceLine,
)
from app.money import to_minor as M  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import documents, mailer  # noqa: E402
from app.services.posting import account_by_code, next_number  # noqa: E402


# --------------------------------------------------------------------------
# A mail server of our own, on this machine, for the length of one test
# --------------------------------------------------------------------------
#
# Written out rather than taken from the standard library: Python's own smtpd
# was removed in 3.12, and a customer who installs a current Python to build
# this must not find the test suite refusing to run.


class Handler(socketserver.StreamRequestHandler):
    """Just enough SMTP to accept a message, or to refuse one."""

    def _say(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        box = self.server
        self._say("220 test.local ESMTP")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            command = raw.decode("utf-8", "replace").strip()
            word = command.split(" ", 1)[0].upper()
            if word in ("EHLO", "HELO"):
                self._say("250-test.local")
                self._say("250 HELP")
            elif word in ("MAIL", "RCPT", "NOOP", "RSET"):
                self._say("250 OK")
            elif word == "DATA":
                if box.refuse:
                    self._say("554 Transaction failed")
                    continue
                self._say("354 End with a dot")
                body = bytearray()
                while True:
                    line = self.rfile.readline()
                    if not line or line in (b".\r\n", b".\n"):
                        break
                    body.extend(line[1:] if line.startswith(b"..") else line)
                box.received.append(bytes(body))
                self._say("250 Queued")
            elif word == "QUIT":
                self._say("221 Bye")
                return
            else:
                self._say("250 OK")


class Postbox(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, refuse: bool = False):
        super().__init__(("127.0.0.1", 0), Handler)
        self.received: list[bytes] = []
        self.refuse = refuse
        self.port = self.server_address[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _running(refuse: bool = False):
    server = Postbox(refuse=refuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def postbox():
    yield from _running()


@pytest.fixture()
def refuser():
    yield from _running(refuse=True)


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-mail-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    yield tmp
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def working(port: int) -> mailer.Settings:
    return mailer.Settings(
        host="127.0.0.1", port=port, security=mailer.NONE, username="",
        from_name="Adeyemi Building Materials", from_email="accounts@adeyemi.example",
    )


def last(server):
    """The most recent message, parsed with the modern policy so that
    get_body() and friends are available."""
    import email.policy

    return email.message_from_bytes(server.received[-1], policy=email.policy.default)


# --------------------------------------------------------------------------
# The settings
# --------------------------------------------------------------------------


def test_nothing_is_configured_to_begin_with(home):
    assert mailer.load().ready is False


def test_settings_survive_a_restart(home):
    mailer.save(mailer.Settings(host="smtp.example.com", port=465,
                                security=mailer.SSL, from_email="a@b.com"))
    again = mailer.load()
    assert (again.host, again.port, again.security) == ("smtp.example.com", 465,
                                                        mailer.SSL)


def test_the_mail_password_is_not_left_readable_by_everybody(home):
    """It is the customer's real mail account. Other users on the machine
    have no business with it."""
    mailer.save(mailer.Settings(host="x", from_email="a@b.com", password="secret"))
    assert fileguard.only_owner_can_read(os.path.join(home, "email.json"))


def test_an_address_that_is_not_an_address_is_spotted():
    assert mailer.looks_like_an_address("accounts@zenith.example")
    for bad in ("", "zenith.example", "two people@x.com", "@x.com", "a@b"):
        assert not mailer.looks_like_an_address(bad), bad


def test_sending_before_setting_up_says_what_to_do(home):
    with pytest.raises(mailer.MailError) as caught:
        mailer.send("a@b.com", "Hi", "there")
    assert "Settings" in str(caught.value)


def test_a_bad_recipient_is_refused_before_the_server_is_troubled(home):
    with pytest.raises(mailer.MailError) as caught:
        mailer.send("not an address", "Hi", "there",
                    settings=mailer.Settings(host="x", from_email="a@b.com"))
    assert "does not look like an email address" in str(caught.value)


# --------------------------------------------------------------------------
# Actually sending
# --------------------------------------------------------------------------


def test_a_message_arrives(home, postbox):
    mailer.send("ap@zenith.example", "Invoice INV-0001",
                "Please find it attached.", settings=working(postbox.port))
    assert len(postbox.received) == 1
    message = last(postbox)
    assert message["To"] == "ap@zenith.example"
    assert message["Subject"] == "Invoice INV-0001"
    assert "Please find it attached." in message.get_body(("plain",)).get_content()


def test_it_comes_from_the_business_not_from_the_software(home, postbox):
    mailer.send("ap@zenith.example", "Hi", "there", settings=working(postbox.port))
    assert "Adeyemi Building Materials" in last(postbox)["From"]
    assert "accounts@adeyemi.example" in last(postbox)["From"]


def test_a_pdf_arrives_whole_and_still_opens(home, postbox):
    from app.pdfwriter import Canvas

    c = Canvas()
    c.text(40, 40, "Invoice INV-0001")
    pdf = c.output()

    mailer.send("ap@zenith.example", "Invoice", "Attached.",
                [("Invoice INV-0001.pdf", pdf, "application/pdf")],
                settings=working(postbox.port))

    parts = [p for p in last(postbox).walk()
             if p.get_filename() == "Invoice INV-0001.pdf"]
    assert parts, "the attachment did not arrive"
    assert parts[0].get_payload(decode=True) == pdf


def test_the_signature_is_added(home, postbox):
    settings = working(postbox.port)
    settings.signature = "Adeyemi Building Materials Ltd\n+234 803 555 0142"
    mailer.send("a@b.com", "Hi", "Body text.", settings=settings)
    body = last(postbox).get_body(("plain",)).get_content()
    assert "Body text." in body
    assert "+234 803 555 0142" in body


def test_a_copy_goes_to_the_cc_address(home, postbox):
    mailer.send("a@b.com", "Hi", "there", cc="boss@example.com",
                settings=working(postbox.port))
    assert last(postbox)["Cc"] == "boss@example.com"


def test_replies_can_be_pointed_somewhere_else(home, postbox):
    settings = working(postbox.port)
    settings.reply_to = "sales@adeyemi.example"
    mailer.send("a@b.com", "Hi", "there", settings=settings)
    assert last(postbox)["Reply-To"] == "sales@adeyemi.example"


def test_a_successful_send_is_remembered(home, postbox):
    mailer.send("a@b.com", "Hi", "there", settings=working(postbox.port))
    assert mailer.load().last_ok
    assert mailer.load().last_error == ""


# --------------------------------------------------------------------------
# When it does not work
# --------------------------------------------------------------------------


def test_a_server_that_is_not_there_is_reported_in_plain_words(home):
    settings = mailer.Settings(host="127.0.0.1", port=free_port(),
                               security=mailer.NONE, from_email="a@b.com")
    with pytest.raises(mailer.MailError) as caught:
        mailer.send("c@d.com", "Hi", "there", settings=settings)
    assert "Could not reach the mail server" in str(caught.value)


def test_a_refusal_is_passed_on_rather_than_hidden(home, refuser):
    with pytest.raises(mailer.MailError):
        mailer.send("c@d.com", "Hi", "there", settings=working(refuser.port))


def test_the_reason_is_kept_for_the_settings_screen(home):
    settings = mailer.Settings(host="127.0.0.1", port=free_port(),
                               security=mailer.NONE, from_email="a@b.com")
    with pytest.raises(mailer.MailError):
        mailer.send("c@d.com", "Hi", "there", settings=settings)
    assert "mail server" in mailer.load().last_error


def test_the_app_password_trap_is_explained_not_just_reported():
    """Everybody with two-step verification hits this, and the server's own
    message does not tell them what to do about it."""
    import smtplib

    text = mailer.explain(smtplib.SMTPAuthenticationError(535, b"Bad credentials"))
    assert "app password" in text


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(home):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123",
                               "next": "/"}, follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c


def an_invoice(slug=None):
    slug = slug or registry.default_slug()
    with dbmod.session_scope_for(slug) as db:
        contact = Contact(code=next_number(db, "CONTACT"),
                          name="Zenith Construction Ltd", is_customer=True)
        db.add(contact)
        db.flush()
        inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=contact.id, date=date.today(),
                      due_date=date.today(), status=DRAFT)
        db.add(inv)
        db.flush()
        db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Materials",
                           qty=1000, unit_price=M("500,000"),
                           account_id=account_by_code(db, "4000").id))
        db.flush()
        db.refresh(inv)
        documents.recalc_invoice(db, inv)
        documents.post_invoice(db, inv)
        return inv.id, contact.id, inv.number


def test_the_email_settings_screen_opens(client):
    page = client.get("/settings/email", follow_redirects=True).text
    assert "Outgoing mail server" in page
    assert "app password" in page


def test_settings_can_be_saved(client):
    client.post("/settings/email", data={
        "host": "smtp.example.com", "port": "587", "security": "STARTTLS",
        "username": "me", "password": "secret", "from_name": "Adeyemi",
        "from_email": "accounts@adeyemi.example", "reply_to": "", "signature": "",
    }, follow_redirects=True)
    saved = mailer.load()
    assert saved.host == "smtp.example.com" and saved.password == "secret"


def test_editing_the_settings_does_not_wipe_the_saved_password(client):
    """The box is empty every time it is drawn. Saving must not read that as
    'the password is now nothing'."""
    client.post("/settings/email", data={
        "host": "smtp.example.com", "port": "587", "security": "STARTTLS",
        "username": "me", "password": "secret", "from_email": "a@b.com",
    }, follow_redirects=True)
    client.post("/settings/email", data={
        "host": "smtp.example.com", "port": "465", "security": "SSL",
        "username": "me", "password": "", "from_email": "a@b.com",
    }, follow_redirects=True)
    assert mailer.load().password == "secret"
    assert mailer.load().port == 465


def test_a_test_message_can_be_sent_from_the_screen(client, postbox):
    r = client.post("/settings/email/test", data={
        "host": "127.0.0.1", "port": str(postbox.port), "security": "NONE",
        "username": "", "password": "", "from_name": "Adeyemi",
        "from_email": "accounts@adeyemi.example", "to": "me@example.com",
    }, follow_redirects=True)
    assert "Sent." in r.text
    assert len(postbox.received) == 1


def test_a_failed_test_says_why_and_is_written_down(client):
    r = client.post("/settings/email/test", data={
        "host": "127.0.0.1", "port": str(free_port()), "security": "NONE",
        "from_email": "a@b.com", "to": "me@example.com",
    }, follow_redirects=True)
    assert "Could not reach the mail server" in r.text
    with dbmod.session_scope_for(registry.default_slug()) as db:
        row = db.scalars(select(EmailLog).order_by(EmailLog.id.desc())).first()
        assert row.ok is False and "mail server" in row.error


def test_the_send_screen_fills_itself_in(client):
    doc_id, _contact_id, number = an_invoice()
    page = client.get(f"/send/invoice/{doc_id}", follow_redirects=True).text
    assert number in page
    assert "Zenith Construction Ltd" in page
    assert f"Invoice {number}.pdf" in page


def test_an_invoice_is_sent_with_its_pdf(client, postbox):
    doc_id, _contact_id, number = an_invoice()
    client.post("/settings/email", data={
        "host": "127.0.0.1", "port": str(postbox.port), "security": "NONE",
        "from_name": "Adeyemi", "from_email": "accounts@adeyemi.example",
    }, follow_redirects=True)

    r = client.post(f"/send/invoice/{doc_id}", data={
        "to": "ap@zenith.example", "cc": "",
        "subject": f"Invoice {number}", "body": "Please find it attached.",
    }, follow_redirects=True)
    assert "sent to ap@zenith.example" in r.text

    parts = [p for p in last(postbox).walk() if p.get_filename()]
    assert parts and parts[0].get_payload(decode=True).startswith(b"%PDF")


def test_the_address_typed_in_is_kept_against_the_customer(client, postbox):
    doc_id, contact_id, number = an_invoice()
    client.post("/settings/email", data={
        "host": "127.0.0.1", "port": str(postbox.port), "security": "NONE",
        "from_email": "accounts@adeyemi.example",
    }, follow_redirects=True)
    client.post(f"/send/invoice/{doc_id}", data={
        "to": "ap@zenith.example", "subject": "Invoice", "body": "Attached.",
    }, follow_redirects=True)

    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert db.get(Contact, contact_id).email == "ap@zenith.example"


def test_a_send_that_fails_leaves_the_invoice_exactly_as_it_was(client):
    doc_id, _contact_id, number = an_invoice()
    client.post("/settings/email", data={
        "host": "127.0.0.1", "port": str(free_port()), "security": "NONE",
        "from_email": "a@b.com",
    }, follow_redirects=True)

    r = client.post(f"/send/invoice/{doc_id}", data={
        "to": "ap@zenith.example", "subject": "Invoice", "body": "Attached.",
    }, follow_redirects=True)
    assert "Not sent." in r.text

    with dbmod.session_scope_for(registry.default_slug()) as db:
        inv = db.get(Invoice, doc_id)
        assert inv.status == "POSTED"
        row = db.scalars(select(EmailLog).order_by(EmailLog.id.desc())).first()
        assert row.ok is False and row.ref_number == number


def test_what_was_sent_is_listed_afterwards(client, postbox):
    doc_id, _contact_id, number = an_invoice()
    client.post("/settings/email", data={
        "host": "127.0.0.1", "port": str(postbox.port), "security": "NONE",
        "from_email": "accounts@adeyemi.example",
    }, follow_redirects=True)
    client.post(f"/send/invoice/{doc_id}", data={
        "to": "ap@zenith.example", "subject": "Invoice", "body": "Attached.",
    }, follow_redirects=True)

    page = client.get(f"/send/invoice/{doc_id}", follow_redirects=True).text
    assert "Already sent" in page
    assert "ap@zenith.example" in page
    assert "Recently sent" in client.get("/settings/email",
                                         follow_redirects=True).text


def test_a_statement_can_be_emailed(client, postbox):
    _doc_id, contact_id, _number = an_invoice()
    client.post("/settings/email", data={
        "host": "127.0.0.1", "port": str(postbox.port), "security": "NONE",
        "from_email": "accounts@adeyemi.example",
    }, follow_redirects=True)

    r = client.post(f"/send/statement/{contact_id}", data={
        "to": "ap@zenith.example", "subject": "Statement", "body": "Attached.",
    }, follow_redirects=True)
    assert "Statement sent" in r.text
    parts = [p for p in last(postbox).walk() if p.get_filename()]
    assert parts[0].get_payload(decode=True).startswith(b"%PDF")


def test_the_send_screen_is_honest_when_email_is_not_set_up(client):
    doc_id, _c, _n = an_invoice()
    page = client.get(f"/send/invoice/{doc_id}", follow_redirects=True).text
    assert "not set up yet" in page
    assert "download the PDF and attach it yourself" in page


# --------------------------------------------------------------------------
# Payslips: the same machinery, deliberately narrower
# --------------------------------------------------------------------------


def a_payslip(db, name="Chinedu Okafor", address="chinedu@adeyemi.example"):
    """One employee, one posted pay run, one payslip."""
    from datetime import date as D

    from app.models import Employee
    from app.services import payroll_run as PR

    employee = Employee(staff_no=next_number(db, "EMPLOYEE"),
                        first_name=name.split()[0], last_name=name.split()[-1],
                        email=address, frequency="MONTHLY", pay_basis="SALARY",
                        basic=M("200,000"), housing=M("50,000"),
                        transport=M("30,000"), hire_date=D(2026, 1, 1))
    db.add(employee)
    db.flush()

    run = PR.build_run(db, "MONTHLY", D(2026, 8, 1), D(2026, 8, 31),
                        D(2026, 8, 28))
    db.flush()
    return run, run.payslips[0]


@pytest.fixture()
def payroll(home):
    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        run, slip = a_payslip(db)
        db.commit()
        return run.id, slip.id


def signed_in(client=None):
    c = client or TestClient(app)
    c.post("/login", data={"username": "admin", "password": "admin123"},
           follow_redirects=True)
    return c


def test_a_payslip_is_emailed_to_the_employee_with_the_pdf(postbox, payroll):
    run_id, slip_id = payroll
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        r = c.post(f"/send/payslip/{slip_id}", data={
            "subject": "Payslip August 2026", "body": "Attached."},
            follow_redirects=True)
        assert r.status_code == 200

    assert len(postbox.received) == 1
    message = last(postbox)
    assert message["To"] == "chinedu@adeyemi.example"
    names = [part.get_filename() for part in message.iter_attachments()]
    assert names and names[0].endswith(".pdf")


def test_a_payslip_is_never_copied_to_anybody(postbox, payroll):
    """Somebody's pay, tax and loan repayments are nobody else's business."""
    run_id, slip_id = payroll
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        c.post(f"/send/payslip/{slip_id}",
               data={"subject": "Payslip", "body": "Attached.",
                     "cc": "everyone@adeyemi.example"},
               follow_redirects=True)

    message = last(postbox)
    assert message["Cc"] is None
    assert "everyone@adeyemi.example" not in str(message)


def test_the_address_cannot_be_typed_on_the_sending_screen(postbox, payroll):
    """A typo there would be somebody's payslip in a stranger's inbox."""
    run_id, slip_id = payroll
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        c.post(f"/send/payslip/{slip_id}",
               data={"to": "wrong.person@example.com",
                     "subject": "Payslip", "body": "Attached."},
               follow_redirects=True)

    assert last(postbox)["To"] == "chinedu@adeyemi.example"


def test_an_employee_with_no_email_is_refused_rather_than_guessed_at(home):
    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        run, slip = a_payslip(db, "Bola Ade", address="")
        slip_id = slip.id
        db.commit()
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        r = c.post(f"/send/payslip/{slip_id}",
                   data={"subject": "Payslip", "body": "Attached."},
                   follow_redirects=True)
    assert "no email address on their employee record" in r.text


def test_sending_a_whole_run_says_who_was_left_out(postbox, home):
    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        from datetime import date as D

        from app.models import Employee
        from app.services import payroll_run as PR

        for first, address in (("Chinedu", "chinedu@adeyemi.example"),
                               ("Bola", "")):
            db.add(Employee(staff_no=next_number(db, "EMPLOYEE"),
                            first_name=first, last_name="Ade", email=address,
                            frequency="MONTHLY", pay_basis="SALARY",
                            basic=M("200,000"), hire_date=D(2026, 1, 1)))
        db.flush()
        run = PR.build_run(db, "MONTHLY", D(2026, 8, 1), D(2026, 8, 31),
                            D(2026, 8, 28))
        run_id = run.id
        db.commit()

    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        r = c.post(f"/send/payslips/run/{run_id}", follow_redirects=True)

    assert len(postbox.received) == 1, "only the one with an address"
    assert "1 payslip sent" in r.text
    assert "No email address on file for" in r.text and "Bola" in r.text


def test_a_failed_payslip_is_written_down_with_the_reason(refuser, payroll):
    run_id, slip_id = payroll
    mailer.save(working(refuser.port))
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        r = c.post(f"/send/payslip/{slip_id}",
                   data={"subject": "Payslip", "body": "Attached."},
                   follow_redirects=True)
    assert "Not sent" in r.text

    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        row = db.scalar(select(EmailLog).where(EmailLog.kind == "PAYSLIP"))
        assert row is not None and row.ok is False and row.error


# --------------------------------------------------------------------------
# Inviting a user: a link, never a password
# --------------------------------------------------------------------------


def test_adding_a_user_with_an_address_emails_them_an_invitation(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        r = c.post("/settings/users/save", data={
            "username": "ngozi", "full_name": "Ngozi Eze",
            "email": "ngozi@adeyemi.example", "role": "clerk", "is_active": "on",
        }, follow_redirects=True)
        assert r.status_code == 200

    assert len(postbox.received) == 1
    message = last(postbox)
    assert message["To"] == "ngozi@adeyemi.example"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "/invite/" in body
    assert "ngozi" in body


def test_the_invitation_never_contains_a_password(postbox, home):
    """The whole point. Mail is not private, so nothing usable travels in it."""
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with TestClient(app) as c:
        signed_in(c)
        c.post("/settings/users/save", data={
            "username": "ngozi", "email": "ngozi@adeyemi.example",
            "role": "clerk", "is_active": "on"}, follow_redirects=True)

    body = last(postbox).get_body(preferencelist=("plain",)).get_content()
    assert "password" in body.lower(), "it should mention choosing one"
    assert "temporary password" not in body.lower()

    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        from app.models import User

        user = db.scalar(select(User).where(User.username == "ngozi"))
        # Nothing in the message is the stored secret, and the token itself is
        # only ever kept as a hash.
        assert user.password_hash not in body
        assert user.invite_hash and user.invite_hash not in body


def _invited(postbox, address="ngozi@adeyemi.example") -> str:
    """Create a user, and dig the invitation link out of the message sent."""
    import re

    with TestClient(app) as c:
        signed_in(c)
        c.post("/settings/users/save", data={
            "username": "ngozi", "email": address, "role": "clerk",
            "is_active": "on"}, follow_redirects=True)
    body = last(postbox).get_body(preferencelist=("plain",)).get_content()
    found = re.search(r"(/invite/[A-Za-z0-9_\-]+)", body)
    assert found, body
    return found.group(1)


def test_the_link_lets_them_choose_a_password_and_signs_them_in(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    with TestClient(app) as c:
        page = c.get(path)
        assert page.status_code == 200
        assert "ngozi" in page.text

        r = c.post(path, data={"new_password": "Lagos2026",
                               "confirm_password": "Lagos2026"},
                   follow_redirects=True)
        assert r.status_code == 200
        # Signed in, not back at the login screen.
        assert "Sign out" in r.text


def test_the_password_they_chose_is_the_one_that_works(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    with TestClient(app) as c:
        c.post(path, data={"new_password": "Lagos2026",
                           "confirm_password": "Lagos2026"},
               follow_redirects=True)

    with TestClient(app) as c:
        r = c.post("/login", data={"username": "ngozi", "password": "Lagos2026"},
                   follow_redirects=True)
        assert "Sign out" in r.text
        assert "choose a new password" not in r.text.lower()


def test_an_invitation_works_once(postbox, home):
    """A link sitting in an old inbox has to be worth nothing."""
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    with TestClient(app) as c:
        c.post(path, data={"new_password": "Lagos2026",
                           "confirm_password": "Lagos2026"},
               follow_redirects=True)

    with TestClient(app) as c:
        again = c.get(path, follow_redirects=True)
        assert "no longer any good" in again.text
        r = c.post(path, data={"new_password": "Somebody3lse",
                               "confirm_password": "Somebody3lse"},
                   follow_redirects=True)
        assert "no longer any good" in r.text

    # And the password they set is untouched.
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "ngozi", "password": "Lagos2026"},
                   follow_redirects=True)
        assert "Sign out" in r.text


def test_an_invitation_expires(postbox, home):
    from datetime import datetime, timedelta

    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        from app.models import User

        user = db.scalar(select(User).where(User.username == "ngozi"))
        user.invite_expires = clock.now() - timedelta(minutes=1)
        db.commit()
    dbmod.reset_all()

    with TestClient(app) as c:
        assert "no longer any good" in c.get(path, follow_redirects=True).text


def test_a_made_up_link_says_nothing_about_who_exists(home):
    with TestClient(app) as c:
        r = c.get("/invite/" + "z" * 43, follow_redirects=True)
        assert r.status_code == 200
        assert "no longer any good" in r.text
        assert "ngozi" not in r.text and "admin" not in r.text


def test_a_switched_off_account_cannot_be_let_in_by_an_old_link(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        from app.models import User

        db.scalar(select(User).where(User.username == "ngozi")).is_active = False
        db.commit()
    dbmod.reset_all()

    with TestClient(app) as c:
        assert "no longer any good" in c.get(path, follow_redirects=True).text


def test_a_weak_password_is_refused_on_the_invitation_too(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    with TestClient(app) as c:
        r = c.post(path, data={"new_password": "abc", "confirm_password": "abc"},
                   follow_redirects=True)
        assert "at least 8 characters" in r.text
        # Still usable afterwards — a rejected attempt must not burn the link.
        assert "Set my password" in r.text


def test_an_invitation_can_be_cancelled(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        from app.models import User

        uid = db.scalar(select(User).where(User.username == "ngozi")).id

    with TestClient(app) as c:
        signed_in(c)
        c.post(f"/settings/users/{uid}/cancel-invite", follow_redirects=True)

    with TestClient(app) as c:
        assert "no longer any good" in c.get(path, follow_redirects=True).text


def test_without_email_set_up_the_temporary_password_is_still_shown(home):
    dbmod.reset_all()
    with TestClient(app) as c:
        signed_in(c)
        r = c.post("/settings/users/save", data={
            "username": "ngozi", "email": "ngozi@adeyemi.example",
            "role": "clerk", "is_active": "on"}, follow_redirects=True)
        assert "Temporary password" in r.text
        assert "Email is not set up yet" in r.text


def test_a_user_with_no_address_gets_the_temporary_password(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    with TestClient(app) as c:
        signed_in(c)
        r = c.post("/settings/users/save", data={
            "username": "ngozi", "email": "", "role": "clerk",
            "is_active": "on"}, follow_redirects=True)
        assert "Temporary password" in r.text
    assert not postbox.received


def test_only_an_administrator_can_invite(postbox, home):
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        from app import security as sec
        from app.models import User

        db.add(User(username="clerk", password_hash=sec.hash_password("Lagos2026"),
                    role="clerk", is_active=True, email="c@adeyemi.example"))
        db.commit()
    dbmod.reset_all()

    with TestClient(app) as c:
        c.post("/login", data={"username": "clerk", "password": "Lagos2026"},
               follow_redirects=True)
        r = c.post("/settings/users/1/invite", follow_redirects=True)
        assert r.status_code == 403


def test_an_invited_clerk_is_not_met_with_a_permission_error(postbox, home):
    """The company is not always set up on the day somebody is given a login,
    and "not allowed" is a miserable first thing to see."""
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    path = _invited(postbox)

    with TestClient(app) as c:
        r = c.post(path, data={"new_password": "Lagos2026",
                               "confirm_password": "Lagos2026"},
                   follow_redirects=True)
        assert r.status_code == 200
        assert "does not allow this" not in r.text
        assert "You are signed in" in r.text


# --------------------------------------------------------------------------
# The address in the link, which is not the address in the sender's browser
# --------------------------------------------------------------------------
#
# Reported from a real office: three people invited, three invitations
# received, three "Hmmm… can't reach this page — 127.0.0.1 refused to connect".
# The administrator had opened Nexora Books on the machine it runs on, where
# the address is 127.0.0.1, and that address means "this computer" on whichever
# computer reads it.


def _at_the_server_machine():
    """A client whose address is the one you get sitting at the computer itself."""
    return TestClient(app, base_url="http://127.0.0.1:8756")


def test_an_invitation_sent_from_the_server_itself_still_reaches_staff(postbox, home,
                                                                      monkeypatch):
    from app import network

    monkeypatch.setattr(network, "lan_addresses", lambda: ["192.168.1.20"])
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with _at_the_server_machine() as c:
        signed_in(c)
        c.post("/settings/users/save", data={
            "username": "ngozi", "email": "ngozi@adeyemi.example",
            "role": "clerk", "is_active": "on"}, follow_redirects=True)

    body = last(postbox).get_body(preferencelist=("plain",)).get_content()
    assert "http://192.168.1.20:8756/invite/" in body
    assert "127.0.0.1" not in body
    assert "localhost" not in body


def test_the_address_an_administrator_wrote_down_is_the_one_used(postbox, home,
                                                                 monkeypatch):
    from app import network

    monkeypatch.setattr(network, "lan_addresses", lambda: ["192.168.1.20"])
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with _at_the_server_machine() as c:
        signed_in(c)
        c.post("/settings/network", data={"staff_url": "books.local:8756"},
               follow_redirects=True)
        c.post("/settings/users/save", data={
            "username": "ngozi", "email": "ngozi@adeyemi.example",
            "role": "clerk", "is_active": "on"}, follow_redirects=True)

    body = last(postbox).get_body(preferencelist=("plain",)).get_content()
    assert "http://books.local:8756/invite/" in body


def test_no_invitation_is_sent_when_no_address_would_work(postbox, home, monkeypatch):
    """Better to say why than to email somebody a link that cannot open."""
    from app import network

    monkeypatch.setattr(network, "lan_addresses", lambda: [])
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with _at_the_server_machine() as c:
        signed_in(c)
        page = c.post("/settings/users/save", data={
            "username": "ngozi", "email": "ngozi@adeyemi.example",
            "role": "clerk", "is_active": "on"}, follow_redirects=True)

    assert not postbox.received
    assert "no address that would work on anybody else" in page.text
    assert "Access from other computers" in page.text

    # The account is still made — they simply cannot be emailed yet
    ref = registry.ensure_at_least_one()
    with dbmod.session_scope_for(ref.slug) as db:
        from app.models import User

        assert db.scalar(select(User).where(User.username == "ngozi")) is not None


def test_inviting_again_after_setting_the_address_works(postbox, home, monkeypatch):
    """The way back for somebody whose staff already got a broken link."""
    from app import network
    from app.models import User

    monkeypatch.setattr(network, "lan_addresses", lambda: [])
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with _at_the_server_machine() as c:
        signed_in(c)
        c.post("/settings/users/save", data={
            "username": "ngozi", "email": "ngozi@adeyemi.example",
            "role": "clerk", "is_active": "on"}, follow_redirects=True)
        assert not postbox.received

        c.post("/settings/network", data={"staff_url": "192.168.1.20:8756"},
               follow_redirects=True)
        ref = registry.ensure_at_least_one()
        with dbmod.session_scope_for(ref.slug) as db:
            uid = db.scalar(select(User).where(User.username == "ngozi")).id
        c.post(f"/settings/users/{uid}/invite", follow_redirects=True)

    body = last(postbox).get_body(preferencelist=("plain",)).get_content()
    assert "http://192.168.1.20:8756/invite/" in body


def test_the_link_that_was_emailed_actually_opens(postbox, home, monkeypatch):
    """Follow the link out of the message, as the member of staff would."""
    import re

    from app import network

    monkeypatch.setattr(network, "lan_addresses", lambda: ["192.168.1.20"])
    mailer.save(working(postbox.port))
    dbmod.reset_all()

    with _at_the_server_machine() as c:
        signed_in(c)
        c.post("/settings/users/save", data={
            "username": "ngozi", "email": "ngozi@adeyemi.example",
            "role": "clerk", "is_active": "on"}, follow_redirects=True)

    body = last(postbox).get_body(preferencelist=("plain",)).get_content()
    found = re.search(r"http://192\.168\.1\.20:8756(/invite/[A-Za-z0-9_\-]+)", body)
    assert found, body

    # Their computer resolves that address to this server; the path is what matters.
    with TestClient(app, base_url="http://192.168.1.20:8756") as staff:
        page = staff.get(found.group(1), follow_redirects=True)
    assert page.status_code == 200
    assert "password" in page.text.lower()


# --------------------------------------------------------------------------
# A company's own covering message
# --------------------------------------------------------------------------
#
# A training business sends the same note with every invoice: thank you for
# registering, here is what to pay, send us the receipt. Retyping that on every
# send is how a line of it eventually goes missing on the invoice that matters.


PROCERT = (
    "Dear {first_name},\n"
    "\n"
    "Thank you for registering for {item} with {company}. Your place is reserved.\n"
    "\n"
    "Your invoice is attached. The amount due is {amount}.\n"
    "Please pay by {due_date}, using the account details shown on the invoice.\n"
    "\n"
    "Warm regards,\n"
    "{company}"
)


def test_a_company_that_writes_its_own_wording_gets_it_used():
    from app.services import mailer as M

    class Line:
        description = "Contract Bidding, Tender & Proposal Administration"

    class Contact_:
        name = "Chinedu Okafor"

    class Doc:
        number = "INV-0042"
        contact = Contact_()
        lines = [Line()]
        date = date(2026, 8, 26)

    class Co:
        name = "Procert Academy Limited"
        phone = "+234 807 395 4668"
        email = "training@procert.academy"
        invoice_email_subject = "Invoice {number} — {item}"
        invoice_email_body = PROCERT

    body = M.invoice_body(Co(), Doc(), "Invoice", "₦250,000.00", "9 September 2026")
    assert body.startswith("Dear Chinedu,")
    assert "Contract Bidding, Tender & Proposal Administration" in body
    assert "₦250,000.00" in body
    assert "9 September 2026" in body
    assert body.endswith("Procert Academy Limited")
    assert "{" not in body                       # nothing left unfilled

    subject = M.invoice_subject(Co(), Doc(), "Invoice")
    assert subject == "Invoice INV-0042 — Contract Bidding, Tender & Proposal Administration"


def test_a_line_about_something_this_document_lacks_is_left_out():
    """A credit note has no due date, and must not say "Please pay by ."."""
    from app.services import mailer as M

    filled = M.fill(PROCERT, {
        "first_name": "Chinedu", "item": "the course", "company": "Procert",
        "amount": "₦250,000.00", "due_date": "",
    })
    assert "Please pay by" not in filled
    assert "The amount due is ₦250,000.00." in filled
    assert filled.startswith("Dear Chinedu,")     # the rest is untouched


def test_a_placeholder_nobody_recognises_is_left_visible_not_blanked():
    from app.services import mailer as M

    filled = M.fill("Dear {first_name}, about {course_name}.", {"first_name": "Ada"})
    assert filled == "Dear Ada, about {course_name}."
    assert M.unknown_placeholders("{course_name} and {venue} and {amount}") == \
        ["{course_name}", "{venue}"]


def test_wording_that_empties_itself_falls_back_rather_than_sending_nothing():
    from app.services import mailer as M

    class Contact_:
        name = "Chinedu Okafor"

    class Doc:
        number = "CN-0007"
        contact = Contact_()
        lines = []
        date = date(2026, 8, 26)

    class Co:
        name = "Procert Academy Limited"
        phone = email = ""
        invoice_email_subject = ""
        invoice_email_body = "Due {due_date}."      # the only line, and it is empty

    body = M.invoice_body(Co(), Doc(), "Credit note", "₦50,000.00", "")
    assert body.strip()
    assert "credit note CN-0007" in body            # the plain wording came back


def test_saving_wording_from_the_screen_changes_what_a_customer_receives(postbox, home):
    """End to end: type it in Settings, then send an invoice and read the mail."""
    mailer.save(working(postbox.port))
    dbmod.reset_all()
    inv_id, _contact_id, _number = an_invoice()

    with TestClient(app) as c:
        signed_in(c)
        saved = c.post("/settings/email/wording", data={
            "invoice_email_subject": "Invoice {number} — {item}",
            "invoice_email_body": PROCERT,
        }, follow_redirects=True)
        assert "That is what goes out" in saved.text

        # The screen shows it back, filled in with an example
        page = c.get("/settings/email", follow_redirects=True)
        assert "Thank you for registering for" in page.text
        assert "{first_name}" in page.text          # the box still holds the template

        form = c.get(f"/send/invoice/{inv_id}", follow_redirects=True)
        assert "Thank you for registering" in form.text
        c.post(f"/send/invoice/{inv_id}", data={
            "to": "chinedu@example.com", "cc": "",
            "subject": "Invoice test", "body": "sent as shown"}, follow_redirects=True)

    assert postbox.received


def test_a_placeholder_that_cannot_be_filled_is_pointed_out_when_saving(home):
    dbmod.reset_all()
    with TestClient(app) as c:
        signed_in(c)
        page = c.post("/settings/email/wording", data={
            "invoice_email_subject": "",
            "invoice_email_body": "Dear {first_name}, about {course_name}.",
        }, follow_redirects=True)
    assert "{course_name}" in page.text
    assert "exactly as typed" in page.text


def test_clearing_the_wording_goes_back_to_the_plain_message(home):
    dbmod.reset_all()
    with TestClient(app) as c:
        signed_in(c)
        c.post("/settings/email/wording", data={
            "invoice_email_subject": "", "invoice_email_body": PROCERT},
            follow_redirects=True)
        page = c.post("/settings/email/wording", data={
            "invoice_email_subject": "", "invoice_email_body": "  "},
            follow_redirects=True)
    assert "plain wording again" in page.text
