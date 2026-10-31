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

For future screens, use the existing tokens and interaction patterns. Keep invoice creation, checkout, API configuration, and gateway routing for their planned increments. Never present their selection during onboarding as an active integration.
