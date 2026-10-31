from decimal import Decimal
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from core.models import Workspace

MONEY = {"max_digits": 12, "decimal_places": 2}
ZERO = Decimal("0.00")
PERCENT = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))]
NOT_NEGATIVE = [MinValueValidator(Decimal("0"))]
QUANTITY = [MinValueValidator(1), MaxValueValidator(100000)]
TERMS = [MinValueValidator(0), MaxValueValidator(365)]


class Customer(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    billing_address = models.TextField(max_length=500, blank=True)
    gstin = models.CharField("GSTIN", max_length=15, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                F("workspace"),
                Lower("email"),
                condition=~Q(email=""),
                name="unique_customer_email_per_workspace",
            )
        ]

    def __str__(self):
        return self.name


class Tax(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=60)
    rate = models.DecimalField(max_digits=5, decimal_places=2, validators=PERCENT)
    is_active = models.BooleanField("Available for new lines", default=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return f"{self.name} ({self.rate.normalize():f}%)"


class Product(models.Model):
    class Kind(models.TextChoices):
        SERVICE = "service", "Service"
        GOODS = "goods", "Goods"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SERVICE)
    sales_price = models.DecimalField(**MONEY, validators=NOT_NEGATIVE)
    cost_price = models.DecimalField(**MONEY, default=ZERO, validators=NOT_NEGATIVE)
    tax = models.ForeignKey(Tax, on_delete=models.PROTECT, null=True, blank=True)
    notes = models.TextField(max_length=500, blank=True)
    is_active = models.BooleanField("Available for new lines", default=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class ProductVariant(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="variants"
    )
    name = models.CharField(max_length=80)
    price_adjustment = models.DecimalField(**MONEY, default=ZERO)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "id"]

    @property
    def unit_price(self):
        return self.product.sales_price + self.price_adjustment


class Plan(models.Model):
    class Period(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    period = models.CharField(
        "Billing period", max_length=10, choices=Period.choices, default=Period.MONTHLY
    )
    price = models.DecimalField(**MONEY, validators=NOT_NEGATIVE)
    tax = models.ForeignKey(Tax, on_delete=models.PROTECT, null=True, blank=True)
    min_quantity = models.PositiveIntegerField(default=1, validators=QUANTITY)
    available_from = models.DateField(null=True, blank=True)
    available_until = models.DateField(null=True, blank=True)
    can_close = models.BooleanField("Customers may close early", default=True)
    can_pause = models.BooleanField("Subscriptions can be paused", default=False)
    can_renew = models.BooleanField("Subscriptions can be renewed", default=True)
    auto_close = models.BooleanField("Close automatically at expiry", default=False)
    is_active = models.BooleanField("Available for new quotations", default=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return f"{self.name} ({self.get_period_display().lower()})"

    def available_on(self, day):
        return (self.available_from is None or self.available_from <= day) and (
            self.available_until is None or day <= self.available_until
        )


class Discount(models.Model):
    class Kind(models.TextChoices):
        PERCENT = "percent", "Percentage"
        FIXED = "fixed", "Fixed amount"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PERCENT)
    value = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    min_amount = models.DecimalField(
        "Minimum purchase", **MONEY, default=ZERO, validators=NOT_NEGATIVE
    )
    min_quantity = models.PositiveIntegerField("Minimum quantity", default=0)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    usage_limit = models.PositiveIntegerField(null=True, blank=True)
    times_used = models.PositiveIntegerField(default=0, editable=False)
    products = models.ManyToManyField(Product, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(usage_limit=None) | Q(times_used__lte=F("usage_limit")),
                name="discount_usage_within_limit",
            )
        ]

    def __str__(self):
        if self.kind == self.Kind.PERCENT:
            return f"{self.name} ({self.value.normalize():f}% off)"
        return f"{self.name} (₹{self.value} off)"


class QuotationTemplate(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True)
    validity_days = models.PositiveIntegerField(
        "Valid for (days)",
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
    )
    notes = models.TextField(max_length=500, blank=True)
    is_active = models.BooleanField("Available for new quotations", default=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class ItemReference(models.Model):
    plan = models.ForeignKey(
        Plan, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    quantity = models.PositiveIntegerField(default=1, validators=QUANTITY)

    class Meta:
        abstract = True


class TemplateLine(ItemReference):
    template = models.ForeignKey(
        QuotationTemplate, on_delete=models.CASCADE, related_name="lines"
    )

    class Meta:
        ordering = ["id"]


# Prices, tax rates, and descriptions are snapshots taken when the line is
# added. Later catalog edits never rewrite existing documents.
class PricedLine(ItemReference):
    description = models.CharField(max_length=200)
    unit_price = models.DecimalField(**MONEY)
    tax_name = models.CharField(max_length=60, blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    subtotal = models.DecimalField(**MONEY, default=ZERO)
    discount_amount = models.DecimalField(**MONEY, default=ZERO)
    tax_amount = models.DecimalField(**MONEY, default=ZERO)
    total = models.DecimalField(**MONEY, default=ZERO)

    class Meta:
        abstract = True
        ordering = ["id"]


class Sequence(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    kind = models.CharField(max_length=20)
    next_value = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "kind"], name="unique_workspace_sequence"
            )
        ]


class Subscription(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        QUOTATION = "quotation", "Quotation sent"
        CONFIRMED = "confirmed", "Confirmed"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        CLOSED = "closed", "Closed"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    number = models.CharField(max_length=20)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True)
    template = models.ForeignKey(
        QuotationTemplate, on_delete=models.SET_NULL, null=True, blank=True
    )
    valid_until = models.DateField("Quotation valid until", null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    payment_terms_days = models.PositiveIntegerField(
        "Payment terms (days)", default=15, validators=TERMS
    )
    notes = models.TextField(max_length=500, blank=True)
    subtotal = models.DecimalField(**MONEY, default=ZERO)
    tax_total = models.DecimalField(**MONEY, default=ZERO)
    total = models.DecimalField(**MONEY, default=ZERO)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "number"], name="unique_subscription_number"
            )
        ]


class SubscriptionLine(PricedLine):
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="lines"
    )


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    number = models.CharField(max_length=20, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    source_channel = models.CharField(max_length=10, default="manual", editable=False)
    currency = models.CharField(max_length=3, default="INR", editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices",
    )
    discount = models.ForeignKey(
        Discount, on_delete=models.PROTECT, null=True, blank=True
    )
    payment_terms_days = models.PositiveIntegerField(
        "Payment terms (days)", default=15, validators=TERMS
    )
    notes = models.TextField(max_length=500, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    subtotal = models.DecimalField(**MONEY, default=ZERO)
    discount_total = models.DecimalField(**MONEY, default=ZERO)
    tax_total = models.DecimalField(**MONEY, default=ZERO)
    total = models.DecimalField(**MONEY, default=ZERO)
    amount_paid = models.DecimalField(**MONEY, default=ZERO)
    # Billing identities are copied at issue so an issued invoice stays as sent.
    discount_label = models.CharField(max_length=120, blank=True)
    customer_name = models.CharField(max_length=150, blank=True)
    customer_email = models.EmailField(blank=True)
    customer_address = models.TextField(max_length=500, blank=True)
    customer_gstin = models.CharField(max_length=15, blank=True)
    business_name = models.CharField(max_length=180, blank=True)
    business_email = models.EmailField(blank=True)
    business_address = models.TextField(max_length=500, blank=True)
    business_gstin = models.CharField(max_length=15, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "number"],
                condition=~Q(number=""),
                name="unique_invoice_number",
            ),
            models.CheckConstraint(
                condition=Q(amount_paid__gte=0) & Q(amount_paid__lte=F("total")),
                name="invoice_paid_within_total",
            ),
        ]

    @property
    def balance(self):
        return self.total - self.amount_paid

    def is_overdue(self, today):
        return (
            self.status == self.Status.ISSUED
            and self.due_date is not None
            and self.due_date < today
        )

    @property
    def label(self):
        return self.number or f"Draft {self.pk}"


class InvoiceLine(PricedLine):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")


class Payment(models.Model):
    class Method(models.TextChoices):
        UPI = "upi", "UPI (collected outside Mandali)"
        BANK = "bank", "Bank transfer (NEFT, RTGS, IMPS)"
        CASH = "cash", "Cash"
        CHEQUE = "cheque", "Cheque"
        CARD = "card", "Card terminal"
        OTHER = "other", "Other"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(**MONEY)
    method = models.CharField(max_length=10, choices=Method.choices)
    received_on = models.DateField()
    reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    reversal_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["received_on", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="payment_amount_positive"
            )
        ]
