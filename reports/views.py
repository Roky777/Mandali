from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.http import urlencode
from core.models import Activity, Membership
from core.views import context, workspace_access
from . import metrics
from .forms import ActivityFilterForm, PeriodForm

SECTIONS = [
    ("reports", "Summary"),
    ("reports_attention", "Needs attention"),
    ("reports_activity", "Activity"),
]


def resolve_period(request, today):
    form = PeriodForm(request.GET or None)
    key, start, end, error = "this_month", None, None, None
    if form.is_bound and form.is_valid():
        key, start, end = form.cleaned_data["period"], form.cleaned_data["start"], form.cleaned_data["end"]
    elif form.is_bound:
        error = "Choose a period from the list."
    try:
        start, end = metrics.period(key, today, start, end)
    except metrics.PeriodError as problem:
        error = str(problem)
        key = "this_month"
        start, end = metrics.period(key, today)
    return form, key, start, end, error


@workspace_access()
def summary(request, membership):
    today = timezone.localdate()
    form, key, start, end, error = resolve_period(request, today)
    query = urlencode({"period": key, "start": start, "end": end})
    return render(
        request,
        "reports/summary.html",
        {
            **context(membership),
            "sections": SECTIONS,
            "section": "reports",
            "form": form,
            "period_key": key,
            "period_label": dict(metrics.PERIODS)[key],
            "start": start,
            "end": end,
            "error": error,
            "query": query,
            "report": metrics.summary(membership.workspace, start, end, today),
            "exports": [("invoices", "Invoices"), ("payments", "Payments"), ("credit_notes", "Credit notes"), ("refunds", "Refunds")],
        },
    )


@workspace_access()
def attention(request, membership):
    today = timezone.localdate()
    groups = metrics.attention(membership.workspace, today)
    return render(
        request,
        "reports/attention.html",
        {
            **context(membership),
            "sections": SECTIONS,
            "section": "reports_attention",
            "groups": groups,
            "open_count": sum(1 for g in groups if g["items"]),
            "today": today,
        },
    )


@workspace_access()
def activity(request, membership):
    members = Membership.objects.filter(workspace=membership.workspace).select_related("user")
    form = ActivityFilterForm(request.GET or None, members=members)
    rows = Activity.objects.filter(workspace=membership.workspace).select_related("actor")
    if form.is_bound and form.is_valid():
        data = form.cleaned_data
        if data["q"]:
            rows = rows.filter(action__icontains=data["q"]) | rows.filter(detail__icontains=data["q"])
        if data["who"] == "system":
            rows = rows.filter(is_system=True)
        elif data["who"] == "customer":
            rows = rows.exclude(actor_label="")
        elif data["who"]:
            rows = rows.filter(actor_id=data["who"])
        if data["start"]:
            rows = rows.filter(created_at__date__gte=data["start"])
        if data["end"]:
            rows = rows.filter(created_at__date__lte=data["end"])
    params = {k: v for k, v in request.GET.items() if k != "page" and v}
    return render(
        request,
        "reports/activity.html",
        {
            **context(membership),
            "sections": SECTIONS,
            "section": "reports_activity",
            "form": form,
            "page": Paginator(rows.order_by("-created_at", "-id"), 50).get_page(request.GET.get("page")),
            "query_string": urlencode(params) + "&" if params else "",
        },
    )


@workspace_access(admin=True)
def export(request, membership, kind):
    if kind not in metrics.EXPORTS:
        raise Http404()
    today = timezone.localdate()
    _, key, start, end, _ = resolve_period(request, today)
    body = metrics.export_csv(kind, membership.workspace, start, end)
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="mandali-{kind}-{start}-to-{end}.csv"'
    return response
