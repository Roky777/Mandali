import re
from django import forms
from django.utils import timezone
from billing.models import Invoice, Product, Plan


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
