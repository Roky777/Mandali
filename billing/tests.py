from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import Activity, Membership, User, Workspace
from . import services
from .models import (
    Customer,
    Discount,
    Invoice,
    InvoiceLine,
    Payment,
    Plan,
    Product,
    ProductVariant,
    QuotationTemplate,
    Subscription,
    Tax,
    TemplateLine,
)
from .services import BillingError, calculate
from .templatetags.billing import inr

PASSWORD = "Local-test!42Safe"
D = Decimal


class BillingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner@example.com", PASSWORD, first_name="Owner")
        cls.staff = User.objects.create_user("staff@example.com", PASSWORD, first_name="Staff")
        cls.outsider = User.objects.create_user("other@example.com", PASSWORD)
        cls.business = Workspace.objects.create(
            name="Example Studio",
            legal_name="Example Studio LLP",
            billing_email="accounts@example.com",
            address="Example address",
            gstin="27ABCDE1234F1Z5",
        )
        cls.other = Workspace.objects.create(
            name="Other Business",
            legal_name="Other LLP",
            billing_email="other@example.com",
            address="Another address",
        )
        Membership.objects.create(workspace=cls.business, user=cls.owner, role="admin")
        Membership.objects.create(workspace=cls.business, user=cls.staff, role="staff")
        Membership.objects.create(workspace=cls.other, user=cls.outsider, role="admin")
        cls.gst = Tax.objects.create(workspace=cls.business, name="GST 18%", rate=D("18"))
        cls.product = Product.objects.create(
            workspace=cls.business, name="Design retainer", sales_price=D("1000"), tax=cls.gst
        )
        cls.untaxed = Product.objects.create(
            workspace=cls.business, name="Workshop seat", sales_price=D("250")
        )
        cls.plan = Plan.objects.create(
            workspace=cls.business, name="Studio", price=D("999"), tax=cls.gst, min_quantity=2
        )
        cls.customer = Customer.objects.create(
            workspace=cls.business, name="Asha Traders", email="asha@example.com"
        )
        cls.other_customer = Customer.objects.create(workspace=cls.other, name="Private Co")
        cls.other_product = Product.objects.create(
            workspace=cls.other, name="Private product", sales_price=D("5")
        )

    def url(self, name, *args, workspace=None):
        return reverse(name, args=[(workspace or self.business).pk, *args])

    def draft(self, *items, discount=None, customer=None):
        invoice = Invoice.objects.create(
            workspace=self.business,
            customer=customer or self.customer,
            discount=discount,
            created_by=self.owner,
        )
        for item, quantity in items:
            services.add_invoice_line(invoice, item, quantity)
        invoice.refresh_from_db()
        return invoice

    def issued(self, *items, **kwargs):
        invoice = self.draft(*(items or [({"product": self.product}, 1)]), **kwargs)
        services.issue_invoice(invoice, self.owner)
        invoice.refresh_from_db()
        return invoice

    def test_staff_can_quote_invoice_and_record_a_partial_payment(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            self.url("customer_create"), {"name": "Ravi Kumar", "email": "RAVI@example.com"}
        )
        customer = Customer.objects.get(workspace=self.business, email="ravi@example.com")
        self.assertRedirects(response, self.url("customer_detail", customer.pk))

        today = timezone.localdate()
        self.client.post(
            self.url("subscription_create"),
            {
                "customer": customer.pk,
                "plan": self.plan.pk,
                "valid_until": today + timedelta(days=10),
                "start_date": today,
                "payment_terms_days": 7,
            },
        )
        subscription = Subscription.objects.get(customer=customer)
        self.assertEqual(subscription.number, "SUB-00001")
        self.client.post(
            self.url("subscription_line_add", subscription.pk),
            {"item": f"plan:{self.plan.pk}", "quantity": 2},
        )
        for action in ["send", "confirm", "activate"]:
            self.client.post(self.url("subscription_action", subscription.pk), {"action": action})
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertEqual(subscription.total, D("2357.64"))

        response = self.client.post(
            self.url("subscription_action", subscription.pk), {"action": "invoice"}
        )
        invoice = Invoice.objects.get(subscription=subscription)
        self.assertRedirects(response, self.url("invoice_detail", invoice.pk))
        self.client.post(self.url("invoice_issue", invoice.pk))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.ISSUED)
        self.assertEqual(invoice.number, "INV-00001")
        self.assertEqual(invoice.due_date, today + timedelta(days=7))
        self.assertEqual(invoice.customer_name, "Ravi Kumar")
        self.assertEqual(invoice.business_gstin, "27ABCDE1234F1Z5")

        self.client.post(
            self.url("payment_record", invoice.pk),
            {"amount": "1000.00", "method": "upi", "received_on": today, "reference": "UTR1"},
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, D("1000.00"))
        self.assertEqual(invoice.balance, D("1357.64"))
        self.assertEqual(invoice.status, Invoice.Status.ISSUED)
        page = self.client.get(self.url("invoice_detail", invoice.pk))
        self.assertContains(page, "₹1,357.64")
        self.assertContains(page, "Partly paid")
        self.assertTrue(
            Activity.objects.filter(workspace=self.business, action="Payment recorded").exists()
        )

    def test_totals_are_calculated_on_the_server(self):
        self.client.force_login(self.staff)
        self.client.post(
            self.url("invoice_create"),
            {
                "customer": self.customer.pk,
                "payment_terms_days": 15,
                "total": "1.00",
                "amount_paid": "999",
                "status": "paid",
                "number": "FORGED",
            },
        )
        invoice = Invoice.objects.get(workspace=self.business)
        self.assertEqual((invoice.status, invoice.total, invoice.number), ("draft", 0, ""))
        self.client.post(
            self.url("invoice_line_add", invoice.pk),
            {"item": f"product:{self.product.pk}", "quantity": 3, "unit_price": "1"},
        )
        invoice.refresh_from_db()
        line = invoice.lines.get()
        self.assertEqual(line.unit_price, D("1000.00"))
        self.assertEqual((invoice.subtotal, invoice.tax_total, invoice.total), (D("3000"), D("540"), D("3540")))

    def test_catalog_price_changes_do_not_rewrite_existing_lines(self):
        invoice = self.issued()
        self.product.sales_price = D("5000")
        self.product.save()
        self.gst.rate = D("28")
        self.gst.save()
        line = invoice.lines.get()
        self.assertEqual((line.unit_price, line.tax_rate), (D("1000"), D("18")))
        self.product.sales_price = D("1000")
        self.product.save()
        self.gst.rate = D("18")
        self.gst.save()

    def test_variant_adjusts_price(self):
        variant = ProductVariant.objects.create(
            product=self.product, name="Priority", price_adjustment=D("250")
        )
        invoice = self.draft(({"variant": variant}, 2))
        line = invoice.lines.get()
        self.assertEqual(line.description, "Design retainer: Priority")
        self.assertEqual(line.subtotal, D("2500"))

    def test_percentage_discount_applies_before_tax(self):
        discount = Discount.objects.create(workspace=self.business, name="Launch", value=D("10"))
        invoice = self.issued(({"product": self.product}, 1), ({"product": self.untaxed}, 2), discount=discount)
        self.assertEqual(invoice.subtotal, D("1500"))
        self.assertEqual(invoice.discount_total, D("150"))
        self.assertEqual(invoice.tax_total, D("162"))
        self.assertEqual(invoice.total, D("1512"))
        self.assertEqual(invoice.discount_label, "Launch (10% off)")
        discount.refresh_from_db()
        self.assertEqual(discount.times_used, 1)

    def test_discount_limited_to_products_and_minimums(self):
        discount = Discount.objects.create(
            workspace=self.business,
            name="Seats",
            kind=Discount.Kind.FIXED,
            value=D("100"),
            min_quantity=3,
        )
        discount.products.add(self.untaxed)
        invoice = self.draft(({"product": self.product}, 5), ({"product": self.untaxed}, 2), discount=discount)
        self.assertEqual(invoice.discount_total, 0)
        with self.assertRaisesMessage(BillingError, "quantity of at least 3"):
            services.issue_invoice(invoice, self.owner)
        services.add_invoice_line(invoice, {"product": self.untaxed}, 1)
        services.issue_invoice(invoice, self.owner)
        invoice.refresh_from_db()
        self.assertEqual(invoice.discount_total, D("100"))
        self.assertEqual(invoice.lines.get(product=self.product).discount_amount, 0)

    def test_expired_and_future_discounts_are_rejected(self):
        today = timezone.localdate()
        for kwargs, message in [
            ({"ends_on": today - timedelta(days=1)}, "expired"),
            ({"starts_on": today + timedelta(days=1)}, "starts on"),
            ({"min_amount": D("5000")}, "at least ₹5000"),
        ]:
            discount = Discount.objects.create(workspace=self.business, name="Rule", value=D("5"), **kwargs)
            invoice = self.draft(({"product": self.product}, 1), discount=discount)
            with self.assertRaisesMessage(BillingError, message):
                services.issue_invoice(invoice, self.owner)
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, Invoice.Status.DRAFT)
            self.assertEqual(invoice.number, "")

    def test_discount_usage_limit_is_enforced_and_returned_on_cancel(self):
        discount = Discount.objects.create(workspace=self.business, name="Once", value=D("5"), usage_limit=1)
        first = self.issued(discount=discount)
        second = self.draft(({"product": self.product}, 1), discount=discount)
        with self.assertRaisesMessage(BillingError, "usage limit"):
            services.issue_invoice(second, self.owner)
        services.cancel_invoice(first, self.owner, "Raised in error")
        discount.refresh_from_db()
        self.assertEqual(discount.times_used, 0)
        services.issue_invoice(second, self.owner)
        second.refresh_from_db()
        self.assertEqual(second.status, Invoice.Status.ISSUED)

    def test_payments_cannot_exceed_the_balance(self):
        invoice = self.issued()
        today = timezone.localdate()
        with self.assertRaisesMessage(BillingError, "more than the outstanding balance"):
            services.record_payment(invoice, self.staff, D("1180.01"), "cash", today)
        services.record_payment(invoice, self.staff, D("180"), "cash", today)
        services.record_payment(invoice, self.staff, D("1000"), "bank", today)
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.balance), (Invoice.Status.PAID, 0))
        with self.assertRaises(BillingError):
            services.record_payment(invoice, self.staff, D("1"), "cash", today)

    def test_payment_form_rejects_zero_and_future_dates(self):
        invoice = self.issued()
        self.client.force_login(self.staff)
        tomorrow = timezone.localdate() + timedelta(days=1)
        for amount, day in [("0", timezone.localdate()), ("10", tomorrow)]:
            response = self.client.post(
                self.url("payment_record", invoice.pk),
                {"amount": amount, "method": "cash", "received_on": day},
            )
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Payment.objects.exists())

    def test_reversal_restores_balance_and_is_admin_only(self):
        invoice = self.issued()
        payment = services.record_payment(invoice, self.staff, invoice.total, "cash", timezone.localdate())
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.post(self.url("payment_reverse", payment.pk), {"reason": "x"}).status_code, 403
        )
        self.client.force_login(self.owner)
        self.client.post(self.url("payment_reverse", payment.pk), {"reason": "Bounced cheque"})
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.amount_paid), (Invoice.Status.ISSUED, 0))
        with self.assertRaisesMessage(BillingError, "already reversed"):
            services.reverse_payment(payment, self.owner, "again")

    def test_duplicate_payment_reference_is_flagged(self):
        first, second = self.issued(), self.issued()
        today = timezone.localdate()
        services.record_payment(first, self.staff, D("10"), "upi", today, reference="UTR77")
        self.client.force_login(self.staff)
        response = self.client.post(
            self.url("payment_record", second.pk),
            {"amount": "10", "method": "upi", "received_on": today, "reference": "utr77"},
            follow=True,
        )
        self.assertContains(response, "was also recorded on INV-00001")

    def test_issued_invoices_are_locked(self):
        invoice = self.issued()
        with self.assertRaisesMessage(BillingError, "can no longer be edited"):
            services.add_invoice_line(invoice, {"product": self.untaxed}, 1)
        with self.assertRaises(BillingError):
            services.issue_invoice(invoice, self.owner)
        self.client.force_login(self.owner)
        self.client.post(self.url("invoice_edit", invoice.pk), {"customer": self.customer.pk, "payment_terms_days": 1})
        self.client.post(self.url("invoice_delete", invoice.pk))
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.payment_terms_days), (Invoice.Status.ISSUED, 15))

    def test_cancellation_requires_no_payments_and_admin(self):
        invoice = self.issued()
        payment = services.record_payment(invoice, self.staff, D("1"), "cash", timezone.localdate())
        with self.assertRaisesMessage(BillingError, "Reverse payments first"):
            services.cancel_invoice(invoice, self.owner, "Wrong")
        services.reverse_payment(payment, self.owner, "Wrong")
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.post(self.url("invoice_cancel", invoice.pk), {"reason": "x"}).status_code, 403
        )
        self.client.force_login(self.owner)
        self.client.post(self.url("invoice_cancel", invoice.pk), {"reason": "Duplicate"})
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.cancel_reason), (Invoice.Status.CANCELLED, "Duplicate"))

    def test_invoice_numbers_are_sequential_per_workspace(self):
        numbers = [self.issued().number for _ in range(3)]
        self.assertEqual(numbers, ["INV-00001", "INV-00002", "INV-00003"])
        draft = self.draft(({"product": self.product}, 1))
        self.assertEqual(draft.number, "")
        other = Invoice.objects.create(workspace=self.other, customer=self.other_customer, created_by=self.outsider)
        services.add_invoice_line(other, {"product": self.other_product}, 1)
        services.issue_invoice(other, self.outsider)
        other.refresh_from_db()
        self.assertEqual(other.number, "INV-00001")

    def test_other_workspace_records_are_not_reachable(self):
        invoice = Invoice.objects.create(workspace=self.other, customer=self.other_customer, created_by=self.outsider)
        self.client.force_login(self.owner)
        for name, args in [
            ("customer_detail", [self.other_customer.pk]),
            ("invoice_detail", [invoice.pk]),
            ("invoice_print", [invoice.pk]),
            ("product_detail", [self.other_product.pk]),
        ]:
            self.assertEqual(self.client.get(self.url(name, *args)).status_code, 404)
        for name, args in [("invoice_issue", [invoice.pk]), ("payment_record", [invoice.pk]), ("invoice_line_add", [invoice.pk])]:
            self.assertEqual(self.client.post(self.url(name, *args), {}).status_code, 404)
        self.assertEqual(self.client.get(self.url("invoices", workspace=self.other)).status_code, 404)
        self.assertNotContains(self.client.get(self.url("customers")), "Private Co")

    def test_forms_reject_other_workspace_ids(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url("invoice_create"), {"customer": self.other_customer.pk, "payment_terms_days": 15}
        )
        self.assertContains(response, "Select a valid choice")
        invoice = self.draft()
        self.client.post(
            self.url("invoice_line_add", invoice.pk), {"item": f"product:{self.other_product.pk}", "quantity": 1}
        )
        self.assertFalse(invoice.lines.exists())
        foreign_line = InvoiceLine.objects.create(
            invoice=Invoice.objects.create(workspace=self.other, customer=self.other_customer, created_by=self.outsider),
            description="Private",
            unit_price=D("5"),
        )
        self.client.post(self.url("invoice_line_remove", invoice.pk, foreign_line.pk))
        self.assertTrue(InvoiceLine.objects.filter(pk=foreign_line.pk).exists())

    def test_staff_cannot_configure_the_catalog(self):
        self.client.force_login(self.staff)
        for kind in ["taxes", "discounts", "products", "plans", "templates"]:
            self.assertEqual(self.client.get(self.url("catalog", kind)).status_code, 200)
            self.assertEqual(self.client.post(self.url("catalog_create", kind), {"name": "x"}).status_code, 403)
        self.assertEqual(
            self.client.post(self.url("product_detail", self.product.pk), {"name": "x", "price_adjustment": "1"}).status_code,
            403,
        )
        self.assertEqual(self.client.get(self.url("catalog", "unknown")).status_code, 404)

    def test_admin_creates_catalog_entries(self):
        self.client.force_login(self.owner)
        self.client.post(self.url("catalog_create", "taxes"), {"name": "GST 5%", "rate": "5", "is_active": "on"})
        self.assertTrue(Tax.objects.filter(workspace=self.business, name="GST 5%").exists())
        response = self.client.post(
            self.url("catalog_create", "discounts"), {"name": "Too much", "kind": "percent", "value": "120"}
        )
        self.assertContains(response, "cannot exceed 100")
        response = self.client.post(
            self.url("product_detail", self.untaxed.pk), {"name": "Cheap", "price_adjustment": "-300"}
        )
        self.assertContains(response, "cannot be below zero")

    def test_plan_minimum_quantity_and_availability(self):
        self.client.force_login(self.staff)
        invoice = self.draft()
        self.client.post(self.url("invoice_line_add", invoice.pk), {"item": f"plan:{self.plan.pk}", "quantity": 1})
        self.assertFalse(invoice.lines.exists())
        self.plan.available_until = timezone.localdate() - timedelta(days=1)
        self.plan.save()
        self.client.post(self.url("invoice_line_add", invoice.pk), {"item": f"plan:{self.plan.pk}", "quantity": 2})
        self.assertFalse(invoice.lines.exists())

    def test_subscription_lifecycle_rules(self):
        today = timezone.localdate()
        subscription = Subscription.objects.create(
            workspace=self.business,
            number="SUB-1",
            customer=self.customer,
            plan=self.plan,
            valid_until=today,
            created_by=self.owner,
        )
        with self.assertRaisesMessage(BillingError, "Add at least one line"):
            services.transition(subscription, "send", self.owner)
        services.add_subscription_line(subscription, {"plan": self.plan}, 2)
        services.transition(subscription, "send", self.owner)
        subscription.refresh_from_db()
        with self.assertRaisesMessage(BillingError, "can no longer be edited"):
            services.add_subscription_line(subscription, {"product": self.product}, 1)
        Subscription.objects.filter(pk=subscription.pk).update(valid_until=today - timedelta(days=1))
        subscription.refresh_from_db()
        with self.assertRaisesMessage(BillingError, "expired"):
            services.transition(subscription, "confirm", self.owner)
        Subscription.objects.filter(pk=subscription.pk).update(valid_until=today)
        subscription.refresh_from_db()
        services.transition(subscription, "confirm", self.owner)
        subscription.refresh_from_db()
        with self.assertRaisesMessage(BillingError, "start date"):
            services.transition(subscription, "activate", self.owner)
        Subscription.objects.filter(pk=subscription.pk).update(start_date=today)
        subscription.refresh_from_db()
        services.transition(subscription, "activate", self.owner)
        subscription.refresh_from_db()
        with self.assertRaisesMessage(BillingError, "does not allow pausing"):
            services.transition(subscription, "pause", self.owner)
        with self.assertRaisesMessage(BillingError, "not available"):
            services.transition(subscription, "resume", self.owner)
        services.transition(subscription, "close", self.owner)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.CLOSED)

    def test_quotation_from_template_copies_lines_and_validity(self):
        template = QuotationTemplate.objects.create(
            workspace=self.business, name="Starter", plan=self.plan, validity_days=14
        )
        TemplateLine.objects.create(template=template, plan=self.plan, quantity=2)
        TemplateLine.objects.create(template=template, product=self.untaxed, quantity=1)
        self.client.force_login(self.staff)
        self.client.post(
            self.url("subscription_create"),
            {"customer": self.customer.pk, "template": template.pk, "payment_terms_days": 15},
        )
        subscription = Subscription.objects.get(workspace=self.business)
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.valid_until, timezone.localdate() + timedelta(days=14))
        self.assertEqual(subscription.lines.count(), 2)
        self.assertEqual(subscription.total, D("2607.64"))

    def test_customer_email_is_unique_per_workspace(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url("customer_create"), {"name": "Copy", "email": "ASHA@example.com"})
        self.assertContains(response, "Another customer in this business uses this email")
        Customer.objects.create(workspace=self.other, name="Same email elsewhere", email="asha@example.com")

    def test_lists_and_summary_render(self):
        invoice = self.issued()
        Invoice.objects.filter(pk=invoice.pk).update(due_date=timezone.localdate() - timedelta(days=1))
        self.client.force_login(self.staff)
        response = self.client.get(self.url("invoices") + "?status=overdue")
        self.assertContains(response, "INV-00001")
        self.assertContains(response, "Overdue")
        dashboard = self.client.get(reverse("dashboard", args=[self.business.pk]))
        self.assertContains(dashboard, "₹1,180.00")
        for name in ["customers", "subscriptions", "catalog_home"]:
            self.assertEqual(self.client.get(self.url(name)).status_code, 200)
        self.assertContains(self.client.get(self.url("invoice_print", invoice.pk)), "Invoice INV-00001")

    def test_rounding_allocates_the_exact_discount(self):
        discount = Discount.objects.create(
            workspace=self.business, name="Ten", kind=Discount.Kind.FIXED, value=D("10")
        )
        lines = [InvoiceLine(unit_price=D("3.33"), quantity=1, tax_rate=D("0")) for _ in range(3)]
        totals = calculate(lines, discount)
        self.assertEqual(totals["discount_total"], D("9.99"))
        self.assertEqual(sum(l.discount_amount for l in lines), D("9.99"))
        self.assertEqual(totals["total"], D("0.00"))

    def test_indian_rupee_formatting(self):
        self.assertEqual(inr(D("1234567.5")), "₹12,34,567.50")
        self.assertEqual(inr(D("999")), "₹999.00")
        self.assertEqual(inr(D("100000")), "₹1,00,000.00")
        self.assertEqual(inr(D("-1500")), "-₹1,500.00")
