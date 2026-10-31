from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import User, Workspace
from .validators import clean_gstin


class SignupForm(UserCreationForm):
    first_name = forms.CharField(label="Your name", max_length=150)
    email = forms.EmailField(label="Work email")

    class Meta:
        model = User
        fields = ["first_name", "email", "password1", "password2"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account already uses this email. Sign in or reset your password."
            )
        return email


class EmailLoginForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = [
            "name",
            "legal_name",
            "billing_email",
            "address",
            "gstin",
            "website",
            "use_api",
            "use_hosted",
            "use_manual",
        ]
        labels = {
            "name": "Business name",
            "legal_name": "Legal business name",
            "billing_email": "Billing email",
            "address": "Billing address",
            "gstin": "GSTIN (optional)",
            "website": "Website (optional)",
            "use_api": "Website / API integration",
            "use_hosted": "Shareable links and QR codes",
            "use_manual": "Manual billing",
        }
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "gstin": "Saved for future invoices. Format validation does not verify GST registration.",
            "website": "Saving a website does not connect its API.",
            "use_api": "API integration is planned. Your preference is saved.",
            "use_hosted": "Hosted checkout is planned. Your preference is saved.",
            "use_manual": "Customers, quotations, invoices, and offline payments are available.",
        }

    def clean_gstin(self):
        return clean_gstin(self.cleaned_data["gstin"])

    def clean(self):
        data = super().clean()
        if not any(data.get(k) for k in ["use_api", "use_hosted", "use_manual"]):
            raise forms.ValidationError(
                "Choose at least one billing channel. You can change it later."
            )
        return data


class InviteForm(forms.Form):
    email = forms.EmailField(label="Staff email address")

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class InviteSignupForm(UserCreationForm):
    first_name = forms.CharField(label="Your name", max_length=150)

    class Meta:
        model = User
        fields = ["first_name", "password1", "password2"]
