const button = document.getElementById('pay-button');
const feedback = document.getElementById('payment-feedback');
const form = document.getElementById('verify-payment');
button?.addEventListener('click', () => {
  if (typeof window.Razorpay !== 'function') {
    feedback.textContent = 'Razorpay Test could not load. Check your connection and try again.';
    return;
  }
  const options = JSON.parse(document.getElementById('gateway-options').textContent);
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
});
