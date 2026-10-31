"""Workspace-scoped finance and campaign reporting; no estimated ad attribution."""
import csv
from datetime import date, timedelta
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncMonth, Greatest
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from core.views import context, workspace_access
from .models import CampaignSpend, CheckoutVisit, Customer, Invoice, Payment, Plan, Subscription, ZERO
from .forms import ScopedForm, DATE
from .services import OWED


class ReportFilter(forms.Form):
    start = forms.DateField(required=False, widget=DATE, label="From")
    end = forms.DateField(required=False, widget=DATE, label="To")
    customer = forms.ModelChoiceField(Customer.objects.none(), required=False)
    plan = forms.ModelChoiceField(Plan.objects.none(), required=False)
    status = forms.ChoiceField(required=False, choices=[("", "All invoice statuses"), ("issued", "Outstanding"), ("partial", "Partially paid"), ("overdue", "Overdue"), ("paid", "Paid")])
    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(workspace=workspace)
        self.fields["plan"].queryset = Plan.objects.filter(workspace=workspace)
    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            raise forms.ValidationError("The end date must be on or after the start date.")
        return data


class SpendForm(ScopedForm):
    class Meta:
        model = CampaignSpend
        fields = ["source", "campaign", "spent_on", "amount"]
        widgets = {"spent_on": DATE}
        labels = {"amount": "Ad spend (₹)", "spent_on": "Spend date"}
    def clean_source(self):
        return self.cleaned_data["source"].strip().lower()
    def clean_campaign(self):
        return self.cleaned_data["campaign"].strip().lower()


def report_data(workspace, params):
    today = timezone.localdate()
    form = ReportFilter(params or None, workspace=workspace, initial={"start": today.replace(day=1), "end": today})
    valid = form.is_valid() if form.is_bound else True
    data = form.cleaned_data if form.is_bound and valid else {}
    start = data.get("start") or today.replace(day=1)
    end = data.get("end") or today
    invoices = Invoice.objects.filter(workspace=workspace, is_sandbox=False, status__in=["issued", "paid"])
    subscriptions = Subscription.objects.filter(workspace=workspace).exclude(schedule__is_sandbox=True)
    if data.get("customer"):
        invoices = invoices.filter(customer=data["customer"])
        subscriptions = subscriptions.filter(customer=data["customer"])
    if data.get("plan"):
        invoices = invoices.filter(subscription__plan=data["plan"])
        subscriptions = subscriptions.filter(plan=data["plan"])
    status = data.get("status")
    if status == "partial":
        invoices = invoices.filter(status="issued", amount_paid__gt=0)
    elif status == "overdue":
        invoices = invoices.filter(status="issued", due_date__lt=today)
    elif status:
        invoices = invoices.filter(status=status)
    period_invoices = invoices.filter(issue_date__range=[start, end])
    payments = Payment.objects.filter(workspace=workspace, invoice__in=invoices, reversed_at=None, received_on__range=[start, end])
    balance = ExpressionWrapper(Greatest(OWED, ZERO), output_field=DecimalField(max_digits=16, decimal_places=2))
    totals = invoices.aggregate(outstanding=Sum(balance, default=ZERO), overdue=Sum(balance, filter=Q(status="issued", due_date__lt=today), default=ZERO), overdue_count=Count("id", filter=Q(status="issued", due_date__lt=today)))
    totals["billed"] = period_invoices.aggregate(n=Sum("total", default=ZERO))["n"]
    totals["received"] = payments.aggregate(n=Sum("amount", default=ZERO))["n"]
    from .models import Refund
    totals["refunded"] = Refund.objects.filter(workspace=workspace, invoice__in=invoices, refunded_on__range=[start,end]).aggregate(n=Sum("amount", default=ZERO))["n"]
    totals["net_received"] = totals["received"] - totals["refunded"]
    totals["active"] = subscriptions.filter(status="active").count()
    factors = {"daily": Decimal(365)/12, "weekly": Decimal(52)/12, "monthly": Decimal(1), "yearly": Decimal(1)/12}
    totals["mrr"] = sum((row["value"] * factors.get(row["plan__period"], ZERO) for row in subscriptions.filter(status="active").values("plan__period").annotate(value=Sum("subtotal", default=ZERO))), ZERO).quantize(Decimal(".01"))
    # Collections over the last six calendar months. Query aggregation, not one query per month.
    month_start = date(today.year - (today.month < 6), (today.month - 6) % 12 + 1, 1)
    monthly = {(r["month"].date() if hasattr(r["month"], "hour") else r["month"]): r["amount"] for r in payments.model.objects.filter(workspace=workspace, invoice__is_sandbox=False, invoice__status__in=["issued", "paid"], reversed_at=None, received_on__range=[month_start, today]).annotate(month=TruncMonth("received_on")).values("month").annotate(amount=Sum("amount"))}
    # TruncMonth(DateField) returns date on some database backends.
    trend = []
    current = month_start
    for _ in range(6):
        amount = monthly.get(current, ZERO)
        trend.append({"label": current.strftime("%b"), "amount": amount})
        current = date(current.year+(current.month == 12), current.month % 12+1, 1)
    peak = max([r["amount"] for r in trend] + [Decimal(1)])
    for row in trend:
        row["level"] = max(0, min(10, int(row["amount"] / peak * 10)))
    forecast = []
    forecast_month = today.replace(day=1)
    for _ in range(3):
        forecast_month = date(forecast_month.year + (forecast_month.month == 12), forecast_month.month % 12 + 1, 1)
        forecast.append({"month": forecast_month, "amount": totals["mrr"]})
    campaigns = {}
    def campaign(key):
        return campaigns.setdefault(key, {"source": key[0] or "Unattributed", "campaign": key[1] or "No campaign", "creative": key[2] or "All creatives", "invoices": 0, "billed": ZERO, "received": ZERO, "spend": None, "visits": 0})
    for row in period_invoices.values("utm_source", "utm_campaign", "utm_content").annotate(n=Count("id"), amount=Sum("total")):
        r = campaign((row["utm_source"], row["utm_campaign"], row["utm_content"]))
        r.update(invoices=row["n"], billed=row["amount"])
    for row in payments.values("invoice__utm_source", "invoice__utm_campaign", "invoice__utm_content").annotate(amount=Sum("amount")):
        campaign((row["invoice__utm_source"], row["invoice__utm_campaign"], row["invoice__utm_content"]))["received"] = row["amount"]
    for row in CheckoutVisit.objects.filter(workspace=workspace, created_at__date__range=[start, end]).values("source", "campaign", "content").annotate(n=Count("id")):
        campaign((row["source"], row["campaign"], row["content"]))["visits"] = row["n"]
    # Spend and ROAS at source/campaign level, avoiding duplication across creatives.
    campaign_summary = {}
    for key, row in campaigns.items():
        summary = campaign_summary.setdefault(key[:2], {"source": row["source"], "campaign": row["campaign"], "received": ZERO, "spend": None, "roas": None})
        summary["received"] += row["received"]
    for row in CampaignSpend.objects.filter(workspace=workspace, spent_on__range=[start, end]).values("source", "campaign").annotate(amount=Sum("amount")):
        summary = campaign_summary.setdefault((row["source"], row["campaign"]), {"source": row["source"], "campaign": row["campaign"], "received": ZERO, "roas": None})
        summary["spend"] = row["amount"]
        if row["amount"] > 0:
            summary["roas"] = (summary["received"] / row["amount"]).quantize(Decimal(".01"))
    channels = period_invoices.values("source_channel").annotate(count=Count("id"), total=Sum("total")).order_by("-total")
    return {"filter_form": form, "start": start, "end": end, "metrics": totals, "trend": trend, "forecast": forecast, "campaigns": sorted(campaigns.values(), key=lambda r: r["received"], reverse=True)[:100], "campaign_summary": sorted(campaign_summary.values(), key=lambda r: r["received"], reverse=True)[:100], "channels": channels, "overdue_invoices": invoices.filter(status="issued", due_date__lt=today).select_related("customer").order_by("due_date")[:4], "report_invoices": period_invoices.select_related("customer"), "today": today}


@workspace_access(admin=True)
def analytics(request, membership):
    data = report_data(membership.workspace, request.GET)
    if request.GET.get("export") == "csv" and not data["filter_form"].errors:
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="mandali-finance.csv"'
        writer = csv.writer(response)
        writer.writerow(["Invoice", "Customer", "Issued", "Due", "Total INR", "Paid INR", "Outstanding INR", "Source", "Campaign", "Ad"])
        for invoice in data["report_invoices"].iterator():
            def safe(value):
                value = str(value or "")
                return "'"+value if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")) else value
            writer.writerow([safe(v) for v in [invoice.number, invoice.customer_name, invoice.issue_date, invoice.due_date, invoice.total, invoice.amount_paid, invoice.balance, invoice.utm_source, invoice.utm_campaign, invoice.utm_content]])
        return response
    return render(request, "billing/analytics.html", {**context(membership), **data, "spend_form": SpendForm(workspace=membership.workspace), "query": request.GET.urlencode()})


@workspace_access(admin=True)
def spend(request, membership):
    form = SpendForm(request.POST or None, workspace=membership.workspace)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Campaign spend recorded. ROAS now uses this amount.")
        return redirect("analytics", workspace_id=membership.workspace.pk)
    from .views import form_page
    return form_page(request, membership, form, "Record ad spend", "Use the same lowercase source and campaign as your UTM links. Add one amount per campaign per day.", "Save spend", reverse("analytics", args=[membership.workspace.pk]))
