from django import forms


class LinkRequestForm(forms.Form):
    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
        help_text="Use the email address the business has for you.",
    )
