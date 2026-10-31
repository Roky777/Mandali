import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import WebhookDelivery, WebhookEndpoint
from .outbound import OutboundError, request
from .serializers import invoice_data, payment_data

# Minutes to wait after each failed attempt; the last failure gives up.
BACKOFF = [1, 5, 30, 120, 360, 720, 1440]
LEASE = timedelta(minutes=2)
EVENTS = [
    "invoice.issued",
    "invoice.paid",
    "invoice.cancelled",
    "payment.recorded",
    "payment.reversed",
]


def new_secret():
    return "whsec_" + secrets.token_urlsafe(32)


def signature(secret, timestamp, body):
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def queue(workspace, event, data):
    endpoints = list(WebhookEndpoint.objects.filter(workspace=workspace, is_active=True))
    deliveries = [
        WebhookDelivery.objects.create(
            endpoint=endpoint,
            event_type=event,
            payload={"type": event, "created": timezone.now().isoformat(), "data": data},
            next_attempt_at=timezone.now(),
        )
        for endpoint in endpoints
    ]
    if deliveries:
        ids = [d.pk for d in deliveries]
        # Try once right after the billing change commits; the scheduled
        # command retries anything that fails.
        transaction.on_commit(lambda: deliver(WebhookDelivery.objects.filter(pk__in=ids)))
    return deliveries


def on_billing_event(sender, workspace, event, invoice, payment=None, **kwargs):
    data = {"invoice": invoice_data(invoice)}
    if payment:
        data["payment"] = payment_data(payment)
    queue(workspace, event, data)


def claim(delivery):
    # A conditional update leases the delivery, so two workers never send it at once.
    return WebhookDelivery.objects.filter(
        pk=delivery.pk,
        status=WebhookDelivery.Status.PENDING,
        next_attempt_at__lte=timezone.now(),
    ).update(next_attempt_at=timezone.now() + LEASE)


def attempt(delivery):
    endpoint = delivery.endpoint
    body = json.dumps(
        {"id": str(delivery.event_id), "workspace": str(endpoint.workspace_id), **delivery.payload},
        separators=(",", ":"),
    ).encode()
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "Mandali-Event-Id": str(delivery.event_id),
        "Mandali-Event-Type": delivery.event_type,
        "Mandali-Signature": f"t={timestamp},v1={signature(endpoint.secret, timestamp, body)}",
    }
    try:
        status, _ = request("POST", endpoint.url, headers, body)
        error = "" if 200 <= status < 300 else f"Endpoint answered {status}."
    except OutboundError as failure:
        status, error = None, str(failure)
    attempts = delivery.attempts + 1
    fields = {"attempts": attempts, "last_status_code": status, "last_error": error[:200]}
    if not error:
        fields.update(status=WebhookDelivery.Status.DELIVERED, delivered_at=timezone.now())
    elif attempts > len(BACKOFF):
        fields.update(status=WebhookDelivery.Status.FAILED)
    else:
        fields.update(next_attempt_at=timezone.now() + timedelta(minutes=BACKOFF[attempts - 1]))
    WebhookDelivery.objects.filter(pk=delivery.pk).update(**fields)
    return not error


def deliver(queryset):
    sent = 0
    for delivery in queryset.select_related("endpoint").filter(endpoint__is_active=True):
        if claim(delivery):
            sent += attempt(delivery)
    return sent


def due():
    return WebhookDelivery.objects.filter(
        status=WebhookDelivery.Status.PENDING, next_attempt_at__lte=timezone.now()
    ).order_by("next_attempt_at")


def resend(delivery):
    return WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.Status.PENDING, attempts=0, next_attempt_at=timezone.now()
    )
