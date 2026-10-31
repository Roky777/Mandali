from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import Activity, Membership, User, Workspace
from integrations.models import WebhookDelivery, WebhookEndpoint
from . import services
from .forms import RefundForm
from .models import CreditNote, Customer, Invoice, Product, Refund, Tax
from .services import BillingError

D = Decimal
PASSWORD = "Local-test!42Safe"


class AdjustmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner@example.com", PASSWORD, first_name="Owner")
        cls.staff = User.objects.create_user("staff@example.com", PASSWORD)
        cls.business = Workspace.objects.create(
            name="Example Studio", legal_name="Example Studio LLP", billing_email="a@example.com", address="x"
        )
        Membership.objects.create(workspace=cls.business, user=cls.owner, role="admin")
        Membership.objects.create(workspace=cls.business, user=cls.staff, role="staff")
        gst = Tax.objects.create(workspace=cls.business, name="GST 18%", rate=D("18"))
        cls.product = Product.objects.create(workspace=cls.business, name="Retainer", sales_price=D("1000"), tax=gst)
        cls.customer = Customer.objects.create(workspace=cls.business, name="Asha")

    def issued(self, sandbox=False):
        invoice = Invoice.objects.create(
            workspace=self.business, customer=self.customer, created_by=self.owner, is_sandbox=sandbox
        )
        services.add_invoice_line(invoice, {"product": self.product}, 1)
        services.issue_invoice(invoice, self.owner)
        invoice.refresh_from_db()
        return invoice

    def pay(self, invoice, amount):
        services.record_payment(invoice, self.owner, D(amount), "bank", timezone.localdate())
        invoice.refresh_from_db()

    def credit(self, invoice, amount, reason="Session not delivered"):
        note = services.issue_credit_note(invoice, self.owner, D(amount), reason)
        invoice.refresh_from_db()
        return note

    def test_credit_reduces_what_is_owed_with_a_tax_split(self):
        invoice = self.issued()
        note = self.credit(invoice, "180")
        self.assertEqual((note.number, note.tax_amount, note.taxable_amount), ("CN-00001", D("27.46"), D("152.54")))
        self.assertEqual((invoice.total, invoice.credited, invoice.balance), (D("1180"), D("180"), D("1000")))
        with self.assertRaises(BillingError):
            services.record_payment(invoice, self.owner, D("1000.01"), "bank", timezone.localdate())
        self.pay(invoice, "1000")
        self.assertEqual((invoice.status, invoice.balance, invoice.refund_due), (Invoice.Status.PAID, 0, 0))
        self.assertEqual(self.credit(invoice, "1").number, "CN-00002")

    def test_full_credit_settles_an_unpaid_invoice(self):
        invoice = self.issued()
        self.credit(invoice, "1180")
        self.assertEqual((invoice.status, invoice.balance), (Invoice.Status.PAID, 0))
        with self.assertRaisesMessage(BillingError, "more than the invoice has left"):
            self.credit(invoice, "0.01")

    def test_credit_on_a_paid_invoice_leaves_a_refund_due_that_reconciles(self):
        invoice = self.issued()
        self.pay(invoice, "1180")
        with self.assertRaisesMessage(BillingError, "Issue a credit note first"):
            services.record_refund(invoice, self.owner, D("1"), "bank", timezone.localdate())
        self.credit(invoice, "500")
        self.assertEqual((invoice.balance, invoice.refund_due), (0, D("500")))
        with self.assertRaises(BillingError):
            services.record_refund(invoice, self.owner, D("500.01"), "bank", timezone.localdate())
        services.record_refund(invoice, self.owner, D("200"), "upi", timezone.localdate(), reference="UTR9")
        services.record_refund(invoice, self.owner, D("300"), "bank", timezone.localdate())
        invoice.refresh_from_db()
        self.assertEqual((invoice.refunded, invoice.refund_due, invoice.balance, invoice.status), (D("500"), 0, 0, "paid"))
        self.assertEqual(invoice.net_paid, invoice.amount_due)

    def test_partial_payment_then_credit_then_refund(self):
        invoice = self.issued()
        self.pay(invoice, "1000")
        self.credit(invoice, "400")
        self.assertEqual((invoice.status, invoice.refund_due), (Invoice.Status.PAID, D("220")))
        services.record_refund(invoice, self.owner, D("220"), "cash", timezone.localdate())
        invoice.refresh_from_db()
        self.assertEqual((invoice.balance, invoice.refund_due), (0, 0))

    def test_guards(self):
        draft = Invoice.objects.create(workspace=self.business, customer=self.customer, created_by=self.owner)
        with self.assertRaises(BillingError):
            services.issue_credit_note(draft, self.owner, D("1"), "x")
        with self.assertRaisesMessage(BillingError, "Sandbox"):
            services.issue_credit_note(self.issued(sandbox=True), self.owner, D("1"), "x")
        invoice = self.issued()
        self.credit(invoice, "10")
        with self.assertRaisesMessage(BillingError, "credit notes can be cancelled"):
            services.cancel_invoice(invoice, self.owner, "Mistake")
        paid = self.issued()
        self.pay(paid, "1180")
        self.credit(paid, "100")
        services.record_refund(paid, self.owner, D("100"), "bank", timezone.localdate())
        with self.assertRaisesMessage(BillingError, "already refunded"):
            services.reverse_payment(paid.payments.get(), self.owner, "Bounced")
        with self.assertRaises(BillingError):
            services.record_refund(paid, self.owner, D("1"), "rzp_test", timezone.localdate())

    def test_events_and_activity(self):
        WebhookEndpoint.objects.create(workspace=self.business, url="https://shop.example.com/h", secret="s", created_by=self.owner)
        invoice = self.issued()
        self.pay(invoice, "1180")
        self.credit(invoice, "100")
        services.record_refund(invoice, self.owner, D("100"), "bank", timezone.localdate())
        events = list(WebhookDelivery.objects.order_by("id").values_list("event_type", flat=True))
        self.assertEqual(events[-2:], ["invoice.credited", "refund.recorded"])
        payload = WebhookDelivery.objects.get(event_type="refund.recorded").payload["data"]
        self.assertEqual((payload["refund"]["amount"], payload["invoice"]["refunded"]), ("100.00", "100.00"))
        self.assertTrue(Activity.objects.filter(action="Credit note issued").exists())

    def test_screens_are_admin_only_and_documents_render(self):
        invoice = self.issued()
        self.pay(invoice, "1180")
        url = lambda name, *a: reverse(name, args=[self.business.pk, *a])
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(url("credit_note_create", invoice.pk), {"amount": "10", "reason": "x"}).status_code, 403)
        self.assertEqual(self.client.post(url("refund_create", invoice.pk), {}).status_code, 403)
        self.client.force_login(self.owner)
        self.client.post(url("credit_note_create", invoice.pk), {"amount": "100", "reason": "Late delivery"})
        note = CreditNote.objects.get()
        self.assertContains(self.client.get(url("invoice_detail", invoice.pk)), "Refund due to customer")
        self.client.post(
            url("refund_create", invoice.pk),
            {"amount": "100", "method": "bank", "refunded_on": timezone.localdate(), "reference": "", "note": ""},
        )
        self.assertEqual(Refund.objects.get().amount, D("100"))
        self.assertContains(self.client.get(url("credit_note_print", note.pk)), "Credit note CN-00001")
        receipt = self.client.get(url("payment_receipt", invoice.pk, invoice.payments.get().pk))
        self.assertContains(receipt, "Payment receipt")
        future = timezone.localdate() + timedelta(days=1)
        self.assertFalse(RefundForm({"amount": "1", "method": "bank", "refunded_on": future}).is_valid())
