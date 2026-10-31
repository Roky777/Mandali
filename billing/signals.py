from django.dispatch import Signal

# Sent inside the billing transaction for invoice.issued, invoice.paid,
# invoice.cancelled, payment.recorded, and payment.reversed.
# Arguments: workspace, event, invoice, payment (or None).
billing_event = Signal()
