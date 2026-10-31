from django import forms
from .metrics import PERIODS

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class PeriodForm(forms.Form):
    period = forms.ChoiceField(label="Period", choices=PERIODS, initial="this_month")
    start = forms.DateField(label="From", required=False, widget=DATE)
    end = forms.DateField(label="To", required=False, widget=DATE)


class ActivityFilterForm(forms.Form):
    q = forms.CharField(label="Search", required=False, max_length=80)
    who = forms.ChoiceField(label="Who", required=False)
    start = forms.DateField(label="From", required=False, widget=DATE)
    end = forms.DateField(label="To", required=False, widget=DATE)

    def __init__(self, *args, members, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["who"].choices = [
            ("", "Everyone"),
            ("system", "Mandali automation"),
            ("customer", "Customers (portal)"),
            *[(str(m.user_id), m.user.first_name or m.user.email) for m in members],
        ]
