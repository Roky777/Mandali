document.querySelectorAll('form').forEach((form) => {
  form.addEventListener('submit', (event) => {
    const button = event.submitter;
    if (button?.dataset.confirm && !window.confirm(button.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (button) {
      button.setAttribute('aria-busy', 'true');
      button.dataset.originalLabel = button.textContent;
      button.textContent = 'Please wait…';
      button.disabled = true;
    }
  });
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('button[aria-busy]').forEach((button) => {
    button.textContent = button.dataset.originalLabel;
    button.disabled = false;
    button.removeAttribute('aria-busy');
  });
});
document.querySelector('[aria-invalid="true"]')?.focus();
document.querySelectorAll('[data-print]').forEach((button) => {
  button.addEventListener('click', () => window.print());
});
