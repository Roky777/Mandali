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


def credentials(account):
    if not re.fullmatch(r"rzp_test_[A-Za-z0-9]+", account.key_id):
        raise CheckoutError("Only Razorpay Test mode is supported.")
    try:
        result = json.loads(decrypt(account.secrets_ciphertext))
        if not isinstance(result, dict) or any(
            not isinstance(result.get(key), str) or not result[key]
            for key in ("key_secret", "webhook_secret")
        ):
            raise ValueError()
        return result
    except (ValueError, KeyError):
        raise CheckoutError("The gateway configuration is invalid.") from None
