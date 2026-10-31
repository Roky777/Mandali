from django.urls import path
from . import views

urlpatterns = [
    path("integrations/", views.keys, name="integration_keys"),
    path("integrations/keys/<int:pk>/revoke/", views.key_revoke, name="integration_key_revoke"),
    path("integrations/webhooks/", views.webhook_list, name="integration_webhooks"),
    path("integrations/webhooks/<int:pk>/", views.webhook_detail, name="integration_webhook"),
    path("integrations/webhooks/<int:pk>/action/", views.webhook_action, name="integration_webhook_action"),
    path("integrations/deliveries/<int:pk>/resend/", views.delivery_resend, name="integration_delivery_resend"),
    path("integrations/sync/", views.connector_list, name="integration_connectors"),
    path("integrations/sync/new/", views.connector_edit, name="integration_connector_create"),
    path("integrations/sync/<int:pk>/", views.connector_detail, name="integration_connector"),
    path("integrations/sync/<int:pk>/edit/", views.connector_edit, name="integration_connector_edit"),
    path("integrations/runs/<int:pk>/", views.run_detail, name="integration_run"),
    path("integrations/runs/<int:pk>/action/", views.run_action, name="integration_run_action"),
]
