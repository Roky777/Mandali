# Part 2: manual billing

This increment adds the shared billing core and its manual channel: customers, a catalog of taxes, products, variants, plans, discounts, and quotation templates, quotations that become subscriptions, one-time and subscription invoices in INR, and payments collected outside Mandali. Online collection, recurring scheduling, and the customer portal remain planned.

Setup is unchanged from the [foundation](FOUNDATION.md). Run `python manage.py migrate` to create the `billing` tables.

## Workflow

1. An administrator adds taxes, products (with optional variants), plans, discounts, and quotation templates under **Catalog**.
2. Staff add a **customer**, then prepare a **quotation**, blank or from a template.
3. A quotation moves through `Draft -> Quotation sent -> Confirmed -> Active -> Closed`. Active subscriptions can be paused and resumed when the plan allows it.
4. **Create draft invoice** on a confirmed or active subscription copies its lines. One-time invoices can also be created directly from **Invoices**.
5. **Issue invoice** assigns the next number (`INV-00001`, per business), fixes the due date from the payment terms, and copies the business and customer billing identities onto the invoice.
6. **Record a payment** for any amount up to the remaining balance. Partial payments leave the invoice `Partly paid`; the final payment marks it `Paid`.

The invoice page shows a running balance: subtotal, discount, tax, total, payments, and what remains. **Printable invoice** opens a print layout; use the browser's print dialog to save it as a PDF.

## Money rules

- INR only, stored as decimals with two places. Every amount is calculated on the server; submitted totals are ignored.
- A line's description, unit price, and tax rate are copied from the catalog when it is added. Later catalog changes never rewrite existing documents. Variant price = product sales price + variant adjustment, never below zero.
- Discounts apply before tax. A discount is spread across its eligible lines in proportion to their amounts, and the last line absorbs any rounding remainder. Tax is then rounded per line, half up.
- One tax rate per line (for example, GST 18%). Mandali does not yet split CGST, SGST, or IGST, determine place of supply, or claim GST invoice compliance. Validate invoices against your obligations before relying on them for filing.

## Discount rules

Only administrators create discounts. A discount can be a percentage (up to 100) or a fixed amount (capped at the eligible total), limited to selected products or the whole invoice, with optional minimum purchase, minimum quantity, start and end dates, and usage limit.

Eligibility is shown on the draft and checked again at issue against today's date in Asia/Kolkata. An ineligible discount blocks issuing with the reason shown. Each issued invoice uses the discount once, claimed by a conditional update so concurrent issues cannot exceed the limit. Cancelling the invoice returns the use.

## Corrections and permissions

Issued amounts are never edited. To correct an issued invoice, an administrator cancels it with a reason and staff issue a new one. Cancelled numbers are kept, not reused. An invoice with active payments cannot be cancelled until those payments are reversed. Credit notes and refunds are planned for Part 7.

| Action | Administrator | Staff |
| --- | --- | --- |
| View catalog | Yes | Yes |
| Configure taxes, products, variants, plans, discounts, templates | Yes | No |
| Manage customers, quotations, subscriptions, draft invoices | Yes | Yes |
| Issue invoices and record payments | Yes | Yes |
| Cancel issued invoices, reverse payments | Yes | No |

Every billing query and form choice is constrained to the authorized workspace, including related records such as a customer or product chosen on an invoice. A record ID from another business returns 404 or fails validation. Billing actions are written to workspace activity.

## Integrity controls

- Invoice and subscription numbers come from a per-workspace counter claimed with a single atomic update, backed by unique constraints.
- Draft edits and issuing first claim the draft with a conditional update, so lines cannot change while an invoice is being issued, and a double submit issues once.
- A payment is applied with one conditional update that requires the remaining balance to cover it, so concurrent payments cannot exceed the total. A database check constraint keeps `amount_paid` between zero and the total.
- Reversals are single-use and restore the balance. A payment reference already recorded in the business (such as a UTR) raises a warning to prevent double recording.
- Payments must be above zero and cannot be dated in the future.

These controls were designed for and tested on SQLite. Re-verify concurrency on PostgreSQL before a multi-instance deployment.

## Not in this increment

Recurring invoice scheduling, renewals, automatic closure, and billing-period duplicate prevention (Part 5; plan policies are stored and shown now). Online payments, payment links, and QR codes (Part 3). Customer portal, refunds, credit notes, and exports (Part 7). Sending invoices by email. Invoices are not tied to billing periods yet, so staff should avoid invoicing the same period twice by hand.

## Verification

Run the commands in the [foundation verification section](FOUNDATION.md#verification). `billing/tests.py` covers the quotation-to-partial-payment flow, server-side totals, catalog snapshotting, variants, discount scope, minimums, dates, usage limits and their return on cancellation, overpayment and concurrency-safe balance checks, reversals, duplicate references, issued-invoice locking, cancellation rules, numbering per workspace, cross-workspace reads, writes and forged IDs, staff catalog restrictions, plan quantity and availability, the subscription lifecycle, templates, rounding allocation, and rupee formatting.

Billing screens were rendered with dummy data and checked with Hallmark's `sloplint --scope app --render` at 375, 768, 1280, 1440, and 1920 pixels: 0 FAIL, 0 REFLEX, 0 WARN across 17 screens. The Cloudflare security-audit skill was used in guidance mode for tenant isolation and business-logic review (object scoping, forged related IDs, double-spend and limit races, negative and zero amounts). No full six-phase audit was run, and no security or compliance certification is claimed.
