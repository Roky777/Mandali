from django.urls import path
from . import views

urlpatterns = [
    path("reports/", views.summary, name="reports"),
    path("reports/attention/", views.attention, name="reports_attention"),
    path("reports/activity/", views.activity, name="reports_activity"),
    path("reports/export/<str:kind>.csv", views.export, name="reports_export"),
]
