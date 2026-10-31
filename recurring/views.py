from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from billing.models import Subscription
from billing.services import BillingError
from checkout.crypto import CheckoutError
from checkout.services import log
from core.views import workspace_access, context
from core.security import rate_limit
from . import services, collection
from .forms import ScheduleForm, RenewalForm
from .models import Schedule, Notice, Mandate


@workspace_access()
def overview(request, membership):
    schedules = Schedule.objects.filter(
        subscription__workspace=membership.workspace
    ).select_related("subscription__customer", "mandate")
    due = schedules.filter(
        enabled=True, mode="invoice", subscription__status="active", next_bill_on__lte=timezone.localdate()
    ).select_related("subscription__plan")
    return render(
        request,
        "recurring/overview.html",
        {
            **context(membership),
            "due": due[:100],
            "due_count": due.count(),
            "schedules": schedules[:100],
            "notices": Notice.objects.filter(
                invoice__workspace=membership.workspace
            ).select_related("invoice")[:100],
        },
    )


@workspace_access(admin=True)
def setup(request, membership, pk):
    sub = get_object_or_404(Subscription, pk=pk, workspace=membership.workspace)
    form = ScheduleForm(request.POST or None, subscription=sub)
    if request.method == "POST" and form.is_valid():
        try:
            schedule = services.configure(sub, request.user, form.cleaned_data)
        except (BillingError, IntegrityError) as error:
            form.add_error(
                None,
                (
                    str(error)
                    if isinstance(error, BillingError)
                    else "A billing schedule already exists."
                ),
            )
        else:
            if schedule.mode == "automatic":
                # Creating an authorization is a deliberate separate POST.
                messages.info(
                    request,
                    "Schedule saved. Prepare an authorization link next; no debit is authorized yet.",
                )
            return redirect(
                "recurring_detail", workspace_id=membership.workspace.pk, pk=schedule.pk
            )
    return render(
        request,
        "core/form.html",
        {
            **context(membership),
            "form": form,
            "title": f"Recurring billing for {sub.number}",
            "description": "Review the first date carefully: existing manual invoices are not imported into this schedule. Dates use Asia/Kolkata. The final partial term is charged at the full period price.",
            "submit": "Save billing schedule",
            "cancel_url": reverse("subscription_detail", args=[membership.workspace.pk, sub.pk]),
        },
    )


@workspace_access()
def detail(request, membership, pk):
    schedule = get_object_or_404(
        Schedule.objects.select_related("subscription__plan", "subscription__customer"),
        pk=pk,
        subscription__workspace=membership.workspace,
    )
    mandate = Mandate.objects.filter(schedule=schedule).first()
    data = {
        **context(membership),
        "schedule": schedule,
        "mandate": mandate,
        "cycles": schedule.cycles.select_related("invoice")[:100],
    }
    if membership.role == "admin" and mandate:
        try:
            if mandate.status in {"created", "authenticated"}:
                data["authorization_url"] = collection.authorization_url(mandate)
        except CheckoutError as error:
            data["authorization_error"] = str(error)
        if not mandate.pending_action:
            data["can_pause"] = (
                mandate.status == "active" and schedule.subscription.plan.can_pause
            )
            data["can_resume"] = mandate.status == "paused"
            data["can_cancel"] = mandate.status in {
                "created",
                "authenticated",
                "active",
                "pending",
                "halted",
                "paused",
            }
    return render(request, "recurring/detail.html", data)


@require_POST
@workspace_access(admin=True)
@rate_limit("recurring-action", limit=20, seconds=900)
def action(request, membership, pk):
    schedule = get_object_or_404(
        Schedule.objects.select_related("subscription__plan"),
        pk=pk,
        subscription__workspace=membership.workspace,
    )
    command = request.POST.get("action", "")
    try:
        if command in {"reminders_on", "reminders_off"}:
            with transaction.atomic():
                services.lock_workspace(membership.workspace.pk)
                Schedule.objects.filter(pk=schedule.pk).update(
                    reminders=command == "reminders_on"
                )
                log(
                    membership.workspace.pk,
                    request.user,
                    "Recurring invoice reminders changed",
                    command,
                )
        elif command == "prepare":
            collection.prepare(schedule, request.user)
        elif command in {"reconcile", "pause", "resume", "cancel"}:
            mandate = get_object_or_404(Mandate, schedule=schedule)
            if command == "reconcile":
                collection.reconcile(
                    mandate,
                    request.POST.get("subscription_id", "")[:80],
                    request.POST.get("plan_id", "")[:80],
                )
                log(
                    membership.workspace.pk,
                    request.user,
                    "Recurring test mandate reconciled",
                    str(mandate.pk),
                )
            else:
                collection.change(mandate, request.user, command)
        else:
            raise CheckoutError("Unknown automation action.")
    except (BillingError, CheckoutError, IntegrityError) as error:
        messages.error(
            request,
            (
                str(error)
                if not isinstance(error, IntegrityError)
                else "Payment evidence conflicts with existing records. Review in Razorpay Test."
            ),
        )
    return redirect("recurring_detail", workspace_id=membership.workspace.pk, pk=pk)


@workspace_access(admin=True)
def renew(request, membership, pk):
    sub = get_object_or_404(Subscription, pk=pk, workspace=membership.workspace)
    form = RenewalForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.renew(sub, request.user, form.cleaned_data["expiry_date"])
        except BillingError as error:
            form.add_error(None, str(error))
        else:
            return redirect(
                "subscription_detail", workspace_id=membership.workspace.pk, pk=pk
            )
    return render(
        request,
        "core/form.html",
        {
            **context(membership),
            "form": form,
            "title": f"Renew {sub.number}",
            "description": "Extend the term at its existing snapshot price. A closed term resumes on its next billing boundary without charging the gap. Automatic terms need a new subscription and fresh authorization.",
            "submit": "Renew subscription",
            "cancel_url": reverse("subscription_detail", args=[membership.workspace.pk, sub.pk]),
        },
    )


@require_POST
@workspace_access(admin=True)
def retry_notice(request, membership, pk):
    notice = get_object_or_404(Notice, pk=pk, invoice__workspace=membership.workspace)
    if request.POST.get("confirm") != "retry":
        messages.error(
            request,
            "Confirm with your email provider before retrying an uncertain delivery.",
        )
    elif Notice.objects.filter(pk=notice.pk, status="unknown").update(
        status="queued",
        detail="Administrator requested an explicit retry.",
        updated_at=timezone.now(),
    ):
        log(
            membership.workspace.pk,
            request.user,
            "Invoice email retry requested",
            str(notice.pk),
        )
    return redirect("recurring_overview", workspace_id=membership.workspace.pk)


@require_POST
@workspace_access(admin=True)
def generate_due(request, membership):
    """Bill every invoice schedule due today, now, instead of waiting for the worker."""
    today = timezone.localdate()
    due = Schedule.objects.filter(
        subscription__workspace=membership.workspace, enabled=True, mode="invoice",
        subscription__status="active", next_bill_on__lte=today,
    ).values_list("pk", flat=True)
    created = 0
    for schedule_id in list(due[:200]):
        # Catch up missed periods one at a time; each period is unique per schedule.
        for _ in range(12):
            try:
                cycle = services.bill_one(schedule_id, today)
            except BillingError:
                break
            if cycle is None:
                break
            created += 1
    messages.success(request, f"{created} invoice{'s' if created != 1 else ''} generated.")
    return redirect("recurring_overview", workspace_id=membership.workspace.pk)

