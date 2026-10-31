from django.db import IntegrityError, transaction
from billing.forms import CustomerForm
from billing.models import Customer

CUSTOMER_FIELDS = ["name", "email", "phone", "billing_address", "gstin"]


class RecordError(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or {}


def field_errors(form):
    return {
        field: [error["message"] for error in errors]
        for field, errors in form.errors.get_json_data().items()
    }


def text(value, field):
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RecordError("Values must be text or numbers.", {field: ["Use text or a number."]})
    return str(value).strip()


def external_id_of(data):
    external_id = text(data.get("external_id"), "external_id")
    if not external_id or len(external_id) > 80:
        raise RecordError(
            "Each record needs an external_id of 1 to 80 characters.",
            {"external_id": ["Required, at most 80 characters."]},
        )
    return external_id


def upsert_customer(workspace, data, source):
    """Create or update the customer with this external ID.

    The connected system owns a linked customer's contact fields: a field it
    sends replaces Mandali's value, and a field it omits is kept. Matching is
    by external ID only; an email already used by another customer is an error,
    never a silent merge.
    """
    if not isinstance(data, dict):
        raise RecordError("A customer must be a JSON object.")
    external_id = external_id_of(data)
    existing = Customer.objects.filter(workspace=workspace, external_id=external_id).first()
    values = {
        field: text(data[field], field) if field in data else (getattr(existing, field) if existing else "")
        for field in CUSTOMER_FIELDS
    }
    form = CustomerForm(values, instance=existing, workspace=workspace)
    if not form.is_valid():
        raise RecordError("The customer is not valid.", field_errors(form))
    if existing and not form.has_changed():
        return existing, "unchanged"
    customer = form.save(commit=False)
    if not existing:
        customer.external_id = external_id
        customer.source = source
    try:
        with transaction.atomic():
            customer.save()
    except IntegrityError:
        raise RecordError("Another request changed this customer at the same time. Retry the request.")
    return customer, "update" if existing else "create"
