from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from django.db import transaction
from django.db.models import Case, Count, F, Q, Sum, Value, When
from django.utils import timezone
from core.models import Activity
from .signals import billing_event
from .models import (
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


def log(workspace, user, action, detail=""):
    Activity.objects.create(
        workspace=workspace, actor=user, action=action, detail=detail[:220]
    )


def emit(invoice, event, payment=None):
    invoice.refresh_from_db()
    billing_event.send(
        sender=Invoice,
        workspace=invoice.workspace,
        event=event,
        invoice=invoice,
        payment=payment,
    )


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
        return f"{variant.product.name}: {variant.name}", variant.unit_price, variant.product.tax
    return product.name, product.sales_price, product.tax


def build_line(line_class, item, quantity, **parent):
    description, unit_price, tax = describe_item(**item)
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
    if not model.objects.filter(pk=pk, status=status).update(
        updated_at=timezone.now()
    ):
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
    build_line(SubscriptionLine, item, quantity, subscription=subscription).save()
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
    subscription.save()
    if template:
        SubscriptionLine.objects.bulk_create(
            build_line(
                SubscriptionLine,
                {"plan": l.plan, "product": l.product, "variant": l.variant},
                l.quantity,
                subscription=subscription,
            )
            for l in template.lines.select_related("plan__tax", "product__tax", "variant__product__tax")
        )
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
        if not subscription.valid_until or subscription.valid_until < today:
            return "Set a quotation validity date that is today or later."
    if action == "confirm" and subscription.valid_until < today:
        return f"This quotation expired on {subscription.valid_until:%d %b %Y}."
    if action == "activate":
        if not plan:
            return "Choose a plan before activating a subscription."
        if not subscription.start_date:
            return "Set a start date before activating."
        if not plan.available_on(subscription.start_date):
            return "The plan is not available on the subscription's start date."
    if action == "pause" and not plan.can_pause:
        return "This plan does not allow pausing."
    if action == "close" and not plan.can_close:
        if not subscription.expiry_date or today < subscription.expiry_date:
            return "This plan does not allow closing before the expiry date."
    return None


@transaction.atomic
def transition(subscription, action, user):
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
    log(subscription.workspace, user, label, subscription.number)


@transaction.atomic
def invoice_from_subscription(subscription, user):
    if subscription.status not in [S.CONFIRMED, S.ACTIVE]:
        raise BillingError("Only confirmed or active subscriptions can be invoiced.")
    invoice = Invoice.objects.create(
        workspace=subscription.workspace,
        customer=subscription.customer,
        subscription=subscription,
        payment_terms_days=subscription.payment_terms_days,
        created_by=user,
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
        if not Discount.objects.filter(
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
        number=next_number(business, "invoice", "INV"),
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
    emit(invoice, "invoice.issued")
    log(business, user, "Invoice issued", f"{invoice.number}: ₹{invoice.total}")


@transaction.atomic
def cancel_invoice(invoice, user, reason):
    if not Invoice.objects.filter(
        pk=invoice.pk, status=Invoice.Status.ISSUED, amount_paid=0
    ).update(
        status=Invoice.Status.CANCELLED,
        cancelled_at=timezone.now(),
        cancel_reason=reason,
    ):
        raise BillingError(
            "Only issued invoices without active payments can be cancelled. Reverse payments first."
        )
    if invoice.discount_id:
        # A cancelled invoice returns its discount use.
        Discount.objects.filter(pk=invoice.discount_id, times_used__gt=0).update(
            times_used=F("times_used") - 1
        )
    emit(invoice, "invoice.cancelled")
    log(invoice.workspace, user, "Invoice cancelled", f"{invoice.number}: {reason}")


@transaction.atomic
def record_payment(invoice, user, amount, method, received_on, reference="", note=""):
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Enter an amount above zero.")
    # One conditional UPDATE checks and applies the balance, so concurrent
    # payments cannot together exceed the invoice total.
    if not Invoice.objects.filter(
        pk=invoice.pk,
        status=Invoice.Status.ISSUED,
        amount_paid__lte=F("total") - amount,
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
    Invoice.objects.filter(
        pk=invoice.pk, status=Invoice.Status.ISSUED, amount_paid=F("total")
    ).update(status=Invoice.Status.PAID)
    emit(invoice, "payment.recorded", payment)
    if invoice.status == Invoice.Status.PAID:
        emit(invoice, "invoice.paid", payment)
    log(invoice.workspace, user, "Payment recorded", f"{invoice.number}: ₹{amount}")
    return payment


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
    if not Payment.objects.filter(pk=payment.pk, reversed_at=None).update(
        reversed_at=timezone.now(), reversed_by=user, reversal_reason=reason
    ):
        raise BillingError("This payment was already reversed.")
    Invoice.objects.filter(pk=payment.invoice_id).update(
        amount_paid=F("amount_paid") - payment.amount,
        status=Case(
            When(status=Invoice.Status.PAID, then=Value(Invoice.Status.ISSUED)),
            default=F("status"),
        ),
        updated_at=timezone.now(),
    )
    payment.refresh_from_db()
    emit(payment.invoice, "payment.reversed", payment)
    log(
        payment.workspace,
        user,
        "Payment reversed",
        f"{payment.invoice.number}: ₹{payment.amount}, {reason}",
    )


def billing_summary(workspace, today):
    open_invoices = Invoice.objects.filter(workspace=workspace, status=Invoice.Status.ISSUED)
    totals = open_invoices.aggregate(
        total=Sum("total", default=ZERO), paid=Sum("amount_paid", default=ZERO)
    )
    overdue = open_invoices.filter(due_date__lt=today).aggregate(
        count=Count("id"), total=Sum("total", default=ZERO), paid=Sum("amount_paid", default=ZERO)
    )
    return {
        "outstanding": totals["total"] - totals["paid"],
        "open_count": open_invoices.count(),
        "overdue_count": overdue["count"],
        "overdue": overdue["total"] - overdue["paid"],
    }
