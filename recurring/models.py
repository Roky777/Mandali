import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q, F
from billing.models import Subscription, Invoice, Payment, Discount
from checkout.models import GatewayAccount


class Schedule(models.Model):
    class Mode(models.TextChoices):
        INVOICE = "invoice", "Invoice and collect manually"
        AUTOMATIC = "automatic", "Authorized Razorpay Test collection"

    subscription = models.OneToOneField(
        Subscription, on_delete=models.PROTECT, related_name="schedule"
    )
    mode = models.CharField(max_length=12, choices=Mode.choices)
    anchor_on = models.DateField()
    next_bill_on = models.DateField()
    period = models.CharField(max_length=10)
    enabled = models.BooleanField(default=True)
    is_sandbox = models.BooleanField(default=False)
    discount = models.ForeignKey(
        Discount, on_delete=models.PROTECT, null=True, blank=True
    )
    reminders = models.BooleanField(default=True)
    total_count = models.PositiveIntegerField(default=12)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=240, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Cycle(models.Model):
    schedule = models.ForeignKey(
        Schedule, on_delete=models.PROTECT, related_name="cycles"
    )
    starts_on = models.DateField()
    ends_before = models.DateField()
    invoice = models.OneToOneField(
        Invoice, on_delete=models.PROTECT, related_name="billing_cycle"
    )
    provider_invoice_id = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-starts_on", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "starts_on"], name="one_invoice_per_billing_cycle"
            ),
            models.CheckConstraint(
                condition=Q(ends_before__gt=F("starts_on")),
                name="billing_cycle_positive_duration",
            ),
            models.UniqueConstraint(
                fields=["schedule", "provider_invoice_id"],
                condition=~Q(provider_invoice_id=""),
                name="one_provider_invoice_per_schedule",
            ),
        ]


class Mandate(models.Model):
    """A provider-managed test subscription. Merchant input never grants consent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.OneToOneField(
        Schedule, on_delete=models.PROTECT, related_name="mandate"
    )
    gateway = models.ForeignKey(GatewayAccount, on_delete=models.PROTECT)
    key_id = models.CharField(max_length=100)
    status = models.CharField(max_length=24, default="preparing")
    plan_id = models.CharField(max_length=80, blank=True)
    subscription_id = models.CharField(max_length=80, blank=True)
    authorization_ciphertext = models.TextField(blank=True)
    amount_paise = models.PositiveBigIntegerField()
    total_count = models.PositiveIntegerField()
    start_at = models.PositiveBigIntegerField()
    pending_action = models.CharField(max_length=12, blank=True)
    detail = models.CharField(max_length=240, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key_id", "subscription_id"],
                condition=~Q(subscription_id=""),
                name="unique_recurring_provider_subscription",
            )
        ]


class Capture(models.Model):
    mandate = models.ForeignKey(
        Mandate, on_delete=models.PROTECT, related_name="captures"
    )
    key_id = models.CharField(max_length=100)
    provider_payment_id = models.CharField(max_length=80)
    provider_invoice_id = models.CharField(max_length=80)
    payment = models.OneToOneField(
        Payment, on_delete=models.PROTECT, null=True, blank=True
    )
    status = models.CharField(max_length=12, default="review")
    detail = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key_id", "provider_payment_id"], name="one_recurring_capture"
            )
        ]


class Notice(models.Model):
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="notices"
    )
    kind = models.CharField(max_length=12)
    status = models.CharField(max_length=12, default="queued")
    recipient = models.EmailField()
    attempts = models.PositiveIntegerField(default=0)
    detail = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "kind"], name="one_notice_per_invoice_stage"
            )
        ]


class JobLease(models.Model):
    name = models.CharField(max_length=30, primary_key=True)
    owner = models.UUIDField(default=uuid.uuid4)
    expires_at = models.DateTimeField()
