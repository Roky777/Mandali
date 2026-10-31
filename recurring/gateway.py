from urllib.parse import urlencode
from checkout.gateways import RazorpayTest, GatewayError, identifier
from checkout.crypto import CheckoutError


class RecurringGateway(RazorpayTest):
    def __init__(self, account):
        if account.provider != "razorpay":
            raise CheckoutError(
                "Recurring collection requires a Razorpay Test account. One-time gateway routing cannot move a mandate."
            )
        super().__init__(account)

    def create_plan(self, mandate):
        return self.request(
            "POST",
            "plans",
            {
                "period": mandate.schedule.period,
                "interval": 1,
                "item": {
                    "name": f"Mandali {mandate.schedule.subscription.number}",
                    "amount": mandate.amount_paise,
                    "currency": "INR",
                },
                "notes": {"mandali_mandate": str(mandate.pk)},
            },
        )

    def fetch_plan(self, plan_id):
        return self.request("GET", "plans/" + identifier(plan_id, "plan"))

    def create_subscription(self, mandate):
        return self.request(
            "POST",
            "subscriptions",
            {
                "plan_id": mandate.plan_id,
                "quantity": 1,
                "total_count": mandate.total_count,
                "start_at": mandate.start_at,
                "expire_by": mandate.start_at,
                "customer_notify": True,
                "notes": {"mandali_mandate": str(mandate.pk)},
            },
        )

    def fetch_subscription(self, subscription_id):
        return self.request(
            "GET", "subscriptions/" + identifier(subscription_id, "sub")
        )

    def change(self, mandate, action):
        endpoint, payload = {
            "pause": ("pause", {"pause_at": "now"}),
            "resume": ("resume", {"resume_at": "now"}),
            "cancel": ("cancel", {"cancel_at_cycle_end": False}),
        }[action]
        return self.request(
            "POST",
            "subscriptions/"
            + identifier(mandate.subscription_id, "sub")
            + "/"
            + endpoint,
            payload,
        )

    def invoices(self, subscription_id):
        subscription_id = identifier(subscription_id, "sub")
        rows = []
        for skip in range(0, 160, 20):
            result = self.request(
                "GET",
                "invoices?"
                + urlencode(
                    {"subscription_id": subscription_id, "count": 20, "skip": skip}
                ),
            )
            items = result.get("items")
            if (
                not isinstance(items, list)
                or len(items) > 20
                or any(not isinstance(i, dict) for i in items)
            ):
                raise GatewayError("Provider invoice history is invalid.")
            rows.extend(items)
            if len(items) < 20:
                return rows
        raise GatewayError(
            "Provider history exceeds the reconciliation limit. Staff review is required."
        )
