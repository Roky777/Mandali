import re
from datetime import timedelta
from decimal import Decimal
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from billing import services
from billing.models import Customer, Invoice, Plan, Product, Subscription
from core.models import Activity, Membership, User, Workspace
from .models import PortalToken
from .services import SESSION_KEY, digest

D = Decimal


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", MANDALI_BASE_URL="http://testserver")
class PortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner@example.com", "Local-test!42Safe", first_name="Owner")
        cls.business = Workspace.objects.create(name="Example Studio", legal_name="Example LLP", billing_email="a@example.com", address="x")
        cls.other = Workspace.objects.create(name="Other Shop", legal_name="Other LLP", billing_email="o@example.com", address="y")
        Membership.objects.create(workspace=cls.business, user=cls.owner, role="admin")
        cls.product = Product.objects.create(workspace=cls.business, name="Service", sales_price=D("500"))
        cls.other_product = Product.objects.create(workspace=cls.other, name="Other", sales_price=D("50"))
        cls.asha = Customer.objects.create(workspace=cls.business, name="Asha", email="asha@example.com")
        cls.ravi = Customer.objects.create(workspace=cls.business, name="Ravi", email="ravi@example.com")
        cls.asha_elsewhere = Customer.objects.create(workspace=cls.other, name="Asha K", email="ASHA@example.com")
        cls.plan = Plan.objects.create(workspace=cls.business, name="Monthly", price=D("100"), can_pause=False, can_close=True)

    def issued(self, customer, product=None):
        invoice = Invoice.objects.create(workspace=customer.workspace, customer=customer, created_by=self.owner)
        services.add_invoice_line(invoice, {"product": product or self.product}, 1)
        services.issue_invoice(invoice, self.owner)
        invoice.refresh_from_db()
        return invoice

    def links(self):
        return [re.search(r"http://testserver(/portal/enter/[^/\s]+/)", m.body).group(1) for m in mail.outbox]

    def sign_in(self, customer):
        mail.outbox.clear()
        self.client.post(reverse("portal_request"), {"email": customer.email})
        path = next(p for p, m in zip(self.links(), mail.outbox) if customer.workspace.name in m.subject)
        self.client.post(path)

    def test_link_request_never_reveals_whether_an_email_is_known(self):
        unknown = self.client.post(reverse("portal_request"), {"email": "nobody@example.com"})
        known = self.client.post(reverse("portal_request"), {"email": "asha@example.com"})
        self.assertContains(unknown, "If a business has a customer record")
        self.assertContains(known, "If a business has a customer record")
        self.assertEqual(sorted(m.subject for m in mail.outbox), ["Your invoices from Example Studio", "Your invoices from Other Shop"])
        self.assertFalse(PortalToken.objects.filter(digest__in=[p.split("/")[3] for p in self.links()]).exists())

    def test_links_are_single_use_confirmed_by_post_and_expire(self):
        self.client.post(reverse("portal_request"), {"email": "ravi@example.com"})
        path = self.links()[0]
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get(reverse("portal_home")).status_code, 302)
        self.assertRedirects(self.client.post(path), reverse("portal_home"))
        self.assertEqual(self.client.session[SESSION_KEY], [self.ravi.pk])
        self.assertEqual(self.client.post(path).status_code, 410)
        token = path.split("/")[3]
        PortalToken.objects.create(customer=self.ravi, digest=digest("late"), expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(reverse("portal_enter", args=["late"])).status_code, 410)
        self.assertTrue(PortalToken.objects.get(digest=digest(token)).used_at)

    def test_customers_see_only_their_own_records(self):
        mine = self.issued(self.asha)
        draft = Invoice.objects.create(workspace=self.business, customer=self.asha, created_by=self.owner)
        theirs = self.issued(self.ravi)
        elsewhere = self.issued(self.asha_elsewhere, self.other_product)
        self.sign_in(self.asha)
        home = self.client.get(reverse("portal_home"))
        self.assertContains(home, mine.number)
        self.assertNotContains(home, theirs.number)
        self.assertNotContains(home, "Other Shop")
        for invoice in [theirs, draft, elsewhere]:
            self.assertEqual(self.client.get(reverse("portal_invoice", args=[invoice.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("portal_invoice", args=[mine.pk])).status_code, 200)

    def test_receipts_and_credit_notes_are_scoped(self):
        mine = self.issued(self.asha)
        payment = services.record_payment(mine, self.owner, D("500"), "bank", timezone.localdate())
        theirs = self.issued(self.ravi)
        note = services.issue_credit_note(theirs, self.owner, D("10"), "Goodwill")
        self.sign_in(self.asha)
        self.assertContains(self.client.get(reverse("portal_receipt", args=[mine.pk, payment.pk])), "Payment receipt")
        self.assertEqual(self.client.get(reverse("portal_receipt", args=[theirs.pk, payment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("portal_credit_note", args=[note.pk])).status_code, 404)

    def test_subscription_actions_follow_plan_policy_and_are_logged(self):
        subscription = Subscription.objects.create(
            workspace=self.business, number="SUB-1", customer=self.asha, plan=self.plan,
            status=Subscription.Status.ACTIVE, created_by=self.owner,
        )
        foreign = Subscription.objects.create(
            workspace=self.business, number="SUB-2", customer=self.ravi, plan=self.plan,
            status=Subscription.Status.ACTIVE, created_by=self.owner,
        )
        self.sign_in(self.asha)
        self.client.post(reverse("portal_subscription_action", args=[subscription.pk, "pause"]))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "active")
        self.assertEqual(self.client.post(reverse("portal_subscription_action", args=[foreign.pk, "close"])).status_code, 404)
        self.assertEqual(self.client.post(reverse("portal_subscription_action", args=[subscription.pk, "activate"])).status_code, 404)
        self.client.post(reverse("portal_subscription_action", args=[subscription.pk, "close"]))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "closed")
        event = Activity.objects.get(action="Subscription closed")
        self.assertEqual((event.actor_label, event.is_system), ("Customer (portal)", False))

    def test_sign_out_and_staff_invite(self):
        self.sign_in(self.asha)
        self.client.post(reverse("portal_sign_out"))
        self.assertEqual(self.client.get(reverse("portal_home")).status_code, 302)
        mail.outbox.clear()
        self.client.force_login(self.owner)
        self.client.post(reverse("portal_invite", args=[self.business.pk, self.ravi.pk]))
        self.assertEqual(mail.outbox[0].to, ["ravi@example.com"])
        self.assertEqual(self.client.post(reverse("portal_invite", args=[self.business.pk, self.asha_elsewhere.pk])).status_code, 404)
