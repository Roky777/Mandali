# Mandali interface direction

The user preferred a crisp blue-and-white interface. Mandali uses a quiet blue-tinted white ground, dark ink, restrained blue actions, system typography, clear form labels, and a top bar with workspace tabs. No stock images, invented financial metrics, testimonials, or imitation invoice data are used.

The signature surface is the workspace essentials ledger: business identity, currency/timezone, actual team count, and selected channels. Business setup is a direct form. Staff access uses a readable member table on desktop and stacked rows on phones. Error messages accompany form fields; server results are shown inline. JavaScript adds submitting feedback and confirmation for removing staff/revoking invitations; normal server forms still work without JavaScript.

## Requested references

- [Hallmark](https://github.com/nutlope/hallmark/tree/4f6a46c6dc325d40ab268c93e25c7b96676ebccd), revision `4f6a46c6dc325d40ab268c93e25c7b96676ebccd`: read the skill and app-screen guidance, dealt Coral/Dusk/Dose, and selected Dose's restrained app surfaces. The user's blue/white preference overrides the green palette. App-screen guidance permits a system font and skips marketing hero rules. Scaffold-derived `tokens.css` and `base.css` retain the upstream MIT notice in `THIRD_PARTY_NOTICES.md`.
- [Cloudflare security-audit skill](https://github.com/cloudflare/security-audit-skill/tree/c1c8a8c1471069fb0e188eeaff69b8e8db6564a8), revision `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8`: used in guidance mode for source review. No upstream code is included and no full-audit report is claimed.

Hallmark browser checks on the implemented login screen reported zero FAIL, REFLEX, or WARN findings after fixing phone navigation. The finish reviewer inspected actual desktop and phone captures of signup, login, onboarding, overview, and team screens and returned `ship`, with no blocking finding. Long member emails on phones were subsequently improved with stacked rows. The reference scripts live outside this repository; they are not runtime dependencies. No skills were installed globally.

## Part 2: manual billing screens

Billing reuses the same tokens, top bar, and tabs, with Customers, Subscriptions, Invoices, and Catalog added between Overview and Team. The tab row scrolls sideways on phones rather than wrapping. Catalog sections and status filters are underlined subtabs, and filters live in the URL. Lists are full-width tables that restack into labelled rows on phones. Each list draws its empty and filtered-empty states.

The signature surface is the invoice balance ledger, beside the lines: subtotal, discount, tax, total, payments, and what remains, with the one primary action for the invoice's state (issue, or record a payment). Subscriptions show their lifecycle as a step rule above the details. Status tags use the accent only for active and paid, the error colour only for overdue. Font weights were reduced to 400, 600, and 700 across the app. The printable invoice is a plain sheet that hides app chrome in print.

Rendered with dummy data, Hallmark's `sloplint --scope app --render` reported 0 FAIL, 0 REFLEX, and 0 WARN across 17 billing screens after fixing a wrapping invoice number and a fourth font weight. Phone and desktop captures of the invoice, subscription, print, list, and form screens were reviewed by hand. A spacing gap in the payment panel and a wrapping section heading were fixed as a result. No finish-review subagent was run for this increment.

## Part 4: integration screens

Integrations is an administrator-only tab between Catalog and Team, with API keys, Webhooks, and Website sync as subtabs. Key and endpoint creation reuse the team invitation strip. Secrets appear once on a dedicated page, in the same pattern as invitation links. Codes, keys, and event names use the token monospace face, because they are literal values people copy. A curl example sits in a scrolling code block. The website sync preview is the signature: counts of what would be added, updated, unchanged, or skipped, then each record with its reason, and nothing changes until **Apply**. On phones, the fetch action sits above the sync history.

Rendered with dummy data, `sloplint --scope app --render` reported 0 FAIL, 0 REFLEX, and 0 WARN across 26 screens, after replacing capitalised sample codes, straight apostrophes, and a wrapping settings link. Captures of the keys, webhook, sync, and preview screens were reviewed at desktop and phone widths. A run-together key prefix and history line were fixed as a result. No finish-review subagent was run.

## Part 6: gateway selection screens

Gateway settings now have one subtab per provider, plus **Gateway selection**. The selection page leads with a comparison table where every missing figure reads **Unknown**, never a placeholder number. Below it sit the routing policy as a plain radio choice, the recommendation tool (a GET form, so its answer lives in the URL), manual overrides, and routing history with the full sentence explaining each decision. The routing history is the signature: every payment's gateway, and why. Status colour stays limited: connected and recommended use the accent soft fill; not routed uses the error outline.

Rendered with dummy data, `sloplint --scope app --render` reported 0 FAIL, 0 REFLEX, and 0 WARN across 32 screens, after capping a long help line. Captures were reviewed at desktop and phone widths, and oversized radio buttons and a truncated override select were fixed as a result. No finish-review subagent was run.

## Part 7: portal, adjustments, and reports

Credit notes and refunds sit in the invoice ledger, which now shows credit notes, refunds, and a red **Refund due to customer** line when money is owed back. The credit-note form is a disclosure, so the panel stays calm. Receipts and credit notes reuse the printable invoice sheet. The customer portal uses the public checkout shell with a **Customer portal** tag, and shows the same ledger and status tags as staff screens, in plain words. Reports open with two stat rows, each figure followed by its own definition, then aging and method tables. **Needs attention** lists only the kinds of work that exist right now. Reports is the signature: every figure states exactly what it counts.

Rendered with dummy data, `sloplint --scope app --render` reported 0 FAIL and 0 WARN across 43 screens. Three REFLEX findings were waived on the record: real customer names wrap at 320px, and a closed disclosure is measured while hidden. A credit-note reason overflowing on phones and a crowded stat row were fixed after reviewing the captures. No finish-review subagent was run.

For future screens, use the existing tokens and interaction patterns. Keep invoice creation, checkout, API configuration, and gateway routing for their planned increments. Never present their selection during onboarding as an active integration.

## Part 3 review

Checkout preserves the existing blue-and-white ledger system. Hallmark rendered the public checkout at 1280, 1440, 1920 and 375px: 0 FAIL, 0 REFLEX, 0 WARN. Ten additional browser captures covered sharing/QR, buyer details, review, prepared payment, and receipt at desktop and mobile widths with no horizontal overflow. The independent finish reviewer found one missing buyer status-recovery control; it was added alongside Pay and the browser flow was recaptured. Payment/browser evidence used local fixtures, not a real provider transaction. Cloudflare security guidance informed focused source checks of tenant scoping, signatures, encrypted credentials, and replay handling; no full security audit or certification is claimed.

## Part 5 review

Automation extends the same top tabs, ledger, and labelled mobile tables. Its signature pairs immutable billing-cycle history with the provider authorization state. Twelve desktop/375px browser captures covered setup, invoice history, authorization preparation, verified captures, and overview; the walkthrough exercised reminders, pause/resume, term renewal, and provider cancellation with local fixtures. The independent finish review found five issues in paused renewal, rejected-action recovery, unpaid review replay, secondary text spacing, and state-specific controls. They were fixed, tested, and recaptured; confirmation returned ship. This is focused implementation review, not a full security audit or genuine provider authorization test.

## Hallmark workspace redesign, October 2026

Applied nutlope/hallmark v2.0.0 at revision `4f6a46c6dc325d40ab268c93e25c7b96676ebccd`. Retained Mandali's blue/white brand and system fonts; introduced grouped sidebar navigation, a native mobile disclosure menu, a calm finance row and a collections chart beside overdue work. Advanced filters stay available in a disclosure rather than crowding the main task. Existing destinations, forms, credit/refund ledgers and reports remain available. Customer portal and checkout keep their established public shell.

Before/after review used locally rendered Django pages with sample data. Thirty desktop/mobile captures cover Overview, Analytics, Reports, invoice, quotation form and login. The kept-content comparison preserves 31/31 links, 16/16 original fields, 5/5 script hooks and 8/8 figures; the new form fields are additive. Dynamic CSRF values were normalized only in review snapshots. Three intentionally changed headings replace the old workspace title and placeholder sections; the workspace name remains in navigation.

Current captures in Hallmark's Garden theme (warm oat paper, deep green ink, serif headings, leaf green for actions and money in, clay for pending): [overview](screenshots/app-overview.png), [mobile overview](screenshots/app-overview-mobile.png), [invoice](screenshots/app-invoice.png), and [analytics](screenshots/app-analytics.png).

The final source/render audit has 0 FAIL and 0 REFLEX findings. Retained warnings concern inherited CSS recipes (the old mark contrast and shared box/button rules, overridden by the new workspace stylesheet), an existing prose apostrophe, and the three intentional heading changes. Two inherited hidden-content measurement findings are waived by the tool: the closed tracking disclosure and invoice credit-note disclosure. These were visually reviewed rather than changing their working interactions. Review was performed in-thread without subagents.
