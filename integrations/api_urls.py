from django.urls import path
from . import api

urlpatterns = [
    path("ping", api.ping, name="api_ping"),
    path("customers", api.customers, name="api_customers"),
    path("customers/<str:external_id>", api.customer, name="api_customer"),
    path("orders", api.orders, name="api_orders"),
    path("orders/<str:external_id>", api.order, name="api_order"),
    path("subscriptions", api.subscriptions, name="api_subscriptions"),
    path("subscriptions/<str:external_id>", api.subscription, name="api_subscription"),
]
