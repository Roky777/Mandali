from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from billing.services import log
from core.views import context, workspace_access
from . import sync, webhooks
from .forms import ApiKeyForm, ConnectorForm, FetchForm, WebhookForm
from .keys import create_key
from .models import ApiKey, Connector, SyncRun, WebhookDelivery, WebhookEndpoint

SECTIONS = [
    ("integration_keys", "API keys"),
    ("integration_webhooks", "Webhooks"),
    ("integration_connectors", "Website sync"),
]


def page(membership, section, **extra):
    return {
        **context(membership),
        "section": section,
        "sections": SECTIONS,
        "api_enabled": membership.workspace.use_api,
        "base_url": settings.MANDALI_BASE_URL,
        **extra,
    }


def url(name, membership, *args):
    return reverse(name, args=[membership.workspace.pk, *args])


def channel_off(request, membership):
    if membership.workspace.use_api:
        return None
    messages.error(request, "Turn on Website / API integration in Settings first.")
    return redirect(url("integration_keys", membership))


@workspace_access(admin=True)
def keys(request, membership):
    form = ApiKeyForm(request.POST or None)
    if request.method == "POST":
        blocked = channel_off(request, membership)
        if blocked:
            return blocked
        if form.is_valid():
            with transaction.atomic():
                key, raw = create_key(membership.workspace, request.user, form.cleaned_data["name"])
                log(membership.workspace, request.user, "API key created", key.name)
            return render(
                request,
                "integrations/secret_created.html",
                page(
                    membership,
                    "integration_keys",
                    title="API key created",
                    explanation=f"Copy the key for {key.name} now. Mandali stores only a fingerprint of it, so it cannot be shown again.",
                    secret=raw,
                    label="API key",
                    back=url("integration_keys", membership),
                ),
            )
    return render(
        request,
        "integrations/keys.html",
        page(
            membership,
            "integration_keys",
            form=form,
            keys=ApiKey.objects.filter(workspace=membership.workspace).select_related("created_by"),
        ),
    )


@require_POST
@workspace_access(admin=True)
def key_revoke(request, membership, pk):
    with transaction.atomic():
        if ApiKey.objects.filter(pk=pk, workspace=membership.workspace, revoked_at=None).update(
            revoked_at=timezone.now()
        ):
            log(membership.workspace, request.user, "API key revoked")
            messages.success(request, "Key revoked. Requests using it are refused from now on.")
    return redirect(url("integration_keys", membership))


@workspace_access(admin=True)
def webhook_list(request, membership):
    form = WebhookForm(request.POST or None)
    if request.method == "POST":
        blocked = channel_off(request, membership)
        if blocked:
            return blocked
        if form.is_valid():
            with transaction.atomic():
                endpoint = form.save(commit=False)
                endpoint.workspace = membership.workspace
                endpoint.created_by = request.user
                endpoint.secret = webhooks.new_secret()
                endpoint.save()
                log(membership.workspace, request.user, "Webhook endpoint added", endpoint.url)
            return render(
                request,
                "integrations/secret_created.html",
                page(
                    membership,
                    "integration_webhooks",
                    title="Webhook endpoint added",
                    explanation="Copy the signing secret now and keep it on your website's server. Use it to check the Mandali-Signature header on every delivery. It is not shown again.",
                    secret=endpoint.secret,
                    label="Signing secret",
                    back=url("integration_webhook", membership, endpoint.pk),
                ),
            )
    return render(
        request,
        "integrations/webhooks.html",
        page(
            membership,
            "integration_webhooks",
            form=form,
            endpoints=WebhookEndpoint.objects.filter(workspace=membership.workspace),
            events=webhooks.EVENTS,
        ),
    )


@workspace_access(admin=True)
def webhook_detail(request, membership, pk):
    endpoint = get_object_or_404(WebhookEndpoint, pk=pk, workspace=membership.workspace)
    return render(
        request,
        "integrations/webhook_detail.html",
        page(
            membership,
            "integration_webhooks",
            endpoint=endpoint,
            deliveries=endpoint.deliveries.all()[:50],
        ),
    )


@require_POST
@workspace_access(admin=True)
def webhook_action(request, membership, pk):
    endpoint = get_object_or_404(WebhookEndpoint, pk=pk, workspace=membership.workspace)
    action = request.POST.get("action")
    if action == "toggle":
        endpoint.is_active = not endpoint.is_active
        endpoint.save(update_fields=["is_active"])
        log(membership.workspace, request.user, f"Webhook endpoint {'resumed' if endpoint.is_active else 'paused'}", endpoint.url)
        messages.success(request, "Endpoint resumed." if endpoint.is_active else "Endpoint paused. New events are not queued for it.")
    elif action == "test" and endpoint.is_active:
        with transaction.atomic():
            delivery = WebhookDelivery.objects.create(
                endpoint=endpoint,
                event_type="ping",
                payload={"type": "ping", "created": timezone.now().isoformat(), "data": {}},
                next_attempt_at=timezone.now(),
            )
        webhooks.deliver(WebhookDelivery.objects.filter(pk=delivery.pk))
        delivery.refresh_from_db()
        if delivery.status == WebhookDelivery.Status.DELIVERED:
            messages.success(request, "Test event delivered.")
        else:
            messages.error(request, f"Test event not delivered: {delivery.last_error}")
    return redirect(url("integration_webhook", membership, pk))


@require_POST
@workspace_access(admin=True)
def delivery_resend(request, membership, pk):
    delivery = get_object_or_404(
        WebhookDelivery, pk=pk, endpoint__workspace=membership.workspace
    )
    webhooks.resend(delivery)
    webhooks.deliver(WebhookDelivery.objects.filter(pk=pk))
    messages.success(request, "Delivery queued again.")
    return redirect(url("integration_webhook", membership, delivery.endpoint_id))


@workspace_access(admin=True)
def connector_list(request, membership):
    return render(
        request,
        "integrations/connectors.html",
        page(
            membership,
            "integration_connectors",
            connectors=Connector.objects.filter(workspace=membership.workspace),
            linked=sync.linked_customers(membership.workspace).count(),
        ),
    )


@workspace_access(admin=True)
def connector_edit(request, membership, pk=None):
    instance = pk and get_object_or_404(Connector, pk=pk, workspace=membership.workspace)
    form = ConnectorForm(request.POST or None, instance=instance or None)
    if request.method == "POST":
        blocked = channel_off(request, membership)
        if blocked:
            return blocked
        if form.is_valid():
            with transaction.atomic():
                connector = form.save(commit=False)
                connector.workspace = membership.workspace
                connector.created_by_id = connector.created_by_id or request.user.pk
                form.save()
                log(membership.workspace, request.user, "Website sync saved", connector.name)
            messages.success(request, "Connection saved. Fetch a preview to see what would change.")
            return redirect(url("integration_connector", membership, connector.pk))
    return render(
        request,
        "core/form.html",
        {
            **context(membership),
            "form": form,
            "title": "Edit website sync" if pk else "Connect your website",
            "description": "Mandali fetches your customer list from your own API, shows a preview, and changes nothing until you apply it.",
            "submit": "Save connection",
            "cancel_url": url("integration_connector", membership, pk) if pk else url("integration_connectors", membership),
            "facts": [
                ("Records", "Customers, up to 500 per fetch"),
                ("Matching", "By your customer ID only"),
                ("Your token", "Used for one fetch, never stored"),
            ],
        },
    )


@workspace_access(admin=True)
def connector_detail(request, membership, pk):
    connector = get_object_or_404(Connector, pk=pk, workspace=membership.workspace)
    form = FetchForm(request.POST or None)
    if request.method == "POST":
        blocked = channel_off(request, membership)
        if blocked:
            return blocked
        if form.is_valid():
            run = sync.fetch_preview(connector, request.user, form.cleaned_data["token"])
            if run.status == SyncRun.Status.FAILED:
                messages.error(request, f"Fetch failed: {run.error} Nothing was changed.")
                return redirect(url("integration_connector", membership, pk))
            return redirect(url("integration_run", membership, run.pk))
    return render(
        request,
        "integrations/connector_detail.html",
        page(
            membership,
            "integration_connectors",
            connector=connector,
            form=form,
            runs=connector.runs.select_related("created_by")[:20],
            mapping=[(label, connector.mapping.get(field, "")) for field, label, _ in Connector.FIELDS],
        ),
    )


@workspace_access(admin=True)
def run_detail(request, membership, pk):
    run = get_object_or_404(
        SyncRun.objects.select_related("connector"), pk=pk, connector__workspace=membership.workspace
    )
    return render(
        request,
        "integrations/run_detail.html",
        page(membership, "integration_connectors", run=run, counts=run.counts()),
    )


@require_POST
@workspace_access(admin=True)
def run_action(request, membership, pk):
    run = get_object_or_404(
        SyncRun.objects.select_related("connector__workspace"), pk=pk, connector__workspace=membership.workspace
    )
    if request.POST.get("action") == "discard":
        SyncRun.objects.filter(pk=pk, status=SyncRun.Status.PREVIEW).update(status=SyncRun.Status.DISCARDED)
        messages.success(request, "Preview discarded. Nothing was changed.")
    else:
        blocked = channel_off(request, membership)
        if blocked:
            return blocked
        results = sync.apply_run(run, request.user)
        if results is None:
            messages.error(request, "This preview was already applied or discarded.")
        else:
            messages.success(
                request,
                f"Sync applied: {results['create']} added, {results['update']} updated, {results['unchanged']} unchanged, {results['error']} skipped.",
            )
    return redirect(url("integration_run", membership, pk))
