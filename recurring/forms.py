from django import forms
from django.utils import timezone
from billing.forms import DATE
from billing.models import Discount
from .models import Schedule


class ScheduleForm(forms.Form):
    mode = forms.ChoiceField(choices=Schedule.Mode.choices)
    first_bill_on = forms.DateField(widget=DATE, initial=timezone.localdate)
    is_sandbox = forms.BooleanField(
        required=False, label="Sandbox invoices (no real money)"
    )
    reminders = forms.BooleanField(
        required=False, initial=True, label="Email invoice notices and due reminders"
    )
    discount = forms.ModelChoiceField(
        queryset=Discount.objects.none(),
        required=False,
        label="Recurring discount (optional)",
        help_text="Applied only while eligible. Automatic collection uses a fixed authorized amount and does not accept discounts.",
    )
    total_count = forms.IntegerField(
        min_value=1,
        max_value=120,
        initial=12,
        label="Automatic collection: number of cycles",
        help_text="Razorpay Test only. The customer must authorize this finite schedule.",
    )
    confirm = forms.BooleanField(
        label="I have reviewed the amount, dates, and customer details"
    )

    def __init__(self, *args, subscription, **kwargs):
        super().__init__(*args, **kwargs)
        self.subscription = subscription
        self.fields["discount"].queryset = Discount.objects.filter(
            workspace=subscription.workspace, is_active=True
        )

    def clean(self):
        data = super().clean()
        first = data.get("first_bill_on")
        sub = self.subscription
        if not sub.plan_id or not sub.start_date:
            raise forms.ValidationError(
                "Set a plan and start date before enabling recurring billing."
            )
        if first and (
            first < sub.start_date or (sub.expiry_date and first > sub.expiry_date)
        ):
            self.add_error(
                "first_bill_on", "Choose a date within the subscription term."
            )
        if data.get("mode") == Schedule.Mode.AUTOMATIC:
            if not data.get("is_sandbox"):
                self.add_error(
                    "is_sandbox", "Automatic collection supports sandbox invoices only."
                )
            if first and first <= timezone.localdate():
                self.add_error(
                    "first_bill_on",
                    "Allow time for authorization: choose tomorrow or later.",
                )
            if sub.plan.period == "daily":
                raise forms.ValidationError(
                    "Daily billing supports invoice automation only. This gateway requires a minimum seven-day interval."
                )
            if sub.expiry_date:
                raise forms.ValidationError(
                    "Automatic collection uses the agreed number of cycles. Use a subscription without an expiry date; the finite provider schedule controls its end."
                )
            if data.get("discount"):
                self.add_error(
                    "discount",
                    "Automatic collection needs a fixed amount. Use invoice automation for changing discounts.",
                )
        return data


class RenewalForm(forms.Form):
    expiry_date = forms.DateField(widget=DATE, label="New expiry date (inclusive)")
