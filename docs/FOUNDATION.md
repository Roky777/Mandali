# Part 1: foundation

This increment implements account registration, login and password recovery, business onboarding, workspace selection, administrator/staff roles, staff invitations, business settings, and workspace activity. Billing and payment collection are still planned.

## Stack and boundaries

- Django 5.2 LTS, Python 3.12 or newer, server-rendered templates, plain CSS and a small progressive-enhancement script.
- SQLite for a single-instance local foundation. No cloud services, payment credentials, or JavaScript build are required. Evaluate PostgreSQL before a multi-instance production rollout and validate its migration and concurrency behavior separately.
- A global email-based `User` can belong to several businesses through `Membership`. The business role is on the membership, never inferred from platform `is_staff` or `is_superuser` flags.
- `Workspace` holds the merchant's identity, optional GSTIN/website, INR, Asia/Kolkata timezone, and selected future billing channels. No orders, invoices, gateways, or customer portal records exist yet.
- `Invitation` stores a hash of a random bearer token, expiry, issuer, and acceptance/revocation timestamps. Invitees always join as staff. `Activity` records business and team changes. `AuthRateBucket` supports database-backed throttling.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DJANGO_DEBUG=1
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/signup/ and create an account. No seeded accounts or shared passwords are shipped. Create a business and open its Team page to invite staff.

`.env.example` documents environment variables; it is not loaded automatically. Export variables in your shell or supply them through your process manager. If you use another origin or port, also set `MANDALI_BASE_URL` to that exact origin so invitation and reset links work.

## Email and invitations

Local development uses Django's console email backend. Invitations and password-reset messages appear in the server terminal; they are not delivered to a real inbox. An administrator can copy the freshly created staff invitation link and share it directly. Only a token hash is stored, so the original link cannot be retrieved later. Issue a replacement invitation for the same email if needed; the previous pending link is revoked.

For real delivery, configure `DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`, `DEFAULT_FROM_EMAIL`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and `EMAIL_USE_TLS`. Test with your own SMTP account before deployment. Delivery to a real mail service has not been tested in this increment. An SMTP failure is reported honestly for staff invitations; reset requests keep a generic response so they do not disclose account existence.

Invitations expire after 48 hours and can be used once. A new invitee sets their name and password. An existing account must sign in with the invited email before joining; an invitation cannot reset that account's password. Removing staff immediately denies subsequent workspace requests from their existing sessions. A new administrator-issued invitation can restore staff access.

## Permissions

| Action | Business administrator | Active staff | Nonmember |
| --- | --- | --- | --- |
| View business overview, activity, and team | Yes | Yes | No |
| Edit business settings | Yes | No | No |
| Invite staff or revoke invitations | Yes | No | No |
| Remove staff access | Yes | No | No |
| Access another business without membership | No | No | No |

Membership is checked on every workspace request and every mutation. A workspace UUID is not a permission. Queries for invitations and staff changes are constrained to the authorized workspace. Administrator removal, administrator transfer, and staff promotion are deliberately not part of this increment, so the sole administrator cannot accidentally remove themselves through the team UI.

Account creation grants no membership in an existing business. A staff member may independently create their own separate business workspace, without gaining administrator access to the business where they work.

## Security behavior and review limits

Authentication uses Django password hashing, sessions, CSRF middleware, and reset tokens. Emails are normalized and unique case-insensitively. Passwords require at least nine characters, uppercase, lowercase, and a special character, with Django's similarity/common-password checks. Reset tokens expire after one hour; changing a password invalidates older authenticated sessions.

All application responses are private and non-cacheable, use a same-origin referrer policy, prohibit framing, and apply a self-only content security policy. The referrer policy intentionally preserves the same-origin form Origin behavior that CSRF verification needs while withholding referrers from other origins. Passwords, invite tokens, and settings are never placed in browser local storage.

POST limits per direct client IP: login and invite acceptance, 10 per 15-minute fixed window; signup and password reset, 5 per hour; invitation creation, 20 per hour. Counters use conditional database updates and atomic increments. These are a foundation control, not a distributed abuse-prevention system. Configure the reverse proxy to supply a trustworthy `REMOTE_ADDR`, and set operational retention for old rate buckets and expired invitations. The app deliberately ignores arbitrary forwarded-IP headers. A bounded local concurrency check sent 20 reset requests in batches of four: five were allowed, fifteen were limited, and no server errors occurred.

The requested Cloudflare skill was used in guidance mode for a focused source review of authorization, invitations, and rate limiting. A full six-phase security audit has not been run; no security or compliance certification is claimed. Source review identified the SQLite row-lock-dependent rate-counter concern, which was replaced with atomic updates. Relevant regressions are tested locally. The full pre-release audit remains in Part 8.

## Verification

```bash
export DJANGO_DEBUG=1
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python manage.py collectstatic --noinput
```

The test suite covers registration, password policy, login, reset and session invalidation, positive and negative CSRF cases, workspace creation, tenant read/write isolation, staff restrictions, settings, invitation identity/expiry/single-use/replacement/revocation, access removal, throttling, headers, and safe redirects. CI runs these checks on Python 3.12 and 3.14.

A local Chrome browser walkthrough uses dummy accounts to exercise signup, onboarding, team invitation, joining as staff, settings changes, and staff denial. Screens were checked at phone and desktop widths. Hallmark's responsive login sweep also checks widths from 320 to 1920 pixels. See [design direction](DESIGN.md) for the reference revisions and review evidence.

## Deployment preparation

No deployment is included in this PR. Before running with `DJANGO_DEBUG=0`, set a fresh secret key, explicit allowed hostnames, an HTTPS `MANDALI_BASE_URL`, SMTP delivery settings, static asset serving, persistent database storage and backups. Serve `staticfiles/` through the web server after `collectstatic`; Gunicorn does not serve it. Run migrations as a release step and use `gunicorn config.wsgi:application` behind a correctly configured HTTPS proxy. Configure proxy TLS signaling only for a trusted proxy that strips client-supplied equivalents; the app does not blindly trust forwarded TLS headers.

Production defaults enable secure cookies, HTTPS redirect, and one-year HSTS. HSTS subdomain inclusion and preload are intentionally off until domain ownership and subdomain HTTPS coverage are known; `check --deploy` reports those two advisory warnings. Do not enable preload casually. Back up the SQLite database and test restoration; do not put a transient filesystem or multiple independent database files behind several workers/instances. Real SMTP, live hosting, PostgreSQL, email verification, MFA, and enterprise lifecycle/administrator transfer remain future work.

Business administrators control merchant workspaces. The project does not expose Django's platform admin site as a merchant shortcut. Sign-up email ownership is not separately verified yet; do not treat it as a verified customer identity in future payment flows.
