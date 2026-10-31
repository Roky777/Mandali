import hashlib
import json
import re
from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from django.conf import settings


class CheckoutError(Exception):
    pass


def cipher():
    keys = [
        key.strip()
        for key in settings.PAYMENT_ENCRYPTION_KEYS.split(",")
        if key.strip()
    ]
    if not keys:
        raise CheckoutError(
            "Payment-link encryption is not configured. Ask the deployment administrator to configure it."
        )
    try:
        return MultiFernet([Fernet(key.encode()) for key in keys])
    except (ValueError, TypeError):
        raise CheckoutError(
            "Payment-link encryption configuration is invalid."
        ) from None


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise CheckoutError(
            "This payment configuration cannot be decrypted. Restore its encryption key before continuing."
        ) from None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# Only sandbox credentials are accepted: Razorpay Test key IDs start with
# rzp_test_, and Cashfree Sandbox app IDs start with TEST.
TEST_KEY_PATTERNS = {
    "razorpay": r"rzp_test_[A-Za-z0-9]+",
    "cashfree": r"TEST[A-Za-z0-9]{4,96}",
}
# Cashfree signs webhooks with the client secret, so it has no separate secret.
SECRET_FIELDS = {
    "razorpay": ("key_secret", "webhook_secret"),
    "cashfree": ("key_secret",),
}


def credentials(account):
    pattern = TEST_KEY_PATTERNS.get(account.provider)
    if not pattern or not re.fullmatch(pattern, account.key_id):
        raise CheckoutError("Only sandbox (test mode) gateway credentials are supported.")
    try:
        result = json.loads(decrypt(account.secrets_ciphertext))
        if not isinstance(result, dict) or any(
            not isinstance(result.get(key), str) or not result[key]
            for key in SECRET_FIELDS[account.provider]
        ):
            raise ValueError()
        return result
    except (ValueError, KeyError):
        raise CheckoutError("The gateway configuration is invalid.") from None
