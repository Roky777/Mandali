import re
from decimal import Decimal
from django import forms
from django.utils import timezone
from billing.models import Invoice, Product, Plan
from .models import GatewayProfile, Provider, RoutingPolicy


class GatewayForm(forms.Form):
    key_id = forms.CharField(label="Razorpay Test key ID", max_length=100)
    key_secret = forms.CharField(
        label="Test key secret",
        max_length=200,
        min_length=8,
        widget=forms.PasswordInput(),
    )
    webhook_secret = forms.CharField(
        label="Webhook secret",
        max_length=200,
        min_length=16,
        widget=forms.PasswordInput(),
        help_text="Use a separate random secret of at least 16 characters in the Razorpay Test webhook settings.",
    )

    def clean_key_id(self):
        value = self.cleaned_data["key_id"].strip()
        if not re.fullmatch(r"rzp_test_[A-Za-z0-9]+", value):
            raise forms.ValidationError(
                "Use an rzp_test_ key. Live payments are not enabled."
            )
        return value


class LinkForm(forms.Form):
    title = forms.CharField(label="Link title", max_length=120)
    invoice = forms.ModelChoiceField(
        queryset=Invoice.objects.none(),
        required=False,
        label="Sandbox invoice (optional)",
    )
    product = forms.ModelChoiceField(
        queryset=Product.objects.none(), required=False, label="Product (optional)"
    )
    plan = forms.ModelChoiceField(
        queryset=Plan.objects.none(),
        required=False,
        label="Plan initial payment (optional)",
    )
    quantity = forms.IntegerField(min_value=1, max_value=100000, initial=1)
    valid_days = forms.IntegerField(
        label="Valid for days", min_value=1, max_value=90, initial=7
    )

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invoice"].queryset = Invoice.objects.filter(
            workspace=workspace, is_sandbox=True, status=Invoice.Status.ISSUED
        )
        self.fields["product"].queryset = Product.objects.filter(
            workspace=workspace, is_active=True, sales_price__gt=0
        )
        self.fields["plan"].queryset = Plan.objects.filter(
            workspace=workspace, is_active=True, price__gt=0
        )
        self.fields["invoice"].label_from_instance = (
            lambda invoice: f"{invoice.number}: {invoice.customer_name}"
        )

    def clean(self):
        data = super().clean()
        if sum(bool(data.get(field)) for field in ["invoice", "product", "plan"]) != 1:
            raise forms.ValidationError(
                "Choose exactly one sandbox invoice, product, or plan."
            )
        plan = data.get("plan")
        if plan and (
            not plan.available_on(timezone.localdate())
            or data.get("quantity", 0) < plan.min_quantity
        ):
            raise forms.ValidationError(
                "Choose an available plan and its minimum quantity."
            )
        return data


class BuyerForm(forms.Form):
    name = forms.CharField(label="Your name", max_length=150)
    email = forms.EmailField(label="Email address")
    phone = forms.CharField(label="Phone (optional)", max_length=20, required=False)
    address = forms.CharField(
        label="Billing address",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    nonce = forms.CharField(widget=forms.HiddenInput())


class CashfreeForm(forms.Form):
    key_id = forms.CharField(label="Cashfree Sandbox app ID", max_length=100)
    key_secret = forms.CharField(
        label="Sandbox secret key",
        max_length=200,
        min_length=8,
        widget=forms.PasswordInput(),
        help_text="Cashfree signs webhooks with this secret, so no separate webhook secret is needed.",
    )

    def clean_key_id(self):
        value = self.cleaned_data["key_id"].strip()
        if not re.fullmatch(r"TEST[A-Za-z0-9]{4,96}", value):
            raise forms.ValidationError(
                "Use a sandbox app ID that starts with TEST. Production keys are not enabled."
            )
        return value


class ProfileForm(forms.ModelForm):
    methods = forms.MultipleChoiceField(
        label="Methods enabled on this account",
        choices=GatewayProfile.METHODS,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = GatewayProfile
        fields = [
            "methods",
            "fee_percent",
            "fee_fixed",
            "settlement_days",
            "min_amount",
            "max_amount",
            "recurring",
        ]
        labels = {
            "fee_percent": "Fee per payment (%) from your contract",
            "fee_fixed": "Fixed fee per payment (₹) from your contract",
            "settlement_days": "Days until settlement (optional)",
            "min_amount": "Smallest payment allowed (₹, optional)",
            "max_amount": "Largest payment allowed (₹, optional)",
            "recurring": "Recurring payments or mandates",
        }
        help_texts = {
            "fee_percent": "Copy it from your agreement with the gateway. Leave both fee fields empty if you do not know them; Mandali will treat the fee as unknown.",
            "fee_fixed": "Enter 0 if your contract has no fixed fee.",
            "recurring": "Mandali does not collect recurring payments yet. This only informs recommendations.",
        }

    def clean_fee_percent(self):
        value = self.cleaned_data["fee_percent"]
        if value is not None and not 0 <= value <= 100:
            raise forms.ValidationError("Use a percentage from 0 to 100.")
        return value

    def clean(self):
        data = super().clean()
        for name in ["fee_fixed", "min_amount", "max_amount"]:
            if data.get(name) is not None and data[name] < 0:
                self.add_error(name, "Use zero or more.")
        if (data.get("fee_percent") is None) != (data.get("fee_fixed") is None):
            raise forms.ValidationError(
                "Enter both fee fields, or leave both empty to keep the fee unknown."
            )
        low, high = data.get("min_amount"), data.get("max_amount")
        if low is not None and high is not None and high < low:
            self.add_error("max_amount", "Use an amount at or above the smallest payment.")
        if data.get("settlement_days") is not None and data["settlement_days"] > 60:
            self.add_error("settlement_days", "Use 60 days or fewer.")
        return data


class PolicyForm(forms.ModelForm):
    class Meta:
        model = RoutingPolicy
        fields = ["mode", "default_provider", "priority"]
        labels = {
            "mode": "How payments choose a gateway",
            "default_provider": "Default gateway",
            "priority": "When choosing automatically, prefer",
        }
        widgets = {"mode": forms.RadioSelect}

    def __init__(self, *args, connected, **kwargs):
        super().__init__(*args, **kwargs)
        self.connected = connected
        self.fields["default_provider"].choices = [("", "No default")] + [
            (value, label) for value, label in Provider.choices if value in connected
        ]

    def clean(self):
        data = super().clean()
        if (
            data.get("mode") == RoutingPolicy.Mode.DEFAULT
            and not data.get("default_provider")
            and len(self.connected) > 1
        ):
            self.add_error(
                "default_provider",
                "Choose a default, or turn on automatic selection, so payments know which gateway to use.",
            )
        return data


class OverrideForm(forms.Form):
    invoice = forms.ModelChoiceField(
        queryset=Invoice.objects.none(), label="Sandbox invoice", empty_label="Choose an invoice"
    )
    provider = forms.ChoiceField(label="Gateway", choices=Provider.choices)
    reason = forms.CharField(label="Reason", max_length=200)

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invoice"].queryset = Invoice.objects.filter(
            workspace=workspace, is_sandbox=True, status=Invoice.Status.ISSUED
        )
        self.fields["invoice"].label_from_instance = (
            lambda invoice: f"{invoice.number}: {invoice.customer_name}"
        )


class RecommendForm(forms.Form):
    methods = forms.MultipleChoiceField(
        label="Methods your customers need",
        choices=GatewayProfile.METHODS,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    recurring = forms.BooleanField(label="I will need recurring payments", required=False)
    amount = forms.DecimalField(
        label="Typical payment (₹, optional)",
        required=False,
        min_value=Decimal("1"),
        max_digits=12,
        decimal_places=2,
    )
    priority = forms.ChoiceField(
        label="What matters most",
        choices=RoutingPolicy.Priority.choices,
        initial=RoutingPolicy.Priority.FEES,
    )
