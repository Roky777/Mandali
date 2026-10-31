"""Create an isolated, explicitly requested demo; never alter an existing business."""
import secrets
from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from billing.models import Customer, Plan, Product, ProductVariant, Subscription, SubscriptionLine, Invoice, InvoiceLine, Payment, CampaignSpend, Sequence
from core.models import Membership, User, Workspace
from recurring.models import Schedule


class Command(BaseCommand):
    help = "Create a separate demo business with 5,000 subscriptions and attributed invoices."
    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Existing administrator account to own this demo")
        parser.add_argument("--count", type=int, default=5000)
    @transaction.atomic
    def handle(self, *args, **options):
        if not 1 <= options["count"] <= 20000:
            raise CommandError("Count must be between 1 and 20,000.")
        owner = User.objects.filter(email__iexact=options["email"], is_active=True).first()
        if not owner:
            raise CommandError("Create your administrator account first. No password is created by this command.")
        today = timezone.localdate()
        ws = Workspace.objects.create(name="Brightloop Demo " + secrets.token_hex(2), legal_name="Brightloop Demo · Sample data", billing_email=owner.email, address="Demo address, Bengaluru", use_manual=True, use_hosted=True)
        Membership.objects.create(workspace=ws, user=owner, role="admin")
        plans = [Plan.objects.create(workspace=ws, name=name, price=price, period=period, can_pause=True) for name,price,period in [("Starter", Decimal("499"),"monthly"),("Growth",Decimal("1499"),"monthly"),("Annual Pro",Decimal("14999"),"yearly")]]
        product = Product.objects.create(workspace=ws, name="Business software", sales_price=499, cost_price=100)
        ProductVariant.objects.create(product=product, name="Odoo", price_adjustment=560)
        customers = Customer.objects.bulk_create([Customer(workspace=ws, name=f"Demo customer {i+1:04d}", email=f"demo-{i+1}@example.invalid") for i in range(min(options["count"],500))])
        count = options["count"]
        subs = Subscription.objects.bulk_create([Subscription(workspace=ws, number=f"SUB-{i+1:05d}", customer=customers[i%len(customers)], plan=plans[i%3], status="paused" if i%17==0 else "active", start_date=today-timedelta(days=180), expiry_date=today+timedelta(days=180), total=plans[i%3].price, subtotal=plans[i%3].price, created_by=owner) for i in range(count)], batch_size=500)
        SubscriptionLine.objects.bulk_create([SubscriptionLine(subscription=sub, plan=sub.plan, quantity=1, description=sub.plan.name, unit_price=sub.plan.price, subtotal=sub.plan.price, total=sub.plan.price) for sub in subs], batch_size=500)
        # Future billing dates avoid accidentally generating demo notices to fake addresses.
        Schedule.objects.bulk_create([Schedule(subscription=sub, mode="invoice", enabled=False, anchor_on=today+timedelta(days=30), next_bill_on=today+timedelta(days=30), period=sub.plan.period, reminders=False, created_by=owner) for sub in subs],batch_size=500)
        invoices = []
        for i, sub in enumerate(subs[:min(count,1500)]):
            day = today-timedelta(days=i%175)
            paid = sub.total if i%3 else (sub.total/2).quantize(Decimal(".01"))
            source, campaign = [("instagram","diwali"),("google","search"),("email","renewal"),("","")][i%4]
            invoices.append(Invoice(workspace=ws, number=f"INV-{i+1:05d}", customer=sub.customer, subscription=sub, status="paid" if paid==sub.total else "issued", issue_date=day, due_date=day+timedelta(days=15), subtotal=sub.total,total=sub.total,amount_paid=paid, customer_name=sub.customer.name, customer_email=sub.customer.email,business_name=ws.legal_name,business_email=ws.billing_email,business_address=ws.address, created_by=owner, utm_source=source,utm_medium="paid" if source in ["instagram","google"] else "email",utm_campaign=campaign,utm_content="video_a" if i%2 else "image_b",source_channel="hosted" if source else "manual"))
        invoices=Invoice.objects.bulk_create(invoices,batch_size=500)
        InvoiceLine.objects.bulk_create([InvoiceLine(invoice=inv, description="Demo software subscription",quantity=1,unit_price=inv.total,subtotal=inv.total,total=inv.total) for inv in invoices],batch_size=500)
        Payment.objects.bulk_create([Payment(workspace=ws,invoice=inv,amount=inv.amount_paid,method="bank",received_on=inv.issue_date,note="Demo receipt; not real money",recorded_by=owner) for inv in invoices],batch_size=500)
        for source,campaign in [("instagram","diwali"),("google","search")]:
            CampaignSpend.objects.create(workspace=ws,source=source,campaign=campaign,spent_on=today,amount=2500)
        Sequence.objects.create(workspace=ws,kind="subscription",next_value=count+1)
        Sequence.objects.create(workspace=ws,kind="invoice",next_value=len(invoices)+1)
        self.stdout.write(self.style.SUCCESS(f"Created {count} demo subscriptions. Open /w/{ws.pk}/ · Sample data only; do not use this workspace for real billing."))
