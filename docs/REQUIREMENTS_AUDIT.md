# Requirements review and manual acceptance

Reviewed against the supplied Subscription Management System checklist on 31 October 2026. This change builds on the latest Part 7 portal, reports, credit-note and refund implementation. It does not replace those features or alter production resources.

## Coverage

| Requirement group | Implementation / review evidence |
| --- | --- |
| Account, recovery, roles | Existing unique email and backend composition rules retained. Added live password checklist; reset lifetime reduced to 30 minutes. Existing account, staff authorization and portal isolation tests retained. |
| Products, variants, plans | Product fields, tax setup and plan periods/policies retained. Plans can now reference a product and prefill its price/tax. A plan variant adds its adjustment to the saved plan price; service-level quantity minimums also apply. |
| Quotations | Templates still copy their lines. Template validity is refreshed when marked sent; expired quotations are rejected and marked Expired by the worker. Draft plan quotations prefill the selected plan line, minimum quantity and calculated term end. |
| Subscription actions | Existing backend lifecycle and policy checks retained. Unavailable actions are hidden. Pauses store their date and shift the remaining scheduled billing time on resume. Renewal remains available to staff, and is now available to the owning customer for one billing period where permitted. Provider-owned terms continue to require provider reconciliation. |
| Invoices and payments | Snapshot pricing/tax, due dates, issue/cancel, print, offline/partial payments, overpayment protection and Decimal money retained. Added explicit Email invoice action with delivery failure feedback, a per-invoice limit and secure portal entry. Latest credit/refund balance rules are preserved. PDF downloads use browser Print → Save as PDF. |
| Automation and discounts | New quotation forms offer automatic invoice generation on activation; this creates an invoice-only schedule, not debit authorization. Existing subscriptions retain their settings. Worker retains transactional cycle uniqueness and bounded catch-up. It also expires quotations and closes completed local terms. Eligible discounts apply automatically; lowest final total wins, with stable ID tie-breaking, while an explicitly selected rule takes priority. Usage limits remain transactional. |
| Portal and finance | Existing customer-specific access and finance reports retained. Overview adds finance filters, actual collections, outstanding, overdue tasks, active contracts and normalized MRR. Analytics adds invoice CSV, campaign/creative breakdown, page-view counts, entered ad spend and receipt-based ROAS. |
| Performance and demo | Added workspace/status/date indexes and SQL report aggregations. `seed_demo` generates an isolated 5,000-subscription workspace with disabled schedules and fake customer addresses. Local seeded responses measured 0.254s Overview, 0.047s Analytics, 0.018s subscriptions and 0.013s invoices. These are local Django-client timings, not a production latency guarantee. |
| Release preparation | Focused regression and monetary/authorization tests passed; desktop/mobile review captures included. Production security audit, live gateway certification, concurrency/load testing and a real seven-scene walkthrough still need release verification. No claim of certification or live payment readiness is made. |

## Manual walkthrough

1. Sign up with a weak password, then a valid one. Try duplicate email, password reset and a staff invitation. Check that staff cannot open admin configuration or Analytics.
2. In Catalog, add a product and Odoo variant (+₹560). Create a plan linked to it. Leave price blank to prefill the product price; then set plan price ₹1,000 and minimum quantity 2.
3. Add a customer with an inbox you control. Create a quotation using the plan. Check its prefilled line, quantity, dates and automatic-invoice checkbox. To try a variant, remove the original line, add the plan with that variant and quantity 2; unit price must be ₹1,560 before tax.
4. Send → Confirm → Activate. Check Automation for the invoice-only schedule. Run the worker when its billing date is due; repeat it and verify that no duplicate cycle invoice appears. Pause/resume and forbidden plan actions should be checked too.
5. Add a dated, usage-limited discount. Generate due invoices, verify the discount once, and verify that exhausted/expired rules stop applying. New invoice-only schedules do not authorize a gateway debit.
6. Record a partial payment, then the remaining amount. Try an overpayment. Email the invoice; use the customer portal to view/print it and manage only the customer's permitted subscriptions. Verify credit notes/refunds still reconcile in Reports.
7. Open Overview and Analytics. Filter by date, customer, plan and status. Add campaign tags to a payment link, e.g. `?utm_source=instagram&utm_medium=paid_social&utm_campaign=launch&utm_content=video_a`. Attribution applies to newly created hosted invoices, not existing invoice payment links. Record real payments and enter campaign spend to check ROAS.

Recorded page views include repeat visits and are not unique people. Campaign ROAS uses gross collected payments divided by entered spend; it is not net profit or an ad-platform integration. Spend and visits are campaign-wide even when finance filters narrow invoice receipts. Untracked historical sales remain Unattributed. The six-month chart covers the whole workspace; the three-month scenario assumes unchanged active recurring subtotal, no churn, discounts or new sales.

## Optional demo

After migrating, use an existing account email:

```sh
python manage.py seed_demo --email=YOUR_EXISTING_ACCOUNT_EMAIL --count=5000
```

This creates a separate sample workspace. Do not use it for real billing. Schedules are disabled, reminders are disabled, and customer emails use `example.invalid`.

## Deployment

Merge the PR, then use the existing manual Cloud Run release process in [GOOGLE_CLOUD.md](GOOGLE_CLOUD.md). Apply billing migrations 0008–0011 with the migration identity before serving the new image. Preserve the deployed SMTP and database Secret Manager mappings. No automatic GitHub deployment is added.
