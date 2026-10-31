from datetime import timedelta
from billing.models import InvoiceLine, Product, ProductVariant
from billing.services import build_line, transition, BillingError
from recurring.models import Schedule
from recurring.services import maintain_terms
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import User, Workspace, Membership
from billing.models import Customer, Invoice, Payment, CampaignSpend, Subscription, Plan
from billing.reporting import report_data


class AnalyticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner=User.objects.create_user('growth-owner@example.invalid','Local-test!42')
        cls.staff=User.objects.create_user('growth-staff@example.invalid','Local-test!42')
        cls.ws=Workspace.objects.create(name='Analytics example',legal_name='Example Ltd',billing_email=cls.owner.email,address='India')
        Membership.objects.create(workspace=cls.ws,user=cls.owner,role='admin')
        Membership.objects.create(workspace=cls.ws,user=cls.staff,role='staff')
        cls.customer=Customer.objects.create(workspace=cls.ws,name='Buyer',email='buyer@example.invalid')
        cls.plan=Plan.objects.create(workspace=cls.ws,name='Monthly',price=100)
        cls.sub=Subscription.objects.create(workspace=cls.ws,number='SUB-1',customer=cls.customer,plan=cls.plan,status='active',subtotal=100,total=100,created_by=cls.owner)
        cls.invoice=Invoice.objects.create(workspace=cls.ws,customer=cls.customer,number='INV-1',status='issued',total=100,credited=30,amount_paid=40,refunded=10,issue_date=timezone.localdate(),due_date=timezone.localdate(),created_by=cls.owner,customer_name='Buyer',utm_source='instagram',utm_campaign='launch')
        Payment.objects.create(workspace=cls.ws,invoice=cls.invoice,amount=40,method='bank',received_on=timezone.localdate(),recorded_by=cls.owner)
        CampaignSpend.objects.create(workspace=cls.ws,source='instagram',campaign='launch',spent_on=timezone.localdate(),amount=20)

    def test_credits_and_refunds_reduce_real_outstanding(self):
        data=report_data(self.ws,{})
        self.assertEqual(data['metrics']['outstanding'],40)
        self.assertEqual(data['metrics']['received'],40)
        self.assertEqual(data['campaign_summary'][0]['roas'],Decimal('2.00'))

    def test_sandbox_never_becomes_real_revenue(self):
        Invoice.objects.create(workspace=self.ws,customer=self.customer,status='paid',total=9999,amount_paid=9999,is_sandbox=True,created_by=self.owner,issue_date=timezone.localdate())
        self.assertEqual(report_data(self.ws,{})['metrics']['billed'],100)

    def test_admin_can_render_and_export_but_staff_cannot(self):
        self.client.force_login(self.owner)
        response=self.client.get(reverse('analytics',args=[self.ws.pk]))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Campaign return')
        csv=self.client.get(reverse('analytics',args=[self.ws.pk]),{'export':'csv'})
        self.assertContains(csv,'INV-1')
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('analytics',args=[self.ws.pk])).status_code,403)

    def test_dashboard_preserves_report_and_billing_destinations(self):
        self.client.force_login(self.owner)
        response=self.client.get(reverse('dashboard',args=[self.ws.pk]))
        self.assertEqual(response.status_code,200)
        for name in ['reports','reports_activity','invoices','analytics','customers','catalog_home']:
            self.assertContains(response,reverse(name,args=[self.ws.pk]))

    def test_spend_without_receipts_does_not_invent_return(self):
        CampaignSpend.objects.create(workspace=self.ws,source='google',campaign='new',spent_on=timezone.localdate(),amount=10)
        row=next(r for r in report_data(self.ws,{})['campaign_summary'] if r['source']=='google')
        self.assertEqual(row['received'],0)
        self.assertEqual(row['roas'],0)

    def test_variant_uses_plan_price_not_product_sales_price(self):
        product=Product.objects.create(workspace=self.ws,name="App",sales_price=9000)
        variant=ProductVariant.objects.create(product=product,name="Odoo",price_adjustment=560)
        line=build_line(InvoiceLine,{"plan":self.plan,"product":product,"variant":variant},1,invoice=self.invoice)
        self.assertEqual(line.unit_price,660)

    def test_term_maintenance_expires_quotes_and_closes_unscheduled_terms(self):
        Subscription.objects.filter(pk=self.sub.pk).update(status="quotation",valid_until=timezone.localdate()-timedelta(days=1))
        maintain_terms(self.ws)
        self.sub.refresh_from_db();self.assertEqual(self.sub.status,"expired")
        with self.assertRaises(BillingError):
            transition(self.sub,"confirm",self.owner)

    def test_paused_period_shifts_billing_and_preserves_remainder(self):
        today=timezone.localdate()
        Schedule.objects.create(subscription=self.sub,mode="invoice",anchor_on=today-timedelta(days=20),next_bill_on=today+timedelta(days=5),period="monthly",created_by=self.owner)
        Subscription.objects.filter(pk=self.sub.pk).update(status="paused",paused_on=today-timedelta(days=7))
        transition(self.sub,"resume",self.owner)
        self.assertEqual(Schedule.objects.get(subscription=self.sub).next_bill_on,today+timedelta(days=12))

    def test_recurring_auto_discount_and_double_run_are_safe(self):
        from billing.models import Discount, SubscriptionLine
        from recurring.services import bill_one
        today=timezone.localdate()
        SubscriptionLine.objects.create(subscription=self.sub,plan=self.plan,quantity=1,description="Plan",unit_price=100,total=100,subtotal=100)
        discount=Discount.objects.create(workspace=self.ws,name="Launch",kind="percent",value=10,usage_limit=1)
        schedule=Schedule.objects.create(subscription=self.sub,mode="invoice",anchor_on=today,next_bill_on=today,period="monthly",created_by=self.owner)
        cycle=bill_one(schedule.pk)
        cycle.invoice.refresh_from_db()
        self.assertEqual(cycle.invoice.total,90)
        self.assertIsNone(bill_one(schedule.pk))
        discount.refresh_from_db();self.assertEqual(discount.times_used,1)
        Schedule.objects.filter(pk=schedule.pk).update(next_bill_on=today+timedelta(days=31))
        second=bill_one(schedule.pk,today=today+timedelta(days=31))
        self.assertEqual(second.invoice.total,100)

    def test_recurring_invoice_retains_subscription_campaign(self):
        from billing.models import SubscriptionLine
        from recurring.services import clone_invoice
        Subscription.objects.filter(pk=self.sub.pk).update(utm_source="instagram",utm_campaign="launch",utm_content="video_a")
        self.sub.refresh_from_db()
        SubscriptionLine.objects.create(subscription=self.sub,plan=self.plan,quantity=1,description="Plan",unit_price=100,total=100,subtotal=100)
        schedule=Schedule.objects.create(subscription=self.sub,mode="invoice",anchor_on=timezone.localdate(),next_bill_on=timezone.localdate(),period="monthly",created_by=self.owner)
        invoice=clone_invoice(schedule)
        self.assertEqual((invoice.utm_source,invoice.utm_campaign,invoice.utm_content),("instagram","launch","video_a"))

    def test_expired_quote_detail_and_plan_price_prefill(self):
        from billing.forms import PlanForm
        Subscription.objects.filter(pk=self.sub.pk).update(status="expired")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("subscription_detail",args=[self.ws.pk,self.sub.pk])).status_code,200)
        product=Product.objects.create(workspace=self.ws,name="App",sales_price=700)
        form=PlanForm({"name":"App plan","product":product.pk,"period":"monthly","min_quantity":1},workspace=self.ws)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.cleaned_data["price"],700)

    def test_invoice_email_is_explicit_and_uses_customer_address(self):
        from django.core import mail
        from django.test import override_settings
        Invoice.objects.filter(pk=self.invoice.pk).update(customer_email="buyer@example.invalid")
        self.client.force_login(self.owner)
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.client.post(reverse("invoice_email",args=[self.ws.pk,self.invoice.pk]))
            self.assertEqual(mail.outbox[0].to,["buyer@example.invalid"])
            self.assertIn("INV-1",mail.outbox[0].subject)

    def test_portal_renewal_is_owned_and_respects_plan_policy(self):
        from portal.services import SESSION_KEY
        session=self.client.session;session[SESSION_KEY]=[self.customer.pk];session.save()
        today=timezone.localdate()
        Subscription.objects.filter(pk=self.sub.pk).update(expiry_date=today)
        response=self.client.post(reverse("portal_subscription_action",args=[self.sub.pk,"renew"]))
        self.assertEqual(response.status_code,302)
        self.sub.refresh_from_db();self.assertGreater(self.sub.expiry_date,today)
        previous=self.sub.expiry_date
        Plan.objects.filter(pk=self.plan.pk).update(can_renew=False)
        self.client.post(reverse("portal_subscription_action",args=[self.sub.pk,"renew"]))
        self.sub.refresh_from_db();self.assertEqual(self.sub.expiry_date,previous)
        session=self.client.session;session[SESSION_KEY]=[];session.save()
        self.assertNotEqual(self.client.post(reverse("portal_subscription_action",args=[self.sub.pk,"renew"])).status_code,200)

    def test_automatic_invoice_choice_creates_schedule_on_activation(self):
        from billing.models import SubscriptionLine
        Subscription.objects.filter(pk=self.sub.pk).update(status="confirmed",start_date=timezone.localdate(),auto_invoice=True)
        SubscriptionLine.objects.create(subscription=self.sub,plan=self.plan,quantity=1,description="Plan",unit_price=100,total=100,subtotal=100)
        transition(self.sub,"activate",self.owner)
        self.assertEqual(Schedule.objects.get(subscription=self.sub).mode,"invoice")
