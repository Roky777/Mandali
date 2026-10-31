import io
import json
from datetime import timedelta

import segno
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
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
from .forms import BuyerForm, GatewayForm, LinkForm
from .gateways import GatewayError, InvalidSignature, adapter
from .models import (
    CheckoutSession,
    GatewayAccount,
    GatewayEvent,
    PaymentAttempt,
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
            "gateway": GatewayAccount.objects.filter(
                workspace=membership.workspace, is_active=True
            ).first(),
            "now": timezone.now(),
            "attempts": PaymentAttempt.objects.filter(
                workspace=membership.workspace
            ).select_related("invoice", "gateway")[:50],
        }
    )
    return render(request, "checkout/links.html", data)


@workspace_access(admin=True)
def gateway_settings(request, membership):
    form = GatewayForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.configure_gateway(
                membership.workspace, request.user, form.cleaned_data
            )
        except CheckoutError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request,
                "Razorpay Test credentials saved. Configure the webhook before collecting test payments.",
            )
            return redirect("checkout_gateway", workspace_id=membership.workspace.pk)
    account = GatewayAccount.objects.filter(
        workspace=membership.workspace, is_active=True
    ).first()
    data = {**context(membership), "form": form, "gateway": account}
    if account:
        data["webhook_url"] = settings.MANDALI_BASE_URL + reverse(
            "checkout_webhook", args=[account.pk]
        )
    return render(request, "checkout/gateway.html", data)


@require_POST
@workspace_access(admin=True)
def gateway_disable(request, membership):
    with transaction.atomic():
        services.lock_workspace(membership.workspace.pk)
        GatewayAccount.objects.filter(
            workspace=membership.workspace, is_active=True
        ).update(is_active=False)
        services.log(
            membership.workspace.pk, request.user, "Razorpay Test gateway disabled"
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
    if (
        attempt
        and attempt.status == "ready"
        and attempt.gateway.is_active
        and data["link_active"]
        and invoice.status == Invoice.Status.ISSUED
        and services.paise(invoice.balance) == attempt.amount_paise
    ):
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


@csrf_exempt
@require_POST
def webhook(request, account_id):
    account = get_object_or_404(GatewayAccount, pk=account_id)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > 262144:
            return JsonResponse({"error": "payload too large"}, status=413)
        body = request.body
        if len(body) > 262144:
            return JsonResponse({"error": "payload too large"}, status=413)
        result = services.process_webhook(
            account,
            body,
            request.headers.get("X-Razorpay-Signature", ""),
            request.headers.get("X-Razorpay-Event-Id", ""),
        )
    except GatewayError:
        # Invalid signatures must fail; valid events with a transient provider
        # failure need a retry. Do the signature check before network reads.
        try:
            valid = adapter(account).valid_webhook(
                request.body, request.headers.get("X-Razorpay-Signature", "")
            )
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
