from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from django.db import transaction
from django.db.models import Case, Count, F, Q, Sum, Value, When
from django.utils import timezone
from core.models import Activity
from .signals import billing_event
from .models import (
    CreditNote,
    Refund,
    ZERO,
    Discount,
    Invoice,
    InvoiceLine,
    Payment,
    Sequence,
    Subscription,
    SubscriptionLine,
)

CENT = Decimal("0.01")


class BillingError(Exception):
    pass


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def log(workspace, user, action, detail="", actor_label=""):
    Activity.objects.create(
        workspace=workspace,
        actor=user,
        is_system=user is None and not actor_label,
        actor_label=actor_label,
        action=action,
        detail=detail[:220],
    )


def emit(invoice, event, payment=None, **documents):
    invoice.refresh_from_db()
    billing_event.send(
        sender=Invoice,
        workspace=invoice.workspace,
        event=event,
        invoice=invoice,
        payment=payment,
        **documents,
    )


# What is still owed: total - credited - (amount_paid - refunded).
OWED = F("total") - F("credited") - F("amount_paid") + F("refunded")


def next_number(workspace, kind, prefix):
    sequence, _ = Sequence.objects.get_or_create(workspace=workspace, kind=kind)
    # A single UPDATE claims the value atomically; the unique constraint on the
    # document number is the backstop.
    Sequence.objects.filter(pk=sequence.pk).update(next_value=F("next_value") + 1)
    sequence.refresh_from_db(fields=["next_value"])
    return f"{prefix}-{sequence.next_value - 1:05d}"


def describe_item(plan=None, product=None, variant=None):
    """Return the description, unit price, and tax for a catalog item."""
    if plan:
        return str(plan), plan.price, plan.tax
    if variant:
        return (
            f"{variant.product.name}: {variant.name}",
            variant.unit_price,
            variant.product.tax,
        )
    return product.name, product.sales_price, product.tax


def build_line(line_class, item, quantity, **parent):
    description, unit_price, tax = describe_item(**item)
    plan = item.get("plan")
    if plan and quantity < plan.min_quantity:
        raise BillingError(f"This plan requires at least {plan.min_quantity} units.")
    if item.get("variant") and plan:
        if plan.product_id and item["variant"].product_id != plan.product_id:
            raise BillingError("The variant does not belong to this plan product.")
        unit_price += item["variant"].price_adjustment
        description += ": " + item["variant"].name
    if unit_price < 0:
        raise BillingError("This item's price is below zero. Update the catalog first.")
    return line_class(
        **parent,
        **item,
        quantity=quantity,
        description=description,
        unit_price=money(unit_price),
        tax_name=tax.name if tax else "",
        tax_rate=tax.rate if tax else ZERO,
    )


def line_matches(discount, line, product_ids):
    return not product_ids or line.product_id in product_ids


def discount_problem(discount, lines, on_date):
    """Explain why a discount cannot apply, or return None when it can."""
    if not discount.is_active:
        return "This discount is switched off."
    if discount.starts_on and on_date < discount.starts_on:
        return f"This discount starts on {discount.starts_on:%d %b %Y}."
    if discount.ends_on and on_date > discount.ends_on:
        return f"This discount expired on {discount.ends_on:%d %b %Y}."
    if discount.usage_limit is not None and discount.times_used >= discount.usage_limit:
        return "This discount has reached its usage limit."
    product_ids = set(discount.products.values_list("pk", flat=True))
    eligible = [line for line in lines if line_matches(discount, line, product_ids)]
    if not eligible:
        return "No line on this invoice is eligible for this discount."
    base = sum((money(line.unit_price * line.quantity) for line in eligible), ZERO)
    if base < discount.min_amount:
        return f"Eligible lines must total at least ₹{discount.min_amount}."
    if sum(line.quantity for line in eligible) < discount.min_quantity:
        return f"Eligible lines need a quantity of at least {discount.min_quantity}."
    return None


def calculate(lines, discount=None):
    """Price lines in place: discount before tax, rounded per line."""
    for line in lines:
        line.subtotal = money(line.unit_price * line.quantity)
        line.discount_amount = ZERO
    if discount:
        product_ids = set(discount.products.values_list("pk", flat=True))
        eligible = [l for l in lines if line_matches(discount, l, product_ids)]
        base = sum((l.subtotal for l in eligible), ZERO)
        if discount.kind == Discount.Kind.PERCENT:
            reduction = money(base * discount.value / 100)
        else:
            reduction = min(money(discount.value), base)
        remaining = reduction
        for index, line in enumerate(eligible):
            # The last eligible line absorbs the rounding remainder.
            if index == len(eligible) - 1:
                share = remaining
            else:
                share = money(reduction * line.subtotal / base) if base else ZERO
            line.discount_amount = share
            remaining -= share
    for line in lines:
        taxable = line.subtotal - line.discount_amount
        line.tax_amount = money(taxable * line.tax_rate / 100)
        line.total = taxable + line.tax_amount
    return {
        "subtotal": sum((l.subtotal for l in lines), ZERO),
        "discount_total": sum((l.discount_amount for l in lines), ZERO),
        "tax_total": sum((l.tax_amount for l in lines), ZERO),
        "total": sum((l.total for l in lines), ZERO),
    }


PRICED_FIELDS = ["subtotal", "discount_amount", "tax_amount", "total"]


def applicable_discount(invoice, lines):
    if invoice.discount and not discount_problem(
        invoice.discount, lines, timezone.localdate()
    ):
        return invoice.discount
    return None


def recalculate_invoice(invoice):
    lines = list(invoice.lines.all())
    totals = calculate(lines, applicable_discount(invoice, lines))
    InvoiceLine.objects.bulk_update(lines, PRICED_FIELDS)
    Invoice.objects.filter(pk=invoice.pk, status=Invoice.Status.DRAFT).update(**totals)


def recalculate_subscription(subscription):
    lines = list(subscription.lines.all())
    totals = calculate(lines)
    SubscriptionLine.objects.bulk_update(lines, PRICED_FIELDS)
    Subscription.objects.filter(pk=subscription.pk).update(
        subtotal=totals["subtotal"],
        tax_total=totals["tax_total"],
        total=totals["total"],
    )


def claim_draft(model, pk, status):
    """Lock a still-editable document for this transaction, or fail."""
    if not model.objects.filter(pk=pk, status=status).update(updated_at=timezone.now()):
        raise BillingError("This document can no longer be edited.")


@transaction.atomic
def add_invoice_line(invoice, item, quantity):
    claim_draft(Invoice, invoice.pk, Invoice.Status.DRAFT)
    build_line(InvoiceLine, item, quantity, invoice=invoice).save()
    recalculate_invoice(invoice)


@transaction.atomic
def remove_invoice_line(invoice, line_id):
    claim_draft(Invoice, invoice.pk, Invoice.Status.DRAFT)
    InvoiceLine.objects.filter(pk=line_id, invoice=invoice).delete()
    recalculate_invoice(invoice)


@transaction.atomic
def add_subscription_line(subscription, item, quantity):
    claim_draft(Subscription, subscription.pk, Subscription.Status.DRAFT)
    replacement = build_line(SubscriptionLine, item, quantity, subscription=subscription)
    existing = subscription.lines.filter(plan=item.get("plan"), variant=item.get("variant")).first() if item.get("plan") else None
    if existing and item.get("plan"):
        replacement.pk = existing.pk
    replacement.save()
    recalculate_subscription(subscription)


@transaction.atomic
def remove_subscription_line(subscription, line_id):
    claim_draft(Subscription, subscription.pk, Subscription.Status.DRAFT)
    SubscriptionLine.objects.filter(pk=line_id, subscription=subscription).delete()
    recalculate_subscription(subscription)


@transaction.atomic
def create_subscription(form, user, template=None):
    subscription = form.save(commit=False)
    subscription.created_by = user
    subscription.number = next_number(subscription.workspace, "subscription", "SUB")
    if template:
        subscription.template = template
        subscription.plan = subscription.plan or template.plan
        subscription.valid_until = subscription.valid_until or (
            timezone.localdate() + timedelta(days=template.validity_days)
        )
        subscription.notes = subscription.notes or template.notes
    if subscription.plan_id and subscription.start_date and not subscription.expiry_date:
        from recurring.calendar import advance
        subscription.expiry_date = advance(subscription.start_date, subscription.plan.period, subscription.start_date) - timedelta(days=1)
    subscription.save()
    if template:
        SubscriptionLine.objects.bulk_create(
            build_line(
                SubscriptionLine,
                {"plan": l.plan, "product": l.product, "variant": l.variant},
                l.quantity,
                subscription=subscription,
            )
            for l in template.lines.select_related(
                "plan__tax", "product__tax", "variant__product__tax"
            )
        )
        recalculate_subscription(subscription)
    elif subscription.plan_id:
        build_line(SubscriptionLine, {"plan": subscription.plan, "product": subscription.plan.product, "variant": None}, subscription.plan.min_quantity, subscription=subscription).save()
        recalculate_subscription(subscription)
    log(subscription.workspace, user, "Quotation created", subscription.number)
    return subscription


S = Subscription.Status
TRANSITIONS = {
    "send": ([S.DRAFT], S.QUOTATION, "Quotation sent"),
    "confirm": ([S.QUOTATION], S.CONFIRMED, "Quotation confirmed"),
    "activate": ([S.CONFIRMED], S.ACTIVE, "Subscription activated"),
    "pause": ([S.ACTIVE], S.PAUSED, "Subscription paused"),
    "resume": ([S.PAUSED], S.ACTIVE, "Subscription resumed"),
    "close": ([S.ACTIVE, S.PAUSED], S.CLOSED, "Subscription closed"),
}


def transition_problem(subscription, action, today):
    plan = subscription.plan
    if action == "send":
        if not subscription.lines.exists():
            return "Add at least one line before sending the quotation."
        if not subscription.template_id and (not subscription.valid_until or subscription.valid_until < today):
            return "Set a quotation validity date that is today or later."
    if action == "confirm" and (not subscription.valid_until or subscription.valid_until < today):
        return "This quotation has expired. Prepare a new quotation before confirming."
    if action == "activate":
        if not plan:
            return "Choose a plan before activating a subscription."
        if not subscription.start_date:
            return "Set a start date before activating."
        if not plan.available_on(subscription.start_date):
            return "The plan is not available on the subscription's start date."
    if action == "pause" and (not plan or not plan.can_pause):
        return "This plan does not allow pausing."
    if action == "close" and (not plan or not plan.can_close):
        if not subscription.expiry_date or today < subscription.expiry_date:
            return "This plan does not allow closing before the expiry date."
    return None


@transaction.atomic
def transition(subscription, action, user, actor_label=""):
    from recurring.models import Schedule
    from recurring.services import lock_subscription, on_transition
    from checkout.services import lock_workspace

    lock_workspace(subscription.workspace_id)
    subscription = lock_subscription(subscription.pk)
    if Schedule.objects.filter(subscription=subscription, mode="automatic").exists():
        raise BillingError(
            "Use Automation to manage this provider-owned subscription. A local status change cannot stop a gateway charge."
        )
    if action not in TRANSITIONS:
        raise BillingError("Unknown subscription action.")
    sources, target, label = TRANSITIONS[action]
    if subscription.status not in sources:
        raise BillingError("This action is not available in the current state.")
    problem = transition_problem(subscription, action, timezone.localdate())
    if problem:
        raise BillingError(problem)
    if not Subscription.objects.filter(
        pk=subscription.pk, status=subscription.status
    ).update(status=target, updated_at=timezone.now()):
        raise BillingError("The subscription changed. Reload and try again.")
    log(subscription.workspace, user, label, subscription.number, actor_label)
    if action == "send" and subscription.template_id:
        Subscription.objects.filter(pk=subscription.pk).update(valid_until=timezone.localdate() + timedelta(days=subscription.template.validity_days))
    on_transition(subscription, action)
    if action == "activate" and subscription.auto_invoice and not Schedule.objects.filter(subscription=subscription).exists():
        Schedule.objects.create(subscription=subscription, mode="invoice", anchor_on=subscription.start_date, next_bill_on=subscription.start_date, period=subscription.plan.period, reminders=True, created_by=user)
    if action == "pause":
        Subscription.objects.filter(pk=subscription.pk).update(paused_on=timezone.localdate())
    elif action == "resume":
        Subscription.objects.filter(pk=subscription.pk).update(paused_on=None)


@transaction.atomic
def invoice_from_subscription(subscription, user):
    from recurring.models import Schedule

    if Schedule.objects.filter(subscription=subscription).exists():
        raise BillingError(
            "This subscription has a recurring billing schedule. Use Automation to avoid duplicate cycle invoices."
        )
    if subscription.status not in [S.CONFIRMED, S.ACTIVE]:
        raise BillingError("Only confirmed or active subscriptions can be invoiced.")
    invoice = Invoice.objects.create(
        workspace=subscription.workspace,
        customer=subscription.customer,
        subscription=subscription,
        payment_terms_days=subscription.payment_terms_days,
        created_by=user,
        source_channel=subscription.source_channel,
        utm_source=subscription.utm_source, utm_medium=subscription.utm_medium,
        utm_campaign=subscription.utm_campaign, utm_content=subscription.utm_content,
    )
    InvoiceLine.objects.bulk_create(
        InvoiceLine(
            invoice=invoice,
            plan_id=l.plan_id,
            product_id=l.product_id,
            variant_id=l.variant_id,
            quantity=l.quantity,
            description=l.description,
            unit_price=l.unit_price,
            tax_name=l.tax_name,
            tax_rate=l.tax_rate,
        )
        for l in subscription.lines.all()
    )
    recalculate_invoice(invoice)
    log(subscription.workspace, user, "Draft invoice created", subscription.number)
    return invoice


@transaction.atomic
def issue_invoice(invoice, user):
    # Claim the draft first so concurrent edits and double submits wait or fail.
    claim_draft(Invoice, invoice.pk, Invoice.Status.DRAFT)
    lines = list(invoice.lines.all())
    if not lines:
        raise BillingError("Add at least one line before issuing.")
    today = timezone.localdate()
    # Read the discount fresh: a cached copy may carry a stale usage count.
    discount = Discount.objects.filter(pk=invoice.discount_id).first()
    if discount:
        problem = discount_problem(discount, lines, today)
        if problem:
            raise BillingError(problem)
        if not invoice.is_sandbox and not Discount.objects.filter(
            Q(usage_limit=None) | Q(times_used__lt=F("usage_limit")),
            pk=discount.pk,
            is_active=True,
        ).update(times_used=F("times_used") + 1):
            raise BillingError("This discount has reached its usage limit.")
    totals = calculate(lines, discount)
    InvoiceLine.objects.bulk_update(lines, PRICED_FIELDS)
    customer, business = invoice.customer, invoice.workspace
    Invoice.objects.filter(pk=invoice.pk, status=Invoice.Status.DRAFT).update(
        **totals,
        status=Invoice.Status.ISSUED,
        number=next_number(
            business,
            "sandbox_invoice" if invoice.is_sandbox else "invoice",
            "TST" if invoice.is_sandbox else "INV",
        ),
        issue_date=today,
        due_date=today + timedelta(days=invoice.payment_terms_days),
        issued_at=timezone.now(),
        discount_label=str(discount) if discount else "",
        customer_name=customer.name,
        customer_email=customer.email,
        customer_address=customer.billing_address,
        customer_gstin=customer.gstin,
        business_name=business.legal_name,
        business_email=business.billing_email,
        business_address=business.address,
        business_gstin=business.gstin,
    )
    # A zero-total invoice owes nothing from the moment it is issued.
    settle(invoice)
    emit(invoice, "invoice.issued")
    log(business, user, "Invoice issued", f"{invoice.number}: ₹{invoice.total}")


@transaction.atomic
def cancel_invoice(invoice, user, reason):
    from recurring.models import Cycle

    if Cycle.objects.filter(invoice=invoice, schedule__mode="automatic").exists():
        raise BillingError(
            "A local cancellation cannot cancel provider collection. Manage the mandate under Automation."
        )
    if not Invoice.objects.filter(
        pk=invoice.pk, status=Invoice.Status.ISSUED, amount_paid=0, credited=0
    ).update(
        status=Invoice.Status.CANCELLED,
        cancelled_at=timezone.now(),
        cancel_reason=reason,
    ):
        raise BillingError(
            "Only issued invoices without active payments or credit notes can be cancelled. Reverse payments first."
        )
    if invoice.discount_id and not invoice.is_sandbox:
        # A cancelled invoice returns its discount use.
        Discount.objects.filter(pk=invoice.discount_id, times_used__gt=0).update(
            times_used=F("times_used") - 1
        )
    emit(invoice, "invoice.cancelled")
    log(invoice.workspace, user, "Invoice cancelled", f"{invoice.number}: {reason}")


@transaction.atomic
def record_payment(invoice, user, amount, method, received_on, reference="", note=""):
    if method != Payment.Method.RAZORPAY_TEST:
        from recurring.models import Cycle

        if Cycle.objects.filter(invoice=invoice, schedule__mode="automatic").exists():
            raise BillingError(
                "The provider owns collection for this invoice. Reconcile the mandate instead of starting another collection."
            )
    if method in Payment.GATEWAY_METHODS and not invoice.is_sandbox:
        raise BillingError("Sandbox payments can only settle sandbox invoices.")
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Enter an amount above zero.")
    # One conditional UPDATE checks and applies the balance, so concurrent
    # payments cannot together exceed the invoice total.
    if not Invoice.objects.alias(owed=OWED).filter(
        pk=invoice.pk,
        status=Invoice.Status.ISSUED,
        owed__gte=amount,
    ).update(amount_paid=F("amount_paid") + amount, updated_at=timezone.now()):
        raise BillingError(
            "This payment is more than the outstanding balance, or the invoice is not open for payments."
        )
    payment = Payment.objects.create(
        workspace=invoice.workspace,
        invoice=invoice,
        amount=amount,
        method=method,
        received_on=received_on,
        reference=reference,
        note=note,
        recorded_by=user,
    )
    settle(invoice)
    emit(invoice, "payment.recorded", payment)
    if invoice.status == Invoice.Status.PAID:
        emit(invoice, "invoice.paid", payment)
    log(invoice.workspace, user, "Payment recorded", f"{invoice.number}: ₹{amount}")
    return payment


def settle(invoice):
    """Mark an issued invoice paid once nothing more is owed."""
    Invoice.objects.alias(owed=OWED).filter(
        pk=invoice.pk, status=Invoice.Status.ISSUED, owed__lte=0
    ).update(status=Invoice.Status.PAID)


@transaction.atomic
def issue_credit_note(invoice, user, amount, reason):
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Enter a credit above zero.")
    if invoice.is_sandbox:
        raise BillingError("Sandbox invoices are refunded in the gateway's test dashboard, not with credit notes.")
    if not Invoice.objects.filter(
        pk=invoice.pk,
        status__in=[Invoice.Status.ISSUED, Invoice.Status.PAID],
        credited__lte=F("total") - amount,
    ).update(credited=F("credited") + amount, updated_at=timezone.now()):
        raise BillingError("The credit is more than the invoice has left to credit, or the invoice is not issued.")
    invoice.refresh_from_db()
    tax = money(amount * invoice.tax_total / invoice.total) if invoice.total else ZERO
    note = CreditNote.objects.create(
        workspace=invoice.workspace,
        invoice=invoice,
        number=next_number(invoice.workspace, "credit_note", "CN"),
        amount=amount,
        taxable_amount=amount - tax,
        tax_amount=tax,
        reason=reason,
        created_by=user,
    )
    settle(invoice)
    emit(invoice, "invoice.credited", credit_note=note)
    log(invoice.workspace, user, "Credit note issued", f"{note.number} on {invoice.number}: ₹{amount}, {reason}")
    return note


@transaction.atomic
def record_refund(invoice, user, amount, method, refunded_on, reference="", note=""):
    """Record money returned outside Mandali, up to the refund due."""
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Enter a refund above zero.")
    if method in Payment.GATEWAY_METHODS or invoice.is_sandbox:
        raise BillingError("Gateway payments are refunded in the gateway's dashboard and are not recorded here.")
    # A refund returns money received beyond what is owed, so a credit
    # note must come first. One conditional UPDATE enforces the limit.
    if not Invoice.objects.alias(owed=OWED).filter(
        pk=invoice.pk,
        status=Invoice.Status.PAID,
        owed__lte=-amount,
    ).update(refunded=F("refunded") + amount, updated_at=timezone.now()):
        raise BillingError("A refund can only return money received beyond what is owed. Issue a credit note first.")
    refund = Refund.objects.create(
        workspace=invoice.workspace,
        invoice=invoice,
        amount=amount,
        method=method,
        refunded_on=refunded_on,
        reference=reference,
        note=note,
        recorded_by=user,
    )
    emit(invoice, "refund.recorded", refund=refund)
    log(invoice.workspace, user, "Refund recorded", f"{invoice.number}: ₹{amount}")
    return refund


def duplicate_reference(payment):
    if not payment.reference:
        return None
    return (
        Payment.objects.filter(
            workspace=payment.workspace,
            reference__iexact=payment.reference,
            reversed_at=None,
        )
        .exclude(pk=payment.pk)
        .select_related("invoice")
        .first()
    )


@transaction.atomic
def reverse_payment(payment, user, reason):
    if payment.method in Payment.GATEWAY_METHODS:
        raise BillingError(
            "Gateway payments are reconciled with the provider and refunded in its dashboard, not reversed here."
        )
    if not Payment.objects.filter(pk=payment.pk, reversed_at=None).update(
        reversed_at=timezone.now(), reversed_by=user, reversal_reason=reason
    ):
        raise BillingError("This payment was already reversed.")
    # Refunds already sent cannot be undone by reversing the payment they came
    # from. The check is part of the UPDATE, so a refund recorded at the same
    # moment cannot slip between a check and the write.
    if not Invoice.objects.filter(
        pk=payment.invoice_id, amount_paid__gte=F("refunded") + payment.amount
    ).update(
        amount_paid=F("amount_paid") - payment.amount,
        status=Case(
            When(status=Invoice.Status.PAID, then=Value(Invoice.Status.ISSUED)),
            default=F("status"),
        ),
        updated_at=timezone.now(),
    ):
        raise BillingError("Part of this payment was already refunded, so it cannot be reversed.")
    # Credit notes can still cover the invoice after the reversal.
    settle(payment.invoice)
    payment.refresh_from_db()
    emit(payment.invoice, "payment.reversed", payment)
    log(
        payment.workspace,
        user,
        "Payment reversed",
        f"{payment.invoice.number}: ₹{payment.amount}, {reason}",
    )


def billing_summary(workspace, today):
    open_invoices = Invoice.objects.filter(
        workspace=workspace, status=Invoice.Status.ISSUED, is_sandbox=False
    )
    totals = open_invoices.aggregate(owed=Sum(OWED, default=ZERO))
    overdue = open_invoices.filter(due_date__lt=today).aggregate(
        count=Count("id"), owed=Sum(OWED, default=ZERO)
    )
    return {
        "outstanding": totals["owed"],
        "open_count": open_invoices.count(),
        "overdue_count": overdue["count"],
        "overdue": overdue["owed"],
    }
