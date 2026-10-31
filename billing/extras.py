"""UPI QR codes, WhatsApp sharing, late-payer flags, and bank statement matching."""

import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode
import segno
from django.db.models import Count, F, Max, Q
from .models import Invoice

LATE_PAYER_THRESHOLD = 2
MAX_STATEMENT_ROWS = 500
INVOICE_NUMBER = re.compile(r"\b(INV-\d{3,})\b", re.I)


def upi_link(invoice):
    """A upi://pay intent for the outstanding balance, or None."""
    vpa = invoice.workspace.upi_id
    if not vpa or invoice.status != Invoice.Status.ISSUED or invoice.balance <= 0 or invoice.is_sandbox:
        return None
    params = {
        "pa": vpa,
        "pn": invoice.business_name or invoice.workspace.legal_name,
        "am": f"{invoice.balance:.2f}",
        "cu": "INR",
        "tn": f"Invoice {invoice.number}",
    }
    return "upi://pay?" + urlencode(params, quote_via=quote)


def upi_svg(invoice):
    link = upi_link(invoice)
    if not link:
        return None
    buffer = io.BytesIO()
    segno.make(link, error="m").save(buffer, kind="svg", scale=5, border=2)
    return buffer.getvalue()


def whatsapp_link(invoice, pay_url=None):
    lines = [
        f"Invoice {invoice.number} from {invoice.business_name}",
        f"Amount due: INR {invoice.balance:.2f}, due {invoice.due_date:%d %b %Y}" if invoice.due_date else "",
    ]
    if invoice.workspace.upi_id and invoice.balance > 0:
        lines.append(f"Pay by UPI to {invoice.workspace.upi_id} with note {invoice.number}")
    if pay_url:
        lines.append(f"Pay online: {pay_url}")
    digits = re.sub(r"\D", "", invoice.customer.phone or "")
    if len(digits) == 10:
        digits = "91" + digits
    text = quote("\n".join(line for line in lines if line))
    return f"https://wa.me/{digits}?text={text}" if digits else f"https://wa.me/?text={text}"


def late_payer_ids(workspace, today):
    """Customers with at least two invoices overdue now or paid after the due date."""
    late = (
        Invoice.objects.filter(workspace=workspace, is_sandbox=False)
        .annotate(last_paid=Max("payments__received_on", filter=Q(payments__reversed_at=None)))
        .filter(
            Q(status=Invoice.Status.ISSUED, due_date__lt=today)
            | Q(status=Invoice.Status.PAID, last_paid__gt=F("due_date"))
        )
        .values("customer_id")
        .annotate(n=Count("id", distinct=True))
        .filter(n__gte=LATE_PAYER_THRESHOLD)
    )
    return {row["customer_id"] for row in late}


def parse_amount(text):
    try:
        value = Decimal(re.sub(r"[^\d.\-]", "", text or ""))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def parse_date(text):
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d/%m/%y"):
        try:
            return datetime.strptime((text or "").strip(), pattern).date()
        except ValueError:
            continue
    return None


def read_statement(data):
    """Read credit rows from a bank CSV with date, amount, and description columns."""
    reader = csv.DictReader(io.StringIO(data))
    columns = {name.strip().lower(): name for name in (reader.fieldnames or [])}

    def pick(*names):
        return next((columns[n] for n in names if n in columns), None)

    date_col = pick("date", "txn date", "transaction date", "value date")
    amount_col = pick("credit", "deposit", "amount", "credit amount")
    text_col = pick("description", "narration", "details", "remarks", "particulars")
    ref_col = pick("reference", "ref no", "utr", "cheque no", "chq/ref no")
    if not (date_col and amount_col and text_col):
        raise ValueError("The file needs date, amount (or credit), and description (or narration) columns.")
    rows = []
    for index, row in enumerate(reader):
        if index >= MAX_STATEMENT_ROWS:
            break
        amount = parse_amount(row.get(amount_col))
        day = parse_date(row.get(date_col))
        if amount is None or day is None:
            continue
        text = (row.get(text_col) or "").strip()[:200]
        reference = (row.get(ref_col) or "").strip()[:80] if ref_col else ""
        rows.append({"date": day.isoformat(), "amount": str(amount), "text": text, "reference": reference or text[:80]})
    return rows


def match(workspace, rows):
    """Suggest an open invoice for each row: by invoice number, else by unique exact balance."""
    open_invoices = list(
        Invoice.objects.filter(workspace=workspace, status=Invoice.Status.ISSUED, is_sandbox=False).select_related("customer")
    )
    by_number = {i.number.upper(): i for i in open_invoices}
    for row in rows:
        amount = Decimal(row["amount"])
        found = INVOICE_NUMBER.search(row["text"])
        invoice = by_number.get(found.group(1).upper()) if found else None
        reason = "Invoice number in the description" if invoice else ""
        if invoice is None:
            same = [i for i in open_invoices if i.balance == amount]
            if len(same) == 1:
                invoice, reason = same[0], "Only open invoice with this exact balance"
        if invoice and amount > invoice.balance:
            invoice, reason = None, "Amount is more than the invoice balance"
        row["invoice_id"] = invoice.pk if invoice else None
        row["invoice"] = invoice
        row["reason"] = reason or "No confident match"
    return rows
