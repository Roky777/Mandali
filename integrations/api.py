import hashlib
import json
from datetime import date
from functools import wraps
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from billing import services
from billing.forms import LineForm
from billing.models import Customer, Invoice, Plan, Product, Source, Subscription
from billing.services import BillingError, log
from core.security import count_attempt, retry_after
from .keys import authenticate
from .models import IdempotencyRecord
from .records import RecordError, external_id_of, field_errors, text, upsert_customer
from .serializers import customer_data, invoice_detail, subscription_data

RATE_LIMIT, RATE_WINDOW = 300, 300
MAX_LINES = 100


class ApiError(Exception):
    def __init__(self, status, code, message, fields=None):
        super().__init__(message)
        self.status, self.code, self.message, self.fields = status, code, message, fields


def error_body(code, message, fields=None):
    body = {"error": {"code": code, "message": message}}
    if fields:
        body["error"]["fields"] = fields
    return body


def error(status, code, message, fields=None):
    return JsonResponse(error_body(code, message, fields), status=status)


def claim_idempotency(workspace, key, route, body):
    request_digest = hashlib.sha256(f"{route}\n".encode() + body).hexdigest()
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(
                workspace=workspace, key=key, route=route, request_digest=request_digest
            ), None
    except IntegrityError:
        record = IdempotencyRecord.objects.filter(workspace=workspace, key=key).first()
    if record is None or record.status_code is None and record.request_digest == request_digest:
        # Either still running or just released after a failure: retrying is safe.
        return None, error(409, "in_progress", "The original request is still being processed. Retry shortly.")
    if record.request_digest != request_digest:
        return None, error(422, "idempotency_mismatch", "This Idempotency-Key was already used with a different request.")
    replay = JsonResponse(record.response, status=record.status_code)
    replay["Idempotent-Replayed"] = "true"
    return None, replay


def endpoint(route, methods, natural_key=None):
    """Authenticate a workspace API key and run the handler as one transaction.

    POSTs are idempotent: by the Idempotency-Key header when sent, otherwise by
    the record's external ID where the route has one.
    """

    def decorate(handler):
        @csrf_exempt
        @wraps(handler)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = error(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
                response["Allow"] = ", ".join(methods)
                return response
            key = authenticate(request.headers.get("Authorization", ""))
            if key is None:
                return error(401, "unauthorized", "Send a valid API key as: Authorization: Bearer mdl_...")
            workspace = key.workspace
            if not workspace.use_api:
                return error(403, "channel_disabled", "Website / API integration is switched off in this business's settings.")
            if count_attempt("api", key.pk, RATE_WINDOW) > RATE_LIMIT:
                response = error(429, "rate_limited", "Too many requests for this API key.")
                response["Retry-After"] = retry_after(RATE_WINDOW)
                return response
            data, record = None, None
            if request.method == "POST":
                if request.content_type != "application/json":
                    return error(415, "unsupported_media_type", "Send JSON with Content-Type: application/json.")
                try:
                    data = json.loads(request.body)
                except (ValueError, UnicodeDecodeError):
                    return error(400, "invalid_json", "The request body is not valid JSON.")
                if not isinstance(data, dict):
                    return error(400, "invalid_json", "The request body must be a JSON object.")
                idempotency_key = request.headers.get("Idempotency-Key", "").strip()
                if len(idempotency_key) > 100 or not idempotency_key.isprintable():
                    return error(400, "invalid_idempotency_key", "Idempotency-Key must be printable and at most 100 characters.")
                if not idempotency_key and natural_key and isinstance(data.get("external_id"), str):
                    idempotency_key = f"{natural_key}:{data['external_id'].strip()}"
                if idempotency_key:
                    record, replay = claim_idempotency(workspace, idempotency_key, route, request.body)
                    if replay:
                        return replay
            status = 500
            try:
                with transaction.atomic():
                    status, body = handler(request, key, data, *args, **kwargs)
            except (ApiError, RecordError) as failure:
                if isinstance(failure, RecordError):
                    failure = validation(failure)
                status = failure.status
                body = error_body(failure.code, failure.message, failure.fields)
            finally:
                # Only successes are replayed; a rejected request can be
                # corrected and resent with the same key.
                if record and not 200 <= status < 300:
                    record.delete()
                    record = None
            if record:
                IdempotencyRecord.objects.filter(pk=record.pk).update(status_code=status, response=body)
            return JsonResponse(body, status=status)

        return wrapped

    return decorate


def validation(problem):
    if isinstance(problem, RecordError):
        return ApiError(422, "invalid_record", str(problem), problem.fields)
    return ApiError(422, "invalid_request", str(problem))


def resolve_customer(key, value):
    if isinstance(value, str):
        customer = Customer.objects.filter(workspace=key.workspace, external_id=value.strip()).first()
        if not customer:
            raise ApiError(422, "unknown_customer", f"No customer has external_id {value!r}. Send the customer object instead.")
        return customer
    try:
        return upsert_customer(key.workspace, value, Source.API)[0]
    except RecordError as problem:
        raise ApiError(422, "invalid_customer", str(problem), {"customer": problem.fields})


def resolve_lines(key, lines):
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise ApiError(422, "invalid_lines", f"Send 1 to {MAX_LINES} lines.")
    items = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict) or ("product" in line) == ("plan" in line):
            raise ApiError(422, "invalid_lines", f"Line {index + 1} needs exactly one of product or plan.")
        model, kind = (Product, "product") if "product" in line else (Plan, "plan")
        code = text(line.get(kind), kind)
        obj = model.objects.filter(workspace=key.workspace, code=code).first() if code else None
        if obj is None:
            raise ApiError(422, "unknown_item", f"Line {index + 1}: no {kind} has code {code!r}.")
        form = LineForm(
            {"item": f"{kind}:{obj.pk}", "quantity": line.get("quantity", 1)},
            workspace=key.workspace,
        )
        if not form.is_valid():
            raise ApiError(422, "invalid_lines", f"Line {index + 1} is not valid.", {f"lines.{index}": field_errors(form)})
        items.append((form.cleaned_data["item"], form.cleaned_data["quantity"]))
    return items


def terms(data):
    value = data.get("payment_terms_days", 15)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 365:
        raise ApiError(422, "invalid_request", "payment_terms_days must be a whole number from 0 to 365.")
    return value


def notes(data):
    value = text(data.get("notes"), "notes")
    if len(value) > 500:
        raise ApiError(422, "invalid_request", "notes must be at most 500 characters.")
    return value


@endpoint("ping", ["GET"])
def ping(request, key, data):
    return 200, {"workspace": key.workspace.name, "key": key.prefix}


@endpoint("customers", ["POST"])
def customers(request, key, data):
    try:
        customer, action = upsert_customer(key.workspace, data, Source.API)
    except RecordError as problem:
        raise validation(problem)
    if action != "unchanged":
        log(key.workspace, key.created_by, f"Customer {'added' if action == 'create' else 'updated'} via API", customer.name)
    return (201 if action == "create" else 200), {**customer_data(customer), "result": action}


@endpoint("customer", ["GET"])
def customer(request, key, data, external_id):
    found = Customer.objects.filter(workspace=key.workspace, external_id=external_id).first()
    if not found:
        raise ApiError(404, "not_found", "No customer has this external_id.")
    return 200, customer_data(found)


@endpoint("orders", ["POST"], natural_key="order")
def orders(request, key, data):
    try:
        external_id = external_id_of(data)
    except RecordError as problem:
        raise validation(problem)
    if Invoice.objects.filter(workspace=key.workspace, external_id=external_id).exists():
        raise ApiError(409, "duplicate_order", "An order with this external_id already exists with different details.")
    issue = data.get("issue", True)
    if not isinstance(issue, bool):
        raise ApiError(422, "invalid_request", "issue must be true or false.")
    customer = resolve_customer(key, data.get("customer"))
    items = resolve_lines(key, data.get("lines"))
    invoice = Invoice(
        workspace=key.workspace,
        customer=customer,
        external_id=external_id,
        source_channel=Source.API,
        payment_terms_days=terms(data),
        notes=notes(data),
        created_by=key.created_by,
    )
    try:
        with transaction.atomic():
            invoice.save()
    except IntegrityError:
        raise ApiError(409, "duplicate_order", "An order with this external_id already exists.")
    try:
        for item, quantity in items:
            services.add_invoice_line(invoice, item, quantity)
        if issue:
            services.issue_invoice(invoice, key.created_by)
    except BillingError as problem:
        raise ApiError(422, "billing_rejected", str(problem))
    invoice.refresh_from_db()
    log(key.workspace, key.created_by, "Order received via API", f"{external_id}: {invoice.label}")
    return 201, invoice_detail(invoice)


@endpoint("order", ["GET"])
def order(request, key, data, external_id):
    invoice = Invoice.objects.filter(workspace=key.workspace, external_id=external_id).first()
    if not invoice:
        raise ApiError(404, "not_found", "No order has this external_id.")
    return 200, invoice_detail(invoice)


def start_date(data):
    value = data.get("start_date")
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_request", "start_date must be a date like 2026-11-01.")


@endpoint("subscriptions", ["POST"], natural_key="subscription")
def subscriptions(request, key, data):
    try:
        external_id = external_id_of(data)
    except RecordError as problem:
        raise validation(problem)
    if Subscription.objects.filter(workspace=key.workspace, external_id=external_id).exists():
        raise ApiError(409, "duplicate_subscription", "A subscription with this external_id already exists with different details.")
    plan_code = text(data.get("plan"), "plan")
    plan = Plan.objects.filter(workspace=key.workspace, code=plan_code, is_active=True).first() if plan_code else None
    if plan is None:
        raise ApiError(422, "unknown_item", f"No available plan has code {plan_code!r}.")
    customer = resolve_customer(key, data.get("customer"))
    extra = data.get("lines") or []
    if not isinstance(extra, list):
        raise ApiError(422, "invalid_lines", "lines must be a list.")
    items = resolve_lines(key, [{"plan": plan_code, "quantity": data.get("quantity", plan.min_quantity)}, *extra])
    subscription = Subscription(
        workspace=key.workspace,
        number=services.next_number(key.workspace, "subscription", "SUB"),
        customer=customer,
        plan=plan,
        start_date=start_date(data),
        payment_terms_days=terms(data),
        notes=notes(data),
        external_id=external_id,
        source_channel=Source.API,
        created_by=key.created_by,
    )
    try:
        with transaction.atomic():
            subscription.save()
    except IntegrityError:
        raise ApiError(409, "duplicate_subscription", "A subscription with this external_id already exists.")
    for item, quantity in items:
        services.add_subscription_line(subscription, item, quantity)
    subscription.refresh_from_db()
    log(key.workspace, key.created_by, "Subscription received via API", f"{external_id}: {subscription.number}")
    return 201, subscription_data(subscription)


@endpoint("subscription", ["GET"])
def subscription(request, key, data, external_id):
    found = Subscription.objects.filter(workspace=key.workspace, external_id=external_id).select_related("plan").first()
    if not found:
        raise ApiError(404, "not_found", "No subscription has this external_id.")
    return 200, subscription_data(found)
