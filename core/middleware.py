class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "no-store, private"
        response["Referrer-Policy"] = "same-origin"
        response["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if getattr(request, "razorpay_checkout", False):
            response["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' https://checkout.razorpay.com https://cdn.razorpay.com; "
                "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
                "connect-src 'self' https://*.razorpay.com; img-src 'self' data: https://*.razorpay.com; "
                "font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
            )
        if getattr(request, "cashfree_checkout", False):
            # Cashfree's sandbox checkout opens in this tab and may post a
            # form to its own domain, so form-action allows it too.
            response["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' https://sdk.cashfree.com; "
                "frame-src https://sandbox.cashfree.com https://*.cashfree.com; "
                "connect-src 'self' https://sandbox.cashfree.com https://*.cashfree.com; "
                "img-src 'self' data: https://*.cashfree.com; font-src 'self'; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'; "
                "form-action 'self' https://sandbox.cashfree.com https://*.cashfree.com"
            )
        return response
