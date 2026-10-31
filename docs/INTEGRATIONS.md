# Part 4: website and API integration

This increment connects a business's own website or app to Mandali through three paths that share the same customers and invoices as manual billing:

- **Push API:** the website sends customers, orders, and subscriptions with a business API key.
- **Webhooks:** Mandali sends signed invoice and payment status events back to the website.
- **Website sync:** Mandali pulls the business's customer list from its own HTTPS API, after field mapping and a reviewed preview.

Online payment collection is not part of this increment (Part 3). A payment event today means staff recorded a payment collected outside Mandali.

## Turning it on

An administrator enables **Website / API integration** in **Settings**, then opens **Integrations**. Only administrators can see Integrations. When the setting is off, API keys are refused (`403 channel_disabled`) and no keys, endpoints, or syncs can be added.

In **Catalog**, give each product and plan the website will reference a **code** (letters, numbers, dots, dashes, underscores; unique per business).

## API reference

Base URL: `https://<your Mandali origin>/api/v1/`. Send `Authorization: Bearer mdl_...` on every request. POST bodies are JSON with `Content-Type: application/json`. Money in responses is a decimal string in INR (`"2360.00"`). Amounts are never accepted from the client: Mandali prices every line from the catalog and calculates taxes and totals.

| Method and path | Purpose |
| --- | --- |
| `GET ping` | Check a key. Returns the business name. |
| `POST customers` | Create or update a customer by `external_id`. `201` created, `200` updated or unchanged. |
| `GET customers/{external_id}` | Read a linked customer. |
| `POST orders` | Create a one-time invoice for an order. Issued by default; send `"issue": false` for a draft. |
| `GET orders/{external_id}` | Invoice status, totals, amount paid, balance, and lines. |
| `POST subscriptions` | Create a draft quotation on a plan for staff to review, send, and activate. |
| `GET subscriptions/{external_id}` | Subscription status and lines. |

### Order

```json
{
  "external_id": "order-1001",
  "customer": {"external_id": "cust-42", "name": "Asha Traders", "email": "asha@example.com"},
  "lines": [{"product": "retainer", "quantity": 2}, {"plan": "studio", "quantity": 2}],
  "payment_terms_days": 15,
  "notes": "Thank you for your order.",
  "issue": true
}
```

`customer` is either a customer object (created or updated first) or the `external_id` string of an existing linked customer. Each line names exactly one `product` or `plan` code. Plan minimum quantity and availability dates are enforced. At most 100 lines.

### Subscription

```json
{"external_id": "sub-77", "customer": "cust-42", "plan": "studio", "quantity": 2, "start_date": "2026-11-01", "lines": [{"product": "setup"}]}
```

Website subscriptions arrive as drafts. Staff move them through the usual lifecycle. Recurring invoices remain Part 5.

### Errors

```json
{"error": {"code": "invalid_lines", "message": "Line 1 is not valid.", "fields": {"lines.0": {"quantity": ["This plan needs a quantity of at least 2."]}}}}
```

| Status | Codes |
| --- | --- |
| 400 | `invalid_json`, `invalid_idempotency_key` |
| 401 | `unauthorized` (missing, wrong, or revoked key) |
| 403 | `channel_disabled` |
| 404 | `not_found` (also for records of another business) |
| 409 | `duplicate_order`, `duplicate_subscription`, `in_progress` |
| 415 | `unsupported_media_type` |
| 422 | `invalid_record`, `invalid_customer`, `invalid_lines`, `unknown_item`, `unknown_customer`, `invalid_request`, `billing_rejected`, `idempotency_mismatch` |
| 429 | `rate_limited`, with `Retry-After`. The limit is 300 requests per 5 minutes per key. |

A rejected request changes nothing: the whole request runs in one database transaction.

## Idempotency and replays

Every POST can carry an `Idempotency-Key` header (up to 100 characters). Orders and subscriptions without one use their `external_id` as the key. A retry with the same key and the same body returns the original response with `Idempotent-Replayed: true` and creates nothing. The same key with a different body returns `422 idempotency_mismatch`. Only successful responses are saved, so a rejected request can be corrected and resent with the same key. A second order with an existing `external_id` under a different key returns `409 duplicate_order`. External IDs are unique per business, backed by database constraints.

## Field ownership

| Record | Owner | Updates | Deletions |
| --- | --- | --- | --- |
| Linked customer contact fields | The website | A field the website sends replaces Mandali's value; an omitted field is kept. Staff edits are overwritten by the next update. | Not propagated. Deleting a customer on the website leaves it in Mandali. |
| Invoices from orders | Mandali, once created | Orders are create-only. Corrections use Mandali's cancel-and-reissue workflow. | Not propagated. |
| Subscriptions | Mandali, once created | Create-only. Lifecycle changes happen in Mandali. | Not propagated. |
| Catalog (prices, taxes, codes) | Mandali | Managed by administrators. | Not applicable. |

Customers are matched by `external_id` only. An email already used by a different customer is a `422` error, never a silent merge. API customers and website-sync customers share one external ID space.

## Webhooks

Add an `https://` endpoint under **Integrations → Webhooks**. The signing secret is shown once. Events: `invoice.issued`, `invoice.paid`, `invoice.cancelled`, `payment.recorded`, `payment.reversed`, plus `ping` from **Send test event**.

```json
{"id": "6f1c…", "workspace": "<workspace id>", "type": "invoice.paid", "created": "2026-11-01T10:15:00+00:00",
 "data": {"invoice": {"number": "INV-00012", "external_id": "order-1001", "status": "paid", "total": "2360.00", "amount_paid": "2360.00", "balance": "0.00"},
          "payment": {"amount": "2360.00", "method": "upi", "reference": "UTR123", "reversed": false}}}
```

Headers: `Mandali-Event-Id`, `Mandali-Event-Type`, and `Mandali-Signature: t=<unix seconds>,v1=<hex>`. Verify every delivery before trusting it:

```python
import hashlib, hmac, time

def verify(secret, header, body):
    parts = dict(item.split("=", 1) for item in header.split(","))
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
    fresh = abs(time.time() - int(parts["t"])) < 300
    return fresh and hmac.compare_digest(expected, parts["v1"])
```

Use the raw request body, reject stale timestamps, and store processed event IDs: a delivery can arrive more than once, and events can arrive out of order, so compare the invoice's `status` and `amount_paid` rather than assuming order.

Events are queued in the same transaction as the billing change, so a rolled-back change sends nothing. Mandali tries once right after the change commits. Any 2xx response counts as delivered. Failures retry after 1, 5, 30, 120, 360, 720, and 1440 minutes, then stop, and an administrator can **Send again**. Each attempt is leased with a conditional update so two workers never send the same delivery at once. Pausing an endpoint stops new events for it; events during the pause are not sent later.

`python manage.py deliver_webhooks` sends due retries. It must run on a schedule; see [Google Cloud deployment](GOOGLE_CLOUD.md#webhook-retries).

## Website sync

Under **Integrations → Website sync**, an administrator saves an `https://` URL that returns customers as JSON, an optional list path (for example `data.customers`), and which key holds each field (dotted paths such as `contact.email` work). Customer ID and name are required.

**Fetch preview** sends one GET, with an optional access token as `Authorization: Bearer`. The token is used for that fetch only and never stored. Mandali validates every record against the same rules as manual customers and shows what would be added, updated, unchanged, or skipped, with reasons: missing ID, invalid GSTIN, an email used by another customer, or a duplicate ID or email in the response. Nothing changes until **Apply**. Applying validates each record again, applies once even if clicked twice, and logs counts to workspace activity. Failed fetches are recorded with the reason and can be retried. Up to 500 records per fetch; page larger lists on the website side.

## Outbound request safety

Webhook and sync URLs are chosen by business administrators, so Mandali treats each outbound request as potential server-side request forgery:

- Only `https://` on port 443, with no credentials in the URL.
- The hostname must resolve only to public addresses. Loopback, private, link-local (including the cloud metadata server at 169.254.169.254), carrier-grade NAT, and IPv4-mapped forms are refused.
- The connection is pinned to the checked address, so DNS rebinding cannot redirect it, and TLS still verifies the hostname.
- Redirects are not followed. Each request has a 10-second timeout and a 2 MB response cap.

For local development only, `DJANGO_DEBUG=1` with `MANDALI_ALLOW_LOCAL_INTEGRATIONS=1` allows `http://localhost` targets, so a sample website on your machine can be tested. Production ignores the flag.

## Security notes

- API keys are 256-bit random values. Mandali stores only a SHA-256 digest and a short prefix for recognition. Revoke a key to refuse it immediately.
- API authentication never accepts a browser session, and API routes are exempt from CSRF only because they accept no cookies for authentication.
- Every lookup is scoped to the key's business. Another business's external IDs return `404`.
- Webhook signing secrets are stored server side because each delivery is signed with them. They are shown once in the interface.
- The Cloudflare security-audit skill was used in guidance mode for this increment's trust boundaries: key authentication, tenant scoping, idempotency races, outbound request forgery, and webhook signing. No full audit was run, and no security certification is claimed.

## Verification

`integrations/tests.py` covers key hashing, revocation, session rejection, the channel switch, server-side order totals, replay without duplicates, idempotency mismatch, rollback of rejected requests, validation errors, customer upsert and email conflicts, subscription ingestion, cross-business isolation, rate limiting, webhook queuing, signing, backoff, give-up and resend, lease claiming, private and metadata address refusal, the local-development flag, sync preview without changes, apply once, revalidation at apply, fetch failures, and the admin-only screens.

The integration screens were rendered with dummy data and checked with Hallmark's `sloplint --scope app --render`: 0 FAIL, 0 REFLEX, 0 WARN across 26 screens, including the billing screens.

## Not in this increment

Online payments, checkout links, and QR codes (Part 3). Recurring billing (Part 5). Pulling orders or subscriptions, scheduled automatic syncs, and platform-specific connectors such as Shopify or WooCommerce (later increments). API key scopes and per-key permissions. Webhook event filtering per endpoint.
