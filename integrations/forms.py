from django import forms
from .models import Connector, WebhookEndpoint
from .outbound import OutboundError, check_url


def validate_outbound(url):
    try:
        check_url(url)
    except OutboundError as problem:
        raise forms.ValidationError(str(problem))
    return url


class ApiKeyForm(forms.Form):
    name = forms.CharField(
        label="Key name",
        max_length=80,
        help_text="Where the key is used, for example: Shop website.",
    )


class WebhookForm(forms.ModelForm):
    class Meta:
        model = WebhookEndpoint
        fields = ["url"]
        help_texts = {
            "url": "An https:// address on your website that accepts POST requests. Private and internal addresses are refused."
        }

    def clean_url(self):
        return validate_outbound(self.cleaned_data["url"])


class ConnectorForm(forms.ModelForm):
    class Meta:
        model = Connector
        fields = ["name", "url", "list_path"]
        labels = {"name": "Connection name", "list_path": "List path (optional)"}
        help_texts = {
            "url": "An https:// address on your website that returns customers as JSON.",
            "list_path": "Where the list sits in the response, for example data.customers. Leave empty when the response is the list itself.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mapping = self.instance.mapping or {}
        for field, label, required in Connector.FIELDS:
            self.fields[f"map_{field}"] = forms.CharField(
                label=f"{label} field{'' if required else ' (optional)'}",
                max_length=80,
                required=required,
                initial=mapping.get(field, field),
                help_text="The key in each record, for example id or contact.email." if field == "external_id" else "",
            )

    def clean_url(self):
        return validate_outbound(self.cleaned_data["url"])

    def save(self, commit=True):
        self.instance.mapping = {
            field: self.cleaned_data[f"map_{field}"].strip()
            for field, _, _ in Connector.FIELDS
            if self.cleaned_data.get(f"map_{field}", "").strip()
        }
        return super().save(commit)


class FetchForm(forms.Form):
    token = forms.CharField(
        label="Access token (optional)",
        required=False,
        max_length=500,
        strip=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
        help_text="Sent once as a Bearer token for this fetch and never stored.",
    )
