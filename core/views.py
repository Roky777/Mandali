import hashlib
import logging
import secrets
from datetime import timedelta
from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordResetView
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from .forms import (
    SignupForm,
    EmailLoginForm,
    WorkspaceForm,
    InviteForm,
    InviteSignupForm,
)
from .models import User, Workspace, Membership, Invitation, Activity
from .security import rate_limit
from billing.services import billing_summary

logger = logging.getLogger(__name__)


@method_decorator(rate_limit("login"), name="dispatch")
class EmailLoginView(LoginView):
    authentication_form = EmailLoginForm
    template_name = "registration/login.html"


@method_decorator(rate_limit("reset", limit=5, seconds=3600), name="dispatch")
class ResetView(PasswordResetView):
    def get_email_options(self):
        from urllib.parse import urlsplit

        return urlsplit(settings.MANDALI_BASE_URL)

    def form_valid(self, form):
        base = self.get_email_options()
        try:
            form.save(
                use_https=base.scheme == "https",
                domain_override=base.netloc,
                email_template_name=self.email_template_name,
                subject_template_name=self.subject_template_name,
                from_email=settings.DEFAULT_FROM_EMAIL,
                request=self.request,
            )
        except Exception:
            logger.error("Password reset email delivery failed.")
        return redirect(self.success_url)


@rate_limit("signup", limit=5, seconds=3600)
def signup(request):
    if request.user.is_authenticated:
        return redirect("workspaces")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save()
        except IntegrityError:
            form.add_error(
                "email", "An account already uses this email. Sign in instead."
            )
        else:
            login(request, user)
            return redirect("workspace_create")
    return render(
        request,
        "core/form.html",
        {
            "form": form,
            "title": "Make room for your business",
            "description": "Create your Mandali account, then set up your workspace.",
            "submit": "Create account",
            "auth_form": True,
        },
    )


# All workspace routes enter through this authorization boundary, including POSTs.
# The UUID is an identifier, never an authorization mechanism.
def workspace_access(admin=False):
    def decorate(view):
        @login_required
        @wraps(view)
        def wrapped(request, workspace_id, *args, **kwargs):
            membership = get_object_or_404(
                Membership.objects.select_related("workspace"),
                workspace_id=workspace_id,
                user=request.user,
                is_active=True,
            )
            if admin and membership.role != Membership.Role.ADMIN:
                return render(
                    request,
                    "core/error.html",
                    {
                        "title": "Administrator access needed",
                        "explanation": "Ask a business administrator to make this change.",
                    },
                    status=403,
                )
            request.workspace_membership = membership
            return view(request, membership, *args, **kwargs)

        return wrapped

    return decorate


@login_required
def workspaces(request):
    memberships = (
        Membership.objects.filter(user=request.user, is_active=True)
        .select_related("workspace")
        .order_by("workspace__name")
    )
    return render(request, "core/workspaces.html", {"memberships": memberships})


@login_required
def workspace_create(request):
    form = WorkspaceForm(
        request.POST or None, initial={"billing_email": request.user.email}
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            workspace = form.save()
            Membership.objects.create(
                workspace=workspace, user=request.user, role=Membership.Role.ADMIN
            )
            Activity.objects.create(
                workspace=workspace, actor=request.user, action="Workspace created"
            )
        messages.success(request, "Your business workspace is ready.")
        return redirect("dashboard", workspace_id=workspace.pk)
    return render(
        request,
        "core/form.html",
        {
            "form": form,
            "title": "Set up your business",
            "description": "Your business details and how you plan to bill. INR and India time are set for you.",
            "submit": "Create workspace",
        },
    )


def context(membership):
    return {
        "workspace": membership.workspace,
        "membership": membership,
        "is_admin": membership.role == Membership.Role.ADMIN,
    }


@workspace_access()
def dashboard(request, membership):
    from billing.reporting import report_data
    data = {**context(membership), **report_data(membership.workspace, request.GET)}
    data["setup_steps"] = [
        ("Add a customer", reverse("customer_create", args=[membership.workspace.pk]), membership.workspace.customer_set.exists()),
        ("Create a plan", reverse("catalog_create", args=[membership.workspace.pk, "plans"]), membership.workspace.plan_set.exists()),
        ("Prepare a quotation", reverse("subscription_create", args=[membership.workspace.pk]), membership.workspace.subscription_set.exists()),
        ("Issue an invoice", reverse("invoices", args=[membership.workspace.pk]), membership.workspace.invoice_set.exclude(status="draft").exists()),
    ]
    data.update(
        {
            "team_count": Membership.objects.filter(
                workspace=membership.workspace, is_active=True
            ).count(),
            "activities": Activity.objects.filter(
                workspace=membership.workspace
            ).select_related("actor")[:8],
            "billing": billing_summary(membership.workspace, timezone.localdate()),
        }
    )
    return render(request, "core/dashboard.html", data)


@workspace_access(admin=True)
def workspace_settings(request, membership):
    form = WorkspaceForm(request.POST or None, instance=membership.workspace)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            Activity.objects.create(
                workspace=membership.workspace,
                actor=request.user,
                action="Business settings updated",
            )
        messages.success(request, "Business settings saved.")
        return redirect("workspace_settings", workspace_id=membership.workspace.pk)
    return render(
        request,
        "core/form.html",
        {
            **context(membership),
            "form": form,
            "title": "Business settings",
            "description": "Keep the details your team will use for billing up to date.",
            "submit": "Save settings",
        },
    )


@workspace_access()
def team(request, membership):
    data = context(membership)
    data.update(
        {
            "members": Membership.objects.filter(
                workspace=membership.workspace, is_active=True
            ).select_related("user"),
            "invitations": Invitation.objects.filter(
                workspace=membership.workspace,
                accepted_at=None,
                revoked_at=None,
                expires_at__gt=timezone.now(),
            ),
            "form": InviteForm(),
        }
    )
    return render(request, "core/team.html", data)


@require_POST
@workspace_access(admin=True)
@rate_limit("invite", limit=20, seconds=3600)
def invite(request, membership):
    form = InviteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a valid staff email address.")
        return redirect("team", workspace_id=membership.workspace.pk)
    email = form.cleaned_data["email"]
    if Membership.objects.filter(
        workspace=membership.workspace, user__email__iexact=email, is_active=True
    ).exists():
        messages.error(request, "This person already has access to this workspace.")
        return redirect("team", workspace_id=membership.workspace.pk)
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        # Serialize invitation replacement per workspace on databases supporting row locks.
        Workspace.objects.select_for_update().get(pk=membership.workspace.pk)
        Invitation.objects.filter(
            workspace=membership.workspace,
            email=email,
            accepted_at=None,
            revoked_at=None,
        ).update(revoked_at=timezone.now())
        Invitation.objects.create(
            workspace=membership.workspace,
            email=email,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            created_by=request.user,
            expires_at=timezone.now() + timedelta(hours=48),
        )
        Activity.objects.create(
            workspace=membership.workspace,
            actor=request.user,
            action="Staff invitation created",
            detail=email,
        )
    link = settings.MANDALI_BASE_URL + reverse("accept_invite", args=[token])
    try:
        send_mail(
            "Join your team on Mandali",
            f"You have been invited to {membership.workspace.name}. Join as staff within 48 hours:\n{link}\nIf you did not expect this invitation, ignore it.",
            settings.DEFAULT_FROM_EMAIL,
            [email],
        )
    except Exception:
        logger.error("Staff invitation email delivery failed.")
        messages.warning(
            request,
            "Invitation created, but email delivery failed. Copy the invitation link and share it directly.",
        )
    else:
        messages.success(
            request, "Invitation created and handed to the configured email backend."
        )
    return render(
        request,
        "core/invite_created.html",
        {**context(membership), "invite_link": link, "invited_email": email},
    )


@rate_limit("accept", limit=10)
def accept_invite(request, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    invitation = get_object_or_404(
        Invitation.objects.select_related("workspace"),
        token_digest=digest,
        accepted_at=None,
        revoked_at=None,
        expires_at__gt=timezone.now(),
    )
    account_exists = User.objects.filter(email=invitation.email).exists()
    if request.user.is_authenticated and request.user.email != invitation.email:
        return render(
            request,
            "core/error.html",
            {
                "title": "This invitation is for another email",
                "explanation": "Sign out and use the invited email address to join.",
            },
            status=403,
        )
    form = InviteSignupForm(request.POST or None) if not account_exists else None
    if request.method == "POST" and (
        (account_exists and request.user.is_authenticated)
        or (not account_exists and form.is_valid())
    ):
        try:
            with transaction.atomic():
                # Conditional update makes the single-use claim atomic on SQLite too.
                claimed = Invitation.objects.filter(
                    pk=invitation.pk,
                    accepted_at=None,
                    revoked_at=None,
                    expires_at__gt=timezone.now(),
                ).update(accepted_at=timezone.now())
                if not claimed:
                    return render(
                        request,
                        "core/error.html",
                        {
                            "title": "Invitation no longer available",
                            "explanation": "Ask your administrator for a new invitation.",
                        },
                        status=409,
                    )
                if account_exists:
                    user = request.user
                else:
                    user = form.save(commit=False)
                    user.email = invitation.email
                    user.save()
                member, created = Membership.objects.get_or_create(
                    workspace=invitation.workspace,
                    user=user,
                    defaults={"role": Membership.Role.STAFF},
                )
                if not member.is_active:
                    member.is_active = True
                    member.role = Membership.Role.STAFF
                    member.save(update_fields=["is_active", "role"])
                Activity.objects.create(
                    workspace=invitation.workspace,
                    actor=user,
                    action="Staff invitation accepted",
                )
        except IntegrityError:
            messages.error(
                request,
                "The account changed while joining. Sign in and open your invitation again.",
            )
            return redirect("login")
        if not request.user.is_authenticated:
            login(request, user)
        messages.success(request, "You have joined the business workspace.")
        return redirect("dashboard", workspace_id=invitation.workspace.pk)
    return render(
        request,
        "core/accept_invite.html",
        {"invitation": invitation, "form": form, "account_exists": account_exists},
    )


@require_POST
@workspace_access(admin=True)
def revoke_invite(request, membership, invitation_id):
    invitation = get_object_or_404(
        Invitation,
        pk=invitation_id,
        workspace=membership.workspace,
        accepted_at=None,
        revoked_at=None,
    )
    with transaction.atomic():
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["revoked_at"])
        Activity.objects.create(
            workspace=membership.workspace,
            actor=request.user,
            action="Staff invitation revoked",
            detail=invitation.email,
        )
    messages.success(request, "Invitation revoked.")
    return redirect("team", workspace_id=membership.workspace.pk)


@require_POST
@workspace_access(admin=True)
def remove_staff(request, membership, member_id):
    member = get_object_or_404(
        Membership.objects.select_related("user"),
        pk=member_id,
        workspace=membership.workspace,
        role=Membership.Role.STAFF,
        is_active=True,
    )
    with transaction.atomic():
        member.is_active = False
        member.save(update_fields=["is_active"])
        # An old pending invitation must not restore revoked access.
        Invitation.objects.filter(
            workspace=membership.workspace,
            email=member.user.email,
            accepted_at=None,
            revoked_at=None,
        ).update(revoked_at=timezone.now())
        Activity.objects.create(
            workspace=membership.workspace,
            actor=request.user,
            action="Staff access removed",
            detail=member.user.email,
        )
    messages.success(
        request,
        "Staff access removed. Existing sessions can no longer open this workspace.",
    )
    return redirect("team", workspace_id=membership.workspace.pk)
