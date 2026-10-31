"""Sandbox gateway adapters. No automatic retries of order creation.

Each adapter returns orders and payments in one internal shape, so the
verification and ledger code in services.py is shared:

order:   {"id", "amount" (paise), "currency", "receipt", "notes": {"mandali_attempt"}}
payment: {"id", "order_id", "amount" (paise), "currency", "status", "captured",
          "amount_refunded"}
"""

import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from django.conf import settings
from .crypto import CheckoutError, credentials


class GatewayError(CheckoutError):
    pass


class InvalidSignature(GatewayError):
    pass


class GatewayRejected(GatewayError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RazorpayTest:
    provider = "razorpay"
    payment_method = "rzp_test"

    def __init__(self, account):
        self.account = account
        self.secrets = credentials(account)

    def request(self, method, path, payload=None):
        authorization = base64.b64encode(
            f'{self.account.key_id}:{self.secrets["key_secret"]}'.encode()
        ).decode()
        request = urllib.request.Request(
            "https://api.razorpay.com/v1/" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": "Basic " + authorization,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=10
            ) as response:
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise GatewayError("Gateway response was too large to verify.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except urllib.error.HTTPError as error:
            if method == "POST" and error.code in (400, 401, 403, 404, 422):
                raise GatewayRejected(
                    "The gateway rejected order creation. An administrator should check its test configuration."
                ) from None
            raise GatewayError(
                "The gateway result is uncertain. Reconcile this attempt before creating another order."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise GatewayError(
                "The gateway could not be reached or its response could not be verified. Check the existing attempt again."
            ) from None

    def create_order(self, attempt):
        return self.request(
            "POST",
            "orders",
            {
                "amount": attempt.amount_paise,
                "currency": "INR",
                "receipt": attempt.pk.hex,
                "notes": {"mandali_attempt": str(attempt.pk)},
                "partial_payment": False,
            },
        )

    def fetch_order(self, order_id):
        return self.request("GET", "orders/" + identifier(order_id, "order"))

    def fetch_payment(self, payment_id, order_id=None):
        return self.request("GET", "payments/" + identifier(payment_id, "pay"))

    def order_payments(self, order_id):
        result = self.request(
            "GET", "orders/" + identifier(order_id, "order") + "/payments"
        )
        items = result.get("items")
        if (
            not isinstance(items, list)
            or len(items) > 100
            or any(not isinstance(item, dict) for item in items)
        ):
            raise GatewayError("Gateway payment history could not be verified.")
        return items

    def valid_callback(self, order_id, payment_id, signature):
        identifier(order_id, "order")
        identifier(payment_id, "pay")
        expected = hmac.new(
            self.secrets["key_secret"].encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return bool(re.fullmatch(r"[a-f0-9]{64}", signature)) and hmac.compare_digest(
            expected, signature
        )

    def valid_webhook(self, body, signature):
        expected = hmac.new(
            self.secrets["webhook_secret"].encode(), body, hashlib.sha256
        ).hexdigest()
        return bool(re.fullmatch(r"[a-f0-9]{64}", signature)) and hmac.compare_digest(
            expected, signature
        )


    def webhook_event(self, payload):
        """Return (event type, payment ID, order ID); IDs are None for other events."""
        event_type = payload["event"]
        if not isinstance(event_type, str) or len(event_type) > 80:
            raise ValueError()
        if event_type not in ("payment.captured", "payment.authorized", "payment.failed"):
            return event_type, None, None
        supplied = payload["payload"]["payment"]["entity"]
        return event_type, supplied["id"], supplied["order_id"]


CASHFREE_API = "https://sandbox.cashfree.com/pg/"
CASHFREE_VERSION = "2023-08-01"
CASHFREE_STATUS = {
    "SUCCESS": "captured",
    "PENDING": "pending",
    "NOT_ATTEMPTED": "pending",
    "FAILED": "failed",
    "USER_DROPPED": "failed",
    "CANCELLED": "failed",
    "VOID": "failed",
}
CASHFREE_EVENTS = (
    "PAYMENT_SUCCESS_WEBHOOK",
    "PAYMENT_FAILED_WEBHOOK",
    "PAYMENT_USER_DROPPED_WEBHOOK",
)


def paise_from_rupees(value):
    """Cashfree sends rupees as JSON numbers; accept only exact paise."""
    try:
        amount = Decimal(str(value)) * 100
    except (InvalidOperation, TypeError, ValueError):
        raise GatewayError("The gateway amount could not be verified.") from None
    if isinstance(value, bool) or amount != amount.to_integral_value() or amount <= 0:
        raise GatewayError("The gateway amount could not be verified.")
    return int(amount)


class CashfreeTest:
    """Cashfree Payments sandbox (PG API version 2023-08-01).

    Mandali sets the Cashfree order ID to order_<attempt UUID hex>, so an order
    created during a timeout can be found again and is never duplicated.
    Cashfree payment IDs are numeric; they are stored as pay_<cf_payment_id>.
    """

    provider = "cashfree"
    payment_method = "cf_test"

    def __init__(self, account):
        self.account = account
        self.secrets = credentials(account)

    def request(self, method, path, payload=None):
        request = urllib.request.Request(
            CASHFREE_API + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "x-client-id": self.account.key_id,
                "x-client-secret": self.secrets["key_secret"],
                "x-api-version": CASHFREE_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise GatewayError("Gateway response was too large to verify.")
                result = json.loads(raw)
                if not isinstance(result, (dict, list)):
                    raise ValueError()
                return result
        except urllib.error.HTTPError as error:
            # 409 means the order ID already exists: the earlier request may
            # have succeeded, so it stays uncertain and is reconciled instead.
            if method == "POST" and error.code in (400, 401, 403, 404, 422):
                raise GatewayRejected(
                    "The gateway rejected order creation. An administrator should check its sandbox configuration."
                ) from None
            raise GatewayError(
                "The gateway result is uncertain. Reconcile this attempt before creating another order."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise GatewayError(
                "The gateway could not be reached or its response could not be verified. Check the existing attempt again."
            ) from None

    def order(self, raw):
        if not isinstance(raw, dict):
            raise GatewayError("The gateway order could not be verified.")
        order_id = identifier(raw.get("order_id"), "order")
        tags = raw.get("order_tags") if isinstance(raw.get("order_tags"), dict) else {}
        return {
            "id": order_id,
            "amount": paise_from_rupees(raw.get("order_amount")),
            "currency": raw.get("order_currency"),
            "receipt": order_id[len("order_"):],
            "notes": {"mandali_attempt": tags.get("mandali_attempt")},
            "client_token": str(raw.get("payment_session_id") or "")[:200],
        }

    def payment(self, raw):
        if not isinstance(raw, dict) or not str(raw.get("cf_payment_id", "")).isdigit():
            raise GatewayError("Gateway payment evidence could not be verified.")
        status = CASHFREE_STATUS.get(raw.get("payment_status"), "unknown")
        return {
            "id": "pay_" + str(raw["cf_payment_id"]),
            "order_id": raw.get("order_id"),
            "amount": paise_from_rupees(raw.get("payment_amount")),
            "currency": raw.get("payment_currency"),
            "status": status,
            "captured": status == "captured",
            # Cashfree reports refunds through its refunds API, not on the
            # payment; refund reconciliation is part of Part 7.
            "amount_refunded": 0,
        }

    def create_order(self, attempt):
        from .models import CheckoutSession

        session = CheckoutSession.objects.filter(attempt=attempt).first()
        invoice = attempt.invoice
        phone = (session.buyer_phone if session else "") or invoice.customer.phone
        if not phone:
            raise GatewayRejected("Cashfree needs the customer's phone number.")
        return_url = settings.MANDALI_BASE_URL
        if session:
            from .services import session_url

            return_url += session_url(session) + "?returned=1"
        return self.order(
            self.request(
                "POST",
                "orders",
                {
                    "order_id": "order_" + attempt.pk.hex,
                    "order_amount": float(Decimal(attempt.amount_paise) / 100),
                    "order_currency": "INR",
                    "customer_details": {
                        "customer_id": f"mandali_{invoice.customer_id}",
                        "customer_name": invoice.customer_name[:100],
                        "customer_email": invoice.customer_email or None,
                        "customer_phone": re.sub(r"[^0-9+]", "", phone)[:15],
                    },
                    "order_meta": {"return_url": return_url},
                    "order_note": f"Sandbox payment for {invoice.number}"[:200],
                    "order_tags": {"mandali_attempt": str(attempt.pk)},
                },
            )
        )

    def fetch_order(self, order_id):
        return self.order(self.request("GET", "orders/" + identifier(order_id, "order")))

    def order_payments(self, order_id):
        items = self.request("GET", "orders/" + identifier(order_id, "order") + "/payments")
        if not isinstance(items, list) or len(items) > 100:
            raise GatewayError("Gateway payment history could not be verified.")
        return [self.payment(item) for item in items]

    def fetch_payment(self, payment_id, order_id=None):
        number = identifier(payment_id, "pay")[len("pay_"):]
        if not number.isdigit() or order_id is None:
            raise GatewayError("The gateway identifier is invalid.")
        return self.payment(
            self.request("GET", f"orders/{identifier(order_id, 'order')}/payments/{number}")
        )

    def valid_callback(self, order_id, payment_id, signature):
        # Cashfree's browser return carries no signature; Mandali always
        # fetches the payment from the API instead.
        return False

    def valid_webhook(self, body, signature):
        """signature is "<x-webhook-timestamp>:<x-webhook-signature>"."""
        timestamp, _, supplied = signature.partition(":")
        if not timestamp.isdigit() or not supplied:
            return False
        expected = base64.b64encode(
            hmac.new(
                self.secrets["key_secret"].encode(), timestamp.encode() + body, hashlib.sha256
            ).digest()
        ).decode()
        return hmac.compare_digest(expected, supplied)

    def webhook_event(self, payload):
        event_type = payload["type"]
        if not isinstance(event_type, str) or len(event_type) > 80:
            raise ValueError()
        if event_type not in CASHFREE_EVENTS:
            return event_type, None, None
        data = payload["data"]
        cf_payment_id = str(data["payment"]["cf_payment_id"])
        if not cf_payment_id.isdigit():
            raise ValueError()
        return event_type, "pay_" + cf_payment_id, data["order"]["order_id"]


def identifier(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(
        prefix + r"_[A-Za-z0-9]{1,64}", value
    ):
        raise GatewayError("The gateway identifier is invalid.")
    return value


ADAPTERS = {"razorpay": RazorpayTest, "cashfree": CashfreeTest}


def adapter(account):
    return ADAPTERS[account.provider](account)
