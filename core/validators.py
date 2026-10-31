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
