# Selling Nexora Books

Everything in the software works. This is the list of things outside the
software that have to be true before money changes hands, in the order they
matter.

---

## 1. The thing that would undo everything else

**Your repository used to carry an MIT licence.** MIT says, in plain terms,
that anybody who receives a copy may use it, change it, and sell it, for free,
forever. Ship that file next to a licence agreement that says the opposite and
the MIT one very probably wins — you would have given the software away to
every customer and to everyone they pass it to.

It has been replaced with `LICENCE.txt`, which points at
`LICENCE-AGREEMENT.txt`. The old MIT text is kept as `LICENSE-MIT-old.txt` so
that nothing is lost, and so you can see what it said.

**If you want it to stay open source, that is a real choice and a respectable
one — but then there is no licence business, and the key machinery in this
build is pointless.** Decide which you want before you send a copy to anybody.
You cannot easily undo giving software away.

---

## 2. Mint your own signing key — five minutes

```
python make_licence_keys.py
```

This build ships with a working keypair so licensing can be tested out of the
box. That keypair is **not secret**: it came with the software, so anyone who
has the software has it, and anyone who has it can issue licences in your name.

Run the script. Paste the public half it prints into `app/licensing.py`. Keep
`seller/private-key.json` on one computer you control, back it up somewhere
only you can reach, and never put it in a folder you send to a customer.

If that private key ever leaks, every licence you have ever sold becomes
forgeable, and the only fix is a new key and a new build for everybody.

---

## 3. Fill in the brackets — an hour

Three documents ship with `[SQUARE BRACKETS]` in them:

| File | What it is |
|---|---|
| `LICENCE-AGREEMENT.txt` | What the customer is buying and what they may do with it |
| `PRIVACY.txt` | What happens to their information — accurate about the software |
| `REFUNDS.txt` | Your refund promise and what support covers |

Every bracket is a decision only you can make: your business name, your
address, your country's law, your refund window.

**Then have a lawyer read them.** I am not a lawyer and these are careful
drafts, not legal advice. Three clauses genuinely need local eyes: the limit of
liability, consumer rights, and which country's courts apply. Selling globally
makes all three harder, not easier. An hour of a lawyer's time here is the
cheapest hour you will spend on this business.

---

## 4. A code-signing certificate — a few hundred a year

Without one, the first person to run your installer sees a blue Windows
SmartScreen box saying the publisher is unknown, with the "Run anyway" option
hidden behind "More info". A meaningful number of people stop there.

You need an **OV (Organisation Validation) code-signing certificate** issued to
your registered business. Sectigo, DigiCert and SSL.com all sell them; expect
roughly **$200–400 a year**, and expect to prove your business exists —
registration documents, a verifiable phone number, sometimes a call.

Since June 2023 the private key must live on approved hardware: either a USB
token they post you, or a cloud signing service. Budget a week for the whole
process the first time.

When it arrives, put its thumbprint in `build_windows.bat`:

```
set SIGN_THUMBPRINT=your certificate thumbprint here
```

and both the application and the installer are signed automatically.

Even signed, a brand-new certificate has no reputation with SmartScreen and may
still warn for the first few hundred downloads. It settles.

**Do not pay extra for an EV certificate to avoid this.** Microsoft's own
documentation now states that EV certificates no longer affect SmartScreen
behaviour, and that paying the premium for that reason is no longer justified.
EV still matters for some enterprise procurement rules; it buys nothing here.

Signing is worth it anyway, for a reason that is easy to miss: an *unsigned*
application cannot carry reputation from one version to the next, so every
update you ship starts the warning again from zero. A signed one accumulates.

---

## 5. Taking the money

The software does not take payment and should not: it runs on the customer's
own computer with no internet connection. You take the money, then you issue a
key.

**In Nigeria** — Paystack and Flutterwave both do cards, bank transfer and USSD,
settle to a Nigerian account, and are what your customers already expect to
see. Around 1.5% + ₦100, capped. A plain bank transfer with a WhatsApp
confirmation also works perfectly well for your first twenty customers, and
costs nothing.

**Selling internationally** — this is where it gets awkward, and it is worth
understanding before it surprises you. Sell software to a customer in the EU
and you may owe VAT *in their country*, from the first sale, with no threshold.
The same pattern now exists in the UK, Australia, and a growing list of others.

Two ways to deal with that:

- **A merchant of record** — Paddle, Lemon Squeezy or FastSpring. They sell to
  the customer, you sell to them; they handle every country's sales tax and it
  stops being your problem. Roughly 5% + 50¢. For a one-person business selling
  abroad this is almost always the right answer, and the extra percent is
  cheaper than one afternoon with a tax adviser.
- **Stripe or Paddle Billing plus your own compliance** — cheaper (about
  2.9% + 30¢) and entirely your problem. Sensible once volume justifies an
  accountant who handles it.

Start with a merchant of record. Move later if the numbers say so.

---

## 6. Issuing a key — two minutes per sale

1. Payment lands.
2. Customer sends the machine code from **Settings › Licence**.
3. You run `python issue_licence.py`, type their name and that code, choose
   perpetual or a year.
4. It writes a text file. You email it.
5. They paste it in.

Nothing here is on the internet, which means nothing here can be down. It also
means it is manual — at about fifty sales a month you will want to automate it,
and the script is small enough to wrap in a web form when that day comes.

**Keep a list.** Name, email, machine code, what they paid, when, expiry. A
spreadsheet is fine. You will need it for re-issues, for renewals, and for your
own accounts.

---

## 6b. Your prices live in app/store.py

Everything the customer sees on Settings > Licence comes from one file:
`app/store.py`. Your business name and WhatsApp number, the bank account the
money goes to, and the price per user at each size. Fill it in, set
`PRICES_ARE_EXAMPLES = False`, and rebuild.

Until you set that flag, every licence screen carries a yellow box saying the
prices are examples. It is there so the mistake is caught on your machine and
not on a customer's.

Two things the pricing does on its own, worth knowing before somebody asks:

* The rate for a company's size applies to all of its users, not just the ones
  above the band line. Easier to say on the phone, and cheaper for them.
* Where a larger licence would cost less than the number asked for — nine users
  at the 5-9 rate cost more than ten at the 10+ rate — the customer is quoted
  the larger one and told why. Charging more for less is the sort of thing a
  customer works out for themselves and does not forget.

---

## 7. What to charge

Only you know your market. Two things worth holding in mind:

**What you are competing with.** QuickBooks and Sage cost a monthly fee
forever, are priced in dollars or pounds, and mostly assume a good internet
connection. Nexora Books is bought once, runs offline, and keeps the books on
the customer's own machine. For a business where the internet is unreliable and
a recurring foreign-currency charge is painful, those are not small
differences.

**A perpetual licence and a subscription pull in opposite directions.**
Perpetual is easier to sell and gives you nothing next year. A yearly licence
funds the support you will actually have to provide. A common middle path is a
perpetual licence for the version bought, plus an optional yearly fee for
updates and support — the software already supports both, because a licence can
be issued with or without an expiry.

Whatever you choose, price it so that supporting a customer for a year does not
cost you more than they paid. Support is the real cost of selling software, and
it is the one people underestimate.

---

## 8. Before the first customer, be able to answer these

- What happens when their computer dies? *(A new key, free. Restore a backup.)*
- Where are their books? *(On their machine. Settings › Diagnostics says where.)*
- Can they get their data out if they stop paying? *(Yes. Everything, always.)*
- Who do they call when something breaks? *(You. Settings › Diagnostics makes
  the file you will ask for.)*
- Is it right for their country's tax? *(The arithmetic is right. The rates are
  theirs to check — and outside Nigeria, theirs to enter and to verify against
  a payslip they already know the answer to.)*

---

## 9. What is built, and what is not

This section used to list bank import, two-factor sign-in, job costing,
consolidation, a point of sale and non-Latin PDFs as missing. **All six have
since been built.** Leaving that list in place would have you underselling your
own software to the first person you show it to.

Built and tested, as of 2.8.4:

- **Bank statement import** from CSV, OFX, Excel and PDF, with automatic
  matching against outstanding invoices and bills.
- **Job and project costing** — income and cost tracked per job.
- **Consolidation** across companies, including different currencies, with
  intercompany balances eliminated and mismatches reported rather than hidden.
- **A point of sale** — till sessions, tenders, change, refunds, over and short
  posted to the ledger at close.
- **Two-factor sign-in**, with recovery codes, an administrator override, and
  an offline rescue script for the case where the only administrator is locked
  out.
- **Non-Latin scripts in PDFs** — Chinese, Japanese, Korean, Cyrillic, Greek,
  Arabic (joined and right-to-left), Hebrew and Thai, with fonts subset and
  embedded.
- **Emailing invoices, quotations, statements and payslips** straight from the
  software, over the business's own mail account, with the covering wording
  editable per company.

Still not built, and worth being able to say plainly:

- **Automatic updates.** New versions are a download and a re-install.
- **A mobile app.** Staff reach it in a browser on the office network; there is
  no phone application, and no access from outside the building without a VPN.
- **Multi-currency within one company.** A second currency means a second
  company, deliberately — every amount is stored as a whole number of minor
  units and reinterpreting them would corrupt the books rather than convert
  them.
- **E-invoicing to FIRS.** Not yet required for businesses under ₦1bn turnover:
  the pilot runs April–June 2027, go-live is 1 July 2027 and enforcement is
  Q1 2028. Businesses between ₦1bn and ₦5bn are already past go-live, with
  enforcement due in Q1 2027.
- **Cash flow forecasting** beyond the projection already on the dashboard.

Saying "not yet, and here is what to do instead" keeps a customer; discovering
it after they have paid does not.
