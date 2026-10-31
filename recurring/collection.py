from datetime import datetime, time, timezone as dt_timezone
import time as clock
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from django.db import transaction
from django.utils import timezone
from billing import services as billing
from billing.models import Payment, Subscription
from checkout.crypto import CheckoutError, encrypt, decrypt
from checkout.gateways import GatewayError, GatewayRejected, identifier
from checkout.models import GatewayAccount, PaymentAttempt
from checkout.services import lock_workspace, paise, log
from .gateway import RecurringGateway
from .models import Schedule, Mandate, Cycle, Capture
from .services import lock_subscription, clone_invoice

TERMINAL = {"cancelled", "completed", "expired"}
STATES = {
    "created",
    "authenticated",
    "active",
    "pending",
    "halted",
    "paused",
} | TERMINAL


def plan_evidence(mandate, entity):
    plan_id = identifier(entity.get("id"), "plan")
    item = entity.get("item", {})
    notes = entity.get("notes")
    if (
        not isinstance(notes, dict)
        or notes.get("mandali_mandate") != str(mandate.pk)
        or (mandate.plan_id and plan_id != mandate.plan_id)
        or entity.get("period") != mandate.schedule.period
        or entity.get("interval") != 1
        or not isinstance(item, dict)
        or type(item.get("amount")) is not int
        or item["amount"] != mandate.amount_paise
        or item.get("currency") != "INR"
    ):
        raise GatewayError("Provider plan does not match the agreed amount and period.")
    return plan_id


def subscription_evidence(mandate, entity):
    subscription_id = identifier(entity.get("id"), "sub")
    notes = entity.get("notes")
    if (
        not isinstance(notes, dict)
        or notes.get("mandali_mandate") != str(mandate.pk)
        or entity.get("plan_id") != mandate.plan_id
        or entity.get("quantity") != 1
        or entity.get("total_count") != mandate.total_count
        or entity.get("start_at") != mandate.start_at
        or entity.get("status") not in STATES
        or entity.get("has_scheduled_changes", False)
        or entity.get("offer_id")
        or (mandate.subscription_id and subscription_id != mandate.subscription_id)
    ):
        raise GatewayError(
            "Provider subscription differs from the requested authorization. Review it in Razorpay Test."
        )
    return subscription_id


def authorization_url(mandate):
    return (
        decrypt(mandate.authorization_ciphertext)
        if mandate.authorization_ciphertext
        else ""
    )


@transaction.atomic
def save_subscription(mandate, entity):
    subscription_id = subscription_evidence(mandate, entity)
    lock_workspace(mandate.schedule.subscription.workspace_id)
    current = Mandate.objects.select_for_update().get(pk=mandate.pk)
    if current.status in TERMINAL and entity["status"] not in TERMINAL:
        raise GatewayError(
            "A stale provider response cannot reopen a completed or cancelled mandate."
        )
    if current.subscription_id and current.subscription_id != subscription_id:
        raise GatewayError("An existing authorization cannot be replaced.")
    current.subscription_id = subscription_id
    url = entity.get("short_url")
    if url:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"rzp.io", "rzp.pay"}
            or parsed.username
            or parsed.password
        ):
            raise GatewayError("The provider returned an unexpected authorization URL.")
        current.authorization_ciphertext = encrypt(url)
    current.status = entity["status"]
    current.verified_at = timezone.now()
    target = {"pause": "paused", "resume": "active", "cancel": "cancelled"}.get(
        current.pending_action
    )
    if current.status == target or current.status in TERMINAL:
        current.pending_action = ""
    current.detail = (
        "Provider action is uncertain. Reconcile it in Razorpay Test before another change."
        if current.pending_action
        else "Provider state verified. Only captured billing-cycle payments settle invoices."
    )
    current.save()
    sub = lock_subscription(mandate.schedule.subscription_id)
    if current.status in TERMINAL:
        Subscription.objects.filter(pk=sub.pk).update(status="closed")
        Schedule.objects.filter(pk=mandate.schedule_id).update(enabled=False)
    elif current.status == "paused":
        Subscription.objects.filter(pk=sub.pk).update(status="paused")
    elif current.status == "active" and not current.pending_action:
        Subscription.objects.filter(pk=sub.pk).update(status="active")
    return current


def prepare(schedule, user):
    with transaction.atomic():
        lock_workspace(schedule.subscription.workspace_id)
        sub = lock_subscription(schedule.subscription_id)
        schedule = Schedule.objects.get(pk=schedule.pk)
        total_count = schedule.total_count
        if (
            schedule.mode != "automatic"
            or not schedule.enabled
            or sub.status != "active"
            or not schedule.is_sandbox
        ):
            raise CheckoutError("This schedule does not allow automatic collection.")
        mandate = Mandate.objects.filter(schedule=schedule).first()
        if mandate and mandate.status not in {"rejected", "plan_ready"}:
            return mandate
        start_at = int(
            datetime.combine(
                schedule.anchor_on, time(), ZoneInfo("Asia/Kolkata")
            ).timestamp()
        )
        if start_at <= int(timezone.now().timestamp()):
            raise CheckoutError(
                "The authorization start time has passed. Use a new subscription with a future start."
            )
        if mandate is None:
            gateway = GatewayAccount.objects.filter(
                workspace_id=sub.workspace_id, is_active=True
            ).first()
            if not gateway:
                raise CheckoutError("Configure a Razorpay Test gateway first.")
            RecurringGateway(
                gateway
            )  # Fail before reserving if encryption is unavailable.
            amount = paise(billing.calculate(list(sub.lines.all()))["total"])
            if amount <= 0 or not 1 <= total_count <= 120:
                raise CheckoutError("Use a positive amount and 1–120 billing cycles.")
            mandate = Mandate.objects.create(
                schedule=schedule,
                gateway=gateway,
                key_id=gateway.key_id,
                total_count=total_count,
                start_at=start_at,
                amount_paise=amount,
            )
        else:
            mandate.status = "preparing"
            mandate.save(update_fields=["status"])
        log(
            sub.workspace_id,
            user,
            "Test authorization preparation started",
            str(mandate.pk),
        )
    gateway = RecurringGateway(mandate.gateway)
    try:
        if not mandate.plan_id:
            plan = gateway.create_plan(mandate)
            mandate.plan_id = plan_evidence(mandate, plan)
            Mandate.objects.filter(pk=mandate.pk).update(plan_id=mandate.plan_id)
        return save_subscription(mandate, gateway.create_subscription(mandate))
    except GatewayRejected as error:
        Mandate.objects.filter(pk=mandate.pk).update(
            status="rejected", detail=str(error)
        )
    except CheckoutError as error:
        Mandate.objects.filter(pk=mandate.pk).update(
            status="unknown", detail=str(error)
        )
    return Mandate.objects.get(pk=mandate.pk)


@transaction.atomic
def ensure_cycle(mandate, entity):
    """Mirror an authenticated provider invoice, whether unpaid or captured."""
    lock_workspace(mandate.schedule.subscription.workspace_id)
    lock_subscription(mandate.schedule.subscription_id)
    invoice_id = identifier(entity.get("id"), "inv")
    if (
        type(entity.get("amount")) is not int
        or entity["amount"] != mandate.amount_paise
        or entity.get("currency") != "INR"
        or entity.get("status") not in {"issued", "paid"}
        or entity.get("subscription_id", mandate.subscription_id)
        != mandate.subscription_id
    ):
        raise GatewayError(
            "Provider invoice differs from the fixed authorized INR amount or subscription."
        )
    start, end = entity.get("billing_start"), entity.get("billing_end")
    if (
        type(start) is not int
        or type(end) is not int
        or not mandate.start_at <= start < end < 4102444800
    ):
        raise GatewayError("Billing period evidence is missing or invalid.")
    starts_on = (
        datetime.fromtimestamp(start, dt_timezone.utc)
        .astimezone(ZoneInfo("Asia/Kolkata"))
        .date()
    )
    ends_before = (
        datetime.fromtimestamp(end, dt_timezone.utc)
        .astimezone(ZoneInfo("Asia/Kolkata"))
        .date()
    )
    prior = (
        Cycle.objects.filter(schedule=mandate.schedule, provider_invoice_id=invoice_id)
        .select_related("invoice")
        .first()
    )
    if prior:
        if prior.starts_on != starts_on or prior.ends_before != ends_before:
            raise GatewayError(
                "A provider invoice changed its billing period. Existing history is preserved."
            )
        return prior
    if (
        ends_before <= starts_on
        or Cycle.objects.filter(
            schedule=mandate.schedule,
            starts_on__lt=ends_before,
            ends_before__gt=starts_on,
        ).exists()
        or Cycle.objects.filter(schedule=mandate.schedule).count()
        >= mandate.total_count
    ):
        raise GatewayError(
            "This billing period overlaps an existing invoice or exceeds the authorized cycle count."
        )
    invoice = clone_invoice(mandate.schedule)
    billing.issue_invoice(invoice, None)
    if paise(invoice.total) != mandate.amount_paise:
        raise GatewayError("Subscription snapshots changed. No payment was applied.")
    return Cycle.objects.create(
        schedule=mandate.schedule,
        invoice=invoice,
        starts_on=starts_on,
        ends_before=ends_before,
        provider_invoice_id=invoice_id,
    )


@transaction.atomic
def apply_invoice(mandate, entity, evidence):
    """Entity comes only from this subscription's authenticated invoice listing."""
    lock_workspace(mandate.schedule.subscription.workspace_id)
    lock_subscription(mandate.schedule.subscription_id)
    invoice_id = identifier(entity.get("id"), "inv")
    payment_id = identifier(evidence.get("id"), "pay")
    prior = Capture.objects.filter(
        key_id=mandate.key_id, provider_payment_id=payment_id
    ).first()
    if prior:
        if prior.mandate_id != mandate.pk or prior.provider_invoice_id != invoice_id:
            raise GatewayError(
                "This provider capture is already claimed by another cycle."
            )
        if prior.payment_id and evidence.get("amount_refunded", 0) != 0:
            Capture.objects.filter(pk=prior.pk).update(
                status="review",
                detail="Provider reports a refund after capture. Recorded payment preserved; refund accounting requires staff review.",
            )
        if prior.payment_id:
            return prior
    capture = prior or Capture.objects.create(
        mandate=mandate,
        key_id=mandate.key_id,
        provider_invoice_id=invoice_id,
        provider_payment_id=payment_id,
    )

    def review(message):
        capture.detail = message
        capture.save(update_fields=["detail"])
        return capture

    if (
        evidence.get("invoice_id") != invoice_id
        or entity.get("payment_id") != payment_id
        or not entity.get("order_id")
        or evidence.get("order_id") != entity.get("order_id")
        or entity.get("subscription_id", mandate.subscription_id)
        != mandate.subscription_id
        or evidence.get("status") != "captured"
        or evidence.get("captured") is not True
        or type(evidence.get("amount")) is not int
        or evidence["amount"] != mandate.amount_paise
        or evidence.get("currency") != "INR"
        or entity.get("currency") != "INR"
        or entity.get("amount") != mandate.amount_paise
        or entity.get("amount_paid") != mandate.amount_paise
        or entity.get("status") != "paid"
        or evidence.get("amount_refunded", 0) != 0
    ):
        return review(
            "Capture does not match the fixed authorized INR amount, order, and paid invoice. No ledger entry was added."
        )
    if (
        PaymentAttempt.objects.filter(
            key_id=mandate.key_id, provider_payment_id=payment_id
        ).exists()
        or Payment.objects.filter(
            workspace_id=mandate.schedule.subscription.workspace_id,
            reference__iexact=payment_id,
            reversed_at=None,
        ).exists()
    ):
        return review(
            "The provider reference is already recorded elsewhere. No duplicate payment was added."
        )
    try:
        cycle = ensure_cycle(mandate, entity)
    except GatewayError as error:
        return review(str(error))
    invoice = cycle.invoice
    if invoice.status != "issued" or paise(invoice.balance) != mandate.amount_paise:
        return review(
            "The cycle invoice balance or status conflicts with this capture. Reconcile it before applying a payment."
        )
    capture.payment = billing.record_payment(
        invoice,
        None,
        Decimal(mandate.amount_paise) / 100,
        Payment.Method.RAZORPAY_TEST,
        timezone.localdate(),
        reference=payment_id,
        note="Verified authorized recurring Razorpay Test capture. No real money moved.",
    )
    capture.status = "paid"
    capture.detail = "Captured cycle verified and recorded once."
    capture.save(update_fields=["payment", "status", "detail"])
    Schedule.objects.filter(
        pk=mandate.schedule_id, next_bill_on__lt=cycle.ends_before
    ).update(next_bill_on=cycle.ends_before)
    return capture


def reconcile(
    mandate,
    subscription_id="",
    plan_id="",
    *,
    deadline=None,
    state_only=False,
    payment_id="",
):
    gateway = RecurringGateway(mandate.gateway)
    if not mandate.plan_id and plan_id:
        mandate.plan_id = plan_evidence(mandate, gateway.fetch_plan(plan_id))
        Mandate.objects.filter(pk=mandate.pk).update(
            plan_id=mandate.plan_id, status="plan_ready"
        )
    if not (mandate.subscription_id or subscription_id):
        return Mandate.objects.get(pk=mandate.pk)
    entity = gateway.fetch_subscription(mandate.subscription_id or subscription_id)
    mandate = save_subscription(mandate, entity)
    # Re-read the remote plan to detect out-of-band amount/cadence changes.
    plan_evidence(mandate, gateway.fetch_plan(mandate.plan_id))
    if state_only:
        return mandate
    for invoice in gateway.invoices(mandate.subscription_id):
        if deadline is not None and clock.monotonic() >= deadline:
            raise GatewayError(
                "Reconciliation time budget reached. The next worker run will continue; captured payments remain recorded."
            )
        if payment_id and invoice.get("payment_id") != payment_id:
            continue
        # Authorization charges have no billing period and never settle a cycle.
        if (
            not invoice.get("billing_start")
            or not invoice.get("billing_end")
            or invoice.get("status") not in {"issued", "paid"}
        ):
            continue
        if invoice.get("status") == "issued":
            ensure_cycle(mandate, invoice)
            continue
        reference = identifier(invoice.get("payment_id"), "pay")
        if (
            deadline is not None
            and Capture.objects.filter(
                mandate=mandate, provider_payment_id=reference, payment__isnull=False
            ).exists()
        ):
            continue
        apply_invoice(mandate, invoice, gateway.fetch_payment(reference))
    return Mandate.objects.get(pk=mandate.pk)


def change(mandate, user, action):
    if action not in {"pause", "resume", "cancel"}:
        raise CheckoutError("Unknown provider action.")
    with transaction.atomic():
        lock_workspace(mandate.schedule.subscription.workspace_id)
        mandate = Mandate.objects.select_related(
            "schedule__subscription__plan", "gateway"
        ).get(pk=mandate.pk)
        if (
            not mandate.subscription_id
            or mandate.status in TERMINAL
            or mandate.pending_action
        ):
            raise CheckoutError(
                "Reconcile this authorization before requesting another provider action."
            )
        if action == "pause" and not mandate.schedule.subscription.plan.can_pause:
            raise CheckoutError("This plan does not allow pausing.")
        mandate.pending_action = action
        mandate.save(update_fields=["pending_action"])
        log(
            mandate.schedule.subscription.workspace_id,
            user,
            f"Test mandate {action} requested",
            str(mandate.pk),
        )
    try:
        RecurringGateway(mandate.gateway).change(mandate, action)
        return reconcile(mandate, state_only=True)
    except GatewayRejected as error:
        Mandate.objects.filter(pk=mandate.pk).update(
            pending_action="", detail=str(error)
        )
        raise
    except CheckoutError:
        Mandate.objects.filter(pk=mandate.pk).update(
            detail="Provider action uncertain. Check Razorpay Test and reconcile; do not create another authorization."
        )
        raise


def process_event(account, payload):
    try:
        entity = payload["payload"]["subscription"]["entity"]
        subscription_id = identifier(entity["id"], "sub")
    except (KeyError, TypeError):
        raise GatewayError("Invalid subscription event.") from None
    mandate = Mandate.objects.filter(
        gateway=account, subscription_id=subscription_id
    ).first()
    if mandate is None:
        import uuid

        notes = entity.get("notes")
        try:
            mandate_id = uuid.UUID(notes["mandali_mandate"])
        except (KeyError, TypeError, ValueError):
            return "ignored"
        mandate = Mandate.objects.filter(gateway=account, pk=mandate_id).first()
    if mandate is None:
        return "ignored"
    payment_id = ""
    if payload.get("event") == "subscription.charged":
        try:
            payment_id = identifier(
                payload["payload"]["payment"]["entity"]["id"], "pay"
            )
        except (KeyError, TypeError):
            raise GatewayError(
                "The charged event is missing its payment reference."
            ) from None
    result = reconcile(
        mandate,
        subscription_id=subscription_id,
        state_only=payload.get("event") != "subscription.charged",
        payment_id=payment_id,
    )
    return result.status
