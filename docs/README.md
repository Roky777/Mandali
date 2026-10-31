# Mandali documentation

Start with the task you are doing. The app keeps the existing theme, routes, and billing behavior.

## Use the product

1. [Accounts, workspaces and team access](FOUNDATION.md)
2. [Catalog, quotations, subscriptions and manual billing](BILLING.md)
3. [Payment links, QR codes and sandbox checkout](CHECKOUT.md)
4. [Recurring invoices, reminders and renewals](RECURRING.md)
5. [Customer portal and finance reports](PORTAL_AND_REPORTS.md)
6. [Gateway selection and routing](GATEWAY_ROUTING.md)
7. [Website APIs and synchronization](INTEGRATIONS.md)

[Requirements coverage and the seven-scene manual walkthrough](REQUIREMENTS_AUDIT.md) connect the implementation to the supplied problem statement. [The project plan](PROJECT_PLAN.md) records scope and remaining boundaries.

## Run and deploy

- [Google Cloud setup, migrations and releases](GOOGLE_CLOUD.md)
- [Release checks and operational commands](RELEASE.md)
- [Design decisions and screenshots](DESIGN.md)

## Find the code

| Directory | Owns |
| --- | --- |
| `config/` | Environment settings and composition of app URL configurations |
| `core/` | Accounts, workspace access, invitations, health endpoints, and sidebar selection |
| `billing/` | Catalog, quotations, subscriptions, invoice ledger, adjustments and acquisition analytics |
| `recurring/` | Schedules, billing cycles, reminders and provider-owned recurring collection |
| `checkout/` | Shareable links, gateway adapters, signed payment evidence and one-time routing |
| `portal/` | Customer sign-in and access to the customer's own records |
| `reports/` | Finance summaries, aging, operational queues and exports |
| `integrations/` | API ingestion, synchronization and outgoing webhooks |
| `operations/` | Request monitoring, data maintenance and ledger checks |
| `templates/<app>/` | Server-rendered screens for the corresponding app |
| `static/` | Shared design assets and browser interactions |
| `scripts/`, `deploy/` | Deployment helpers, production image checks and example configuration |

Keep feature URLs in their owning app. `config/urls.py` only connects those URL configurations. Route names and public paths remain stable for existing links, emails and integrations. Sidebar selection is presentation only: permission checks stay in views and services.

## Quick manual polish check

- Open a recurring setup or renewal form: **Automation** alone should be selected in the sidebar; Back/Cancel returns to its subscription.
- Open **Create payment link**: Back/Cancel returns to Payment links.
- Open bank matching, a receipt or a credit note: **Invoices** stays selected.
- Open a staff invitation: **Team** stays selected.
- Sign in as staff: Analytics, Integrations and Settings remain hidden and their existing backend restrictions remain in effect.

No styling or database migration is needed for this cleanup.
