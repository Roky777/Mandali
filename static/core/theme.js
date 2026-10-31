/* Apply before styles paint; all scripting stays compatible with our CSP. */
(() => {
  const key = 'mandali-theme';
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  const valid = value => ['light', 'dark', 'system'].includes(value);
  let preference = 'system';
  try { const saved = localStorage.getItem(key); if (valid(saved)) preference = saved; } catch (_) {}
  function apply() {
    document.documentElement.dataset.theme = preference === 'system' ? (system.matches ? 'dark' : 'light') : preference;
    document.querySelectorAll('[data-theme-picker]').forEach(picker => { picker.value = preference; });
    document.dispatchEvent(new Event('mandali:themechange'));
  }
  apply();
  system.addEventListener('change', () => { if (preference === 'system') apply(); });
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    preference = valid(event.newValue) ? event.newValue : 'system';
    apply();
  });
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme-picker]').forEach(picker => {
      picker.hidden = false;
      picker.value = preference;
      picker.addEventListener('change', () => {
        preference = valid(picker.value) ? picker.value : 'system';
        try { localStorage.setItem(key, preference); } catch (_) {}
        apply();
      });
    });
  });
})();
