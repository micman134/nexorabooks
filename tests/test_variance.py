"""The variance engine.

These tests exist to defend three promises the Time Machine screen makes to
the person reading it:

  * the top figure is the same profit the profit-and-loss report shows;
  * every breakdown adds back exactly to the line above it;
  * every number on the screen can be followed down to a real transaction.

If any of those stops being true the feature is worse than useless, because it
looks authoritative while being wrong. So they are asserted to the penny.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-var-")

from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    STOCK_ITEM,
    Contact,
    Invoice,
    InvoiceLine,
    Item,
)
from app.money import fmt  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import documents, reports, tax  # noqa: E402
from app.services import variance as V  # noqa: E402
from app.services.posting import EntryDraft, next_number, post_entry, sys_account  # noqa: E402

MAY = V.Period("May", date(2026, 5, 1), date(2026, 5, 31))
APRIL = V.Period("April", date(2026, 4, 1), date(2026, 4, 30))


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-var-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    dbmod.init_db()
    session = dbmod.SessionLocal()
    bootstrap(session)
    session.commit()
    yield session
    session.close()
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def acc(db, key):
    return sys_account(db, key)


def customer(db, name):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def product(db, name, sale=500000, cost=300000):
    it = Item(
        code=next_number(db, "ITEM"), name=name, item_type=STOCK_ITEM, unit="each",
        sale_price=sale, purchase_price=cost, track_stock=False,
        sales_account_id=acc(db, "SALES").id,
        cogs_account_id=acc(db, "COGS").id,
    )
    db.add(it)
    db.flush()
    return it


def sell(db, cust, on, amount, item=None):
    """One posted invoice, no tax, so the figures stay easy to reason about."""
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=cust.id, date=on, due_date=on + timedelta(days=30),
                  status=DRAFT)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        invoice_id=inv.id, line_no=1, item_id=item.id if item else None,
        description=item.name if item else "Services",
        qty=1000, unit_price=amount,
        account_id=acc(db, "SALES").id,
    ))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    documents.post_invoice(db, inv)
    db.flush()
    return inv


def spend(db, on, amount, key="RENT", memo="Rent"):
    draft = EntryDraft(date=on, memo=memo)
    draft.debit(acc(db, key), amount)
    draft.credit(acc(db, "CASH"), amount)
    return post_entry(db, draft)


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def test_compare_choices_are_whole_months(db):
    for key, cur, prior in V.compare_choices(date(2026, 6, 15)):
        if key.startswith(("month", "quarter")):
            assert cur.start.day == 1
            assert prior.start.day == 1
            # The end of a whole period is the last day of a month
            assert (cur.end + timedelta(days=1)).day == 1
            assert (prior.end + timedelta(days=1)).day == 1


def test_last_month_is_the_last_complete_one(db):
    cur, prior = V.choice("month_prev", date(2026, 6, 15))
    assert (cur.start, cur.end) == (date(2026, 5, 1), date(2026, 5, 31))
    assert (prior.start, prior.end) == (date(2026, 4, 1), date(2026, 4, 30))


def test_same_month_last_year_lands_on_the_right_month(db):
    cur, prior = V.choice("month_year", date(2026, 6, 15))
    assert (prior.start, prior.end) == (date(2025, 5, 1), date(2025, 5, 31))


def test_quarter_comparison_spans_three_months(db):
    cur, prior = V.choice("quarter_prev", date(2026, 8, 20))
    assert (cur.start, cur.end) == (date(2026, 4, 1), date(2026, 6, 30))
    assert (prior.start, prior.end) == (date(2026, 1, 1), date(2026, 3, 31))


def test_february_does_not_break_the_shift(db):
    cur, prior = V.choice("month_prev", date(2026, 4, 3))
    assert (cur.start, cur.end) == (date(2026, 3, 1), date(2026, 3, 31))
    assert (prior.start, prior.end) == (date(2026, 2, 1), date(2026, 2, 28))


def test_unknown_choice_is_none(db):
    assert V.choice("nonsense") is None


# --------------------------------------------------------------------------
# The headline ties to the profit and loss
# --------------------------------------------------------------------------


def test_headline_profit_equals_the_profit_and_loss(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 1_000_000)
    sell(db, a, date(2026, 5, 18), 400_000)
    sell(db, a, date(2026, 4, 9), 900_000)
    spend(db, date(2026, 5, 6), 250_000)
    spend(db, date(2026, 4, 6), 200_000)
    db.commit()

    level = V.explore(db, MAY, APRIL)
    pl_now = reports.profit_and_loss(db, MAY.start, MAY.end)
    pl_was = reports.profit_and_loss(db, APRIL.start, APRIL.end)

    assert level.node.current == pl_now.net_profit
    assert level.node.prior == pl_was.net_profit
    assert level.node.delta == pl_now.net_profit - pl_was.net_profit


def test_sections_add_back_to_profit(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 1_000_000)
    spend(db, date(2026, 5, 6), 250_000)
    spend(db, date(2026, 4, 6), 700_000)
    db.commit()

    level = V.explore(db, MAY, APRIL)
    assert level.unexplained == 0
    assert sum(c.profit_effect for c in level.children) == level.node.profit_effect


def test_every_level_adds_back_to_the_one_above(db):
    a, b = customer(db, "Acme Ltd"), customer(db, "Borno Traders")
    widget, gadget = product(db, "Widget"), product(db, "Gadget")
    sell(db, a, date(2026, 5, 4), 1_000_000, widget)
    sell(db, a, date(2026, 5, 11), 300_000, gadget)
    sell(db, b, date(2026, 5, 20), 700_000, widget)
    sell(db, a, date(2026, 4, 4), 1_500_000, widget)
    sell(db, b, date(2026, 4, 14), 200_000, gadget)
    db.commit()

    # Walk every branch to the bottom and check the arithmetic at each step
    def walk(path, depth=0):
        level = V.explore(db, MAY, APRIL, path)
        assert level.unexplained == 0, f"{path} is out by {fmt(level.unexplained)}"
        if depth >= 3:
            return
        for child in level.children:
            if child.can_open:
                walk(path + [child.key], depth + 1)

    walk([])


# --------------------------------------------------------------------------
# Drilling down
# --------------------------------------------------------------------------


def test_drilling_reaches_the_customer_who_caused_it(db):
    a, b = customer(db, "Acme Ltd"), customer(db, "Borno Traders")
    sell(db, a, date(2026, 5, 4), 200_000)
    sell(db, b, date(2026, 5, 4), 200_000)
    sell(db, a, date(2026, 4, 4), 200_000)
    sell(db, b, date(2026, 4, 4), 2_000_000)  # Borno bought far less in May
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    below = V.explore(db, MAY, APRIL, ["revenue", sales.key])
    worst = below.ranked[0]
    assert worst.label == "Borno Traders"
    assert worst.profit_effect == -1_800_000


def test_drilling_reaches_the_transaction(db):
    a = customer(db, "Acme Ltd")
    inv = sell(db, a, date(2026, 5, 4), 640_000)
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    contacts = V.explore(db, MAY, APRIL, ["revenue", sales.key])
    acme = contacts.ranked[0]
    leaves = V.explore(db, MAY, APRIL, ["revenue", sales.key, acme.key])

    assert leaves.child_dim == "entry"
    assert any(c.current == 640_000 for c in leaves.children)
    assert all(c.ref.startswith("/journals/") for c in leaves.children)
    assert all(not c.can_open for c in leaves.children)


def test_lines_with_no_customer_stay_separate(db):
    """An unassigned bucket must never be merged into a real customer's row."""
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 500_000)
    draft = EntryDraft(date=date(2026, 5, 9), memo="Cash sale over the counter")
    draft.debit(acc(db, "CASH"), 300_000)
    draft.credit(acc(db, "SALES"), 300_000)
    post_entry(db, draft)
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    contacts = V.explore(db, MAY, APRIL, ["revenue", sales.key])
    labels = {c.label: c.current for c in contacts.children}
    assert labels["Acme Ltd"] == 500_000
    assert labels["Not linked to anyone"] == 300_000
    assert contacts.unexplained == 0


def test_unassigned_drill_does_not_swallow_everyone(db):
    """Opening the unassigned row must not silently show all customers.

    This is the bug the '-' key exists to prevent: if 'no contact' were
    treated as 'no filter', this row would show the whole account's total and
    nobody would notice until they relied on it.
    """
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 500_000)
    draft = EntryDraft(date=date(2026, 5, 9), memo="Cash sale")
    draft.debit(acc(db, "CASH"), 300_000)
    draft.credit(acc(db, "SALES"), 300_000)
    post_entry(db, draft)
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    below = V.explore(db, MAY, APRIL, ["revenue", sales.key, "-"])
    assert sum(c.current for c in below.children) == 300_000


def test_a_bad_path_shows_the_level_above_rather_than_failing(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 500_000)
    db.commit()

    for path in (["nonsense"], ["revenue", "99999"], ["revenue", "abc", "1"]):
        level = V.explore(db, MAY, APRIL, path)
        assert level.node is not None  # no exception, no blank screen


def test_children_are_ranked_by_what_they_did_to_profit(db):
    a, b, c = (customer(db, n) for n in ("A Ltd", "B Ltd", "C Ltd"))
    sell(db, a, date(2026, 4, 4), 100_000)
    sell(db, b, date(2026, 4, 4), 5_000_000)
    sell(db, c, date(2026, 4, 4), 900_000)
    sell(db, a, date(2026, 5, 4), 100_000)
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    contacts = V.explore(db, MAY, APRIL, ["revenue", sales.key])
    sizes = [abs(x.profit_effect) for x in contacts.children]
    assert sizes == sorted(sizes, reverse=True)


# --------------------------------------------------------------------------
# Signs — the part that is easy to get backwards
# --------------------------------------------------------------------------


def test_a_cost_going_up_hurts_profit(db):
    spend(db, date(2026, 5, 6), 500_000)
    spend(db, date(2026, 4, 6), 200_000)
    db.commit()

    level = V.explore(db, MAY, APRIL)
    costs = next(c for c in level.children if c.key == "expenses")
    assert costs.delta == 300_000  # spending rose
    assert costs.profit_effect == -300_000  # which is bad
    assert not costs.helped


def test_revenue_going_up_helps_profit(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 900_000)
    sell(db, a, date(2026, 4, 4), 400_000)
    db.commit()

    level = V.explore(db, MAY, APRIL)
    rev = next(c for c in level.children if c.key == "revenue")
    assert rev.delta == 500_000
    assert rev.profit_effect == 500_000
    assert rev.helped


def test_a_cost_that_fell_helps_profit(db):
    spend(db, date(2026, 5, 6), 100_000)
    spend(db, date(2026, 4, 6), 800_000)
    db.commit()

    level = V.explore(db, MAY, APRIL)
    costs = next(c for c in level.children if c.key == "expenses")
    assert costs.profit_effect == 700_000
    assert costs.helped


# --------------------------------------------------------------------------
# Percentages, new things, gone things
# --------------------------------------------------------------------------


def test_percentage_is_none_rather_than_zero_when_there_is_no_prior(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 900_000)
    db.commit()
    level = V.explore(db, MAY, APRIL)
    rev = next(c for c in level.children if c.key == "revenue")
    assert rev.prior == 0
    assert rev.pct_change is None  # "up 100%" would be a lie
    assert rev.is_new


def test_something_that_stopped_is_reported_as_gone(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 4, 4), 900_000)
    db.commit()
    level = V.explore(db, MAY, APRIL)
    rev = next(c for c in level.children if c.key == "revenue")
    assert rev.is_gone
    assert rev.profit_effect == -900_000


def test_percentage_uses_the_size_of_the_prior_figure(db):
    node = V.Node("x", "X", "account", 150, 100, 1)
    assert node.pct_change == pytest.approx(50.0)
    # A cost that halved: -50%, measured against the magnitude
    node = V.Node("x", "X", "account", 50, 100, -1)
    assert node.pct_change == pytest.approx(-50.0)


def test_share_of_a_movement_never_divides_by_zero(db):
    node = V.Node("x", "X", "account", 100, 0, 1)
    assert node.share_of(0) == 0.0
    assert node.share_of(200) == pytest.approx(50.0)


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------


def test_narration_says_which_way_profit_went(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 2_000_000)
    sell(db, a, date(2026, 4, 4), 500_000)
    db.commit()

    lines = V.narrate(V.explore(db, MAY, APRIL), fmt)
    assert "Profit is up" in lines[0]
    assert any("Revenue" in ln for ln in lines)


def test_narration_names_the_one_big_cause(db):
    a, b = customer(db, "Acme Ltd"), customer(db, "Small Ltd")
    sell(db, a, date(2026, 4, 4), 5_000_000)
    sell(db, b, date(2026, 4, 4), 100_000)
    sell(db, b, date(2026, 5, 4), 100_000)
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    contacts = V.explore(db, MAY, APRIL, ["revenue", sales.key])
    text = " ".join(V.narrate(contacts, fmt))
    assert "Acme Ltd" in text


def test_narration_handles_a_period_where_nothing_moved(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 500_000)
    sell(db, a, date(2026, 4, 4), 500_000)
    db.commit()
    lines = V.narrate(V.explore(db, MAY, APRIL), fmt)
    assert "did not move" in lines[0]


def test_narration_on_empty_books_does_not_crash(db):
    lines = V.narrate(V.explore(db, MAY, APRIL), fmt)
    assert lines and isinstance(lines[0], str)


# --------------------------------------------------------------------------
# Top movers
# --------------------------------------------------------------------------


def test_top_movers_finds_the_biggest_swings(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 3_000_000)
    sell(db, a, date(2026, 4, 4), 500_000)
    spend(db, date(2026, 5, 6), 900_000)
    db.commit()

    movers = V.top_movers(db, MAY, APRIL, limit=5)
    assert movers
    sizes = [abs(m.profit_effect) for m in movers]
    assert sizes == sorted(sizes, reverse=True)
    assert all(m.section in V.SECTION_BY_KEY for m in movers)


def test_top_movers_ignores_accounts_that_did_not_move(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 500_000)
    sell(db, a, date(2026, 4, 4), 500_000)
    db.commit()
    assert V.top_movers(db, MAY, APRIL) == []


def test_top_movers_respects_the_limit(db):
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 500_000)
    for key in ("RENT", "SALARIES", "UTILITIES"):
        try:
            spend(db, date(2026, 5, 6), 100_000, key)
        except Exception:  # not every seeded chart has every account
            pass
    db.commit()
    assert len(V.top_movers(db, MAY, APRIL, limit=2)) <= 2


# --------------------------------------------------------------------------
# Void entries
# --------------------------------------------------------------------------


def test_a_reversed_invoice_nets_out_of_the_variance(db):
    """Reversal is how this software corrects things, so it must cancel here."""
    a = customer(db, "Acme Ltd")
    inv = sell(db, a, date(2026, 5, 4), 800_000)
    db.commit()
    before = V.explore(db, MAY, APRIL).node.current

    documents.void_invoice(db, inv, on=date(2026, 5, 20))
    db.commit()
    after = V.explore(db, MAY, APRIL).node.current

    assert before == 800_000
    assert after == 0


def test_narration_names_both_sides_when_one_thing_overshoots(db):
    """A child bigger than the net movement must not be quoted as ">100%".

    Revenue up nine hundred, one product up a thousand and another down a
    hundred: "111% of the movement" is true and unreadable.
    """
    a = customer(db, "Acme Ltd")
    up, down = product(db, "Riser"), product(db, "Faller")
    sell(db, a, date(2026, 4, 4), 100_000, up)
    sell(db, a, date(2026, 4, 4), 200_000, down)
    sell(db, a, date(2026, 5, 4), 1_100_000, up)
    sell(db, a, date(2026, 5, 4), 100_000, down)
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    contacts = V.explore(db, MAY, APRIL, ["revenue", sales.key])
    acme = contacts.ranked[0]
    items = V.explore(db, MAY, APRIL, ["revenue", sales.key, acme.key])
    text = " ".join(V.narrate(items, fmt))

    assert "Riser" in text and "Faller" in text
    assert "went the other way" in text
    assert "%" not in text.split("—")[-1]  # no share percentage in that clause


def test_a_slightly_oversized_child_is_not_called_offsetting(db):
    """Half again is the threshold; just over the net movement is not."""
    level = V.Level(
        V.Node("", "Net profit", "root", 1000, 0, 1),
        [V.Node("a", "A", "account", 1080, 0, 1), V.Node("b", "B", "account", 0, 80, 1)],
    )
    assert not level.offsetting
    level.children.append(V.Node("c", "C", "account", 0, 2000, 1))
    assert level.offsetting


def test_opposed_is_empty_when_nothing_moved(db):
    level = V.Level(V.Node("", "Net profit", "root", 0, 0, 1), [])
    assert level.opposed == []


def test_narration_does_not_call_every_transaction_new(db):
    """At the bottom of the tree every row is new; saying so is noise."""
    a = customer(db, "Acme Ltd")
    sell(db, a, date(2026, 5, 4), 640_000)
    db.commit()

    level = V.explore(db, MAY, APRIL, ["revenue"])
    sales = level.children[0]
    contacts = V.explore(db, MAY, APRIL, ["revenue", sales.key])
    leaves = V.explore(db, MAY, APRIL, ["revenue", sales.key, contacts.ranked[0].key])

    assert leaves.child_dim == "entry"
    text = " ".join(V.narrate(leaves, fmt))
    assert "New this period" not in text
