"""Razorpay Test adapter. No automatic retries of order creation."""

import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
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

    def fetch_payment(self, payment_id):
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


def identifier(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(
        prefix + r"_[A-Za-z0-9]{1,64}", value
    ):
        raise GatewayError("The gateway identifier is invalid.")
    return value


def adapter(account):
    return RazorpayTest(account)
