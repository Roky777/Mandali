# Mandali interface direction

The user preferred a crisp blue-and-white interface. Mandali uses a quiet blue-tinted white ground, dark ink, restrained blue actions, system typography, clear form labels, and a top bar with workspace tabs. No stock images, invented financial metrics, testimonials, or imitation invoice data are used.

The signature surface is the workspace essentials ledger: business identity, currency/timezone, actual team count, and selected channels. Business setup is a direct form. Staff access uses a readable member table on desktop and stacked rows on phones. Error messages accompany form fields; server results are shown inline. JavaScript adds submitting feedback and confirmation for removing staff/revoking invitations; normal server forms still work without JavaScript.

## Requested references

- [Hallmark](https://github.com/nutlope/hallmark/tree/4f6a46c6dc325d40ab268c93e25c7b96676ebccd), revision `4f6a46c6dc325d40ab268c93e25c7b96676ebccd`: read the skill and app-screen guidance, dealt Coral/Dusk/Dose, and selected Dose's restrained app surfaces. The user's blue/white preference overrides the green palette. App-screen guidance permits a system font and skips marketing hero rules. Scaffold-derived `tokens.css` and `base.css` retain the upstream MIT notice in `THIRD_PARTY_NOTICES.md`.
- [Cloudflare security-audit skill](https://github.com/cloudflare/security-audit-skill/tree/c1c8a8c1471069fb0e188eeaff69b8e8db6564a8), revision `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8`: used in guidance mode for source review. No upstream code is included and no full-audit report is claimed.

Hallmark browser checks on the implemented login screen reported zero FAIL, REFLEX, or WARN findings after fixing phone navigation. The finish reviewer inspected actual desktop and phone captures of signup, login, onboarding, overview, and team screens and returned `ship`, with no blocking finding. Long member emails on phones were subsequently improved with stacked rows. The reference scripts live outside this repository; they are not runtime dependencies. No skills were installed globally.

For future screens, use the existing tokens and interaction patterns. Keep invoice creation, checkout, API configuration, and gateway routing for their planned increments. Never present their selection during onboarding as an active integration.
