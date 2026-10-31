import io
import json
from datetime import timedelta

import segno
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe

from billing.models import Invoice
from core.security import rate_limit
from core.views import context, workspace_access
from . import services
from .crypto import CheckoutError, digest
from . import routing
from .forms import (
    BuyerForm,
    CashfreeForm,
    GatewayForm,
    LinkForm,
    OverrideForm,
    PolicyForm,
    ProfileForm,
    RecommendForm,
)
from .gateways import GatewayError, InvalidSignature, adapter
from .models import (
    CheckoutSession,
    GatewayAccount,
    GatewayEvent,
    GatewayProfile,
    InvoiceRoute,
    PaymentAttempt,
    Provider,
    RoutingDecision,
    RoutingPolicy,
    ShareLink,
)


def checkout_error(request, error, status=400):
    return render(
        request, "checkout/unavailable.html", {"explanation": str(error)}, status=status
    )


@workspace_access()
def links(request, membership):
    data = context(membership)
    data.update(
        {
            "links": ShareLink.objects.filter(workspace=membership.workspace)[:100],
            "gateways": GatewayAccount.objects.filter(
                workspace=membership.workspace, is_active=True
            ).order_by("provider"),
            "now": timezone.now(),
            "attempts": PaymentAttempt.objects.filter(
                workspace=membership.workspace
            ).select_related("invoice", "gateway", "routing")[:50],
        }
    )
    return render(request, "checkout/links.html", data)


CREDENTIAL_FORMS = {Provider.RAZORPAY: GatewayForm, Provider.CASHFREE: CashfreeForm}
WEBHOOK_ROUTES = {Provider.RAZORPAY: "checkout_webhook", Provider.CASHFREE: "checkout_webhook_cashfree"}


def provider_or_404(provider):
    if provider not in Provider.values:
        raise Http404("Unknown gateway.")
    return Provider(provider)


@workspace_access(admin=True)
def gateway_settings(request, membership, provider=Provider.RAZORPAY):
    provider = provider_or_404(provider)
    form = CREDENTIAL_FORMS[provider](request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.configure_gateway(
                membership.workspace, request.user, form.cleaned_data, provider
            )
        except CheckoutError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request,
                f"{provider.label} credentials saved. Configure the webhook before collecting test payments.",
            )
            return redirect(
                "checkout_gateway_provider", workspace_id=membership.workspace.pk, provider=provider
            )
    account = GatewayAccount.objects.filter(
        workspace=membership.workspace, provider=provider, is_active=True
    ).first()
    data = {
        **context(membership),
        "form": form,
        "gateway": account,
        "provider": provider,
        "providers": Provider.choices,
        "profile": GatewayProfile.objects.filter(workspace=membership.workspace, provider=provider).first(),
    }
    if account:
        data["webhook_url"] = settings.MANDALI_BASE_URL + reverse(
            WEBHOOK_ROUTES[provider], args=[account.pk]
        )
    return render(request, "checkout/gateway.html", data)


@workspace_access(admin=True)
def gateway_profile(request, membership, provider):
    provider = provider_or_404(provider)
    instance = GatewayProfile.objects.filter(workspace=membership.workspace, provider=provider).first()
    form = ProfileForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            profile = form.save(commit=False)
            profile.workspace = membership.workspace
            profile.provider = provider
            profile.updated_by = request.user
            profile.save()
            services.log(membership.workspace.pk, request.user, "Gateway account details updated", provider.label)
        messages.success(request, f"{provider.label} account details saved.")
        return redirect("checkout_routing", workspace_id=membership.workspace.pk)
    return render(
        request,
        "core/form.html",
        {
            **context(membership),
            "form": form,
            "title": f"{provider.label} account details",
            "description": "Describe this gateway account as your own contract and dashboard show it. Mandali uses these figures to explain recommendations and automatic routing, and never fills them in for you.",
            "submit": "Save details",
            "cancel_url": reverse("checkout_routing", args=[membership.workspace.pk]),
            "facts": [
                ("Unknown values", "Ranked after known ones, never guessed"),
                ("Success rates", "Measured from your own attempts"),
                ("Currency", "INR only"),
            ],
        },
    )


@require_POST
@workspace_access(admin=True)
def gateway_disable(request, membership):
    provider = request.POST.get("provider", "")
    with transaction.atomic():
        services.lock_workspace(membership.workspace.pk)
        accounts = GatewayAccount.objects.filter(workspace=membership.workspace, is_active=True)
        if provider in Provider.values:
            accounts = accounts.filter(provider=provider)
        accounts.update(is_active=False)
        services.log(
            membership.workspace.pk,
            request.user,
            f"{Provider(provider).label if provider in Provider.values else 'Sandbox'} gateway disabled",
        )
    messages.success(
        request,
        "New gateway orders are disabled. Existing captures can still be verified.",
    )
    return redirect("checkout_links", workspace_id=membership.workspace.pk)


@workspace_access()
def link_create(request, membership):
    form = LinkForm(
        request.POST or None,
        workspace=membership.workspace,
        initial={"invoice": request.GET.get("invoice")},
    )
    if request.method == "POST" and form.is_valid():
        try:
            link = services.create_link(
                membership.workspace, request.user, form.cleaned_data
            )
        except CheckoutError as error:
            form.add_error(None, str(error))
        else:
            return redirect(
                "checkout_link_detail", workspace_id=membership.workspace.pk, pk=link.pk
            )
    return render(
        request,
        "core/form.html",
        {
            **context(membership),
            "form": form,
            "title": "Create a sandbox checkout link",
            "description": "Share one invoice, or reuse a product or plan link for separate customers. All payments here are test payments.",
            "submit": "Create link",
        },
    )


@workspace_access()
def link_detail(request, membership, pk):
    link = get_object_or_404(ShareLink, pk=pk, workspace=membership.workspace)
    try:
        url = services.link_url(link)
    except CheckoutError as error:
        return checkout_error(request, error, 503)
    return render(
        request,
        "checkout/link_detail.html",
        {
            **context(membership),
            "link": link,
            "share_url": url,
            "active": services.available(link),
        },
    )


@require_POST
@workspace_access()
def link_revoke(request, membership, pk):
    with transaction.atomic():
        services.lock_workspace(membership.workspace.pk)
        link = get_object_or_404(ShareLink, pk=pk, workspace=membership.workspace)
        if link.revoked_at is None:
            link.revoked_at = timezone.now()
            link.save(update_fields=["revoked_at"])
            services.log(
                membership.workspace.pk,
                request.user,
                "Sandbox checkout link revoked",
                link.title,
            )
    messages.success(
        request,
        "Link revoked. Payment evidence from already-started orders can still be reconciled.",
    )
    return redirect("checkout_link_detail", workspace_id=membership.workspace.pk, pk=pk)


@require_safe
@workspace_access()
def link_qr(request, membership, pk):
    link = get_object_or_404(ShareLink, pk=pk, workspace=membership.workspace)
    if not services.available(link):
        return checkout_error(
            request, CheckoutError("This link is no longer available."), 410
        )
    try:
        url = services.link_url(link)
    except CheckoutError as error:
        return checkout_error(request, error, 503)
    buffer = io.BytesIO()
    segno.make(url, micro=False, error="m").save(buffer, kind="svg", scale=5, border=4)
    response = HttpResponse(buffer.getvalue(), content_type="image/svg+xml")
    response["Content-Disposition"] = (
        f'attachment; filename="mandali-sandbox-{link.pk}.svg"'
    )
    return response


@require_POST
@workspace_access(admin=True)
@rate_limit("gateway-reconcile", limit=30, seconds=900)
def attempt_reconcile(request, membership, pk):
    attempt = get_object_or_404(PaymentAttempt, pk=pk, workspace=membership.workspace)
    try:
        result = services.reconcile(attempt, request.POST.get("order_id", "")[:100])
        services.log(
            membership.workspace.pk,
            request.user,
            "Sandbox payment reconciled",
            str(attempt.pk),
        )
        messages.success(request, result.detail or result.get_status_display())
    except (CheckoutError, IntegrityError) as error:
        messages.error(
            request,
            (
                str(error)
                if isinstance(error, CheckoutError)
                else "Payment evidence conflicts with another attempt. Staff review is required."
            ),
        )
    return redirect("checkout_links", workspace_id=membership.workspace.pk)


@rate_limit("public-checkout", limit=10, seconds=900)
def public_link(request, token):
    link = get_object_or_404(
        ShareLink.objects.select_related("workspace"), token_digest=digest(token)
    )
    try:
        current = services.quote(link)
    except CheckoutError as error:
        return checkout_error(request, error, 410)
    if current["invoice"] and current["invoice"].status not in (
        Invoice.Status.ISSUED,
        Invoice.Status.PAID,
    ):
        return checkout_error(
            request,
            CheckoutError("This invoice is no longer available for payment."),
            410,
        )
    form = BuyerForm(
        request.POST or None, initial={"nonce": services.nonce(link, current)}
    )
    if request.method == "POST":
        data = request.POST.copy()
        if link.invoice_id:
            invoice = current["invoice"]
            data.update(
                {
                    "name": invoice.customer_name,
                    "email": invoice.customer_email or "invoice@example.com",
                    "address": invoice.customer_address or "Invoice address",
                }
            )
            form = BuyerForm(data)
        if form.is_valid():
            try:
                session = services.create_session(
                    link, form.cleaned_data, form.cleaned_data["nonce"]
                )
            except CheckoutError as error:
                form.add_error(None, str(error))
            else:
                return redirect(services.session_url(session))
    return render(
        request,
        "checkout/public_link.html",
        {
            "link": link,
            "quote": current,
            "form": form,
            "settled": bool(current["invoice"] and current["invoice"].balance <= 0),
        },
    )


@require_safe
def session_view(request, token):
    try:
        session = services.load_session(token)
    except CheckoutError as error:
        return checkout_error(request, error, 410)
    invoice = session.invoice
    data = {
        "session": session,
        "invoice": invoice,
        "attempt": session.attempt,
        "token": token,
        "link_active": services.available(session.link),
        "lines": invoice.lines.all(),
    }
    attempt = session.attempt
    ready = (
        attempt
        and attempt.status == "ready"
        and attempt.gateway.is_active
        and data["link_active"]
        and invoice.status == Invoice.Status.ISSUED
        and services.paise(invoice.balance) == attempt.amount_paise
    )
    if ready and attempt.gateway.provider == Provider.CASHFREE and attempt.client_token:
        data["gateway_options"] = {
            "provider": "cashfree",
            "mode": "sandbox",
            "payment_session_id": attempt.client_token,
        }
        data["cashfree_checkout"] = True
        request.cashfree_checkout = True
    elif ready and attempt.gateway.provider == Provider.RAZORPAY:
        data["gateway_options"] = {
            "key": attempt.key_id,
            "amount": attempt.amount_paise,
            "currency": "INR",
            "name": invoice.business_name,
            "description": f"Sandbox payment for {invoice.number}",
            "order_id": attempt.provider_order_id,
            "prefill": {"name": invoice.customer_name, "email": invoice.customer_email},
        }
        data["razorpay_checkout"] = True
        request.razorpay_checkout = True
    return render(request, "checkout/session.html", data)


@require_POST
@rate_limit("checkout-begin", limit=10, seconds=900)
def session_begin(request, token):
    try:
        session = services.load_session(token)
        services.begin(session)
    except CheckoutError as error:
        return checkout_error(request, error, 409)
    return redirect(services.session_url(session))


@require_POST
@rate_limit("checkout-verify", limit=30, seconds=900)
def session_verify(request, token):
    try:
        session = services.load_session(token)
        if not session.attempt_id:
            raise CheckoutError("Start a payment before verifying it.")
        services.verify_callback(
            session,
            request.POST.get("razorpay_payment_id", "")[:100],
            request.POST.get("razorpay_order_id", "")[:100],
            request.POST.get("razorpay_signature", "")[:128],
        )
    except InvalidSignature as error:
        return checkout_error(request, error, 403)
    except GatewayError as error:
        return checkout_error(request, error, 503)
    except CheckoutError as error:
        return checkout_error(request, error, 409)
    except IntegrityError:
        return checkout_error(
            request,
            CheckoutError(
                "This payment requires staff review. No additional payment was applied."
            ),
            409,
        )
    return redirect(services.session_url(session))


@require_POST
@rate_limit("checkout-status", limit=30, seconds=900)
def session_recheck(request, token):
    try:
        session = services.load_session(token)
        if session.attempt_id and session.attempt.provider_order_id:
            services.reconcile(session.attempt)
    except CheckoutError as error:
        return checkout_error(request, error, 503)
    except IntegrityError:
        return checkout_error(
            request, CheckoutError("The payment needs staff reconciliation."), 409
        )
    return redirect(services.session_url(session))


def webhook_signature(account, request):
    """Return the provider's signature value and event ID from the headers."""
    if account.provider == Provider.CASHFREE:
        timestamp = request.headers.get("x-webhook-timestamp", "")[:20]
        return f"{timestamp}:{request.headers.get('x-webhook-signature', '')[:200]}", ""
    return (
        request.headers.get("X-Razorpay-Signature", ""),
        request.headers.get("X-Razorpay-Event-Id", ""),
    )


@csrf_exempt
@require_POST
def webhook(request, account_id, provider=Provider.RAZORPAY):
    account = get_object_or_404(GatewayAccount, pk=account_id, provider=provider)
    signature, event_id = webhook_signature(account, request)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > 262144:
            return JsonResponse({"error": "payload too large"}, status=413)
        body = request.body
        if len(body) > 262144:
            return JsonResponse({"error": "payload too large"}, status=413)
        result = services.process_webhook(account, body, signature, event_id)
    except GatewayError:
        # Invalid signatures must fail; valid events with a transient provider
        # failure need a retry. Do the signature check before network reads.
        try:
            valid = adapter(account).valid_webhook(request.body, signature)
        except CheckoutError:
            valid = False
        return JsonResponse(
            {"error": "verification unavailable" if valid else "invalid signature"},
            status=503 if valid else 400,
        )
    except CheckoutError:
        return JsonResponse({"error": "invalid event"}, status=400)
    except (IntegrityError, ValueError):
        return JsonResponse({"error": "event requires reconciliation"}, status=409)
    return JsonResponse({"status": result})


def routing_context(membership, recommend_form=None, policy_form=None, override_form=None):
    workspace = membership.workspace
    connected = set(
        GatewayAccount.objects.filter(workspace=workspace, is_active=True).values_list("provider", flat=True)
    )
    policy = routing.policy_for(workspace)
    found = routing.profiles(workspace)
    rates = routing.success_rates(workspace)
    gateways = [
        {
            "provider": value,
            "label": label,
            "connected": value in connected,
            "profile": found.get(value),
            "rate": rates.get(value),
            "is_default": bool(policy and policy.default_provider == value),
        }
        for value, label in Provider.choices
    ]
    recommendation = None
    if recommend_form is not None and recommend_form.is_bound and recommend_form.is_valid():
        recommendation = routing.recommend(workspace, recommend_form.cleaned_data)
    return {
        **context(membership),
        "policy": policy,
        "gateways": gateways,
        "connected": connected,
        "policy_form": policy_form or PolicyForm(instance=policy, connected=connected),
        "override_form": override_form or OverrideForm(workspace=workspace),
        "recommend_form": recommend_form or RecommendForm(),
        "recommendation": recommendation,
        "overrides": InvoiceRoute.objects.filter(invoice__workspace=workspace).select_related("invoice", "set_by"),
        "decisions": RoutingDecision.objects.filter(workspace=workspace).select_related("invoice", "attempt")[:50],
        "min_sample": routing.SUCCESS_MIN_SAMPLE,
        "providers": Provider.choices,
        "provider": "",
        "method_labels": dict(GatewayProfile.METHODS),
    }


@workspace_access(admin=True)
def routing_overview(request, membership):
    recommend_form = RecommendForm(request.GET) if "priority" in request.GET else None
    return render(request, "checkout/routing.html", routing_context(membership, recommend_form=recommend_form))


@require_POST
@workspace_access(admin=True)
def routing_policy(request, membership):
    workspace = membership.workspace
    policy = routing.policy_for(workspace)
    connected = set(
        GatewayAccount.objects.filter(workspace=workspace, is_active=True).values_list("provider", flat=True)
    )
    form = PolicyForm(request.POST, instance=policy, connected=connected)
    if not form.is_valid():
        return render(
            request, "checkout/routing.html", routing_context(membership, policy_form=form), status=400
        )
    with transaction.atomic():
        saved = form.save(commit=False)
        saved.workspace = workspace
        saved.updated_by = request.user
        saved.save()
        services.log(
            workspace.pk,
            request.user,
            "Gateway routing policy updated",
            f"{saved.get_mode_display()}; default {saved.get_default_provider_display() or 'none'}; prefer {saved.get_priority_display().lower()}",
        )
    messages.success(request, "Routing policy saved. It applies to payments prepared from now on.")
    return redirect("checkout_routing", workspace_id=workspace.pk)


@require_POST
@workspace_access(admin=True)
def routing_override(request, membership):
    form = OverrideForm(request.POST, workspace=membership.workspace)
    if not form.is_valid():
        return render(
            request, "checkout/routing.html", routing_context(membership, override_form=form), status=400
        )
    invoice = form.cleaned_data["invoice"]
    with transaction.atomic():
        InvoiceRoute.objects.update_or_create(
            invoice=invoice,
            defaults={
                "provider": form.cleaned_data["provider"],
                "reason": form.cleaned_data["reason"],
                "set_by": request.user,
            },
        )
        services.log(
            membership.workspace.pk,
            request.user,
            "Gateway override set",
            f"{invoice.number}: {Provider(form.cleaned_data['provider']).label}, {form.cleaned_data['reason']}",
        )
    messages.success(
        request,
        "Override saved. It applies to the next payment prepared for this invoice; an order already started keeps its gateway.",
    )
    return redirect("checkout_routing", workspace_id=membership.workspace.pk)


@require_POST
@workspace_access(admin=True)
def routing_override_remove(request, membership, pk):
    with transaction.atomic():
        route = get_object_or_404(InvoiceRoute, pk=pk, invoice__workspace=membership.workspace)
        services.log(membership.workspace.pk, request.user, "Gateway override removed", route.invoice.number)
        route.delete()
    messages.success(request, "Override removed. The routing policy applies again.")
    return redirect("checkout_routing", workspace_id=membership.workspace.pk)

