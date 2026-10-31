from django.urls import path
from . import views

urlpatterns = [
    path("w/<uuid:workspace_id>/checkout/", views.links, name="checkout_links"),
    path(
        "w/<uuid:workspace_id>/checkout/gateway/",
        views.gateway_settings,
        name="checkout_gateway",
    ),
    path(
        "w/<uuid:workspace_id>/checkout/gateway/disable/",
        views.gateway_disable,
        name="checkout_gateway_disable",
    ),
    path(
        "w/<uuid:workspace_id>/checkout/new/",
        views.link_create,
        name="checkout_link_create",
    ),
    path(
        "w/<uuid:workspace_id>/checkout/link/<uuid:pk>/",
        views.link_detail,
        name="checkout_link_detail",
    ),
    path(
        "w/<uuid:workspace_id>/checkout/link/<uuid:pk>/revoke/",
        views.link_revoke,
        name="checkout_link_revoke",
    ),
    path(
        "w/<uuid:workspace_id>/checkout/link/<uuid:pk>/qr/",
        views.link_qr,
        name="checkout_link_qr",
    ),
    path(
        "w/<uuid:workspace_id>/checkout/attempt/<uuid:pk>/reconcile/",
        views.attempt_reconcile,
        name="checkout_attempt_reconcile",
    ),
    path("pay/<str:token>/", views.public_link, name="checkout_public"),
    path("checkout/session/<str:token>/", views.session_view, name="checkout_session"),
    path(
        "checkout/session/<str:token>/begin/",
        views.session_begin,
        name="checkout_begin",
    ),
    path(
        "checkout/session/<str:token>/verify/",
        views.session_verify,
        name="checkout_verify",
    ),
    path(
        "checkout/session/<str:token>/status/",
        views.session_recheck,
        name="checkout_status",
    ),
    path(
        "webhooks/razorpay-test/<uuid:account_id>/",
        views.webhook,
        name="checkout_webhook",
    ),
]
