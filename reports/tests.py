import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from billing import services
from billing.models import Customer, Invoice, Plan, Product, Subscription
from core.models import Activity, Membership, User, Workspace
from integrations.models import WebhookDelivery, WebhookEndpoint
from . import metrics

D = Decimal


class ReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner@example.com", "Local-test!42Safe", first_name="Owner")
        cls.staff = User.objects.create_user("staff@example.com", "Local-test!42Safe", first_name="Staff")
        cls.outsider = User.objects.create_user("other@example.com", "Local-test!42Safe")
        cls.business = Workspace.objects.create(name="Example Studio", legal_name="Example LLP", billing_email="a@example.com", address="x")
        cls.other = Workspace.objects.create(name="Other", legal_name="Other LLP", billing_email="o@example.com", address="y")
        Membership.objects.create(workspace=cls.business, user=cls.owner, role="admin")
        Membership.objects.create(workspace=cls.business, user=cls.staff, role="staff")
        Membership.objects.create(workspace=cls.other, user=cls.outsider, role="admin")
        cls.product = Product.objects.create(workspace=cls.business, name="Service", sales_price=D("1000"))
        cls.customer = Customer.objects.create(workspace=cls.business, name="=HYPERLINK(\"x\")")
        cls.today = timezone.localdate()

    def issued(self, sandbox=False, due_days_ago=None):
        invoice = Invoice.objects.create(workspace=self.business, customer=self.customer, created_by=self.owner, is_sandbox=sandbox)
        services.add_invoice_line(invoice, {"product": self.product}, 1)
        services.issue_invoice(invoice, self.owner)
        if due_days_ago is not None:
            Invoice.objects.filter(pk=invoice.pk).update(due_date=self.today - timedelta(days=due_days_ago))
        invoice.refresh_from_db()
        return invoice

    def report(self):
        return metrics.summary(self.business, self.today.replace(day=1), self.today, self.today)

    def test_figures_have_separate_meanings(self):
        paid = self.issued()
        services.record_payment(paid, self.owner, D("1000"), "bank", self.today)
        services.issue_credit_note(paid, self.owner, D("200"), "Partial service")
        services.record_refund(paid, self.owner, D("200"), "bank", self.today)
        open_invoice = self.issued()
        cancelled = self.issued()
        services.cancel_invoice(cancelled, self.owner, "Duplicate")
        reversed_invoice = self.issued()
        payment = services.record_payment(reversed_invoice, self.owner, D("300"), "cash", self.today)
        services.reverse_payment(payment, self.owner, "Bounced")
        self.issued(sandbox=True)
        report = self.report()
        self.assertEqual((report["invoiced"]["total"], report["invoiced"]["count"]), (D("3000"), 3))
        self.assertEqual(report["cancelled"]["total"], D("1000"))
        self.assertEqual(report["credited"]["total"], D("200"))
        self.assertEqual(report["collected"]["total"], D("1000"))
        self.assertEqual(report["refunded"]["total"], D("200"))
        self.assertEqual(report["net_collected"], D("800"))
        self.assertEqual(report["now"]["outstanding"], D("2000"))
        self.assertEqual(open_invoice.status, "issued")

    def test_overdue_aging_and_recurring_value(self):
        self.issued(due_days_ago=5)
        self.issued(due_days_ago=45)
        self.issued(due_days_ago=120)
        self.issued()
        buckets = self.report()["aging"]
        self.assertEqual([b["count"] for b in buckets], [1, 1, 0, 1])
        for period, total, expected in [("yearly", "1200", "100.00"), ("weekly", "120", "520.00"), ("monthly", "99", "99.00")]:
            plan = Plan.objects.create(workspace=self.business, name=period, period=period, price=D(total))
            Subscription.objects.create(
                workspace=self.business, number=f"S-{period}", customer=self.customer, plan=plan,
                status="active", total=D(total), created_by=self.owner,
            )
        recurring = metrics.recurring(self.business)
        self.assertEqual((recurring["monthly"], recurring["active"]), (D("719.00"), 3))

    def test_periods(self):
        self.assertEqual(metrics.period("this_fy", date(2026, 3, 15)), (date(2025, 4, 1), date(2026, 3, 15)))
        self.assertEqual(metrics.period("last_fy", date(2026, 10, 31)), (date(2025, 4, 1), date(2026, 3, 31)))
        self.assertEqual(metrics.period("last_month", date(2026, 1, 10)), (date(2025, 12, 1), date(2025, 12, 31)))
        for args in [(None, None), (date(2026, 2, 1), date(2026, 1, 1)), (date(2020, 1, 1), date(2026, 1, 1))]:
            with self.assertRaises(metrics.PeriodError):
                metrics.period("custom", self.today, *args)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("reports", args=[self.business.pk]) + "?period=custom&start=2026-02-01&end=2026-01-01")
        self.assertContains(response, "The end date must be on or after the start date.")

    def test_exports_are_admin_only_and_formula_safe(self):
        self.issued()
        url = reverse("reports_export", args=[self.business.pk, "invoices"]) + "?period=this_month"
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.owner)
        response = self.client.get(url)
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][0], "Number")
        self.assertEqual(rows[1][1], "'=HYPERLINK(\"x\")")
        self.assertEqual(rows[1][8], "1000.00")
        self.assertEqual(self.client.get(reverse("reports_export", args=[self.business.pk, "users"])).status_code, 404)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_needs_attention_lists_manual_work(self):
        self.issued(due_days_ago=3)
        refund_due = self.issued()
        services.record_payment(refund_due, self.owner, D("1000"), "bank", self.today)
        services.issue_credit_note(refund_due, self.owner, D("100"), "Late")
        endpoint = WebhookEndpoint.objects.create(workspace=self.business, url="https://x.example.com/h", secret="s", created_by=self.owner)
        WebhookDelivery.objects.create(endpoint=endpoint, event_type="invoice.paid", payload={}, status="failed", next_attempt_at=timezone.now(), last_error="Endpoint answered 500.")
        groups = {g["key"]: list(g["items"]) for g in metrics.attention(self.business, self.today)}
        self.assertEqual(len(groups["overdue"]), 1)
        self.assertEqual(groups["refunds"], [refund_due])
        self.assertEqual(len(groups["webhooks"]), 1)
        self.client.force_login(self.staff)
        page = self.client.get(reverse("reports_attention", args=[self.business.pk]))
        self.assertContains(page, "Refunds due")
        self.assertContains(page, "₹100.00 due back")

    def test_activity_history_filters_and_is_scoped(self):
        Activity.objects.create(workspace=self.business, actor=self.owner, action="Invoice issued", detail="INV-00099")
        Activity.objects.create(workspace=self.business, actor_label="Customer (portal)", action="Subscription closed")
        Activity.objects.create(workspace=self.other, actor=self.outsider, action="Private event")
        self.client.force_login(self.staff)
        url = reverse("reports_activity", args=[self.business.pk])
        page = self.client.get(url)
        self.assertNotContains(page, "Private event")
        self.assertContains(self.client.get(url + "?q=INV-00099"), "Invoice issued")
        self.assertNotContains(self.client.get(url + "?q=INV-00099"), "Subscription closed")
        customer_only = self.client.get(url + "?who=customer")
        self.assertContains(customer_only, "Customer (portal)")
        self.assertNotContains(customer_only, "Invoice issued")
        self.assertEqual(self.client.get(url + f"?who={self.outsider.pk}").status_code, 200)
