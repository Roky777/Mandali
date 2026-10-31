from django.urls import path
from . import views

urlpatterns = [
    path("portal/", views.request_link, name="portal_request"),
    path("portal/enter/<str:token>/", views.enter, name="portal_enter"),
    path("portal/home/", views.home, name="portal_home"),
    path("portal/invoices/<int:pk>/", views.invoice, name="portal_invoice"),
    path("portal/invoices/<int:pk>/receipts/<int:payment_id>/", views.receipt, name="portal_receipt"),
    path("portal/credit-notes/<int:pk>/", views.credit_note, name="portal_credit_note"),
    path("portal/invoices/<int:pk>/upi.svg", views.upi_qr, name="portal_upi_qr"),
    path("portal/subscriptions/<int:pk>/<str:action>/", views.subscription_action, name="portal_subscription_action"),
    path("portal/sign-out/", views.sign_out, name="portal_sign_out"),
    path("w/<uuid:workspace_id>/customers/<int:customer_id>/portal-link/", views.invite, name="portal_invite"),
]
