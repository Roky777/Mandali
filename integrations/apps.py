from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "integrations"

    def ready(self):
        from billing.signals import billing_event
        from .webhooks import on_billing_event

        billing_event.connect(on_billing_event, dispatch_uid="integrations-webhooks")
