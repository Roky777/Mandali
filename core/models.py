import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.db.models.functions import Lower


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=email.strip().lower(), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def get_by_natural_key(self, username):
        return self.get(email__iexact=username.strip())

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.update(is_staff=True, is_superuser=True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="unique_lower_user_email")
        ]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    legal_name = models.CharField(max_length=180)
    billing_email = models.EmailField()
    address = models.TextField(max_length=500)
    gstin = models.CharField("GSTIN", max_length=15, blank=True)
    website = models.URLField(blank=True)
    use_api = models.BooleanField(default=False)
    use_hosted = models.BooleanField(default=True)
    use_manual = models.BooleanField(default=True)
    currency = models.CharField(max_length=3, default="INR", editable=False)
    timezone = models.CharField(max_length=40, default="Asia/Kolkata", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)


class Membership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        STAFF = "staff", "Staff"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"], name="unique_workspace_user"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=["admin", "staff"]),
                name="valid_membership_role",
            ),
        ]


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    email = models.EmailField()
    token_digest = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Activity(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    is_system = models.BooleanField(default=False)
    # Who acted when it was not a team member, for example "Customer (portal)".
    actor_label = models.CharField(max_length=40, blank=True)
    action = models.CharField(max_length=80)
    detail = models.CharField(max_length=220, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class AuthRateBucket(models.Model):
    key = models.CharField(max_length=64, unique=True)
    window = models.BigIntegerField()
    attempts = models.PositiveIntegerField(default=0)
    # Lets old buckets be pruned without knowing each scope's window length.
    touched_at = models.DateTimeField(default=timezone.now)
