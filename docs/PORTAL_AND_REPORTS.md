# Part 7: customer portal, reporting, refunds, and activity

This increment adds credit notes and refunds to the billing ledger, a customer self-service portal, business reports with explicit definitions and CSV exports, a "needs attention" list for manual work and failed automation, and a searchable activity history.

## Credit notes and refunds

An issued invoice is never edited. Adjustments are separate documents.

- **Credit note** (administrators): reduces what an issued or paid real invoice asks for, with a reason. Numbered `CN-00001` per business. The tax inside the credit is split in proportion to the original invoice, so a ₹180 credit on a ₹1,180 invoice with ₹180 GST carries ₹27.46 tax. Credits can never exceed the invoice total. An invoice with credit notes cannot be cancelled.
- **Refund** (administrators): records money returned to the customer outside Mandali, by bank transfer, UPI, cash, cheque, card terminal, or other. A refund can only return money received beyond what is owed, so a credit note comes first. Refunds cannot exceed that amount.
- **Receipts:** every payment that hasn't been reversed has a printable receipt, for staff and for the customer.

Each invoice keeps running totals, so every view uses the same arithmetic:

```text
owed       = total - credited
net paid   = amount_paid - refunded
balance    = max(owed - net paid, 0)     what the customer still owes
refund due = max(net paid - owed, 0)     what the business owes back
```

A payment is applied only if it fits the balance, a credit only if it fits the total, and a refund only if it fits the refund due. Each check and update is one conditional database statement, so concurrent requests cannot overshoot, and a database constraint backs the totals. Reversing a payment that funded a refund is refused. An invoice becomes **Paid** when nothing more is owed, whether by payment or credit.

Credit notes and refunds apply to real invoices only. Sandbox invoices are refunded in the gateway's test dashboard; gateway refund APIs arrive with live collection. Website webhooks gain `invoice.credited` and `refund.recorded`, and invoice payloads now include `credited`, `refunded`, and `refund_due`.

## Customer portal

Customers are records, not accounts, so the portal signs them in by email link:

1. The customer opens `/portal/` and enters their email. Each business with a customer record at that address emails a link, up to 10 businesses. The page shows the same message whether or not the email is known.
2. Staff can also press **Email portal link** on a customer's page.
3. The link works once and expires after 30 minutes. Only a SHA-256 digest is stored. Opening it shows a **Continue** button, so a mail scanner that follows links cannot use it up. Continuing starts a fresh session.

Signed-in customers see only invoices issued to their own customer records; drafts are hidden. Each invoice shows its lines, balance, payments with receipts, credit notes, and refunds, plus **Pay online** when the business has shared an active payment link. Customers also see their subscriptions. They can pause, resume, or close one where the plan allows it, logged in activity as **Customer (portal)**. Any other record returns 404. Requests are rate limited (5 link requests an hour, 10 sign-ins per 15 minutes, per client IP).

## Reports

**Reports** is available to every team member. It has three subtabs: Summary, Needs attention, and Activity. Periods are this month, last month, the last 90 days, this or last Indian financial year (April to March), or custom dates up to three years. The period is part of the URL.

| Figure | Meaning |
| --- | --- |
| Invoiced | Totals of invoices issued in the period, including tax, excluding cancelled ones |
| Cancelled | Invoices issued in the period and later cancelled, shown separately |
| Credited | Credit notes issued in the period |
| Collected | Payments received in the period, excluding reversed payments |
| Refunded | Refunds paid out in the period |
| Net collected | Collected minus refunded |
| Outstanding, overdue | What is owed today after credits, whenever it was invoiced; overdue also by age (1–30, 31–60, 61–90, over 90 days) |
| Monthly recurring value | Active subscriptions' per-period totals converted to a month (daily × 365/12, weekly × 52/12, yearly ÷ 12). A run rate before discounts, not money collected |

Every figure excludes sandbox invoices and test payments. Administrators can export invoices, payments, credit notes, and refunds for the period as CSV. Text cells that start with `=`, `+`, `-`, or `@` are prefixed so spreadsheet apps cannot run them as formulas.

**Needs attention** lists work for a person:

- overdue invoices;
- refunds due;
- drafts more than a week old;
- expired quotations;
- gateway orders that are uncertain or in review;
- webhook deliveries that gave up;
- website syncs that failed in the last week.

**Activity** is the full history, filterable by text, who acted (a team member, Mandali automation, or customers through the portal), and dates. The Overview page links to it.

## Following a payment end to end

An online payment's path is visible at each step:

1. Its routing decision under **Payments → Gateway selection**.
2. Its gateway attempt and verified capture under **Payments**.
3. The ledger entry and receipt on the invoice.
4. The totals under **Reports**.
5. The customer's copy in the portal.
6. The activity history.

A manual payment follows the same path from the invoice onward.

## Verification and limits

- **Credit notes and refunds** (`billing/test_adjustments.py`): the tax split; numbering; credits limiting later payments; full credit settling an invoice; refunds capped at the refund due; partial payment then credit then refund reconciling to zero; guards on drafts, sandbox, cancellation, reversal after refund, and gateway methods; webhook events; admin-only screens and documents.
- **Customer portal** (`portal/tests.py`): no email enumeration; single-use, POST-confirmed, expiring links; customers seeing only their own invoices, receipts, and credit notes; plan-governed subscription actions labelled in activity; sign-out; staff invites limited to their own business.
- **Reports** (`reports/tests.py`): each figure's meaning, including the cancelled, reversed, and sandbox exclusions; aging buckets; recurring conversion; financial-year periods; validation; admin-only, formula-safe CSV; needs-attention lists; scoped activity filters.

Screens were rendered with dummy data and checked with Hallmark's `sloplint --scope app --render`: 0 FAIL and 0 WARN across 43 screens. Three findings were waived on the record: long real customer names wrap at 320px, and a closed disclosure is measured while hidden.

Not included: GST-compliant credit note formats, gateway refund APIs, partial credit by line, customer-initiated refunds or disputes, emailing invoices and receipts automatically, and portal accounts with passwords.
