import json
from django.db import transaction
from django.utils import timezone
from billing.models import Customer, Source
from billing.services import log
from .models import SyncRun
from .outbound import OutboundError, request
from .records import CUSTOMER_FIELDS, RecordError, upsert_customer

MAX_RECORDS = 500


def resolve(value, path):
    """Follow a dotted path such as data.customers or address.0.line1."""
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def map_record(connector, record):
    return {
        field: resolve(record, connector.mapping[field])
        for field in ["external_id", *CUSTOMER_FIELDS]
        if connector.mapping.get(field)
    }


def plan_item(workspace, data, seen_ids, seen_emails):
    """Decide what applying one mapped record would do, without saving it."""
    item = {"external_id": str(data.get("external_id") or "")[:80], "data": data, "errors": []}
    try:
        with transaction.atomic():
            customer, action = upsert_customer(workspace, data, Source.PULL)
            transaction.set_rollback(True)
        item["action"] = action
        item["name"] = customer.name
    except RecordError as problem:
        item["action"] = "error"
        item["errors"] = [str(problem), *[m for ms in problem.fields.values() for m in ms]]
        return item
    email = str(data.get("email") or "").strip().lower()
    if item["external_id"] in seen_ids:
        item.update(action="error", errors=["This customer ID appears more than once in the response."])
    elif email and email in seen_emails:
        item.update(action="error", errors=["Another record in this response uses the same email."])
    seen_ids.add(item["external_id"])
    if email:
        seen_emails.add(email)
    return item


def fetch_preview(connector, user, token=""):
    """Fetch the business's customer list and record what a sync would change."""
    headers = {"Accept": "application/json"}
    if token:
        # The token authorizes this one fetch and is never stored.
        headers["Authorization"] = f"Bearer {token}"
    run = SyncRun(connector=connector, created_by=user)
    try:
        status, body = request("GET", connector.url, headers)
        if status != 200:
            raise OutboundError(f"The website answered {status}. Check the URL and token.")
        try:
            payload = json.loads(body)
        except ValueError:
            raise OutboundError("The response is not valid JSON.")
        records = resolve(payload, connector.list_path) if connector.list_path else payload
        if not isinstance(records, list):
            raise OutboundError("The list path does not lead to a JSON list.")
        if len(records) > MAX_RECORDS:
            raise OutboundError(f"The response has more than {MAX_RECORDS} records. Page the list on your side.")
    except OutboundError as failure:
        run.status, run.error = SyncRun.Status.FAILED, str(failure)[:200]
        run.save()
        return run
    seen_ids, seen_emails = set(), set()
    run.items = [
        plan_item(connector.workspace, map_record(connector, r) if isinstance(r, dict) else {}, seen_ids, seen_emails)
        for r in records
    ]
    run.status = SyncRun.Status.PREVIEW
    run.save()
    return run


@transaction.atomic
def apply_run(run, user):
    # Claim the preview so a double click applies it once.
    if not SyncRun.objects.filter(pk=run.pk, status=SyncRun.Status.PREVIEW).update(
        status=SyncRun.Status.APPLIED, applied_at=timezone.now()
    ):
        return None
    workspace = run.connector.workspace
    results = {"create": 0, "update": 0, "unchanged": 0, "error": 0}
    items = []
    for item in run.items:
        if item["action"] != "error":
            # Records are validated again: the customer list may have changed since the preview.
            try:
                with transaction.atomic():
                    _, action = upsert_customer(workspace, item["data"], Source.PULL)
                item["action"] = action
            except RecordError as problem:
                item.update(action="error", errors=[str(problem), *[m for ms in problem.fields.values() for m in ms]])
        results[item["action"]] += 1
        items.append(item)
    SyncRun.objects.filter(pk=run.pk).update(items=items)
    log(
        workspace,
        user,
        "Website sync applied",
        f"{run.connector.name}: {results['create']} added, {results['update']} updated, {results['error']} skipped",
    )
    return results


def linked_customers(workspace):
    return Customer.objects.filter(workspace=workspace).exclude(external_id="")
