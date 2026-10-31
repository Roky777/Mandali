from datetime import timedelta
import time
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from billing.models import Invoice
from .models import Notice


@transaction.atomic
def claim(pk):
    # A write-first claim serializes workers on both supported databases.
    if not Notice.objects.filter(pk=pk, status="queued").update(
        status="sending", attempts=F("attempts") + 1, updated_at=timezone.now()
    ):
        return None
    notice = Notice.objects.select_related(
        "invoice__workspace", "invoice__billing_cycle__schedule"
    ).get(pk=pk)
    invoice = notice.invoice
    stale = notice.kind in {"issued", "due"} and invoice.due_date < timezone.localdate()
    if (
        invoice.status != Invoice.Status.ISSUED
        or invoice.balance <= 0
        or not notice.recipient
        or not invoice.billing_cycle.schedule.reminders
        or stale
    ):
        notice.status = "skipped"
        notice.detail = (
            "Invoice settled/cancelled, reminder superseded/disabled, or no recipient."
        )
        notice.save(update_fields=["status", "detail", "updated_at"])
        return None
    return notice


def deliver(limit=100, deadline=None):
    # An interrupted SMTP exchange is uncertain. Do not resend automatically.
    Notice.objects.filter(
        status="sending", updated_at__lt=timezone.now() - timedelta(minutes=15)
    ).update(
        status="unknown",
        detail="Worker interrupted. Confirm delivery before explicitly retrying.",
    )
    sent = 0
    for pk in (
        Notice.objects.filter(status="queued")
        .order_by("id")
        .values_list("pk", flat=True)[:limit]
    ):
        if deadline is not None and time.monotonic() >= deadline:
            break
        notice = claim(pk)
        if not notice:
            continue
        invoice = notice.invoice
        label = "Sandbox " if invoice.is_sandbox else ""
        subject = f"{label}{invoice.business_name}: invoice {invoice.number}"
        body = (
            f"{label}Invoice {invoice.number}\nBusiness: {invoice.business_name}\n"
            f"Outstanding: INR {invoice.balance:.2f}\nDue: {invoice.due_date}\n"
            f"Contact {invoice.business_email} for your invoice or payment instructions.\n"
        )
        if invoice.is_sandbox:
            body += "This is a test invoice. Do not send real money.\n"
        else:
            body += "This email does not authorize an automatic debit.\n"
        try:
            count = EmailMessage(
                subject,
                body,
                to=[notice.recipient],
            ).send(fail_silently=False)
            if count != 1:
                raise RuntimeError("Delivery was not acknowledged.")
        except Exception:
            Notice.objects.filter(pk=pk, status="sending").update(
                status="unknown",
                detail="Email delivery uncertain. Confirm with your mail provider before retrying.",
                updated_at=timezone.now(),
            )
        else:
            Notice.objects.filter(pk=pk, status="sending").update(
                status="sent",
                sent_at=timezone.now(),
                detail="SMTP accepted the message; inbox delivery is not confirmed.",
                updated_at=timezone.now(),
            )
            sent += 1
    return sent
