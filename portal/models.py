from django.db import models
from billing.models import Customer


class PortalToken(models.Model):
    """A single-use sign-in link for one customer record. Only a digest is stored."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
