"""JSON shapes shared by the API responses and webhook payloads.

Amounts are decimal strings in INR so no client parses money as a float.
"""


def money(value):
    return f"{value:.2f}"


def day(value):
    return value.isoformat() if value else None


def customer_data(customer):
    return {
        "id": customer.pk,
        "external_id": customer.external_id or None,
        "name": customer.name,
        "email": customer.email,
        "phone": customer.phone,
        "billing_address": customer.billing_address,
        "gstin": customer.gstin,
        "source": customer.source,
    }


def line_data(line):
    return {
        "description": line.description,
        "quantity": line.quantity,
        "unit_price": money(line.unit_price),
        "tax_name": line.tax_name,
        "tax_rate": money(line.tax_rate),
        "discount": money(line.discount_amount),
        "tax": money(line.tax_amount),
        "total": money(line.total),
    }


def invoice_data(invoice):
    return {
        "id": invoice.pk,
        "number": invoice.number or None,
        "external_id": invoice.external_id or None,
        "status": invoice.status,
        "source": invoice.source_channel,
        "is_sandbox": invoice.is_sandbox,
        "currency": invoice.currency,
        "customer_id": invoice.customer_id,
        "issue_date": day(invoice.issue_date),
        "due_date": day(invoice.due_date),
        "subtotal": money(invoice.subtotal),
        "discount_total": money(invoice.discount_total),
        "tax_total": money(invoice.tax_total),
        "total": money(invoice.total),
        "amount_paid": money(invoice.amount_paid),
        "credited": money(invoice.credited),
        "refunded": money(invoice.refunded),
        "balance": money(invoice.balance),
        "refund_due": money(invoice.refund_due),
    }


def invoice_detail(invoice):
    return {**invoice_data(invoice), "lines": [line_data(l) for l in invoice.lines.all()]}


def payment_data(payment):
    return {
        "id": payment.pk,
        "amount": money(payment.amount),
        "method": payment.method,
        "received_on": day(payment.received_on),
        "reference": payment.reference,
        "reversed": payment.reversed_at is not None,
    }


def subscription_data(subscription):
    return {
        "id": subscription.pk,
        "number": subscription.number,
        "external_id": subscription.external_id or None,
        "status": subscription.status,
        "customer_id": subscription.customer_id,
        "plan": subscription.plan.code if subscription.plan else None,
        "start_date": day(subscription.start_date),
        "total_per_period": money(subscription.total),
        "lines": [line_data(l) for l in subscription.lines.all()],
    }


def credit_note_data(note):
    return {
        "number": note.number,
        "amount": money(note.amount),
        "taxable_amount": money(note.taxable_amount),
        "tax_amount": money(note.tax_amount),
        "reason": note.reason,
    }


def refund_data(refund):
    return {
        "id": refund.pk,
        "amount": money(refund.amount),
        "method": refund.method,
        "refunded_on": day(refund.refunded_on),
        "reference": refund.reference,
    }

