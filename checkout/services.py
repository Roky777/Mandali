import hashlib
import json
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from billing import services as billing
from billing.models import (
    Customer,
    Invoice,
    InvoiceLine,
    Payment,
    Plan,
    Product,
    Tax,
    Source,
)
from core.models import Activity, Workspace
from .crypto import CheckoutError, cipher, decrypt, digest, encrypt
from .gateways import (
    GatewayError,
    GatewayRejected,
    InvalidSignature,
    adapter,
    identifier,
)
from .models import (
    CheckoutSession,
    GatewayAccount,
    GatewayEvent,
    PaymentAttempt,
    ShareLink,
)

PENDING = ["creating", "ready", "unknown", "authorized", "review"]
SESSION_SALT = "mandali.checkout.session.v1"
NONCE_SALT = "mandali.checkout.quote.v1"


def paise(value):
    return int(billing.money(value) * 100)


def lock_workspace(workspace_id):
    # A write first gives PostgreSQL row serialization and avoids SQLite's
    # deferred read-to-write transaction upgrade during duplicate submissions.
    Workspace.objects.filter(pk=workspace_id).update(name=F("name"))


def lock_invoice(invoice_id):
    Invoice.objects.filter(pk=invoice_id).update(updated_at=timezone.now())
    return Invoice.objects.get(pk=invoice_id)


def log(workspace_id, user, action, detail=""):
    Activity.objects.create(
        workspace_id=workspace_id,
        actor=user,
        is_system=user is None,
        action=action,
        detail=detail[:220],
    )


@transaction.atomic
def configure_gateway(workspace, user, data):
    payload = encrypt(
        json.dumps(
            {"key_secret": data["key_secret"], "webhook_secret": data["webhook_secret"]}
        )
    )
    lock_workspace(workspace.pk)
    GatewayAccount.objects.filter(workspace=workspace, is_active=True).update(
        is_active=False
    )
    account = GatewayAccount.objects.create(
        workspace=workspace,
        key_id=data["key_id"],
        secrets_ciphertext=payload,
        created_by=user,
    )
    log(workspace.pk, user, "Razorpay Test gateway configured")
    return account


@transaction.atomic
def create_link(workspace, user, data):
    cipher()
    lock_workspace(workspace.pk)
    target = [data.get(field) for field in ("invoice", "product", "plan")]
    if sum(bool(item) for item in target) != 1 or any(
        item.workspace_id != workspace.pk for item in target if item
    ):
        raise CheckoutError("Choose one item from this workspace.")
    if data.get("invoice"):
        invoice = Invoice.objects.get(pk=data["invoice"].pk)
        if (
            not invoice.is_sandbox
            or invoice.status != Invoice.Status.ISSUED
            or invoice.balance <= 0
        ):
            raise CheckoutError(
                "Issue a sandbox invoice with an outstanding balance first."
            )
    token = secrets.token_urlsafe(32)
    link = ShareLink.objects.create(
        workspace=workspace,
        created_by=user,
        title=data["title"],
        invoice=data.get("invoice"),
        product=data.get("product"),
        plan=data.get("plan"),
        quantity=data["quantity"],
        expires_at=timezone.now() + timedelta(days=data["valid_days"]),
        token_digest=digest(token),
        token_ciphertext=encrypt(token),
    )
    quote(link)
    log(workspace.pk, user, "Sandbox checkout link created", link.title)
    return link


def link_url(link):
    return settings.MANDALI_BASE_URL + reverse(
        "checkout_public", args=[decrypt(link.token_ciphertext)]
    )


def available(link):
    return link.revoked_at is None and link.expires_at > timezone.now()


def quote(link, locked=False):
    if not available(link):
        raise CheckoutError(
            "This payment link has expired or was revoked. Ask the business for a new link."
        )
    if link.invoice_id:
        invoice = Invoice.objects.get(
            pk=link.invoice_id, workspace_id=link.workspace_id
        )
        if not invoice.is_sandbox or invoice.currency != "INR":
            raise CheckoutError("This invoice is not available for sandbox checkout.")
        return {
            "description": invoice.label,
            "total": invoice.balance,
            "amount": paise(invoice.balance),
            "fingerprint": digest(f"{invoice.pk}:{invoice.status}:{invoice.balance}"),
            "invoice": invoice,
            "item": None,
        }
    model, pk = (Product, link.product_id) if link.product_id else (Plan, link.plan_id)
    queryset = model.objects.select_for_update() if locked else model.objects
    item = queryset.get(pk=pk, workspace_id=link.workspace_id)
    if not item.is_active or (
        link.plan_id
        and (
            not item.available_on(timezone.localdate())
            or link.quantity < item.min_quantity
        )
    ):
        raise CheckoutError(
            "This item is no longer available. Ask the business for an updated link."
        )
    if item.tax_id and locked:
        item.tax = Tax.objects.select_for_update().get(pk=item.tax_id)
    if item.tax_id and not item.tax.is_active:
        raise CheckoutError(
            "This item needs an updated tax configuration. Contact the business."
        )
    reference = {"plan": item} if link.plan_id else {"product": item}
    line = billing.build_line(InvoiceLine, reference, link.quantity)
    totals = billing.calculate([line])
    if totals["total"] > Decimal("9999999999.99"):
        raise CheckoutError("The checkout amount exceeds the supported invoice limit.")
    if totals["total"] <= 0:
        raise CheckoutError("The checkout amount must be above zero.")
    fingerprint = digest(
        f'{line.description}:{line.unit_price}:{line.tax_name}:{line.tax_rate}:{link.quantity}:{totals["total"]}'
    )
    return {
        "description": line.description,
        "total": totals["total"],
        "amount": paise(totals["total"]),
        "subtotal": totals["subtotal"],
        "tax": totals["tax_total"],
        "fingerprint": fingerprint,
        "item": reference,
        "line": line,
        "invoice": None,
    }


def nonce(link, current_quote):
    return signing.dumps(
        {
            "link": str(link.pk),
            "nonce": uuid.uuid4().hex,
            "quote": current_quote["fingerprint"],
        },
        salt=NONCE_SALT,
    )


@transaction.atomic
def create_session(link, buyer, signed_nonce):
    try:
        claim = signing.loads(signed_nonce, salt=NONCE_SALT, max_age=900)
    except signing.BadSignature:
        raise CheckoutError(
            "The checkout form expired. Refresh the link and try again."
        ) from None
    if claim.get("link") != str(link.pk):
        raise CheckoutError("This checkout form belongs to another link.")
    lock_workspace(link.workspace_id)
    prior = CheckoutSession.objects.filter(
        creation_digest=digest(signed_nonce), link=link
    ).first()
    if prior:
        return prior
    link = ShareLink.objects.select_for_update().get(pk=link.pk)
    current = quote(link, locked=True)
    if current["fingerprint"] != claim.get("quote"):
        raise CheckoutError(
            "The price or balance changed. Refresh the link and review the new amount."
        )
    if link.invoice_id:
        invoice = lock_invoice(link.invoice_id)
        if invoice.status != Invoice.Status.ISSUED or invoice.balance <= 0:
            raise CheckoutError("This invoice is no longer awaiting payment.")
    else:
        email = buyer["email"].strip().lower()
        customer = Customer.objects.filter(
            workspace_id=link.workspace_id, email__iexact=email
        ).first()
        if customer is None:
            try:
                with transaction.atomic():
                    customer = Customer.objects.create(
                        workspace_id=link.workspace_id,
                        name=buyer["name"],
                        email=email,
                        phone=buyer.get("phone", ""),
                        billing_address=buyer["address"],
                        source=Source.HOSTED,
                    )
            except IntegrityError:
                customer = Customer.objects.get(
                    workspace_id=link.workspace_id, email__iexact=email
                )
        invoice = Invoice.objects.create(
            workspace_id=link.workspace_id,
            customer=customer,
            created_by=link.created_by,
            source_channel="hosted",
            currency="INR",
            is_sandbox=True,
            payment_terms_days=0,
        )
        line = current["line"]
        line.invoice = invoice
        line.save()
        billing.issue_invoice(invoice, link.created_by)
        # A public email claim must never reveal or overwrite an existing
        # customer's stored identity/address. Only submitted details are shown.
        Invoice.objects.filter(pk=invoice.pk).update(
            customer_name=buyer["name"],
            customer_email=email,
            customer_address=buyer["address"],
            customer_gstin="",
        )
        invoice.refresh_from_db()
        if paise(invoice.total) != current["amount"]:
            raise CheckoutError(
                "The price changed. Refresh checkout before continuing."
            )
    session = CheckoutSession.objects.create(
        link=link,
        invoice=invoice,
        creation_digest=digest(signed_nonce),
        expires_at=timezone.now() + timedelta(hours=2),
    )
    log(link.workspace_id, None, "Sandbox checkout started", invoice.number)
    return session


def session_token(session):
    return signing.dumps(str(session.pk), salt=SESSION_SALT)


def session_url(session):
    return reverse("checkout_session", args=[session_token(session)])


def load_session(token):
    try:
        pk = signing.loads(token, salt=SESSION_SALT, max_age=7200)
        uuid.UUID(pk)
    except (signing.BadSignature, ValueError, TypeError):
        raise CheckoutError(
            "This checkout session has expired or is invalid. Open the original payment link again."
        ) from None
    session = (
        CheckoutSession.objects.select_related(
            "link__workspace", "invoice", "attempt__gateway", "attempt__payment"
        )
        .filter(pk=pk, expires_at__gt=timezone.now())
        .first()
    )
    if not session:
        raise CheckoutError(
            "This checkout session has expired. Open the original link again."
        )
    return session


def validate_order(attempt, order):
    order_id = identifier(order.get("id"), "order")
    if (
        type(order.get("amount")) is not int
        or order["amount"] != attempt.amount_paise
        or order.get("currency") != "INR"
        or order.get("receipt") != attempt.pk.hex
        or not isinstance(order.get("notes"), dict)
        or order["notes"].get("mandali_attempt") != str(attempt.pk)
    ):
        raise GatewayError("The gateway order does not match this checkout attempt.")
    return order_id


@transaction.atomic
def store_order(attempt, order):
    order_id = validate_order(attempt, order)
    lock_invoice(attempt.invoice_id)
    attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
    if attempt.provider_order_id and attempt.provider_order_id != order_id:
        raise GatewayError("This attempt already belongs to another gateway order.")
    attempt.provider_order_id = order_id
    if attempt.status in ("creating", "unknown", "rejected"):
        attempt.status = "ready"
        attempt.detail = "Gateway order created. No payment has been recorded."
    attempt.save(update_fields=["provider_order_id", "status", "detail", "updated_at"])
    return attempt


def begin(session):
    with transaction.atomic():
        invoice = lock_invoice(session.invoice_id)
        link = ShareLink.objects.get(pk=session.link_id)
        if (
            not available(link)
            or session.expires_at <= timezone.now()
            or not invoice.is_sandbox
            or invoice.status != Invoice.Status.ISSUED
            or invoice.balance <= 0
        ):
            raise CheckoutError("This checkout is no longer available for payment.")
        attempt = PaymentAttempt.objects.filter(
            invoice=invoice, status__in=PENDING
        ).first()
        created = attempt is None
        if created:
            gateway = GatewayAccount.objects.filter(
                workspace_id=invoice.workspace_id, is_active=True
            ).first()
            if gateway is None:
                raise CheckoutError(
                    "This business has not enabled sandbox payments yet."
                )
            adapter(gateway)  # Validate/decrypt before claiming a reservation.
            attempt = PaymentAttempt.objects.create(
                workspace_id=invoice.workspace_id,
                invoice=invoice,
                gateway=gateway,
                key_id=gateway.key_id,
                amount_paise=paise(invoice.balance),
            )
        CheckoutSession.objects.filter(pk=session.pk).update(attempt=attempt)
    if not created:
        return attempt
    try:
        order = adapter(attempt.gateway).create_order(attempt)
        return store_order(attempt, order)
    except GatewayRejected as error:
        PaymentAttempt.objects.filter(pk=attempt.pk, status="creating").update(
            status="rejected", detail=str(error)
        )
    except GatewayError as error:
        PaymentAttempt.objects.filter(pk=attempt.pk, status="creating").update(
            status="unknown", detail=str(error)
        )
    attempt.refresh_from_db()
    return attempt


@transaction.atomic
def apply_payment(attempt, entity):
    invoice = lock_invoice(attempt.invoice_id)
    attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
    payment_id = identifier(entity.get("id"), "pay")
    if (
        entity.get("order_id") != attempt.provider_order_id
        or type(entity.get("amount")) is not int
        or entity["amount"] != attempt.amount_paise
        or entity.get("currency") != "INR"
    ):
        raise GatewayError(
            "Payment evidence does not match the stored order, amount, and currency."
        )
    if attempt.payment_id:
        return attempt
    if entity.get("status") == "captured" and entity.get("captured") is True:
        attempt.observed_payment_id = payment_id
        already_claimed = (
            PaymentAttempt.objects.filter(
                workspace_id=attempt.workspace_id,
                key_id=attempt.key_id,
                provider_payment_id=payment_id,
            )
            .exclude(pk=attempt.pk)
            .exists()
        )
        if already_claimed:
            attempt.status = "review"
            attempt.detail = "This capture belongs to another recorded attempt. Staff reconciliation is required."
        elif (
            type(entity.get("amount_refunded", 0)) is not int
            or entity.get("amount_refunded", 0) != 0
        ):
            attempt.provider_payment_id = payment_id
            attempt.status = "review"
            attempt.detail = "The gateway reports a refund. Staff must reconcile it; no payment was added."
        elif (
            not invoice.is_sandbox
            or invoice.status != Invoice.Status.ISSUED
            or paise(invoice.balance) < attempt.amount_paise
        ):
            attempt.status = "review"
            attempt.detail = "Captured test payment conflicts with the invoice balance or status. Review external/manual payments before resolving."
        elif Payment.objects.filter(
            workspace_id=attempt.workspace_id,
            reference__iexact=payment_id,
            reversed_at=None,
        ).exists():
            attempt.status = "review"
            attempt.detail = "This provider reference is already recorded. Staff must reconcile it before applying another payment."
        else:
            attempt.provider_payment_id = payment_id
            # Claim the provider capture before touching the ledger. A unique
            # constraint deduplicates callback/webhook races and changed headers.
            attempt.save(update_fields=["provider_payment_id"])
            attempt.payment = billing.record_payment(
                invoice,
                None,
                Decimal(attempt.amount_paise) / 100,
                Payment.Method.RAZORPAY_TEST,
                timezone.localdate(),
                reference=payment_id,
                note="Verified Razorpay Test capture. No real money moved.",
            )
            attempt.status = "paid"
            attempt.detail = "Sandbox capture verified and recorded once."
    elif entity.get("status") == "authorized":
        attempt.status = "authorized"
        attempt.detail = (
            "Payment is authorized but not captured. No payment was recorded."
        )
    elif attempt.status not in ("review", "paid", "authorized"):
        attempt.detail = "No captured payment yet. A failed payment can be retried against the same gateway order."
    attempt.save(
        update_fields=[
            "provider_payment_id",
            "observed_payment_id",
            "payment",
            "status",
            "detail",
            "updated_at",
        ]
    )
    return attempt


def verify_callback(session, payment_id, returned_order, signature):
    attempt = PaymentAttempt.objects.select_related("gateway").get(
        pk=session.attempt_id
    )
    gateway = adapter(attempt.gateway)
    if returned_order != attempt.provider_order_id or not gateway.valid_callback(
        attempt.provider_order_id, payment_id, signature
    ):
        raise InvalidSignature(
            "Payment signature verification failed. No payment was recorded."
        )
    entity = gateway.fetch_payment(payment_id)
    if entity.get("id") != payment_id:
        raise GatewayError("The gateway returned another payment.")
    return apply_payment(attempt, entity)


def reconcile(attempt, order_id=""):
    gateway = adapter(attempt.gateway)
    if not attempt.provider_order_id:
        if not order_id:
            raise CheckoutError(
                "Find the order by its receipt in Razorpay Test, then enter its order ID. A new order will not be created automatically."
            )
        attempt = store_order(attempt, gateway.fetch_order(order_id))
    entities = gateway.order_payments(attempt.provider_order_id)
    captured = [entity for entity in entities if entity.get("status") == "captured"]
    authorized = [entity for entity in entities if entity.get("status") == "authorized"]
    if len(captured) > 1:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status="review",
            detail="Multiple captures reported for one order. Staff reconciliation is required.",
        )
        return PaymentAttempt.objects.get(pk=attempt.pk)
    for entity in captured or authorized[:1]:
        payment_id = identifier(entity.get("id"), "pay")
        return apply_payment(attempt, gateway.fetch_payment(payment_id))
    if not attempt.payment_id and attempt.status in ("ready", "authorized"):
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status="ready",
            detail="No captured or authorized payment found. The existing order can be retried.",
        )
    return PaymentAttempt.objects.get(pk=attempt.pk)


def process_webhook(account, body, signature, event_id):
    gateway = adapter(account)
    if not gateway.valid_webhook(body, signature):
        raise GatewayError("Invalid webhook signature.")
    try:
        payload = json.loads(body)
        event_type = payload["event"]
        if not isinstance(event_type, str) or len(event_type) > 80:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise CheckoutError("Invalid webhook payload.") from None
    body_hash = hashlib.sha256(body).hexdigest()
    event_id = event_id or body_hash
    if len(event_id) > 120:
        raise CheckoutError("Invalid event identifier.")
    prior = GatewayEvent.objects.filter(gateway=account, event_id=event_id).first()
    if prior:
        if prior.body_digest != body_hash:
            raise CheckoutError("Event identifier was reused with different content.")
        return "duplicate"
    if GatewayEvent.objects.filter(gateway=account, body_digest=body_hash).exists():
        return "duplicate"
    attempt = None
    entity = None
    if event_type in ("payment.captured", "payment.authorized", "payment.failed"):
        try:
            supplied = payload["payload"]["payment"]["entity"]
            payment_id = identifier(supplied["id"], "pay")
            order_id = identifier(supplied["order_id"], "order")
        except (KeyError, TypeError):
            raise CheckoutError("Invalid payment event.") from None
        attempt = PaymentAttempt.objects.filter(
            gateway=account, provider_order_id=order_id
        ).first()
        if attempt is None:
            order = gateway.fetch_order(order_id)
            notes = order.get("notes")
            try:
                attempt_id = uuid.UUID(notes["mandali_attempt"])
            except (KeyError, ValueError, TypeError):
                attempt_id = None
            candidate = (
                PaymentAttempt.objects.filter(pk=attempt_id, gateway=account).first()
                if attempt_id
                else None
            )
            if candidate:
                attempt = store_order(candidate, order)
        if attempt:
            entity = gateway.fetch_payment(payment_id)
            if entity.get("id") != payment_id or entity.get("order_id") != order_id:
                raise GatewayError(
                    "Webhook evidence belongs to another payment or order."
                )
    with transaction.atomic():
        # A concurrent retry can finish while provider verification is running.
        lock_workspace(account.workspace_id)
        if (
            GatewayEvent.objects.filter(gateway=account, event_id=event_id).exists()
            or GatewayEvent.objects.filter(
                gateway=account, body_digest=body_hash
            ).exists()
        ):
            return "duplicate"
        outcome = "ignored"
        if attempt and entity:
            result = apply_payment(attempt, entity)
            outcome = result.status
        GatewayEvent.objects.create(
            gateway=account,
            event_id=event_id,
            body_digest=body_hash,
            event_type=event_type,
            attempt=attempt,
            outcome=outcome,
        )
    return outcome
