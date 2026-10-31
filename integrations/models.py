import uuid
from django.conf import settings
from django.db import models
from core.models import Workspace


class ApiKey(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    # Only a SHA-256 digest of the random key is stored; the prefix helps
    # people recognise a key without revealing it.
    prefix = models.CharField(max_length=16)
    digest = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class IdempotencyRecord(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    key = models.CharField(max_length=100)
    route = models.CharField(max_length=40)
    request_digest = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField(null=True)
    response = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "key"], name="unique_idempotency_key"
            )
        ]


class WebhookEndpoint(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    url = models.URLField("Endpoint URL", max_length=300)
    # Needed to sign each delivery, so it is kept server side and shown once.
    secret = models.CharField(max_length=80, editable=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WebhookDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Waiting to send"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Gave up"

    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries"
    )
    event_id = models.UUIDField(default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=40)
    payload = models.JSONField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class Connector(models.Model):
    """An authorized pull of customer records from the business's own API."""

    FIELDS = [
        ("external_id", "Customer ID", True),
        ("name", "Name", True),
        ("email", "Email", False),
        ("phone", "Phone", False),
        ("billing_address", "Billing address", False),
        ("gstin", "GSTIN", False),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    url = models.URLField("Customer list URL", max_length=300)
    list_path = models.CharField(max_length=80, blank=True)
    mapping = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class SyncRun(models.Model):
    class Status(models.TextChoices):
        PREVIEW = "preview", "Ready to review"
        APPLIED = "applied", "Applied"
        DISCARDED = "discarded", "Discarded"
        FAILED = "failed", "Fetch failed"

    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=10, choices=Status.choices)
    items = models.JSONField(default=list)
    error = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def counts(self):
        result = {"create": 0, "update": 0, "unchanged": 0, "error": 0}
        for item in self.items:
            result[item["action"]] += 1
        return result
