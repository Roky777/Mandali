from django import forms
from django.db.models import Q
from django.utils import timezone
from core.validators import clean_gstin
from .models import (
    Customer,
    Discount,
    Invoice,
    Payment,
    Plan,
    Product,
    ProductVariant,
    QuotationTemplate,
    Subscription,
    Tax,
)
from .templatetags.billing import inr

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class ScopedForm(forms.ModelForm):
    """Bind the instance to one workspace and limit every related choice to it."""

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.instance.workspace = workspace
        for name, field in self.fields.items():
            if not isinstance(field, forms.ModelChoiceField):
                continue
            queryset = field.queryset.filter(workspace=workspace)
            if hasattr(queryset.model, "is_active"):
                # Inactive records stay selectable only where already chosen.
                current = Q(pk__in=[])
                if self.instance.pk and isinstance(
                    field, forms.ModelMultipleChoiceField
                ):
                    current = Q(pk__in=getattr(self.instance, name).all())
                elif self.instance.pk:
                    current = Q(pk=getattr(self.instance, f"{name}_id"))
                queryset = queryset.filter(Q(is_active=True) | current)
            field.queryset = queryset


class CustomerForm(ScopedForm):
    class Meta:
        model = Customer
        fields = ["name", "email", "phone", "billing_address", "gstin"]
        labels = {
            "name": "Customer name",
            "email": "Email (optional)",
            "phone": "Phone (optional)",
            "billing_address": "Billing address (optional)",
            "gstin": "GSTIN (optional)",
        }
        widgets = {"billing_address": forms.Textarea(attrs={"rows": 3})}

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if (
            email
            and Customer.objects.filter(workspace=self.workspace, email__iexact=email)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(
                "Another customer in this business uses this email."
            )
        return email

    def clean_gstin(self):
        return clean_gstin(self.cleaned_data["gstin"])


class TaxForm(ScopedForm):
    class Meta:
        model = Tax
        fields = ["name", "rate", "is_active"]
        labels = {"name": "Tax name", "rate": "Rate (%)"}
        help_texts = {
            "name": "For example, GST 18%. Mandali applies one rate per line and does not yet split CGST, SGST, or IGST.",
            "rate": "Changing a rate affects lines added later, never issued invoices.",
        }


class ProductForm(ScopedForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "kind",
            "sales_price",
            "cost_price",
            "tax",
            "notes",
            "is_active",
        ]
        labels = {
            "name": "Product name",
            "kind": "Type",
            "sales_price": "Sales price (₹)",
            "cost_price": "Cost price (₹)",
            "tax": "Tax (optional)",
            "notes": "Notes (optional)",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class VariantForm(forms.ModelForm):
    class Meta:
        model = ProductVariant
        fields = ["name", "price_adjustment"]
        labels = {"name": "Variant name", "price_adjustment": "Price change (₹)"}

    def __init__(self, *args, product, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.product = product

    def clean_price_adjustment(self):
        value = self.cleaned_data["price_adjustment"]
        if self.instance.product.sales_price + value < 0:
            raise forms.ValidationError("The variant price cannot be below zero.")
        return value


class PlanForm(ScopedForm):
    class Meta:
        model = Plan
        fields = [
            "name",
            "period",
            "price",
            "tax",
            "min_quantity",
            "available_from",
            "available_until",
            "can_close",
            "can_pause",
            "can_renew",
            "auto_close",
            "is_active",
        ]
        labels = {
            "name": "Plan name",
            "price": "Price per period (₹)",
            "tax": "Tax (optional)",
            "min_quantity": "Minimum quantity",
            "available_from": "Available from (optional)",
            "available_until": "Available until (optional)",
        }
        widgets = {"available_from": DATE, "available_until": DATE}
        help_texts = {
            "can_renew": "Saved as plan policy. Renewals arrive with recurring billing.",
            "auto_close": "Saved as plan policy. Automatic closure arrives with recurring billing.",
        }

    def clean(self):
        data = super().clean()
        start, end = data.get("available_from"), data.get("available_until")
        if start and end and end < start:
            self.add_error(
                "available_until", "Choose a date on or after the start date."
            )
        return data


class DiscountForm(ScopedForm):
    class Meta:
        model = Discount
        fields = [
            "name",
            "kind",
            "value",
            "min_amount",
            "min_quantity",
            "starts_on",
            "ends_on",
            "usage_limit",
            "products",
            "is_active",
        ]
        labels = {
            "name": "Discount name",
            "kind": "Discount type",
            "value": "Value",
            "min_amount": "Minimum purchase (₹)",
            "starts_on": "Starts on (optional)",
            "ends_on": "Ends on (optional)",
            "usage_limit": "Usage limit (optional)",
            "products": "Limit to products (optional)",
            "is_active": "Available for invoices",
        }
        widgets = {
            "starts_on": DATE,
            "ends_on": DATE,
            "products": forms.CheckboxSelectMultiple,
        }
        help_texts = {
            "value": "A percentage, or a rupee amount for fixed discounts.",
            "min_amount": "Measured on the eligible lines before tax.",
            "usage_limit": "Each issued invoice uses it once. Cancelling the invoice returns the use.",
            "products": "Leave empty to apply to the whole invoice.",
        }

    def clean(self):
        data = super().clean()
        if (
            data.get("kind") == Discount.Kind.PERCENT
            and data.get("value") is not None
            and data["value"] > 100
        ):
            self.add_error("value", "A percentage discount cannot exceed 100.")
        start, end = data.get("starts_on"), data.get("ends_on")
        if start and end and end < start:
            self.add_error("ends_on", "Choose a date on or after the start date.")
        limit = data.get("usage_limit")
        if limit is not None and limit < self.instance.times_used:
            self.add_error(
                "usage_limit",
                f"It has already been used {self.instance.times_used} times.",
            )
        return data


class TemplateForm(ScopedForm):
    class Meta:
        model = QuotationTemplate
        fields = ["name", "plan", "validity_days", "notes", "is_active"]
        labels = {
            "name": "Template name",
            "plan": "Plan (optional)",
            "notes": "Notes for the customer (optional)",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


def item_choices(workspace):
    plans = Plan.objects.filter(workspace=workspace, is_active=True)
    products = Product.objects.filter(
        workspace=workspace, is_active=True
    ).prefetch_related("variants")
    product_choices = []
    for product in products:
        product_choices.append(
            (f"product:{product.pk}", f"{product.name}, {inr(product.sales_price)}")
        )
        for variant in product.variants.all():
            if variant.is_active:
                product_choices.append(
                    (
                        f"variant:{variant.pk}",
                        f"{product.name}: {variant.name}, {inr(product.sales_price + variant.price_adjustment)}",
                    )
                )
    return [
        ("", "Choose a plan or product"),
        ("Plans", [(f"plan:{p.pk}", f"{p}, {inr(p.price)}") for p in plans]),
        ("Products", product_choices),
    ]


class LineForm(forms.Form):
    item = forms.ChoiceField(label="Plan or product")
    quantity = forms.IntegerField(
        label="Quantity", min_value=1, max_value=100000, initial=1
    )

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.fields["item"].choices = item_choices(workspace)

    def clean_item(self):
        kind, _, pk = self.cleaned_data["item"].partition(":")
        lookups = {
            "plan": Plan.objects.select_related("tax").filter(workspace=self.workspace),
            "product": Product.objects.select_related("tax").filter(
                workspace=self.workspace
            ),
            "variant": ProductVariant.objects.select_related("product__tax").filter(
                product__workspace=self.workspace, product__is_active=True
            ),
        }
        if kind not in lookups or not pk.isdigit():
            raise forms.ValidationError("Choose a plan or product from the list.")
        obj = lookups[kind].filter(pk=pk, is_active=True).first()
        if obj is None:
            raise forms.ValidationError("That item is no longer available.")
        return {kind: obj}

    def clean(self):
        data = super().clean()
        plan = (data.get("item") or {}).get("plan")
        quantity = data.get("quantity")
        if plan and quantity is not None and quantity < plan.min_quantity:
            self.add_error(
                "quantity",
                f"This plan needs a quantity of at least {plan.min_quantity}.",
            )
        if plan and not plan.available_on(timezone.localdate()):
            self.add_error("item", "This plan is outside its availability dates.")
        return data


class SubscriptionForm(ScopedForm):
    template = forms.ModelChoiceField(
        QuotationTemplate.objects.all(),
        required=False,
        label="Start from a template (optional)",
        help_text="Copies the template's plan, lines, notes, and validity period at today's prices.",
    )

    class Meta:
        model = Subscription
        fields = [
            "customer",
            "plan",
            "valid_until",
            "start_date",
            "expiry_date",
            "payment_terms_days",
            "notes",
        ]
        labels = {
            "plan": "Plan (optional for one-time quotations)",
            "valid_until": "Quotation valid until (optional)",
            "start_date": "Start date (optional)",
            "expiry_date": "Expiry date (optional)",
            "notes": "Notes for the customer (optional)",
        }
        widgets = {
            "valid_until": DATE,
            "start_date": DATE,
            "expiry_date": DATE,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            del self.fields["template"]
        else:
            self.fields["template"].queryset = QuotationTemplate.objects.filter(
                workspace=self.workspace, is_active=True
            )
            self.order_fields(["customer", "template"])

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("expiry_date")
        if start and end and end < start:
            self.add_error("expiry_date", "Choose a date on or after the start date.")
        return data


class InvoiceForm(ScopedForm):
    class Meta:
        model = Invoice
        fields = ["customer", "payment_terms_days", "discount", "notes", "is_sandbox"]
        labels = {
            "discount": "Discount (optional)",
            "notes": "Notes for the customer (optional)",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "payment_terms_days": "The due date is set from the issue date.",
            "is_sandbox": "Test invoices use TST numbers and are excluded from financial summaries. Required for sandbox checkout.",
            "discount": "Eligibility, dates, and usage limits are checked again when you issue the invoice.",
        }


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["amount", "method", "received_on", "reference", "note"]
        labels = {
            "amount": "Amount received (₹)",
            "method": "How it was paid",
            "received_on": "Received on",
            "reference": "Reference (optional)",
            "note": "Note (optional)",
        }
        widgets = {"received_on": DATE}
        help_texts = {"reference": "UTR, cheque number, or receipt number."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = [
            ("", "Choose how it was paid"),
            *[
                (value, label)
                for value, label in Payment.Method.choices
                if value != Payment.Method.RAZORPAY_TEST
            ],
        ]

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Enter an amount above zero.")
        return amount

    def clean_received_on(self):
        day = self.cleaned_data["received_on"]
        if day > timezone.localdate():
            raise forms.ValidationError(
                "A received payment cannot be dated in the future."
            )
        return day


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=200)
