from django.urls import path
from . import views

urlpatterns = [
    path("automation/", views.overview, name="recurring_overview"),
    path(
        "automation/subscriptions/<int:pk>/setup/", views.setup, name="recurring_setup"
    ),
    path(
        "automation/subscriptions/<int:pk>/renew/", views.renew, name="recurring_renew"
    ),
    path("automation/schedules/<int:pk>/", views.detail, name="recurring_detail"),
    path(
        "automation/schedules/<int:pk>/action/", views.action, name="recurring_action"
    ),
    path(
        "automation/notices/<int:pk>/retry/",
        views.retry_notice,
        name="recurring_notice_retry",
    ),
]
