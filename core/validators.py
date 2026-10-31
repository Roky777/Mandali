import re
from django.core.exceptions import ValidationError


class PasswordCompositionValidator:
    def validate(self, password, user=None):
        if not (
            re.search(r"[A-Z]", password)
            and re.search(r"[a-z]", password)
            and re.search(r"[^\w\s]", password)
        ):
            raise ValidationError(self.get_help_text(), code="password_composition")

    def get_help_text(self):
        return "Use an uppercase letter, a lowercase letter, and a special character."


def clean_gstin(value):
    value = value.strip().upper()
    if value and not re.fullmatch(
        r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", value
    ):
        raise ValidationError("Use a 15-character GSTIN, for example 27ABCDE1234F1Z5.")
    return value
