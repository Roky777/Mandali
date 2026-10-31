"""Compose app URL configurations; keep feature routes in their owning app."""
from django.urls import include, path

urlpatterns = [
    path("", include("core.urls")),
    path("w/<uuid:workspace_id>/", include("billing.urls")),
    path("", include("checkout.urls")),
    path("w/<uuid:workspace_id>/", include("integrations.urls")),
    path("w/<uuid:workspace_id>/", include("reports.urls")),
    path("", include("portal.urls")),
    path("api/v1/", include("integrations.api_urls")),
    path("w/<uuid:workspace_id>/", include("recurring.urls")),
]
