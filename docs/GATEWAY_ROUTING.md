# Part 6: gateway selection and routing

This increment adds a second sandbox gateway, Cashfree Payments, beside Razorpay Test. It also adds recommendations, a merchant default, opt-in automatic routing, manual overrides for single invoices, and a recorded explanation for every routing decision. It builds on the [Part 3 checkout](CHECKOUT.md): the same verified-capture ledger, single pending order per invoice, and reconciliation rules apply to both gateways.

Everything remains sandbox only. Razorpay key IDs must start with `rzp_test_` and Cashfree app IDs with `TEST`. Production credentials are rejected.

## Connect Cashfree Sandbox

1. Under **Payments → Configure gateways → Cashfree Sandbox**, save the sandbox app ID and secret key. They are encrypted with `PAYMENT_ENCRYPTION_KEYS` like Razorpay credentials.
2. In the Cashfree Sandbox dashboard, add the webhook URL shown in Mandali (`/webhooks/cashfree-sandbox/<account>/`) for API version 2023-08-01. Subscribe to payment success, failure, and user-dropped events. Cashfree signs webhooks with the secret key: Mandali checks `x-webhook-signature` as base64 HMAC-SHA256 of `x-webhook-timestamp` followed by the raw body.
3. The buyer pays on Cashfree's hosted checkout in the same tab, then returns to the Mandali session page. That page asks the server to check the order. Mandali records a payment only after fetching it from Cashfree's API as `SUCCESS` for the exact order, amount in paise, and INR. A browser return is never proof of payment.

Cashfree requires a customer phone number. Routing skips Cashfree for a payment without one, using the phone entered at checkout or the customer record. Mandali sets the Cashfree order ID to `order_<attempt UUID hex>`. If order creation times out, the attempt stays uncertain. Because the ID is deterministic, a Cashfree `409` on retry also counts as uncertain, and reconciliation finds the original order instead of creating a second one. Cashfree payment IDs are stored as `pay_<cf_payment_id>`. Cashfree reports refunds through a separate API that Mandali does not read yet; gateway refund reconciliation arrives with live collection.

## Account details

For each gateway, an administrator can enter what their own contract and dashboard say:

- enabled methods (UPI, cards, netbanking, wallets);
- fee as a percentage plus a fixed amount;
- days to settlement;
- smallest and largest payment;
- whether recurring payments are confirmed for the account.

Mandali never fills these in. An empty value is **unknown**, shown as unknown, and ranked after known values.

Payment success is measured, never claimed. It is the share of this business's orders on each gateway, between one hour and 30 days old, that were paid. It is shown only after at least 20 such orders, and is otherwise unknown. Today these are sandbox orders.

## Routing policy

| Mode | Behaviour |
| --- | --- |
| **Always use the default gateway** (initial) | The default is used if it can take the payment. If it cannot, nothing is routed and the reason is recorded; payments never switch gateways silently. With one connected gateway and no default, that gateway is used, which keeps Part 3's behaviour. With two and no default, the policy asks for one. |
| **Choose automatically** (opt-in) | Ineligible gateways are removed, then the rest are ranked by the preferred figure: lowest estimated fee, fastest settlement, or highest measured success. Unknown figures rank last. Ties go to the default, then to a fixed order (Razorpay, then Cashfree), so the same payment under the same policy always routes the same way. |

A gateway is **eligible** only if it is connected, its saved credentials decrypt and validate, the currency is INR, the amount is within the account's limits, and Cashfree has a phone number. Every payment is one-time today, so recurring capability is informational only.

**Manual override:** an administrator can pin one sandbox invoice to a gateway, with a reason. If that gateway cannot take the payment, nothing is routed elsewhere. An override affects the next payment prepared. An order already started keeps its gateway.

**Fallback:** in automatic mode only, after a gateway definitively rejects order creation, so no money can have moved, the next prepared payment for that invoice excludes it and is recorded as a fallback. A timeout or uncertain result is never a reason to try another gateway. It stays the invoice's single pending order until reconciled, exactly as in Part 3. Tokens or mandates are never moved between providers.

## Explanations and history

Each time a payment is prepared, Mandali records the policy snapshot, every gateway's eligibility and figures, the chosen gateway or none, the basis (override, default, only eligible, automatic, fallback, or none), and a sentence such as:

> Cashfree Sandbox chosen automatically for lowest estimated fee (estimated fee ₹265.50; Razorpay Test: estimated fee ₹300.00)

A decision that routes nothing is kept too, so staff can see why a buyer could not pay. **Gateway selection** shows the latest 50 decisions, and **Links and payments** shows the explanation beside each attempt. Policy changes, overrides, and account details are written to workspace activity.

## Recommendations

**Get a recommendation** takes the methods customers need, whether recurring payments will be needed, a typical amount, and what matters most. Each gateway is checked against the entered account details: a missing method or a limit that does not fit counts as a mismatch; a detail never entered counts as unknown. Gateways are then ranked by the chosen figure. A recommendation cannot open, approve, or check a merchant account with a provider.

## Permissions

Only administrators can connect gateways, edit account details, change the policy, set overrides, or view gateway selection. Every lookup is scoped to the workspace, and an invoice from another business cannot be overridden.

## Verification and limits

`checkout/test_routing.py` covers:

- **Routing decisions:** single-gateway compatibility; default required with two gateways; default never silently switching; fee ranking with unknowns last; deterministic ties; ineligible gateways never selected, by amount limit, missing phone, or broken credentials; the success-rate minimum sample; overrides; fallback only after rejection.
- **Checkout with routing:** recording decisions, including unrouted ones; no fallback after an uncertain order; the Cashfree checkout script and content policy.
- **Cashfree adapter:** order request mapping; definitive rejection without a phone; `409` treated as uncertain; status mapping and exact paise.
- **Cashfree webhooks:** signature check; settle once, with replay rejected; refusal of a Razorpay account on the Cashfree route; reconciliation after return; no ledger entry for failed payments.
- **Screens:** admin-only access, policy validation, the both-or-neither fee fields, override scoping, and recommendations.

Provider responses are mocked. A real Cashfree Sandbox order, hosted checkout, and webhook still need verifying with the merchant's sandbox account on the deployed HTTPS service, as for Razorpay in Part 3. Field names follow Cashfree's PG API version 2023-08-01; confirm them against the current documentation before relying on them. Screens were rendered with dummy data and checked with Hallmark's `sloplint --scope app --render`: 0 FAIL, 0 REFLEX, 0 WARN across 32 screens.

Not included: live credentials, payment-method selection before routing, recurring or mandate routing (Part 5), gateway refund APIs, and more than two providers.
