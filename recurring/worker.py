import time
import uuid
from datetime import timedelta
from django.db import transaction, IntegrityError
from django.db.models import F
from django.utils import timezone
from billing.services import BillingError
from checkout.crypto import CheckoutError
from .models import Schedule, JobLease
from . import collection, services, notices


def run(limit=100):
    owner = uuid.uuid4()
    now = timezone.now()
    with transaction.atomic():
        JobLease.objects.get_or_create(name="billing", defaults={"expires_at": now})
        if not JobLease.objects.filter(name="billing", expires_at__lte=now).update(
            owner=owner, expires_at=now + timedelta(minutes=15)
        ):
            return {"busy": True, "schedules": 0, "errors": 0, "emails": 0}
    counts = {"busy": False, "schedules": 0, "errors": 0, "emails": 0}
    started = time.monotonic()
    try:
        for schedule in Schedule.objects.select_related("subscription").order_by(
            F("last_run_at").asc(nulls_first=True), "pk"
        )[:limit]:
            if time.monotonic() - started > 420:
                break
            Schedule.objects.filter(pk=schedule.pk).update(last_run_at=timezone.now())
            try:
                if schedule.mode == "automatic":
                    mandate = collection.Mandate.objects.filter(
                        schedule=schedule
                    ).first()
                    if mandate and mandate.subscription_id:
                        collection.reconcile(mandate, deadline=started + 420)
                else:
                    # Bound catch-up. A later run continues from the saved cursor.
                    for _ in range(12):
                        if services.bill_one(schedule.pk) is None:
                            break
                Schedule.objects.filter(pk=schedule.pk).update(last_error="")
                counts["schedules"] += 1
            except (BillingError, CheckoutError, IntegrityError) as error:
                Schedule.objects.filter(pk=schedule.pk).update(
                    last_error=str(error)[:240]
                )
                counts["errors"] += 1
        counts.update(services.maintain_terms())
        services.queue_due_notices()
        counts["emails"] = notices.deliver(limit=limit, deadline=started + 480)
        return counts
    finally:
        JobLease.objects.filter(name="billing", owner=owner).update(
            expires_at=timezone.now()
        )
