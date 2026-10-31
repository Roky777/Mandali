const button = document.getElementById('pay-button');
const feedback = document.getElementById('payment-feedback');
const form = document.getElementById('verify-payment');
const statusForm = document.getElementById('status-form');
const optionsNode = document.getElementById('gateway-options');
const options = optionsNode ? JSON.parse(optionsNode.textContent) : null;

function openRazorpay() {
  if (typeof window.Razorpay !== 'function') {
    feedback.textContent = 'Razorpay Test could not load. Check your connection and try again.';
    return;
  }
  options.handler = (result) => {
    feedback.textContent = 'Verifying the payment on the server…';
    button.disabled = true;
    for (const key of ['razorpay_payment_id', 'razorpay_order_id', 'razorpay_signature']) {
      form.elements[key].value = result[key] || '';
    }
    form.requestSubmit();
  };
  options.modal = { ondismiss: () => { feedback.textContent = 'Checkout closed. If a test payment was submitted, check its status before trying again.'; } };
  const checkout = new window.Razorpay(options);
  checkout.on('payment.failed', () => { feedback.textContent = 'The gateway reported a failed attempt. You can retry in the same checkout. This page has not recorded a payment.'; });
  checkout.open();
}

function openCashfree() {
  if (typeof window.Cashfree !== 'function') {
    feedback.textContent = 'Cashfree Sandbox could not load. Check your connection and try again.';
    return;
  }
  // Cashfree returns to this page afterwards; the server then fetches the
  // payment from Cashfree. Nothing from the browser is trusted as proof.
  window.Cashfree({ mode: options.mode }).checkout({
    paymentSessionId: options.payment_session_id,
    redirectTarget: '_self',
  });
}

button?.addEventListener('click', () => {
  if (options?.provider === 'cashfree') openCashfree();
  else openRazorpay();
});

if (statusForm && new URLSearchParams(window.location.search).has('returned')) {
  if (feedback) feedback.textContent = 'Checking the payment with the gateway…';
  statusForm.requestSubmit();
}
