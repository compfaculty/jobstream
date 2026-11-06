'use strict';
for (const event of ['htmx:responseError', 'htmx:sendError', 'htmx:timeout']) {
  document.addEventListener(event, () => {
    const alert = document.getElementById('request-error');
    if (alert) {
      alert.textContent = 'The request failed. Check your connection and try again.';
      alert.hidden = false;
    }
  });
}
document.addEventListener('htmx:afterRequest', event => {
  if (event.detail.successful) {
    const alert = document.getElementById('request-error');
    if (alert) alert.hidden = true;
  }
});

// Progressive enhancement: the native company select remains usable without JavaScript.
for (const select of document.querySelectorAll('[data-company-picker]')) {
  const container = select.parentElement;
  const input = document.createElement('input');
  const list = document.createElement('ul');
  const options = Array.from(select.options, option => ({ value: option.value, text: option.text }));
  input.id = `${select.id}-search`;
  input.type = 'search';
  input.placeholder = 'Search companies';
  input.autocomplete = 'off';
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-expanded', 'false');
  list.id = `${select.id}-options`;
  list.className = 'company-options';
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'Companies');
  list.hidden = true;
  input.setAttribute('aria-controls', list.id);
  container.querySelector('label').htmlFor = input.id;
  input.value = select.value ? select.selectedOptions[0].text : '';
  select.hidden = true;
  container.append(input, list);
  let visible = [];
  let active = -1;
  const close = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  };
  const choose = option => {
    select.value = option.value;
    input.value = option.value ? option.text : '';
    input.setCustomValidity('');
    select.dispatchEvent(new Event('change', { bubbles: true }));
    close();
  };
  const highlight = () => {
    Array.from(list.children).forEach((item, index) => {
      item.setAttribute('aria-selected', String(index === active));
    });
    if (active >= 0) {
      const item = list.children[active];
      input.setAttribute('aria-activedescendant', item.id);
      item.scrollIntoView({ block: 'nearest' });
    } else input.removeAttribute('aria-activedescendant');
  };
  const show = () => {
    const term = select.value ? '' : input.value.toLocaleLowerCase().trim();
    visible = options.filter(option => !option.value || option.text.toLocaleLowerCase().includes(term)).slice(0, 30);
    list.replaceChildren();
    active = -1;
    for (const [index, option] of visible.entries()) {
      const item = document.createElement('li');
      item.id = `${list.id}-${index}`;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', 'false');
      item.textContent = option.text;
      item.addEventListener('pointerdown', event => event.preventDefault());
      item.addEventListener('click', () => choose(option));
      list.append(item);
    }
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    input.removeAttribute('aria-activedescendant');
  };
  input.addEventListener('focus', show);
  input.addEventListener('click', show);
  input.addEventListener('input', () => {
    select.value = '';
    input.setCustomValidity(input.value ? 'Choose a company from the suggestions, or clear this field.' : '');
    show();
  });
  input.addEventListener('blur', close);
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      if (!select.value) { input.value = ''; input.setCustomValidity(''); }
      close();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.hidden) show();
      active = active < 0
        ? (event.key === 'ArrowDown' ? 0 : visible.length - 1)
        : (active + (event.key === 'ArrowDown' ? 1 : -1) + visible.length) % visible.length;
      highlight();
    } else if (event.key === 'Enter' && !list.hidden && active >= 0) {
      event.preventDefault();
      choose(visible[active]);
    }
  });
}

// Preserve ready-item selections across progress polls; never reselect a disabled item.
let batchSelection = [];
let batchFocus = null;
document.addEventListener('htmx:beforeSwap', event => {
  if (event.detail.target?.id !== 'batch-items') return;
  batchSelection = Array.from(event.detail.target.querySelectorAll('input[name="items"]:checked'), input => input.value);
  const focused = document.activeElement;
  batchFocus = focused?.name === 'items' ? focused.value : null;
});
document.addEventListener('htmx:afterSwap', event => {
  if (event.detail.target?.id !== 'batch-items') return;
  const root = document.getElementById('batch-items');
  if (!root) return;
  for (const input of root.querySelectorAll('input[name="items"]')) {
    if (!input.disabled && batchSelection.includes(input.value)) input.checked = true;
    if (input.value === batchFocus) input.focus({ preventScroll: true });
  }
});

// Preserve the drawer DOM during result swaps so navigation never flashes or loses focus.
const categoryPanel = document.getElementById('category-panel');
const categoryToggle = document.getElementById('category-toggle');
if (categoryPanel && categoryToggle) {
  const closeButton = document.getElementById('category-close');
  const backdrop = document.querySelector('.category-backdrop');
  document.body.classList.add('category-enhanced');
  categoryToggle.hidden = false;
  closeButton.hidden = false;
  categoryPanel.inert = true;
  const setOpen = open => {
    document.body.classList.toggle('category-open', open);
    categoryToggle.setAttribute('aria-expanded', String(open));
    categoryPanel.inert = !open;
    backdrop.hidden = !open;
    if (open) closeButton.focus({ preventScroll: true });
    else categoryToggle.focus({ preventScroll: true });
  };
  categoryToggle.addEventListener('click', () => setOpen(true));
  closeButton.addEventListener('click', () => setOpen(false));
  backdrop.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', event => {
    if (!document.body.classList.contains('category-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false); }
    if (event.key !== 'Tab') return;
    const controls = Array.from(categoryPanel.querySelectorAll('button, a[href]'));
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });
  document.addEventListener('htmx:afterSwap', event => {
    if (event.detail.target?.id !== 'results') return;
    const selected = document.querySelector('input[name="category"][form="job-search"]').value;
    for (const link of categoryPanel.querySelectorAll('a')) {
      const slug = new URL(link.href).searchParams.get('category');
      const url = new URL(location.href);
      url.searchParams.set('category', slug);
      url.searchParams.delete('page');
      const href = `${url.pathname}${url.search}`;
      link.setAttribute('href', href);
      link.setAttribute('hx-get', href);
      if (slug === selected) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    // Reprocess changed HTMX URLs on the preserved links.
    htmx.process(categoryPanel);
  });
}

// Django formset indices remain stable, including entries marked for deletion.
for (const group of document.querySelectorAll('[data-profile-group]')) {
  const button = group.querySelector('[data-profile-add]');
  const total = group.querySelector('input[name$="-TOTAL_FORMS"]');
  const maximum = Number(group.querySelector('input[name$="-MAX_NUM_FORMS"]').value);
  button.hidden = false;
  button.disabled = Number(total.value) >= maximum;
  button.addEventListener('click', () => {
    const index = Number(total.value);
    if (index >= maximum) return;
    const template = group.querySelector('[data-profile-template]');
    const html = template.innerHTML.replaceAll('__prefix__', String(index));
    group.querySelector('[data-profile-entries]').insertAdjacentHTML('beforeend', html);
    total.value = index + 1;
    button.disabled = index + 1 >= maximum;
    group.querySelector('[data-profile-entries]').lastElementChild.querySelector('input').focus();
  });
}
