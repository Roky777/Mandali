import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from core.models import Workspace
from billing.models import Invoice, Payment, Product, Plan, ProductVariant


class GatewayAccount(models.Model):
    """Immutable credential versions keep pending orders verifiable after rotation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    key_id = models.CharField(max_length=100)
    secrets_ciphertext = models.TextField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace"],
                condition=Q(is_active=True),
                name="one_active_test_gateway",
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
