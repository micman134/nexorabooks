# Nexora Books

Double-entry accounting software for a small business anywhere. Runs on your own
Windows computer, stores your books in a single file on that computer, and lets
the rest of your staff work in it over the office network.

Each company keeps its books in its own currency, chosen at setup from sixty-five
world currencies, and uses its own country's wording — VAT or GST or IVA, a TIN
or a KRA PIN or an ABN, and the date written the way your customers read it. The
Nigerian tax rules are built in and stay built in; everywhere else you set your
own rates, and there is a screen that checks them against a payslip whose answer
you already know before anybody is paid from them.

Nothing is sent anywhere. There is no subscription and no internet requirement.

---

## What it does

**Bookkeeping**
Chart of accounts (90+ accounts, laid out for a Nigerian company), manual
journals, opening balances, general ledger, trial balance, period locking and
year-end close.

**Sales**
Customers, quotations, invoices, credit notes, receipts, part payments,
settlement discounts, customer statements, receivables ageing.

**Purchases**
Suppliers, purchase orders, bills, debit notes, supplier payments, quick cash
expenses, payables ageing.

**Inventory**
Stock and service items, weighted-average *or* first-in-first-out costing per
item, automatic cost of sales on every sale, stock adjustments with a full
movement history, low-stock warnings, and a valuation report that reconciles to
the inventory account.

More than one place to keep stock — yards, depots, shops, vans — with transfers
between them that move the goods without moving the books. Batch and lot numbers
with expiry dates, where the batch expiring soonest goes out first and a report
shows what is about to go off. Serial numbers on machines, tracked from the day
they arrive to the day they are sold, with the customer and the warranty date.

**Landed cost**
Freight, import duty and clearing agents' fees spread into the value of the
goods they brought in — by value, by quantity or by weight. It creates no new
expense: cost moves out of the freight accounts and into the stock, so the
profit on every sale from that container is the real one.

**Banking**
Multiple bank, cash, domiciliary and POS accounts, transfers between them, and
bank reconciliation against your statement.

**Bank statement import, with the matching done for you**
Give it the CSV, Excel or OFX file your bank exports and it works the rest out. It
finds the heading row wherever the bank buried it, recognises the columns
whatever they are called — including in German, Spanish, French and Italian —
copes with debit and credit as two columns or one signed one, and checks its own
reading against the running balance. If a file was read upside down, it says so
and turns it round. An Excel workbook is read directly, day-numbers and all,
so there is no need to open it and re-save as CSV first.

**And it reads the PDF too**, which is what most banks actually hand you. A PDF
contains no rows and no columns — only text at points on a page — so the table
is reconstructed from where everything sits: pieces sharing a baseline become a
row, and the blank gutters running down the page become the columns. Right
aligned figures and blank debit or credit cells come out in the right places.

That is the least certain of the three readers and the screen says so on every
PDF import. It is safe to offer because this import posts nothing by itself:
every line is shown for confirmation first, and where the bank printed a
running balance the software checks its own reading against it and tells you
whether the figures add up exactly. A scan or a photograph of a statement has
no text in it at all, and is refused plainly rather than coming back as a
statement with no transactions in it.

Then it goes through your books line by line and says what each one is: an
invoice waiting to be paid, a bill you have entered, a payment somebody already
recorded on the day, or a cost it has seen from that payee before. Every
suggestion carries its reasons. **It suggests; you confirm** — and confirming a
line posts that line and nothing else.

The screen shows money in and out across the month, how much was recognised,
and where the money actually went, largest first. The same file can be imported
twice safely: lines that came in before are recognised and left alone.

**Payroll**
Employee register, monthly, fortnightly or weekly pay runs, staff on a daily or
hourly rate paid for the days they worked, allowances and deductions, staff
loans recovered from payslips, printable payslips showing the tax working, a
bank transfer schedule, and PAYE and pension schedules for the authorities.
PAYE is calculated under the Nigeria Tax Act 2025, with pension, NHF, NSITF,
ITF and NHIS. Every run posts straight to the ledger and tells you what to
remit and by when.

**Nigerian tax**
VAT at 7.5% with zero-rated and exempt handling, the full withholding tax
schedule, the no-Tax-ID uplift, the small-company exemption, a VAT return and
WHT schedules ready to file. VAT withheld at source by government agencies and
oil-and-gas customers is recorded as a credit and comes off the return, so the
same VAT is never paid twice.

**Reports**
Profit & loss (with the previous period alongside), balance sheet, cash flow
statement, trial balance, general ledger, ageing, VAT return, WHT schedules,
stock valuation, stock going off, serial number history, fixed asset schedule,
budget against actual, audit trail. Every one prints, and most export to CSV.

**Insights — the software explaining itself**
Reports tell you what the figures are. These three tell you *why*, and they do
it without sending a single number anywhere. See "How the insights work" below.

*Why did that change?* Pick two periods. It takes the movement in profit apart —
revenue against costs, then account by account, then customer by customer, then
product by product — until you are looking at the transactions that caused it.
Every level adds back exactly to the one above it, and the top figure is the
same profit the profit & loss report shows.

*Today's brief* One page each morning: what is overdue and who to chase first,
what is sitting in draft, what recurring invoices have not gone out, what is
waiting for your approval, whether a bank account is overdrawn, and what moved
last month. Ranked by the money involved, because that is what deserves the
first ten minutes. When there is nothing worth raising it says so and stops.

*Board meeting in a box* The whole pack as one PDF: profit & loss, balance
sheet, cash flow, the measures that moved, budget against plan, what to raise —
and the questions a board member is going to ask, with the answers worked out
from the ledger and ready to read out.

**Cash — the three screens that look forward**
Everything else in the software reports what happened. These say what is going
to, and they are careful about the difference. See "How the cash screens work"
below.

*What is coming* A day-by-day projection of the bank balance, and the date it
goes under if it is going to. Built only from what you have already committed
to — invoices you have raised, bills you have entered, your pay run on its own
cycle, the repeating costs you set up. The one thing estimated is *when* each
customer pays, and that comes from their own record in your ledger. It ends
with the three things that would push the date furthest out.

*What if…* Your real figures for a real period, run again with your changes:
prices up, volume down, suppliers dearer, a new hire, everybody paying thirty
days later. It also works out the break-even — how much volume you could afford
to lose before a price rise stopped being worth having.

*Who to chase* Every customer who owes you, in the order they need dealing with
rather than by size, with the approach matched to how that customer has actually
paid you: a nudge, a telephone call, a letter, or an account on hold. Each one
comes with a message already drafted in your words — and the software never
sends it. There is also an honest answer to "should I offer 2% for early
payment?", which is usually no.

**Fixed assets**
An asset register with straight-line and reducing-balance depreciation, monthly
runs you approve before anything posts, disposals and write-offs with the gain
or loss worked out, and the fixed asset schedule an auditor asks for — built
from the register, so it is a genuine check on the balance sheet.

**Recurring invoices and bills**
Rent, retainers, subscriptions, the monthly electricity bill. Set it up once
with an end date or a fixed number of times. Nothing is raised until you say so,
and a template left alone for three months raises three documents, not one.

**Budgets**
A plan for the year, built from last year's figures plus a percentage or typed
in month by month, and a variance report that says plainly whether each
difference is favourable or adverse — because overspending by ₦400,000 and
over-earning by ₦400,000 are not the same news.

**Requisitions**
Staff ask for money, their named line manager approves or sends it back, a
director signs off anything over a limit you set, and finance releases it into
the person's own bank account. A rejection always carries a reason and goes
straight back to whoever raised it. Afterwards they retire it with the vendor's
receipts, and anything unspent comes back and comes off the expense. A running
list shows who is holding company money and how long they have had it.

Three rules are enforced by the system, not by good manners: nobody approves
their own requisition whatever their role, only the named manager can give the
manager's approval, and nobody pays themselves.

**Attachments**
Clip files to any invoice, bill, receipt, payment, journal, employee, customer
or asset, and to any requisition — PDFs, photographs of receipts, vendor
invoices, WHT credit notes, signed delivery notes, spreadsheets. Files are checked by their actual contents, not their
name, so something renamed to `.pdf` to sneak past is refused.

**Jobs and projects**
Code any invoice line, bill line or journal line to a job, and every figure a
job reports is the ledger's own figure filtered — so a job's profit and the
company's profit can never drift apart. Cost against budget, revenue against
the contract, and the two things worth knowing this week rather than at the
year end: work done and not yet billed, and jobs quietly losing money. Whatever
has been coded to no job at all is shown on the same screen, so nobody mistakes
a partial picture for a complete one.

**Point of sale**
A till screen for selling over a counter: scan a barcode or type a name, and the
basket adds up as you go. Split a sale between cash, card and transfer, and it
works out the change. A sale is not a special kind of record — it is an invoice
paid the moment it was raised, so it moves the stock, books the cost of sale,
charges the VAT and appears in every report like everything else. Receipts print
on an 80mm roll or as a PDF, and a refund raises a credit note beside the sale
rather than editing it away.

At the end of the shift you count the drawer. The till says what should be in
it — the float plus the cash taken, with card and transfer takings kept
separate because they went to the bank instead — and whatever the difference
turns out to be is posted to *Till Differences (Over and Short)* where it shows
on the profit and loss. A till that is short every Friday is exactly the thing
this software exists to show its owner, and it can only be seen if it is
written down.

**More than one company**
Each company keeps entirely separate books in its own file, with its own users,
its own numbering and its own logo. Switch between them from the sidebar.

**Group accounts**
Several companies read as one: a combined profit & loss, balance sheet and cash
position, with a column for each member beside the group total. Sales between
group companies are taken out of both revenue and costs, and balances between
them out of both what the group is owed and what it owes — but only as far as
the two sides agree. Where they do not, the difference is reported rather than
tidied away, because an invoice one company has posted and the other has not is
a real error worth finding. A member keeping its books in another currency is
translated at the rates you enter, and the difference two rates leave behind is
shown in equity under its own name. The whole thing reads and never writes: no
member company is touched by looking at the group.

**People**
Individual logins with four roles, and an audit trail recording who did what.

**The rest of the world's alphabets**
Invoices and reports print in Chinese, Japanese, Korean, Greek, Cyrillic, Hebrew
and Arabic as well as Latin. Nothing is bundled to make that work: the software
finds a font already on the computer, embeds only the characters the document
actually uses, and respects the font's own embedding permission. Arabic is
joined up properly and set right to left, so a customer's name reads as their
name. (Indic scripts and Thai print all their characters but without the
ligatures a full typesetting engine would apply.) Anything you drop into the
`fonts` folder in the data directory is used first, and the diagnostics report
says which alphabets this computer can print.

**Sending things out**
Invoices, quotations, credit notes and customer statements go by email from the
business's own address — the same account already set up in Outlook or on a
phone, so the document arrives from the business the customer knows rather than
from a service they have never heard of. Payslips go the same way, with one
deliberate difference: a payslip is only ever sent to the address on that
employee's own record, it is copied to nobody, and sending a whole pay run
tells you plainly who was left out for want of an address rather than quietly
skipping them.

**Giving somebody a login**
Adding a user emails them an invitation: a link on which they choose their own
password. No password is ever sent, because mail is not private and a password
that has travelled by email has been written down in a dozen places nobody
controls. The link works once, expires after a week, is stored only as a hash,
and can be cancelled. Where there is no email address, or no mail set up, the
temporary password is shown on screen to hand over in person, exactly as before.

**Two-factor sign-in**
A code from an authenticator app as well as a password. Each person sets it up
by scanning a QR code — the code is drawn on your own computer and the key never
goes anywhere. Ten single-use recovery codes cover a lost phone, and an
administrator can clear somebody's second factor when those are gone too, so
nobody can end up permanently locked out of their own books. An administrator
can require it for everybody; doing so sends people to the setup screen rather
than turning them away.

---

## Installing it

### On the main computer

1. **Build it once.** On a Windows machine with
   [Python 3.11 or newer](https://www.python.org/downloads/) installed (tick
   *Add Python to PATH* during setup), unzip this folder and double-click
   **`build_windows.bat`**.

   It installs what it needs, runs the test suite, and packages everything into
   `dist\NexoraBooks\`. Takes a few minutes the first time.

2. **Run it.** Open `dist\NexoraBooks\` and double-click **`NexoraBooks.exe`**.
   A window opens with the application in it. A small console window also opens
   showing the network address — leave it alone.

3. **Allow it through the firewall.** The first time it runs, Windows asks
   whether to allow it on the network. Choose **Private networks** and allow.
   Without this, other computers cannot reach it.

4. **Sign in** as `admin` with the password `admin123`. You will be asked to
   choose a new one immediately. Do it.

5. **Fill in your company details** when prompted — name, address, TIN, VAT
   number, financial year. These appear on every invoice and report.

To install on another Windows computer later, just copy the whole
`dist\NexoraBooks` folder across and run the `.exe` there. There is no installer
and nothing is written to the Windows registry.

**Make a shortcut:** right-click `NexoraBooks.exe` → *Send to* → *Desktop*.
To start it automatically when Windows starts, press <kbd>Win</kbd>+<kbd>R</kbd>,
type `shell:startup`, and drop a shortcut in the folder that opens.

### For your staff

Everyone else uses a web browser — nothing to install on their machines.

1. On the main computer, open **Settings › Access from other computers**. It
   shows an address like `http://192.168.1.20:8756`.
2. On each staff computer, open that address in Chrome, Edge or Firefox and
   bookmark it.
3. Give each person their own login under **Settings › Users**. Never share one
   account — the audit trail is only useful when it names a person.

The main computer must be switched on and running Nexora Books for staff to work.

### Roles

| Role | Reports | Invoices &amp; payments | Journals &amp; reconciliation | Void | Users &amp; settings |
|---|---|---|---|---|---|
| Administrator | ✓ | ✓ | ✓ | ✓ | ✓ |
| Accountant | ✓ | ✓ | ✓ | ✓ | — |
| Data entry | ✓ | ✓ | — | — | — |
| Viewer | ✓ | — | — | — | — |

---

## Getting started with your own figures

1. **Settings › Company** — your details, financial year and VAT status.
2. **Chart of accounts** — the standard chart is ready to use. Add or rename
   anything you need; system accounts can be renamed but not deleted.
3. **Banking** — add each bank account, with its own ledger account.
4. **Customers and suppliers** — record the Tax ID for each one. Without it,
   withholding tax doubles. Tick *small company* for anyone under ₦50m turnover.
5. **Items** — anything you sell repeatedly. Stock items track quantities;
   service items do not.
6. **Journals › Opening balances** — enter what each account was worth on the
   day you start. Anything that does not balance goes to Opening Balance Equity
   for you to clear.
7. **Set a lock date** each month once the figures are agreed, under
   **Settings › Period lock**.

Want to look around first? Run `python seed_demo.py` on an empty installation
to load five months of realistic trading and payroll for a fictional Lagos
building merchant — ten staff, five monthly pay runs, PAYE and pension remitted
— then explore. Clear the data folder afterwards to start clean.

---

## Country and currency

**Settings › Company details** carries the country. Choosing one fills in the
currency, the words your invoices and reports use, and the date format — and
every one of them stays editable afterwards, because you know your country
better than a built-in table does.

**Currency.** One currency per company, chosen at setup. It knows that the yen
has no minor unit and the Kuwaiti dinar has a thousand of them, where the symbol
sits, and which character groups the thousands — so a Brazilian typing `1.500`
gets fifteen hundred and a Nigerian typing `1,500` gets the same, and neither has
to think about it.

Once you post your first transaction the currency is **fixed**. Every figure in
the books is stored as a whole number of the smallest unit, so changing the
currency later would not convert anything — it would relabel it, and your
accounts would be wrong by whatever the exchange rate is. To keep books in a
second currency, make a second company under **Companies**; they stay entirely
separate, which is what a set of books in one currency needs to be.

**Wording.** Sixty-eight countries are pre-filled: what the sales tax is called
and its standard rate, what a tax ID and a registration number are called, the
tax authority's name, and the date format. These are labels — no rule anywhere
depends on them.

---

## Bringing your existing books in

**Settings › Bring in your data** takes a CSV out of a spreadsheet, QuickBooks,
Sage, Wave, Zoho or a hand-kept book, and reads it row by row before writing
anything.

Eight things can come across, and the order matters — customers have to exist
before their unpaid invoices do:

1. **Chart of accounts** — extra accounts of your own on top of the built-in ones
2. **Customers** and 3. **Suppliers**
4. **Products and services**
5. **Opening balances** — your closing trial balance from the old system
6. **Unpaid customer invoices** and 7. **Unpaid supplier bills**
8. **Employees**

Headings are matched by meaning, so "Customer Name", "CLIENT", and "name" are
all understood, and any column nobody recognises is reported rather than
quietly dropped. Every upload shows you which rows are new, which update
something already there, and which are wrong and why — and writes nothing until
you click a second time.

Unpaid invoices come in as **real invoices**, so ageing and statements are right
from the first day. Their income side goes to Opening Balances rather than to
sales, because the sale was already counted in the old system and counting it
twice would inflate this year's turnover.

---

## Sending documents to customers

Every invoice, quotation, credit note, receipt, statement and payslip can be
downloaded as a **PDF**, or emailed straight to the customer with the PDF
attached, from the business's own address.

**Settings › Email** takes the same details already in Outlook or a phone —
server, port, username, password. Nothing is relayed through anybody else and
no account has to be created. Send yourself a test before sending to a customer;
the button is there for that reason.

If you use Gmail or Microsoft 365 with two-step verification, your ordinary
password will not work: create an *app password* in your mail account's security
settings. The software says so on the screen, because everybody hits this.

What was sent, to whom and when is recorded — including failures, with the mail
server's reason.

---

## Backups that look after themselves

**Settings › Backup and restore** runs a backup daily or weekly on its own, keeps
as many as you ask for, and deletes only the ones it made — a backup taken by
hand before doing something frightening is never touched.

The setting that matters is **"Also put a copy in"**. A backup on the same disk
as your books survives a mistake; it does not survive the disk failing or the
laptop being stolen. Point it at a flash drive, a network share, or a folder
that syncs to Google Drive, Dropbox or OneDrive. *Save and run one now* proves
it works rather than finding out on the night it matters.

---

## When something goes wrong

Every unexpected error is written down with the page, the user and the full
traceback, and the person on screen gets a calm message and a reference number
rather than a stack trace.

**Settings › Diagnostics** collects all of it — version, operating system, how
big the books are, whether backups are running, whether the licence is valid,
and the recent errors — into one text file to send to whoever supports the
software. It is shown in full first: it describes the installation, not the
books.

---

## Licensing

Nexora Books runs for **30 days** on a fresh installation with nothing to enter.
After that it needs a key, which is tied to one computer.

A customer opens **Settings › Licence** and reads off their machine code — a
hash of the installation that carries no name and nothing identifying. You run
`python issue_licence.py`, type in their name and that code, and it writes a
text file you email back. They paste it in. Nothing goes over the internet in
either direction; the check happens on their computer.

**Before you sell anything, run `python make_licence_keys.py`.** This copy ships
with a working signing key so that licensing can be tested out of the box — and
that key is not secret, because it came with the software. Generating your own
takes a few seconds and prints the public half to paste into `app/licensing.py`.
Keep the private half on one computer you control, never in the folder you send
to customers and never in the `.exe`.

**What a lapsed licence does.** It stops new entries reaching the ledger, and
that is all. Every screen still opens, every report still prints and exports,
every past transaction is still there, and a full backup can still be taken and
restored anywhere. Holding somebody's own bookkeeping hostage would be wrong, and
a business that cannot get its records out of software it has stopped paying for
is a business that should never have started using it.

---

## How the Nigerian tax rules are applied

Rates follow the Nigeria Tax Act 2025 and the withholding tax regulations in
force for 2026. All of them are editable under **Settings › Tax codes**.

**VAT** — standard rate 7.5%, with zero-rated and exempt codes. From January
2026 input VAT on taxable supplies is fully claimable, and a claim may be made
up to five years after the end of the tax period. Returns are due by the 21st of
the following month.

**Withholding tax**

| What is being paid for | Rate | No Tax ID |
|---|---|---|
| Supply of goods | 2% | 4% |
| All other services | 2% | 4% |
| Professional, consultancy, technical and management fees | 5% | 10% |
| Commission, brokerage and agency fees | 5% | 10% |
| Construction — roads, bridges, buildings, power | 2% | 4% |
| Construction — other and ancillary | 5% | 10% |
| Rent, hire and lease | 10% | 20% |
| Dividends | 10% | 10% |
| Interest | 10% | 10% |
| Royalties — company / individual | 10% / 5% | 20% / 10% |
| Directors' fees | 15% | 20% |

The rules Nexora Books applies for you:

- WHT is calculated on the amount **before VAT**, never on the VAT itself.
- Where no Tax ID is on record the rate doubles, capped at 20%. Dividends and
  interest are exempt from this uplift.
- A small company (turnover ₦50m or less) holding a valid Tax ID does not suffer
  WHT on transactions of ₦2,000,000 or less. Tick *small company* on the contact.
- WHT on purchases is recognised when you **pay**, not when the bill is entered.
- Remittance is due by the 21st of the following month.

Tax rates change. Check the rates against the current law before you file, and
update them under Settings if they have moved. Nexora Books keeps records at the
rate they were posted at.

---

## Payroll

### PAYE

Under the Nigeria Tax Act 2025, in force from 1 January 2026, the annual bands
are:

| Annual chargeable income | Rate |
|---|---|
| First ₦800,000 | 0% |
| Next ₦2,200,000 — to ₦3,000,000 | 15% |
| Next ₦9,000,000 — to ₦12,000,000 | 18% |
| Next ₦13,000,000 — to ₦25,000,000 | 21% |
| Next ₦25,000,000 — to ₦50,000,000 | 23% |
| Above ₦50,000,000 | 25% |

Two things changed that catch people out: the **Consolidated Relief Allowance
is gone**, and so is the old 1% minimum tax. In their place is a **rent
relief** — the lower of 20% of the rent an employee actually pays, or ₦500,000
a year. Record their annual rent on the employee card, and keep the receipts;
someone who owns their home or lives in accommodation you provide gets no
relief beyond the ₦800,000 zero band.

Anyone earning **no more than the national minimum wage of ₦70,000 a month
pays no PAYE at all**. Nexora Books applies that automatically and says so on the
payslip.

Each period's pay is annualised, the reliefs are deducted, the bands are
applied, and the result is divided back down. Every payslip shows the working
band by band, so an employee who queries their deduction can be answered from
the payslip itself.

### Contributions

| | Employee | Employer | Calculated on |
|---|---|---|---|
| Pension | 8% | 10% | Basic + housing + transport |
| NHF | 2.5% | — | Basic |
| NSITF | — | 1% | Total payroll |
| ITF | — | 1% | Total payroll (5+ staff, or ₦50m turnover) |
| NHIS | 5% | 10% | Basic, where you operate the scheme |

All of these are editable under **Payroll › Payroll scheme**, and each can be
switched off if you do not operate it.

### Payroll outside Nigeria

The Nigerian scheme above is what you get out of the box. Everywhere else,
**Payroll › Payroll scheme** lets you build your own:

- **Tax bands** of your own — as many or as few as your country has. They are
  annual and run on from one another, and the last one takes everything above.
  A flat tax is a scheme with one band; a country with no income tax is a scheme
  with one band at zero.
- **Five contribution slots**, each with the name your payslips use, what it is
  charged on (basic, basic-plus-allowances, gross or taxable pay), the employee
  and employer percentages, an optional per-period cap, and whether the
  employee's share comes off pay before tax is worked out. Rename the ones your
  country has — NSSF, NHIF, EPF, social security — and leave the rest off.
- **Your own words** for the tax itself, the tax-free threshold and any relief.

### Checking a scheme before anybody is paid from it

Typing your own rates is the only way to run payroll outside Nigeria. It is also
the fastest way to get payroll wrong, and a wrong payslip is somebody's rent.

**Payroll › Check the scheme** is the answer to that. Take one salary whose
answer you already know — last month's payslip from whatever you used before, or
your tax office's own worked example — type the pay in, type in the gross, tax
and net it should produce, and see whether the scheme reproduces them. It shows
the working band by band, so when a figure is out you can see where.

Every check is re-run automatically whenever you change a rate, so a scheme that
was right in March cannot quietly stop being right in April. Until at least one
real check passes, the payroll scheme screen says plainly that nothing has been
verified — it will not show you a reassuring tick you have not earned.

### Running it

1. **Add your staff** under Payroll › Employees. Split their pay into basic,
   housing and transport — those three are what pension is calculated on.
   Everything else goes in the allowances table, where you decide whether each
   line is taxed and whether it counts for pension.
2. Site workers and casuals go on a **daily or hourly rate**. Set the rate, set
   how often you pay them, and enter the days worked on each run. Paying a
   daily-rate worker monthly is normal and handled properly.
3. **Run payroll** for the period. You get a draft — change anyone's days,
   take someone off, then post it.
4. **Posting** puts the cost in your P&L and creates every liability: net pay
   owed to staff, PAYE to the tax office, pension to the PFAs, and the rest.
5. **Pay the staff** from a bank account. Print the bank schedule to make the
   transfers from, and the payslips to hand out.
6. **Remit** under Payroll › Remittances due, which shows what you are holding
   and the deadline for each.

### Deadlines

| | Where | When |
|---|---|---|
| PAYE | Internal Revenue Service of the state where the employee **lives** | By the 10th of the following month |
| Pension | Each employee's PFA | Within 7 working days of payday — 2% a month penalty if late |
| NHF | Federal Mortgage Bank of Nigeria | Within one month of deduction |
| NSITF | NSITF | By the 16th of the following month |
| ITF | Industrial Training Fund | Annually, by 1 April |

### Staff loans

Advance money to an employee and set how much comes off each payslip.
Nexora Books recovers it automatically, never takes more than is outstanding,
stops when the balance reaches nil, and puts the balance back if you void the
run. The outstanding amount sits in **1310 Staff Advances and Loans** on your
balance sheet.

---

## Looking after your data

Your books live in one file:

```
C:\Users\<you>\AppData\Roaming\Nexora Books\company.db
```

**Back up weekly.** Settings › Backup and restore takes a snapshot with one
click. *Back up and download* saves a copy through your browser so you can put
it on a flash drive or in cloud storage. Keep at least one copy somewhere other
than this computer — a stolen laptop takes the books with it.

**Restoring** replaces everything. Nexora Books checks the file is really a
Nexora Books backup, saves a copy of your current books first, and completes the
restore the next time it starts.

---

## Design decisions worth knowing

**Money is never a floating-point number.** Every amount is stored as a whole
number of kobo. Rounding happens once, explicitly, half-up. This is why the
trial balance is always exactly zero rather than nearly zero.

**Posted documents are never edited or deleted.** They are voided, which posts a
reversing journal. The original stays on record. This is what an auditor expects
to see, and it is what makes the audit trail worth having. Drafts, which have
not touched the ledger, can be deleted freely.

**One posting function writes the ledger.** Everything — invoices, bills,
receipts, stock adjustments, year-end — goes through `post_entry`, which refuses
anything that does not balance. There is no second path that could get it wrong.

**Stock is valued at weighted average**, held as a running total of quantity and
value rather than a per-unit cost, so rounding cannot drift. Issuing the last of
an item releases exactly the remaining value.

**A bank statement import never posts anything by itself.** It is the feature
most likely to save a customer a day a month, and the one most likely to wreck
their books if it acted alone: a receipt imported that somebody had already
entered by hand credits the customer twice and doubles the bank balance. So the
first question the matcher asks of every line is "is this already in the books?"
— and if it is, confirming ticks it off rather than recording it again.

**Two-factor sign-in works with no internet and no account anywhere.** It is
standard TOTP — RFC 6238, the same six digits every authenticator app produces —
computed on your own computer. The QR code that carries the key is drawn here
too, rather than fetched from a website, because fetching it would mean handing
somebody's key to a stranger's server.

**The cash flow statement uses the direct method** — every naira that moved
through a bank account is traced to what it was for. It therefore always ties
back to the change in your balances rather than approximately agreeing.

---

## How the cash screens work

These are the only screens that look forward, so it is worth saying exactly
what they do and do not assume.

**Committed** means a fact: an invoice you have raised, a bill you have
entered, a pay run on its known cycle. The amount is certain.

**Expected** means one thing only — *when* a committed amount will actually
move. It comes from that customer's own record of paying you, measured against
the due date rather than the invoice date, so a customer on sixty-day terms who
pays on day sixty is not counted as sixty days late.

**No revenue is ever invented.** A business with nothing outstanding has nothing
coming in on the chart, and there is a test that proves it. A forecast that
quietly assumes next month looks like last month is the kind that reassures a
business straight into trouble.

Three details that took some thought:

  * **A customer needs at least three settled invoices** before their record is
    used. One unusual payment should not set the pattern for ever.
  * **An overdue bill is not paid on the first morning.** Nobody clears every
    late supplier at once, and modelling it that way makes the balance dive on
    day one of every forecast, which makes the date meaningless. Overdue bills
    sit a week out, and the overdue total is shown separately so nothing is
    hidden.
  * **An invoice a year overdue is left off the chart entirely**, and said to
    be. That money is either collectable — in which case chase it — or it is
    not, in which case putting a date on it is fiction.

For *What if…*, the link between price and volume is **your assumption, not a
finding**. The software does not know how many customers you would lose by
charging more and will not pretend to. What it does instead is give you the
break-even, which is arithmetic.

---

## How the insights work

This is the part people ask about, so it is worth being plain.

**Nothing is estimated, predicted or generated.** The insight screens contain no
model, no forecast, no scoring you would have to take on trust, and no text
written by a machine that was not handed the figures first. Every number on them
is a sum of posted journal lines, and every sentence is assembled from those
numbers. If a figure appears, it is in your ledger, and you can click until you
are looking at the transaction.

**Nothing leaves your computer.** There is no service to call, no account to
create and no internet connection involved. The software works exactly the same
with the network cable pulled out. This is a deliberate choice, not a limitation
we have yet to remove: your books contain your customers, your margins and your
staff's pay, and the safest place for that is the machine you control.

**It is allowed to draw attention. It is never allowed to act.** The brief will
tell you an invoice is overdue and draft the chasing email. It will not send it.
Nothing on these screens posts, pays, allocates or changes anything: every one of
them ends at a link to a screen where *you* decide.

**Where the figures come from.**

| Screen | What it reads | What it can never do |
|---|---|---|
| Why did that change? | Posted journal lines for two periods, grouped by section, account, customer and product | Show a total that does not add back to the level above it |
| Today's brief | Ageing, draft documents, recurring templates, requisitions, stock levels, bank balances, the tax return | Rank by anything other than the money at stake |
| Board pack | The profit & loss, balance sheet, cash flow and budget reports, unchanged | Print a figure the reports themselves do not show |

The board pack and the screen version are built from one set of figures by one
function, so they cannot quietly disagree. The only thing the PDF does
differently is write "NGN 1,000.00" where the screen writes "₦1,000.00", because
the fonts built into the PDF format have no glyph for every currency symbol and
a missing glyph prints as a box.

**Why it works this way.** An accounting package that invents a figure in a
board pack is not a product with a bug in it. Deterministic arithmetic over your
own ledger cannot invent anything, costs nothing to run, works offline, and
gives the same answer twice. For money, that is not a compromise — it is the
better engineering.

---

## Running it without building the .exe

Any computer with Python 3.11+:

```bash
pip install -r requirements.txt
python run.py                 # opens a window, or your browser
python run.py --server        # no window, for a machine used as a server
python run.py --port 9000     # a different port
```

Run the tests with:

```bash
pip install pytest httpx
python -m pytest tests -q
```

A hundred and sixty-one tests cover the money arithmetic, the posting engine,
Nigerian VAT, withholding tax and PAYE, a full trading cycle, three months of
payroll, and every page in the interface. Several of them
end by asserting that debits equal credits and the balance sheet balances — if
you change anything, they will tell you if the books can go wrong.

---

## If something goes wrong

**Staff cannot reach it.** Check the Windows Firewall step above, and that both
machines are on the same network. Settings › Access from other computers has a
longer checklist.

**The address changed.** Windows gave the computer a new one. Open Settings ›
Access from other computers on the main machine for the current address, or ask
whoever set up your router to reserve a fixed address for it.

**"The books are locked."** An administrator set a lock date. Change it under
Settings › Period lock, or date the entry after the lock.

**"This entry does not balance."** The debits and credits are not equal. The
message says by how much.

**A posted invoice is wrong.** Void it and raise a new one, or issue a credit
note. If a payment has been applied to it, void the receipt first.

**Forgotten admin password.** Any other administrator can reset it under
Settings › Users. If nobody can sign in, restore a backup from before the
password was changed.

---

## Layout of the code

```
app/
  money.py            integer-kobo money, rounding, allocation
  totp.py             the six-digit codes, RFC 6238
  qrcode.py           QR codes as SVG, so nothing has to be installed
  models.py           every table
  security.py         passwords, sessions, role permissions
  seed.py             the Nigerian chart of accounts
  companies.py        the company registry — one database file each
  services/
    posting.py        the double-entry engine — the only writer of the ledger
    tax.py            VAT and withholding tax
    costing.py        stock: locations, batches, serials, average and FIFO
    landed.py         freight and duty into the cost of the goods
    documents.py      invoices, bills, credit and debit notes
    cash.py           receipts, payments, transfers
    payroll.py        PAYE and the statutory deductions
    payroll_run.py    pay runs and remittances
    assets.py         the asset register and depreciation
    recurring.py      invoices and bills that repeat
    budgets.py        budgets and variance
    variance.py       the drill-down engine behind "why did that change?"
    brief.py          today's brief and the board pack
    boardpdf.py       the board pack, laid out for paper
    requisitions.py   the approval route and the money
    attachments.py    files kept against a record
    reports.py        every report
    twofactor.py      the rules around the six-digit codes
    cashtimeline.py   what the bank balance is going to do
    whatif.py         the same year run against different assumptions
    collections.py    who to chase, how, and what to say
    statements.py     reading a bank statement, whatever shape it came in
    matching.py       working out what each statement line actually is
    bankimport.py     turning a confirmed line into a posting
    charts.py         the small SVG charts, drawn without a library
  routers/            one module per area of the application
  templates/          the interface
  static/             stylesheet and the line-item editor
tests/                1,294 tests
desktop.py            the Windows launcher
seed_demo.py          demo company
build_windows.bat     builds NexoraBooks.exe
```

---

Nexora Books 2.8.0 · accounting for a business anywhere · licensed software — see LICENCE.txt
