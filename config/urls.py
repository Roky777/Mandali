from django.urls import include, path, reverse_lazy
from django.contrib.auth import views as auth
from core import views, health

urlpatterns = [
    path("health/live/", health.live, name="health_live"),
    path("health/ready/", health.ready, name="health_ready"),
    path("", views.workspaces, name="workspaces"),
    path("signup/", views.signup, name="signup"),
    path("login/", views.EmailLoginView.as_view(), name="login"),
    path("logout/", auth.LogoutView.as_view(), name="logout"),
    path(
        "password/reset/",
        views.ResetView.as_view(success_url=reverse_lazy("password_reset_done")),
        name="password_reset",
    ),
    path(
        "password/reset/sent/",
        auth.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password/reset/<uidb64>/<token>/",
        auth.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password/reset/complete/",
        auth.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("workspaces/new/", views.workspace_create, name="workspace_create"),
    path("w/<uuid:workspace_id>/", views.dashboard, name="dashboard"),
    path(
        "w/<uuid:workspace_id>/settings/",
        views.workspace_settings,
        name="workspace_settings",
    ),
    path("w/<uuid:workspace_id>/team/", views.team, name="team"),
    path("w/<uuid:workspace_id>/team/invite/", views.invite, name="invite"),
    path(
        "w/<uuid:workspace_id>/team/invitations/<uuid:invitation_id>/revoke/",
        views.revoke_invite,
        name="revoke_invite",
    ),
    path(
        "w/<uuid:workspace_id>/team/<int:member_id>/remove/",
        views.remove_staff,
        name="remove_staff",
    ),
    path("w/<uuid:workspace_id>/", include("billing.urls")),
    path("join/<str:token>/", views.accept_invite, name="accept_invite"),
]
