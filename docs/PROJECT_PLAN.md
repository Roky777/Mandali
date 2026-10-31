# Mandali build plan

Updated: 31 October 2026

## Product direction

Mandali will provide automated and manual invoicing, subscription billing, and payment tracking for Indian businesses. The initial market is India, the initial currency is INR, and local payment methods are a priority. International expansion is deferred.

There are two business audiences: those with a website or application, and those without one. Manual workflows remain available to both. A business can use multiple channels simultaneously without creating separate customer or invoice records.

Terminology: a **business** is a merchant using Mandali; a **customer** buys from that business. Every business owns an isolated workspace. Business administrators, staff, and end customers have different permissions. Platform administration must be separate from merchant administration.

This document is a proposed implementation sequence. Part 1 is implemented with Django 5.2 LTS and SQLite for local development; see [foundation documentation](FOUNDATION.md). Part 2 is implemented; see [manual billing documentation](BILLING.md). Part 3 is implemented with Razorpay Test only; see [checkout setup](CHECKOUT.md). Part 4 is implemented; see [API integration](INTEGRATIONS.md). Part 6 is implemented in sandbox with Cashfree as the second gateway; see [gateway routing](GATEWAY_ROUTING.md). Parts 5, 7, and 8 remain planned. Each part gets its own detailed design, implementation, verification, and review before moving to the next. Do not build all parts at once.

## Three ways to use the same billing system

| Channel | Intended workflow |
| --- | --- |
| Website/API | The business authorizes access to its API or sends data to Mandali's API. Map customer, order, and subscription fields, preview the import, and synchronize approved data. Mandali returns invoice and payment status through APIs and webhooks. |
| Hosted links and QR codes | The business creates a shareable checkout or invoice-payment link in Mandali. A QR code opens the same mobile-friendly page. Customers enter the required details, review the amount, and pay without the business needing a website. |
| Manual | Staff creates customers, quotations, subscriptions, and invoices in the dashboard, shares payment links, or records externally collected payments with a reference and audit history. |

An arbitrary website URL is not enough to fetch billing data. API integrations require authorization, a supported connector or documented API, and explicit field mapping. We will implement one real reference integration first rather than promise universal compatibility.

A reusable plan checkout link creates a new checkout session for each customer. A specific invoice link grants access only to that invoice through a scoped, revocable token. Neither should reveal a customer's other invoices. A QR code encodes the link; it is not an independent payment confirmation mechanism or automatically a native UPI payment QR.

## Build sequence

### Part 1: Foundation and business onboarding

**Build:** Select the stack and deployment approach; establish the project structure, database migrations, sign-in and password recovery, isolated business workspaces, admin/staff permissions, and business settings. Capture INR, Asia/Kolkata billing timezone, billing identity, optional GST details, and whether the business wants API, hosted, manual, or combined access. Define the core data model and choose an original visual direction using Hallmark.

**Reviewable result:** An administrator can create a business workspace, configure it, and add staff. Two businesses cannot access each other's data. Main navigation and the onboarding screens are usable on mobile and desktop.

**Boundary:** No real payments, automatic charges, or complete billing engine in this part. The foundation implementation selects Django 5.2 LTS with server-rendered pages and SQLite for local development.

### Part 2: Manual billing and the shared billing core

**Build:** Customers; products and variants; reusable quotation templates with validity periods; daily, weekly, monthly, and yearly plan definitions; quotations; manual subscriptions; one-time invoices; quantities, prices, configured taxes, discounts, due dates, invoice numbering, printable/PDF invoices, and externally collected payment records. Preserve the original PDF's product, quotation, plan-policy, and discount requirements. Support partial payments and outstanding balances. Validate India-specific invoice requirements for the chosen business context before claiming tax readiness.

**Reviewable result:** Staff can create a customer, prepare a quotation, issue an INR invoice, record a partial offline payment, and see the correct remaining balance. Totals are calculated on the server. Discount eligibility, expiry, and usage limits are enforced.

**Status:** Implemented. Corrections use cancellation with a reason; credit notes remain in Part 7. Tax handling applies one configured rate per line and does not claim GST invoice compliance. Printable invoices use the browser's print-to-PDF.

**Boundary:** Plan definitions and manually managed subscriptions exist; recurring scheduling comes in Part 5. Issued invoice amounts are preserved; corrections use a documented cancellation or credit-note workflow rather than silent edits.

### Part 3: Hosted checkout, links, QR codes, and one gateway

**Build:** A branded mobile checkout, reusable product/plan links, invoice-specific payment links, downloadable QR codes, link expiry/revocation, one-time payment collection through one gateway in sandbox, payment attempts, signed webhook handling, receipts, and reconciliation of uncertain payment results. Design the gateway adapter boundary so a second provider can be added later. Gateway credentials and test/live modes are scoped to each business.

**Reviewable result:** A business without a website shares a link or QR; a customer completes a sandbox payment; verified server-side evidence updates the correct invoice once. Repeated callbacks cannot create duplicate payment records. Expired or revoked links cannot be used.

**Status:** Implemented with Razorpay Test: encrypted workspace credentials, server-side capture verification, replay protection, separate sandbox invoices, reconciliation, and mobile receipts. Real provider-account testing remains required before release.

**Boundary:** Only sandbox collection is enabled. Merchant eligibility and enabled payment methods depend on the provider account. Subscription links can collect customer details or an initial payment here; automatic renewal requires Part 5.

### Part 4: Website/API integration

**Build:** Business-scoped API keys, documented customer/order/subscription ingestion endpoints, idempotency keys, one authorized pull connector, external-ID mapping, sync previews and logs, retry handling, and outgoing signed event notifications. Define which system owns each field and how updates and deletions are handled. Secrets stay on the server.

**Reviewable result:** A sample business website supplies a customer and order, Mandali creates the appropriate invoice or checkout session, and the website receives the verified payment result. Replaying or resynchronizing the same source record does not duplicate the invoice.

**Status:** Implemented before Part 3, so payment events currently come from offline payments that staff record. The pull connector covers customers. Pulling orders, scheduled syncs, and platform-specific connectors remain later increments.

**Boundary:** No generic scraping. Additional website platforms and connectors are separate increments. Manual and API changes share an audit trail and explicit conflict rules.

### Part 5: Recurring billing and automation

**Build:** Activate the subscription lifecycle and plan-specific pause, renewal, and closure rules. Add reliable billing jobs, timezone-aware period calculation, invoice scheduling, eligible discount application, reminders, and bounded failure recovery. Add provider-supported recurring collection only after a customer authorizes an appropriate mandate. Provide invoice automation with customer-initiated payment as an option independent of auto-debit.

**Reviewable result:** A due subscription produces exactly one invoice per billing period even when a job is retried. Pausing or closing a subscription prevents future scheduled billing as configured. Mandated collection, failed collection, cancellation, and manual intervention have explicit, tested outcomes.

**Boundary:** Choose one scheduling authority for each subscription: Mandali or the provider. Never let both independently trigger the same charge. Existing paid invoices and completed periods are not regenerated when settings change.

### Part 6: Gateway recommendations and automatic selection

**Build:** Integrate a second eligible gateway. Offer a recommendation during onboarding, a merchant-selected default, and an opt-in automatic routing policy. Filter by connected and enabled merchant account, supported method, currency, transaction type, recurring capability, amount limits, and token/mandate compatibility before ranking eligible options. Rank by configured priorities such as estimated effective fees, settlement needs, current availability, and measured payment success. Explain each recommendation and preserve manual override and routing history.

**Reviewable result:** The same eligible payment is routed predictably under a saved policy, with an explanation. An incompatible gateway is never selected. A merchant can change the default or disable automatic selection. Missing performance data is shown as unknown, with a deterministic default.

**Status:** Implemented in sandbox. Cashfree is the second gateway. Fees, limits, and settlement times are entered by the merchant, and success rates are measured from the business's own orders. Real provider verification on a deployed HTTPS service is still required.

**Boundary:** There is no universally best gateway. Recommendations require current, merchant-specific inputs; no invented fees or success-rate claims. Automatic selection cannot open or approve a merchant account. Do not assume tokens or mandates can move between providers. On a timeout, reconcile the original attempt before considering a new charge; fallback is allowed only after a definitive safe failure and with a compatible payment authorization.

### Part 7: Customer portal, reporting, and operational controls

**Build:** Customer access to their own invoices, receipts, and subscription actions; business dashboards for collections, outstanding balances, overdue invoices, and active subscriptions; filters and exports; refund and credit-note workflows; and clear activity history. Separate invoice totals, payment receipts, refunds, and recurring revenue metrics so reports have explicit meanings.

**Reviewable result:** A business can follow a payment from source event to invoice, receipt, and report. A customer sees only their own records. Partial payments and refunds reconcile correctly. Manual tasks and failed automation are visible for staff action.

### Part 8: Security audit, reliability, and controlled release

**Build:** Run the Cloudflare security-audit workflow against implemented code; validate and fix findings; check data isolation, authorization, public links, API keys, webhook replay handling, and payment-state transitions. Exercise delayed/out-of-order events, concurrent manual/API actions, failed billing jobs, and restore procedures. Add monitoring, backups, rate limits, operational documentation, and measured performance checks. Complete live-provider verification and a small controlled pilot before wider release.

**Reviewable result:** Publish review evidence and known limitations, resolve confirmed release-blocking issues, demonstrate backup restoration, and measure the original under-two-second page target on a representative dataset of thousands of subscriptions under a documented load and environment.

**Boundary:** Security checks start in Part 1 and accompany every part; this final review consolidates them. A skill run is evidence to assess, not a security or compliance certification.

## Shared rules from the beginning

- Keep one invoice and payment model across API, hosted, and manual channels. Record the source channel and external identifiers.
- Separate invoice lifecycle, subscription lifecycle, and individual payment-attempt state. A failed attempt does not erase an invoice balance.
- Use explicit money precision and rounding rules, currency on every monetary record, server-side totals, and historical price/tax snapshots.
- Prevent duplicate billing with database constraints, idempotency, and transactional updates. Use durable jobs for asynchronous work.
- Verify provider events and reconcile ambiguous outcomes. A browser redirect, customer screenshot, or QR scan is not sufficient proof of payment.
- Record manual payment references, actor, time, reason, and reversals. Detect references already recorded by an integration.
- Use gateway-hosted payment collection or supported tokenization; do not collect or store raw card details in Mandali.
- Gate auto-collection on valid authorization and provider capability. Turning on automated invoicing does not authorize debiting a customer.
- Preserve all original PDF requirements unless an explicit later decision changes them, including password recovery, admin-only staff creation, product variants, quotation templates, plan policies, taxes, discounts, and reporting.

## How the requested repositories will be used

### Hallmark

[nutlope/hallmark](https://github.com/nutlope/hallmark) is a design skill. Before interface implementation, read its `skills/hallmark/SKILL.md` and relevant references, record the upstream revision, and use its design and browser-review workflow for an original Mandali interface. Preserve working interactions, accessible forms, responsive layouts, and honest product states. It is not a billing framework. Review license and attribution requirements before reusing assets.

### Cloudflare security-audit skill

[cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill) supplies a structured security review workflow. Read its `SKILL.md` and referenced instructions before auditing, record the upstream revision, use its coverage and finding validators, and distinguish confirmed findings from unresolved leads. Apply focused review as features land and full review before release. No audit has been run on this documentation-only repository.

These repositories have been reviewed as planning inputs; they have not yet been installed or executed. Install or vendor only the needed reviewed material during the appropriate implementation part, with license notices preserved.

## Product references, not copies

- [OpenPay payment routing](https://docs.getopenpay.com/get-started/set-up-your-account/settings/payment-routing): reference for processor policies and routing dimensions.
- [OpenPay API tour](https://docs.getopenpay.com/developer-resources/api-tour): reference for object relationships and event-driven integrations.
- [Chargebee hosted checkout](https://www.chargebee.com/docs/billing/2.0/hosted-capabilities/hosted-checkout): reference for hosted customer journeys and integration choices.
- [Chargebee routing guidance](https://www.chargebee.com/docs/payments/2.0/kb/billing/how-do-i-process-transactions-or-plans-through-a-specific-gateway): reference for defaults and explicit routing configuration.

For this plan, OpenPay means getopenpay.com, the subscription billing and payment orchestration product. These references inform requirements; Mandali's interface, language, architecture, and implementation will be its own. Their features do not imply that Mandali has those features today.

## Decisions for Part 1

Confirmed: India first, INR, businesses with or without websites, API plus hosted links/QR plus manual workflows, automated and manual invoicing, optional automated collection, gateway recommendations and later automatic routing, incremental delivery.

Stack selected for Part 1: Django 5.2 LTS with SQLite locally. Hosting is selected: Google Cloud Run, Cloud SQL PostgreSQL, Artifact Registry, and Secret Manager; see [deployment guide](GOOGLE_CLOUD.md). Still to select during later design: first supported website/API shape; live gateway merchant eligibility; supported business/tax scenarios; partial-payment policies; subscription upgrade/downgrade and proration rules; delivery provider; and business branding controls. Validate current provider capabilities, account access, and tax obligations at implementation time rather than hard-coding assumptions now.

Parts 1 through 4 are implemented for review. Next implementation task after that review: Part 5 only. Continue to take each later part as a separate increment.
