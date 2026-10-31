"""Business reports with one explicit meaning per figure.

Every figure excludes sandbox invoices and their test payments.
- Invoiced: totals of invoices issued in the period, excluding cancelled ones.
- Credited: credit notes issued in the period.
- Collected: payments received in the period, excluding reversed payments.
- Refunded: refunds recorded as paid out in the period.
- Outstanding and overdue: what is owed today, whenever it was invoiced.
- Recurring value: active subscriptions' per-period totals converted to a
  monthly figure. It is a run rate before discounts, not money collected.
"""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Count, Sum
from billing.models import ZERO, CreditNote, Invoice, Payment, Plan, Refund, Subscription
from billing.services import OWED, billing_summary

PERIODS = [
    ("this_month", "This month"),
    ("last_month", "Last month"),
    ("last_90", "Last 90 days"),
    ("this_fy", "This financial year"),
    ("last_fy", "Last financial year"),
    ("custom", "Custom dates"),
]
MONTHLY_FACTOR = {
    Plan.Period.DAILY: Decimal(365) / 12,
    Plan.Period.WEEKLY: Decimal(52) / 12,
    Plan.Period.MONTHLY: Decimal(1),
    Plan.Period.YEARLY: Decimal(1) / 12,
}
AGING = [(1, 30), (31, 60), (61, 90), (91, None)]
MAX_RANGE_DAYS = 3 * 366


class PeriodError(ValueError):
    pass


def financial_year_start(day):
    """Indian financial years run from 1 April to 31 March."""
    return date(day.year if day.month >= 4 else day.year - 1, 4, 1)


def period(key, today, start=None, end=None):
    if key == "this_month":
        return today.replace(day=1), today
    if key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if key == "last_90":
        return today - timedelta(days=89), today
    if key == "this_fy":
        return financial_year_start(today), today
    if key == "last_fy":
        begin = financial_year_start(today)
        return begin.replace(year=begin.year - 1), begin - timedelta(days=1)
    if key == "custom":
        if not start or not end:
            raise PeriodError("Choose both a start and an end date.")
        if end < start:
            raise PeriodError("The end date must be on or after the start date.")
        if (end - start).days > MAX_RANGE_DAYS:
            raise PeriodError("Choose a range of three years or less.")
        return start, end
    raise PeriodError("Choose a period from the list.")


def real_invoices(workspace):
    return Invoice.objects.filter(workspace=workspace, is_sandbox=False)


def summary(workspace, start, end, today):
    invoices = real_invoices(workspace).filter(issue_date__range=(start, end))
    issued = invoices.exclude(status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED]).aggregate(
        total=Sum("total", default=ZERO), tax=Sum("tax_total", default=ZERO), count=Count("id")
    )
    cancelled = invoices.filter(status=Invoice.Status.CANCELLED).aggregate(
        total=Sum("total", default=ZERO), count=Count("id")
    )
    credits = CreditNote.objects.filter(
        workspace=workspace, invoice__is_sandbox=False, created_at__date__range=(start, end)
    ).aggregate(total=Sum("amount", default=ZERO), count=Count("id"))
    payments = Payment.objects.filter(
        workspace=workspace, invoice__is_sandbox=False, reversed_at=None, received_on__range=(start, end)
    )
    collected = payments.aggregate(total=Sum("amount", default=ZERO), count=Count("id"))
    refunds = Refund.objects.filter(
        workspace=workspace, invoice__is_sandbox=False, refunded_on__range=(start, end)
    ).aggregate(total=Sum("amount", default=ZERO), count=Count("id"))
    by_method = [
        {"method": dict(Payment.Method.choices).get(row["method"], row["method"]), "total": row["total"], "count": row["count"]}
        for row in payments.values("method").annotate(total=Sum("amount"), count=Count("id")).order_by("-total")
    ]
    return {
        "invoiced": issued,
        "cancelled": cancelled,
        "credited": credits,
        "collected": collected,
        "refunded": refunds,
        "net_collected": collected["total"] - refunds["total"],
        "by_method": by_method,
        "now": billing_summary(workspace, today),
        "aging": aging(workspace, today),
        "recurring": recurring(workspace),
        "top_customers": top_customers(workspace),
    }


def aging(workspace, today):
    rows = (
        real_invoices(workspace)
        .filter(status=Invoice.Status.ISSUED, due_date__lt=today)
        .annotate(owed=OWED)
        .values_list("due_date", "owed")
    )
    buckets = [{"low": low, "high": high, "total": ZERO, "count": 0} for low, high in AGING]
    for due, owed in rows:
        days = (today - due).days
        for bucket in buckets:
            if days >= bucket["low"] and (bucket["high"] is None or days <= bucket["high"]):
                bucket["total"] += owed
                bucket["count"] += 1
                break
    return buckets


def recurring(workspace):
    active = Subscription.objects.filter(
        workspace=workspace, status=Subscription.Status.ACTIVE, plan__isnull=False
    ).values_list("plan__period", "total")
    monthly = sum((total * MONTHLY_FACTOR[period] for period, total in active), ZERO)
    counts = dict(
        Subscription.objects.filter(
            workspace=workspace, status__in=[Subscription.Status.ACTIVE, Subscription.Status.PAUSED]
        )
        .values_list("status")
        .annotate(n=Count("id"))
    )
    return {
        "monthly": monthly.quantize(Decimal("0.01")),
        "active": counts.get(Subscription.Status.ACTIVE, 0),
        "paused": counts.get(Subscription.Status.PAUSED, 0),
    }


def top_customers(workspace, limit=10):
    return (
        real_invoices(workspace)
        .filter(status=Invoice.Status.ISSUED)
        .values("customer_id", "customer__name")
        .annotate(owed=Sum(OWED), count=Count("id"))
        .order_by("-owed")[:limit]
    )


def attention(workspace, today):
    """Work that needs a person: overdue money, refunds owed, failed automation."""
    from checkout.models import PaymentAttempt
    from integrations.models import SyncRun, WebhookDelivery

    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    return [
        {
            "key": "overdue",
            "title": "Overdue invoices",
            "help": "Issued invoices past their due date with money still owed.",
            "items": real_invoices(workspace).filter(status=Invoice.Status.ISSUED, due_date__lt=today).order_by("due_date")[:20],
        },
        {
            "key": "refunds",
            "title": "Refunds due",
            "help": "Credit notes left these customers paying more than they owe. Return the money and record the refund.",
            "items": real_invoices(workspace).alias(owed=OWED).filter(status=Invoice.Status.PAID, owed__lt=0)[:20],
        },
        {
            "key": "drafts",
            "title": "Drafts older than a week",
            "help": "Invoices prepared but never issued.",
            "items": real_invoices(workspace).filter(status=Invoice.Status.DRAFT, created_at__date__lt=week_ago)[:20],
        },
        {
            "key": "quotes",
            "title": "Expired quotations",
            "help": "Sent quotations past their validity date. Revise or close them.",
            "items": Subscription.objects.filter(
                workspace=workspace, status=Subscription.Status.QUOTATION, valid_until__lt=today
            ).select_related("customer")[:20],
        },
        {
            "key": "gateway",
            "title": "Gateway payments to reconcile",
            "help": "Sandbox orders that are uncertain or need review. Check them under Payments.",
            "items": PaymentAttempt.objects.filter(
                workspace=workspace, status__in=["unknown", "review"]
            ).select_related("invoice", "gateway")[:20],
        },
        {
            "key": "webhooks",
            "title": "Webhook deliveries that gave up",
            "help": "Your website did not accept these events after every retry. Resend them under Integrations.",
            "items": WebhookDelivery.objects.filter(
                endpoint__workspace=workspace, status=WebhookDelivery.Status.FAILED, created_at__date__gte=month_ago
            ).select_related("endpoint")[:20],
        },
        {
            "key": "syncs",
            "title": "Website syncs that failed",
            "help": "Customer fetches from your website in the last week that did not complete.",
            "items": SyncRun.objects.filter(
                connector__workspace=workspace, status=SyncRun.Status.FAILED, created_at__date__gte=week_ago
            ).select_related("connector")[:20],
        },
    ]


def safe(value):
    """Stop spreadsheet apps from running a text cell as a formula."""
    if value is None:
        return ""
    if isinstance(value, (int, Decimal, date)):
        return str(value)
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


EXPORTS = {
    "invoices": (
        ["Number", "Customer", "Issue date", "Due date", "Status", "Subtotal", "Discount", "Tax", "Total", "Credited", "Paid", "Refunded", "Outstanding", "Source"],
        lambda ws, start, end: real_invoices(ws).filter(issue_date__range=(start, end)).exclude(status=Invoice.Status.DRAFT).order_by("issue_date", "id"),
        lambda i: [i.number, i.customer_name, i.issue_date, i.due_date, i.get_status_display(), i.subtotal, i.discount_total, i.tax_total, i.total, i.credited, i.amount_paid, i.refunded, i.balance if i.status == Invoice.Status.ISSUED else ZERO, i.get_source_channel_display()],
    ),
    "payments": (
        ["Received on", "Invoice", "Customer", "Amount", "Method", "Reference", "Reversed"],
        lambda ws, start, end: Payment.objects.filter(workspace=ws, invoice__is_sandbox=False, received_on__range=(start, end)).select_related("invoice").order_by("received_on", "id"),
        lambda p: [p.received_on, p.invoice.number, p.invoice.customer_name, p.amount, p.get_method_display(), p.reference, "Yes" if p.reversed_at else "No"],
    ),
    "credit_notes": (
        ["Number", "Issued", "Invoice", "Customer", "Taxable value", "Tax", "Credit", "Reason"],
        lambda ws, start, end: CreditNote.objects.filter(workspace=ws, invoice__is_sandbox=False, created_at__date__range=(start, end)).select_related("invoice").order_by("created_at", "id"),
        lambda n: [n.number, n.created_at.date(), n.invoice.number, n.invoice.customer_name, n.taxable_amount, n.tax_amount, n.amount, n.reason],
    ),
    "refunds": (
        ["Refunded on", "Invoice", "Customer", "Amount", "Method", "Reference"],
        lambda ws, start, end: Refund.objects.filter(workspace=ws, invoice__is_sandbox=False, refunded_on__range=(start, end)).select_related("invoice").order_by("refunded_on", "id"),
        lambda r: [r.refunded_on, r.invoice.number, r.invoice.customer_name, r.amount, r.get_method_display(), r.reference],
    ),
}


def export_csv(kind, workspace, start, end):
    header, rows, cells = EXPORTS[kind]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows(workspace, start, end).iterator():
        writer.writerow([safe(cell) for cell in cells(row)])
    return buffer.getvalue()
