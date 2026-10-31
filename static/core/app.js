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

// Keep every destination available without scripting; collapse the menu on phones.
const workspaceMenu = document.querySelector('.workspace-menu');
if (workspaceMenu) {
  const smallScreen = window.matchMedia('(max-width: 960px)');
  const adaptMenu = () => { workspaceMenu.open = !smallScreen.matches; };
  adaptMenu();
  smallScreen.addEventListener('change', adaptMenu);
}

// Make password requirements visible before a failed submission.
const newPassword = document.querySelector('input[name="password1"], input[name="new_password1"]');
if (newPassword) {
  const checks = [
    ['At least 9 characters', value => value.length > 8],
    ['One uppercase letter', value => /[A-Z]/.test(value)],
    ['One lowercase letter', value => /[a-z]/.test(value)],
    ['One special character', value => /[^\w\s]/.test(value)],
  ];
  const list = document.createElement('ul');
  list.className = 'password-checklist';
  list.setAttribute('aria-label', 'Password requirements');
  checks.forEach(([label]) => { const li = document.createElement('li'); li.textContent = label; list.append(li); });
  newPassword.after(list);
  const update = () => checks.forEach(([, test], i) => list.children[i].classList.toggle('passed', test(newPassword.value)));
  newPassword.addEventListener('input', update);
  update();
}

// Let the decorative background rest while this tab is not visible.
const updateMeshMotion = () => document.body.classList.toggle('mesh-paused', document.hidden);
document.addEventListener('visibilitychange', updateMeshMotion);
updateMeshMotion();
