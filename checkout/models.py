import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from core.models import Workspace
from billing.models import Invoice, Payment, Product, Plan


class Provider(models.TextChoices):
    RAZORPAY = "razorpay", "Razorpay Test"
    CASHFREE = "cashfree", "Cashfree Sandbox"


class GatewayAccount(models.Model):
    """Immutable credential versions keep pending orders verifiable after rotation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    provider = models.CharField(
        max_length=10, choices=Provider.choices, default=Provider.RAZORPAY
    )
    key_id = models.CharField(max_length=100)
    secrets_ciphertext = models.TextField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "provider"],
                condition=Q(is_active=True),
                name="one_active_test_gateway_per_provider",
            )
        ]


class ShareLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    title = models.CharField(max_length=120)
    token_digest = models.CharField(max_length=64, unique=True)
    token_ciphertext = models.TextField()
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, null=True, blank=True
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, null=True, blank=True
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(invoice__isnull=False, product__isnull=True, plan__isnull=True)
                    | Q(invoice__isnull=True, product__isnull=False, plan__isnull=True)
                    | Q(invoice__isnull=True, product__isnull=True, plan__isnull=False)
                ),
                name="checkout_link_one_target",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=1) & Q(quantity__lte=100000),
                name="checkout_quantity_range",
            ),
        ]


class PaymentAttempt(models.Model):
    class Status(models.TextChoices):
        CREATING = "creating", "Preparing gateway order"
        READY = "ready", "Ready for payment"
        UNKNOWN = "unknown", "Order creation needs reconciliation"
        AUTHORIZED = "authorized", "Awaiting capture"
        PAID = "paid", "Sandbox payment verified"
        REVIEW = "review", "Needs staff review"
        REJECTED = "rejected", "Gateway rejected order creation"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="gateway_attempts"
    )
    gateway = models.ForeignKey(GatewayAccount, on_delete=models.PROTECT)
    key_id = models.CharField(max_length=100)
    amount_paise = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    provider_order_id = models.CharField(max_length=100, blank=True)
    provider_payment_id = models.CharField(max_length=100, blank=True)
    observed_payment_id = models.CharField(max_length=100, blank=True)
    # A provider's browser checkout handle (Cashfree payment_session_id).
    client_token = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.CREATING
    )
    payment = models.OneToOneField(
        Payment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gateway_attempt",
    )
    detail = models.CharField(max_length=220, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice"],
                condition=Q(
                    status__in=["creating", "ready", "unknown", "authorized", "review"]
                ),
                name="one_pending_gateway_order_per_invoice",
            ),
            models.UniqueConstraint(
                fields=["workspace", "key_id", "provider_order_id"],
                condition=~Q(provider_order_id=""),
                name="unique_gateway_order",
            ),
            models.UniqueConstraint(
                fields=["workspace", "key_id", "provider_payment_id"],
                condition=~Q(provider_payment_id=""),
                name="unique_gateway_capture",
            ),
            models.CheckConstraint(
                condition=Q(amount_paise__gt=0), name="gateway_amount_positive"
            ),
        ]


class CheckoutSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link = models.ForeignKey(ShareLink, on_delete=models.PROTECT)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    creation_digest = models.CharField(max_length=64, unique=True)
    # Some gateways require a phone number; it is kept only for this session.
    buyer_phone = models.CharField(max_length=20, blank=True)
    attempt = models.ForeignKey(
        PaymentAttempt, on_delete=models.PROTECT, null=True, blank=True
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class GatewayEvent(models.Model):
    gateway = models.ForeignKey(GatewayAccount, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=120)
    body_digest = models.CharField(max_length=64)
    event_type = models.CharField(max_length=80)
    attempt = models.ForeignKey(PaymentAttempt, on_delete=models.PROTECT, null=True)
    outcome = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["gateway", "event_id"], name="unique_gateway_event_id"
            ),
            models.UniqueConstraint(
                fields=["gateway", "body_digest"], name="unique_gateway_event_body"
            ),
        ]


class GatewayProfile(models.Model):
    """What the business knows about its own gateway contract.

    Mandali never supplies fees, limits, or settlement times. Anything left
    empty is treated as unknown.
    """

    class Recurring(models.TextChoices):
        UNKNOWN = "unknown", "Not confirmed"
        YES = "yes", "Yes, confirmed for this account"
        NO = "no", "No"

    METHODS = [
        ("upi", "UPI"),
        ("card", "Cards"),
        ("netbanking", "Netbanking"),
        ("wallet", "Wallets"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    provider = models.CharField(max_length=10, choices=Provider.choices)
    methods = models.JSONField(default=list, blank=True)
    min_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    max_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    fee_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    fee_fixed = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    settlement_days = models.PositiveSmallIntegerField(null=True, blank=True)
    recurring = models.CharField(
        max_length=10, choices=Recurring.choices, default=Recurring.UNKNOWN
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "provider"], name="one_profile_per_provider"
            )
        ]


class RoutingPolicy(models.Model):
    class Mode(models.TextChoices):
        DEFAULT = "default", "Always use the default gateway"
        AUTOMATIC = "automatic", "Choose automatically among eligible gateways"

    class Priority(models.TextChoices):
        FEES = "fees", "Lowest estimated fee"
        SETTLEMENT = "settlement", "Fastest settlement"
        SUCCESS = "success", "Highest measured success"

    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.DEFAULT)
    default_provider = models.CharField(
        max_length=10, choices=Provider.choices, blank=True
    )
    priority = models.CharField(
        max_length=12, choices=Priority.choices, default=Priority.FEES
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class InvoiceRoute(models.Model):
    """A manual override: this invoice uses this gateway or no gateway."""

    invoice = models.OneToOneField(Invoice, on_delete=models.CASCADE, related_name="gateway_route")
    provider = models.CharField(max_length=10, choices=Provider.choices)
    reason = models.CharField(max_length=200)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class RoutingDecision(models.Model):
    class Basis(models.TextChoices):
        OVERRIDE = "override", "Manual override"
        DEFAULT = "default", "Default gateway"
        ONLY = "only", "Only eligible gateway"
        AUTOMATIC = "automatic", "Automatic selection"
        FALLBACK = "fallback", "Fallback after a rejected order"
        NONE = "none", "No eligible gateway"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="routing_decisions")
    attempt = models.OneToOneField(
        PaymentAttempt, on_delete=models.SET_NULL, null=True, blank=True, related_name="routing"
    )
    provider = models.CharField(max_length=10, choices=Provider.choices, blank=True)
    basis = models.CharField(max_length=10, choices=Basis.choices)
    explanation = models.CharField(max_length=400)
    candidates = models.JSONField(default=list)
    policy = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

