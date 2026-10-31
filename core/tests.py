import hashlib
import re
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .forms import SignupForm
from .models import Activity, Invitation, Membership, User, Workspace

PASSWORD = "Local-test!42Safe"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    MANDALI_BASE_URL="http://testserver",
)
class FoundationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            "owner@example.com", PASSWORD, first_name="Owner"
        )
        cls.staff = User.objects.create_user(
            "staff@example.com", PASSWORD, first_name="Staff"
        )
        cls.outsider = User.objects.create_user("other@example.com", PASSWORD)
        cls.business = Workspace.objects.create(
            name="Example Studio",
            legal_name="Example Studio LLP",
            billing_email="accounts@example.com",
            address="Example address",
        )
        cls.other = Workspace.objects.create(
            name="Other Business",
            legal_name="Other LLP",
            billing_email="other@example.com",
            address="Another address",
        )
        cls.admin_member = Membership.objects.create(
            workspace=cls.business, user=cls.owner, role="admin"
        )
        cls.staff_member = Membership.objects.create(
            workspace=cls.business, user=cls.staff, role="staff"
        )
        Membership.objects.create(workspace=cls.other, user=cls.outsider, role="admin")

    def url(self, name, workspace=None, **kwargs):
        return reverse(
            name, kwargs={"workspace_id": (workspace or self.business).pk, **kwargs}
        )

    def invite_token(self, email="new@example.com"):
        self.client.force_login(self.owner)
        response = self.client.post(self.url("invite"), {"email": email})
        self.assertEqual(response.status_code, 200)
        return response.context["invite_link"].rsplit("/", 2)[1]

    def test_login_is_required_for_every_workspace_read(self):
        for name in ["dashboard", "team", "workspace_settings"]:
            self.assertEqual(self.client.get(self.url(name)).status_code, 302)

    def test_registration_normalizes_email_and_creates_no_implicit_membership(self):
        result = self.client.post(
            reverse("signup"),
            {
                "first_name": "New Owner",
                "email": "NEW@EXAMPLE.COM",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertRedirects(result, reverse("workspace_create"))
        user = User.objects.get(email="new@example.com")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertFalse(Membership.objects.filter(user=user).exists())

    def test_case_insensitive_login_and_duplicate_email_rejection(self):
        response = self.client.post(
            reverse("login"), {"username": "OWNER@EXAMPLE.COM", "password": PASSWORD}
        )
        self.assertRedirects(response, reverse("workspaces"))
        self.assertFalse(
            SignupForm(
                {
                    "first_name": "Another",
                    "email": "OWNER@EXAMPLE.COM",
                    "password1": PASSWORD,
                    "password2": PASSWORD,
                }
            ).is_valid()
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(email="OWNER@EXAMPLE.COM")

    def test_password_requirements_are_enforced(self):
        for weak in ["Abcdef!1", "lowercase!42", "UPPERCASE!42", "MixedCaseOnly42"]:
            form = SignupForm(
                {
                    "first_name": "Test",
                    "email": "new@example.com",
                    "password1": weak,
                    "password2": weak,
                }
            )
            self.assertFalse(form.is_valid(), weak)
            self.assertIn("password2", form.errors)

    def test_login_throttle_is_enforced(self):
        for _ in range(10):
            self.assertEqual(
                self.client.post(
                    reverse("login"),
                    {"username": self.owner.email, "password": "wrong"},
                ).status_code,
                200,
            )
        result = self.client.post(
            reverse("login"), {"username": self.owner.email, "password": PASSWORD}
        )
        self.assertEqual(result.status_code, 429)
        self.assertIn("Retry-After", result)

    def test_logout_requires_post_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.get(reverse("logout")).status_code, 405)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)

    def test_workspace_creation_ignores_forged_owner_currency_and_role(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("workspace_create"),
            {
                "name": "New Business",
                "legal_name": "New LLP",
                "billing_email": "new@example.com",
                "address": "Mumbai",
                "use_manual": "on",
                "currency": "USD",
                "role": "staff",
                "user": self.outsider.pk,
            },
        )
        business = Workspace.objects.get(name="New Business")
        self.assertRedirects(response, self.url("dashboard", business))
        self.assertEqual(business.currency, "INR")
        self.assertEqual(business.timezone, "Asia/Kolkata")
        self.assertTrue(
            Membership.objects.filter(
                workspace=business, user=self.owner, role="admin"
            ).exists()
        )
        self.assertFalse(
            Membership.objects.filter(workspace=business, user=self.outsider).exists()
        )
        self.assertTrue(
            Activity.objects.filter(
                workspace=business, action="Workspace created"
            ).exists()
        )

    def test_onboarding_requires_a_channel_and_valid_gstin(self):
        self.client.force_login(self.owner)
        data = {
            "name": "Invalid",
            "legal_name": "Invalid LLP",
            "billing_email": "new@example.com",
            "address": "Mumbai",
            "gstin": "invalid",
        }
        response = self.client.post(reverse("workspace_create"), data)
        self.assertContains(response, "Choose at least one billing channel")
        self.assertContains(response, "15-character GSTIN")
        self.assertFalse(Workspace.objects.filter(name="Invalid").exists())

    def test_workspace_directory_and_activity_are_scoped(self):
        self.client.force_login(self.owner)
        Activity.objects.create(
            workspace=self.other, actor=self.outsider, action="Private other event"
        )
        self.assertNotContains(self.client.get(reverse("workspaces")), "Other Business")
        self.assertNotContains(
            self.client.get(self.url("dashboard")), "Private other event"
        )
        for name in ["dashboard", "team", "workspace_settings"]:
            self.assertEqual(
                self.client.get(self.url(name, self.other)).status_code, 404
            )

    def test_cross_workspace_writes_are_denied(self):
        self.client.force_login(self.outsider)
        for name, data in [
            ("workspace_settings", {"name": "Taken over"}),
            ("invite", {"email": "attacker@example.com"}),
        ]:
            self.assertEqual(self.client.post(self.url(name), data).status_code, 404)
        self.business.refresh_from_db()
        self.assertEqual(self.business.name, "Example Studio")
        self.assertFalse(Invitation.objects.exists())

    def test_staff_can_view_but_cannot_administer(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(self.url("team")).status_code, 200)
        self.assertEqual(
            self.client.get(self.url("workspace_settings")).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                self.url("workspace_settings"), {"name": "Changed"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                self.url("invite"), {"email": "new@example.com"}
            ).status_code,
            403,
        )
        self.assertFalse(Invitation.objects.exists())

    def test_platform_superuser_has_no_implicit_business_access(self):
        self.outsider.is_superuser = True
        self.outsider.save()
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url("dashboard")).status_code, 404)

    def test_admin_can_update_settings_and_audit_is_recorded(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url("workspace_settings"),
            {
                "name": "Renamed",
                "legal_name": "Renamed LLP",
                "billing_email": "bill@example.com",
                "address": "Pune",
                "use_api": "on",
                "use_hosted": "on",
                "use_manual": "on",
                "gstin": "27abcde1234f1z5",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.business.refresh_from_db()
        self.assertEqual(self.business.name, "Renamed")
        self.assertEqual(self.business.gstin, "27ABCDE1234F1Z5")
        self.assertTrue(
            Activity.objects.filter(
                action="Business settings updated", workspace=self.business
            ).exists()
        )

    def test_invitation_is_hashed_scoped_and_new_account_joins_as_staff(self):
        token = self.invite_token()
        invite = Invitation.objects.get()
        self.assertNotEqual(invite.token_digest, token)
        self.assertEqual(
            invite.token_digest, hashlib.sha256(token.encode()).hexdigest()
        )
        self.assertIn(token, mail.outbox[0].body)
        self.client.logout()
        path = reverse("accept_invite", args=[token])
        response = self.client.post(
            path,
            {
                "first_name": "New Staff",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "role": "admin",
                "email": "attacker@example.com",
            },
        )
        self.assertRedirects(response, self.url("dashboard"))
        member = Membership.objects.get(user__email="new@example.com")
        self.assertEqual(member.role, "staff")
        self.assertEqual(member.workspace_id, self.business.pk)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_existing_account_must_sign_in_and_cannot_be_reset_by_invitation(self):
        token = self.invite_token("other@example.com")
        self.client.logout()
        path = reverse("accept_invite", args=[token])
        self.assertContains(self.client.get(path), "Sign in to join")
        self.client.post(
            path, {"password1": "Attacker!123", "password2": "Attacker!123"}
        )
        self.assertFalse(
            Membership.objects.filter(
                user=self.outsider, workspace=self.business
            ).exists()
        )
        self.outsider.refresh_from_db()
        self.assertTrue(self.outsider.check_password(PASSWORD))
        self.client.force_login(self.outsider)
        self.assertRedirects(self.client.post(path), self.url("dashboard"))

    def test_wrong_signed_in_identity_cannot_accept_invite(self):
        token = self.invite_token()
        self.client.force_login(self.outsider)
        self.assertEqual(
            self.client.post(reverse("accept_invite", args=[token])).status_code, 403
        )
        self.assertFalse(
            Membership.objects.filter(
                workspace=self.business, user=self.outsider
            ).exists()
        )

    def test_expired_revoked_and_invalid_tokens_are_denied(self):
        token = self.invite_token()
        invitation = Invitation.objects.get()
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("accept_invite", args=[token])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("accept_invite", args=["bad-token"])).status_code,
            404,
        )
        token = self.invite_token("another@example.com")
        invitation = Invitation.objects.get(email="another@example.com")
        self.client.post(self.url("revoke_invite", invitation_id=invitation.pk))
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("accept_invite", args=[token])).status_code, 404
        )

    def test_replacement_invite_revokes_previous_link(self):
        first = self.invite_token()
        second = self.invite_token()
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("accept_invite", args=[first])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("accept_invite", args=[second])).status_code, 200
        )

    def test_cross_workspace_revoke_and_remove_denied(self):
        self.invite_token()
        invitation = Invitation.objects.get()
        self.client.force_login(self.outsider)
        self.assertEqual(
            self.client.post(
                self.url("revoke_invite", self.other, invitation_id=invitation.pk)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                self.url("remove_staff", self.other, member_id=self.staff_member.pk)
            ).status_code,
            404,
        )
        invitation.refresh_from_db()
        self.assertIsNone(invitation.revoked_at)
        self.staff_member.refresh_from_db()
        self.assertTrue(self.staff_member.is_active)

    def test_removal_revokes_active_session_and_admin_cannot_be_removed(self):
        staff_client = Client()
        staff_client.force_login(self.staff)
        self.assertEqual(staff_client.get(self.url("dashboard")).status_code, 200)
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(
                self.url("remove_staff", member_id=self.admin_member.pk)
            ).status_code,
            404,
        )
        self.client.post(self.url("remove_staff", member_id=self.staff_member.pk))
        self.assertEqual(staff_client.get(self.url("dashboard")).status_code, 404)
        self.assertEqual(
            staff_client.post(
                self.url("invite"), {"email": "new@example.com"}
            ).status_code,
            404,
        )

    def test_mutation_endpoints_require_post(self):
        self.client.force_login(self.owner)
        for path in [
            self.url("invite"),
            self.url("remove_staff", member_id=self.staff_member.pk),
        ]:
            self.assertEqual(self.client.get(path).status_code, 405)

    def test_csrf_protects_workspace_creation_and_invites(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(reverse("workspace_create"), {}).status_code, 403)
        self.assertEqual(
            client.post(self.url("invite"), {"email": "new@example.com"}).status_code,
            403,
        )

    def test_email_failure_is_honest_and_preserves_shareable_invitation(self):
        with patch("core.views.send_mail", side_effect=OSError("mail unavailable")):
            token = self.invite_token()
        self.assertTrue(
            Invitation.objects.filter(
                token_digest=hashlib.sha256(token.encode()).hexdigest()
            ).exists()
        )

    def test_reset_uses_trusted_domain_and_does_not_enumerate_accounts(self):
        self.client.post(
            reverse("password_reset"),
            {"email": "owner@example.com"},
            HTTP_HOST="testserver",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("http://testserver/password/reset/", mail.outbox[0].body)
        known = self.client.post(
            reverse("password_reset"), {"email": "owner@example.com"}
        )
        unknown = self.client.post(
            reverse("password_reset"), {"email": "missing@example.com"}
        )
        self.assertEqual(known.url, unknown.url)
        self.assertEqual(len(mail.outbox), 2)

    def test_password_reset_changes_password_and_invalidates_old_sessions(self):
        client = Client()
        client.force_login(self.owner)
        self.client.post(reverse("password_reset"), {"email": self.owner.email})
        link = re.search(r"http://testserver([^\s]+)", mail.outbox[0].body).group(1)
        response = self.client.get(link)
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            response.url,
            {
                "new_password1": "Replacement!Safe42",
                "new_password2": "Replacement!Safe42",
            },
        )
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("Replacement!Safe42"))
        self.assertEqual(client.get(self.url("dashboard")).status_code, 302)
        self.assertFalse(
            default_token_generator.check_token(
                self.owner, link.rstrip("/").rsplit("/", 1)[-1]
            )
        )

    def test_no_store_csp_and_same_origin_referrer_headers(self):
        response = self.client.get(reverse("login"))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("form-action 'self'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_login_does_not_redirect_to_external_next(self):
        response = self.client.post(
            reverse("login") + "?next=https://example.com",
            {"username": self.owner.email, "password": PASSWORD},
        )
        self.assertEqual(response.url, reverse("workspaces"))

    def test_browser_origin_with_valid_csrf_token_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        page = client.get(reverse("login"))
        token = re.search(
            r'name="csrfmiddlewaretoken" value="([^" ]+)"', page.content.decode()
        ).group(1)
        result = client.post(
            reverse("login"),
            {
                "username": self.owner.email,
                "password": PASSWORD,
                "csrfmiddlewaretoken": token,
            },
            HTTP_ORIGIN="http://testserver",
        )
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result["Referrer-Policy"], "same-origin")
