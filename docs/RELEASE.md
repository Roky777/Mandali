# Part 8: release preparation

This increment adds the tools and fixes needed before a controlled pilot, plus the remaining requirement extras.

## Security review (partial pass)

Cloudflare security-audit skill, revision `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8`, quick profile, run by a single reviewer. Two things were not done as the skill requires: hunting by isolated agents, and independent verification. The verifier agent stopped early on a usage limit, so no finding below is independently verified. This is not a complete audit or a certification.

Covered by source review:

- every route's authorization (all workspace views go through `workspace_access`, and every lookup is scoped to the workspace);
- template escaping and use of `|safe`;
- raw SQL;
- CSRF exemptions (only the signed gateway webhooks and the key-authenticated API);
- outbound requests;
- the portal sign-in tokens;
- ledger races;
- CSV export.

| Result | Detail | Status |
| --- | --- | --- |
| Confirmed, low | Anyone holding a public product or plan checkout link could enter another customer's email. Hosted checkout attached the invoice to that customer, so an issued test invoice under a name the attacker chose appeared in the victim's portal. | Fixed: the portal hides hosted-checkout invoices that are sandbox or unpaid (`portal/views.py`). |
| Needs validation | Rate limits use `REMOTE_ADDR` unless `trusted_proxy_hops` is set. Whether Cloud Run gives each client its own address is a deployment fact. | Owner check: from two networks, make 11 failed logins on one and confirm the other can still log in. Set `trusted_proxy_hops` to match the real forwarding chain. |
| Reliability fix | Reversing a payment checked refunds and then updated separately, and always reset the status to Issued. | Fixed: the check is now part of a single update, and settlement is re-checked afterwards. |
| Hardening | Portal sign-in links were limited only per IP address. | Added a limit of 3 per recipient per hour. |
| Hardening | Unauthenticated API calls had no limit. | Added a limit of 30 per 5 minutes per IP address. |

## Operations

- `python manage.py check_ledger`: verifies that paid, credited, and refunded totals match their documents; that invoice totals match their lines; status consistency; gateway evidence; and discount usage. It exits non-zero on any failure. Run it daily, and after every restore.
- `python manage.py prune_operational_data [--dry-run]`: removes expired rate-limit buckets, portal links, invitations, API idempotency records older than 30 days, and finished webhook deliveries older than 90 days. It also clears customer data from sync previews older than 90 days, and expired sessions. Ledger records and activity are never removed.
- **Monitoring:** every request is logged as one JSON line with its route pattern (never the path with tokens), status, and duration. Requests over 2 seconds and server errors log at WARNING. In Cloud Logging, alert on `jsonPayload.message="slow request"` and on 5xx responses.
- **Pilot:** set `MANDALI_PILOT_ALLOWLIST=owner@shop.in,@trusted.in` so that only those emails and domains can sign up and create businesses. Invited staff can still join.
- **Backups:** turn on Cloud SQL automated backups and point-in-time recovery. Before the pilot, restore one backup to a new instance, run `check_ledger` against it, and record the result.

## Requirement extras added

- **GST split:** invoices and printed invoices show CGST and SGST when the business and customer GSTIN states match, or when the customer has no GSTIN. They show IGST when the states differ. Place of supply is inferred from the GSTIN only.
- **UPI QR:** add a UPI ID in Settings. Issued real invoices with money due then show a UPI QR code on the invoice page, the printed invoice, and the customer portal. Record the payment when it arrives; a scan is not proof of payment.
- **WhatsApp share:** issued invoices have a **Share on WhatsApp** button with a prefilled summary, sent to the customer's phone when known.
- **Overdue reminders:** a **Remind** button on overdue rows in the invoice list, and **Send reminder** on overdue invoices. Each is limited to 5 emails per invoice per hour.
- **Late payer tag:** a customer with two or more invoices that are overdue now, or that were paid after their due date.
- **Discount usage:** shown on the invoice, for example "Used 32 of 100 times".
- **Product margin:** shown in the catalog and on the product page (sales price minus cost price, and the percentage).
- **Bank statement matching:** under **Invoices → Match bank statement**, upload a CSV with date, amount or credit, and description or narration. An invoice number in the description wins; otherwise only a unique exact balance is suggested. Review, then record the matches as bank transfers.
- **Bulk billing:** **Automation → Due today** lists invoice schedules due now. **Generate N invoices now** bills them immediately, each period once, safe even if the worker also runs.

## Manual checks

1. **Settings:** add a GSTIN and a UPI ID such as `shop@okaxis`.
2. **Invoices:** issue an invoice to a customer whose GSTIN is from another state, and check that it shows IGST. Use a customer from the same state, or one without a GSTIN, and check CGST and SGST.
3. **UPI and WhatsApp:** on that invoice, scan the UPI QR with a phone. The amount and note should be prefilled; don't pay. Then press **Share on WhatsApp**.
4. **Reminders:** set an issued invoice's due date in the past, for example with a short payment term, and press **Remind** in the list.
5. **Bank statement:** create a CSV with `Date,Narration,Credit` and the row `31/10/2026,NEFT INV-00001,1180`. Upload it, record the match, and check the invoice is paid.
6. **Bulk billing:** with an invoice-only schedule due today, open **Automation** and generate. Generate again and confirm no duplicate invoice appears.
7. **Ledger and pruning:** run `python manage.py check_ledger`, then `python manage.py prune_operational_data --dry-run`.
8. **Portal fix:** open a product checkout link, enter an existing customer's email, and stop before paying. That customer's portal should not show the new invoice.
