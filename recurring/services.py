from datetime import timedelta
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from billing import services as billing
from billing.models import Subscription, Invoice, InvoiceLine, Discount
from checkout.services import lock_workspace, log
from .calendar import advance
from .models import Schedule, Cycle, Notice


def lock_subscription(pk):
    Subscription.objects.filter(pk=pk).update(updated_at=timezone.now())
    return Subscription.objects.select_related("workspace", "plan", "customer").get(
        pk=pk
    )


@transaction.atomic
def configure(subscription, user, data):
    lock_workspace(subscription.workspace_id)
    sub = lock_subscription(subscription.pk)
    if (
        sub.status != Subscription.Status.ACTIVE
        or not sub.plan_id
        or not sub.lines.exists()
    ):
        raise billing.BillingError(
            "Activate a subscription with a plan and priced lines first."
        )
    if Schedule.objects.filter(subscription=sub).exists():
        raise billing.BillingError(
            "This subscription already has a billing schedule. Its history cannot be replaced."
        )
    # Validate at the service boundary as well as in the UI.
    from .forms import ScheduleForm

    values = {**data, "discount": data["discount"].pk if data.get("discount") else ""}
    form = ScheduleForm(values, subscription=sub)
    if not form.is_valid():
        raise billing.BillingError("Review the schedule dates, mode, and discount.")
    schedule = Schedule.objects.create(
        subscription=sub,
        mode=data["mode"],
        anchor_on=data["first_bill_on"],
        next_bill_on=data["first_bill_on"],
        period=sub.plan.period,
        is_sandbox=data["is_sandbox"],
        reminders=data["reminders"],
        discount=data.get("discount"),
        total_count=data["total_count"],
        created_by=user,
    )
    log(sub.workspace_id, user, "Recurring billing configured", sub.number)
    return schedule


def clone_invoice(schedule):
    sub = schedule.subscription
    invoice = Invoice.objects.create(
        workspace_id=sub.workspace_id,
        customer_id=sub.customer_id,
        subscription=sub,
        payment_terms_days=(
            0 if schedule.mode == "automatic" else sub.payment_terms_days
        ),
        created_by=schedule.created_by,
        is_sandbox=schedule.is_sandbox,
        source_channel=sub.source_channel,
    )
    InvoiceLine.objects.bulk_create(
        InvoiceLine(
            invoice=invoice,
            plan_id=line.plan_id,
            product_id=line.product_id,
            variant_id=line.variant_id,
            quantity=line.quantity,
            description=line.description,
            unit_price=line.unit_price,
            tax_name=line.tax_name,
            tax_rate=line.tax_rate,
        )
        for line in sub.lines.all()
    )
    billing.recalculate_invoice(invoice)
    return invoice


def queue_notice(invoice, kind):
    Notice.objects.get_or_create(
        invoice=invoice, kind=kind, defaults={"recipient": invoice.customer_email}
    )


@transaction.atomic
def bill_one(schedule_id, today=None):
    today = today or timezone.localdate()
    initial = Schedule.objects.select_related("subscription").get(pk=schedule_id)
    lock_workspace(initial.subscription.workspace_id)
    sub = lock_subscription(initial.subscription_id)
    schedule = Schedule.objects.select_related("subscription").get(pk=schedule_id)
    if not schedule.enabled or schedule.mode != "invoice" or sub.status != "active":
        return None
    start = schedule.next_bill_on
    if sub.expiry_date and start > sub.expiry_date:
        if sub.plan.auto_close and today > sub.expiry_date:
            Subscription.objects.filter(pk=sub.pk).update(status="closed")
            Schedule.objects.filter(pk=schedule.pk).update(enabled=False)
            log(sub.workspace_id, None, "Subscription closed at expiry", sub.number)
        return None
    if start > today:
        return None
    boundary = advance(start, schedule.period, schedule.anchor_on)
    end = boundary
    if sub.expiry_date:
        end = min(end, sub.expiry_date + timedelta(days=1))
    prior = Cycle.objects.filter(schedule=schedule, starts_on=start).first()
    if prior:
        # History is immutable, including cancelled/paid invoices.
        Schedule.objects.filter(pk=schedule.pk).update(next_bill_on=boundary)
        return prior
    invoice = clone_invoice(schedule)
    if schedule.discount_id:
        discount = Discount.objects.select_for_update().get(pk=schedule.discount_id)
        if not billing.discount_problem(discount, list(invoice.lines.all()), today):
            invoice.discount = discount
            invoice.save(update_fields=["discount"])
    billing.issue_invoice(invoice, None)
    cycle = Cycle.objects.create(
        schedule=schedule, starts_on=start, ends_before=end, invoice=invoice
    )
    Schedule.objects.filter(pk=schedule.pk).update(next_bill_on=boundary, last_error="")
    if schedule.reminders:
        queue_notice(invoice, "issued")
    log(
        sub.workspace_id,
        None,
        "Recurring invoice generated",
        f"{sub.number}: {invoice.number}",
    )
    return cycle


def on_transition(sub, action):
    schedule = Schedule.objects.filter(subscription=sub).first()
    if not schedule:
        return
    if action == "close":
        schedule.enabled = False
    elif action == "resume":
        # Paused cycles are skipped, never retroactively charged on resume.
        today = timezone.localdate()
        while schedule.next_bill_on < today:
            schedule.next_bill_on = advance(
                schedule.next_bill_on, schedule.period, schedule.anchor_on
            )
    schedule.save(update_fields=["enabled", "next_bill_on"])


@transaction.atomic
def renew(subscription, user, expiry_date):
    lock_workspace(subscription.workspace_id)
    sub = lock_subscription(subscription.pk)
    schedule = Schedule.objects.filter(subscription=sub).first()
    if (
        not sub.plan_id
        or not sub.plan.can_renew
        or sub.status not in ("active", "closed", "paused")
    ):
        raise billing.BillingError(
            "This subscription cannot be renewed in its current state."
        )
    if schedule and schedule.mode == "automatic":
        raise billing.BillingError(
            "A completed automatic term needs a new subscription and fresh customer authorization."
        )
    today = timezone.localdate()
    if expiry_date < today or (sub.expiry_date and expiry_date <= sub.expiry_date):
        raise billing.BillingError(
            "Choose a future expiry date later than the current term."
        )
    sub.expiry_date = expiry_date
    sub.status = "paused" if sub.status == "paused" else "active"
    sub.save(update_fields=["expiry_date", "status", "updated_at"])
    if schedule:
        # Explicit renewal does not generate a backlog after a closed term.
        if not schedule.enabled:
            while schedule.next_bill_on < today:
                schedule.next_bill_on = advance(
                    schedule.next_bill_on, schedule.period, schedule.anchor_on
                )
        schedule.enabled = True
        schedule.save(update_fields=["enabled", "next_bill_on"])
    log(
        sub.workspace_id,
        user,
        "Subscription renewed",
        f"{sub.number}, through {expiry_date}",
    )


def queue_due_notices(today=None):
    today = today or timezone.localdate()
    invoices = Invoice.objects.filter(
        billing_cycle__schedule__mode="invoice",
        billing_cycle__schedule__reminders=True,
        status="issued",
        amount_paid__lt=F("total"),
        due_date__lte=today,
    )
    for invoice in invoices.iterator():
        if invoice.due_date == today and invoice.issue_date < today:
            queue_notice(invoice, "due")
        elif invoice.due_date + timedelta(days=3) <= today:
            queue_notice(invoice, "overdue")
