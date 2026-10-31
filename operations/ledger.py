"""Ledger invariants that must hold at all times.

Each check returns the offending record IDs. They are written as plain
queries so they can run against a production replica or a restored backup.
"""

from decimal import Decimal
from django.db.models import Count, DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from billing.models import CreditNote, Discount, Invoice, InvoiceLine, Payment, Refund
from billing.services import OWED

ZERO = Value(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))


def total_of(model, field, **filters):
    rows = (
        model.objects.filter(invoice=OuterRef("pk"), **filters)
        .values("invoice")
        .annotate(total=Sum(field))
        .values("total")
    )
    return Coalesce(Subquery(rows, output_field=DecimalField(max_digits=12, decimal_places=2)), ZERO)


def checks(workspace=None):
    invoices = Invoice.objects.all()
    payments = Payment.objects.all()
    discounts = Discount.objects.all()
    if workspace is not None:
        invoices = invoices.filter(workspace=workspace)
        payments = payments.filter(workspace=workspace)
        discounts = discounts.filter(workspace=workspace)
    sums = invoices.annotate(
        paid_sum=total_of(Payment, "amount", reversed_at=None),
        credit_sum=total_of(CreditNote, "amount"),
        refund_sum=total_of(Refund, "amount"),
        line_total=total_of(InvoiceLine, "total"),
        owed=OWED,
    )
    issued = sums.exclude(status=Invoice.Status.DRAFT)
    duplicate_refs = (
        payments.filter(reversed_at=None, method__in=Payment.GATEWAY_METHODS)
        .values("workspace", "reference")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    from checkout.models import PaymentAttempt

    attempts = PaymentAttempt.objects.all()
    if workspace is not None:
        attempts = attempts.filter(workspace=workspace)
    counted = Invoice.objects.filter(
        discount=OuterRef("pk"),
        is_sandbox=False,
        status__in=[Invoice.Status.ISSUED, Invoice.Status.PAID],
    ).values("discount").annotate(n=Count("id")).values("n")
    return [
        ("Paid amount matches recorded payments", sums.exclude(amount_paid=F("paid_sum"))),
        ("Credited amount matches credit notes", sums.exclude(credited=F("credit_sum"))),
        ("Refunded amount matches refunds", sums.exclude(refunded=F("refund_sum"))),
        ("Invoice total matches its lines", issued.exclude(total=F("line_total"))),
        ("Issued invoices still owe money", sums.filter(status=Invoice.Status.ISSUED, owed__lte=0)),
        ("Paid invoices owe nothing", sums.filter(status=Invoice.Status.PAID, owed__gt=0)),
        ("Drafts and cancelled invoices hold no money", sums.filter(
            Q(status=Invoice.Status.DRAFT) | Q(status=Invoice.Status.CANCELLED)
        ).filter(Q(amount_paid__gt=0) | Q(credited__gt=0))),
        ("Issued invoices have a number", issued.filter(number="")),
        ("Paid gateway attempts have exactly one ledger payment", attempts.filter(status="paid", payment__isnull=True)),
        ("Gateway payment references are recorded once", duplicate_refs),
        ("Discount usage matches issued invoices", discounts.annotate(
            counted=Coalesce(Subquery(counted), Value(0))
        ).exclude(times_used=F("counted"))),
    ]


def run(workspace=None, sample=10):
    results = []
    for name, queryset in checks(workspace):
        rows = list(queryset[:sample])
        identifiers = [
            row.get("reference") if isinstance(row, dict) else getattr(row, "number", "") or str(row.pk)
            for row in rows
        ]
        results.append({"check": name, "failures": queryset.count(), "examples": identifiers})
    return results
