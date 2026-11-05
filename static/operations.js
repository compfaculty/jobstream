// Never reload while hidden or while an operator is editing history filters.
(() => {
  let editing = false;
  document.querySelectorAll('form select, form input').forEach((field) => {
    field.addEventListener('change', () => { editing = true; });
  });
  window.setInterval(() => {
    if (!document.hidden && !editing && !document.querySelector('form :focus')) {
      window.location.reload();
    }
  }, 10000);
})();
