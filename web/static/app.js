// mmex_web entry form: show the fields that fit the type, fill the payee's
// usual category, quick dates and a tag filter. The form also works
// without this file; the server checks everything again.
(function () {
  'use strict';
  var form = document.querySelector('form.entry');
  var dataNode = document.getElementById('entry-data');
  if (!form || !dataNode) return;  // other forms borrow the .entry look only
  var data = JSON.parse(dataNode.textContent);
  var el = function (name) { return form.elements.namedItem(name); };

  function type() {
    var checked = form.querySelector('[name=type]:checked');
    return checked ? checked.value : el('type').value;
  }

  function showAll(selector, show) {
    form.querySelectorAll(selector).forEach(function (node) { node.hidden = !show; });
  }

  function refresh() {
    var t = type();
    var transfer = t === 'Transfer', twin = t === 'Twin';
    showAll('.only-transfer', transfer || twin);
    showAll('.only-payee', !transfer);
    showAll('.only-twin', twin);
    var a = el('account').value, b = el('to_account').value;
    showAll('.only-cross', transfer && a && b && data.currency[a] !== data.currency[b]);
    var payee = el('payee').value.trim().toLowerCase();
    showAll('.only-new-payee', !transfer && payee !== '' && !(payee in data.payees));
    form.querySelector('.account-label').textContent = transfer ? 'From account' : twin ? 'Paid from' : 'Account';
    form.querySelector('.notes-label').textContent = twin ? 'Notes for the expense row' : 'Notes';
    el('category').placeholder = transfer ? 'Transfer' : 'Start typing';
    if (twin) twinPreview();
  }

  // Twin: fill the transfer's notes from the pattern as the payee and
  // accounts change, unless the user has typed their own; then show what
  // the two rows will be.
  var autoNotes = null;
  function twinPreview() {
    var tw = data.twin;
    if (!tw) return;
    var name = function (field, blank) { var id = el(field).value; return id ? (data.names[id] || '…') : blank; };
    var payee = el('payee').value.trim();
    var to = name('to_account', ''), expense = name('expense_account', '');
    var notesBox = el('transfer_notes');
    if (payee && to && expense && (notesBox.value === '' || notesBox.value === autoNotes)) {
      autoNotes = tw.pattern.split('{payee}').join(payee).split('{expense_account}').join(expense)
                            .split('{to_account}').join(to);
      notesBox.value = autoNotes;
    }
    var box = form.querySelector('.twin-preview');
    if (!box) return;
    var amount = el('amount').value.trim() || '0.00';
    box.textContent = '1. Transfer ' + name('account', '<Paid from>') + ' → ' + (to || '<To account>') + ' ' + amount +
      (notesBox.value ? ', notes "' + notesBox.value + '"' : '') + '.  2. Withdrawal in ' + (expense || '<Expense account>') +
      ': ' + (payee || '<payee>') + (el('category').value ? ', ' + el('category').value : '') + ' ' + amount + '.';
  }

  // Fill the category from the payee's last use, unless the user typed one.
  var autoCategory = null;
  function payeeChosen() {
    var cat = data.payees[el('payee').value.trim().toLowerCase()];
    var current = el('category').value;
    if (cat && (current === '' || current === autoCategory)) {
      el('category').value = cat;
      autoCategory = cat;
    }
  }

  // Leaving Transfer: its "Transfer" category rarely fits a payee.
  var lastType = type();
  form.addEventListener('change', function (event) {
    if (event.target.name === 'type') {
      var now = type();
      if (lastType === 'Transfer' && now !== 'Transfer' && el('category').value === 'Transfer') {
        el('category').value = '';
      }
      // Twin: preselect the To and expense accounts used last time (usually the same).
      if (now === 'Twin' && data.twin) {
        if (!el('to_account').value && data.twin.last_to) el('to_account').value = String(data.twin.last_to);
        if (!el('expense_account').value && data.twin.last_expense) el('expense_account').value = String(data.twin.last_expense);
      }
      lastType = now;
    }
  });
  form.addEventListener('change', refresh);
  form.addEventListener('input', refresh);
  el('payee').addEventListener('change', payeeChosen);
  el('payee').addEventListener('input', function () {
    // A datalist pick fires input with the full name.
    if (el('payee').value.trim().toLowerCase() in data.payees) payeeChosen();
  });

  form.querySelectorAll('[data-days]').forEach(function (button) {
    button.addEventListener('click', function () {
      var d = new Date();
      d.setDate(d.getDate() + Number(button.dataset.days));
      var pad = function (n) { return String(n).padStart(2, '0'); };
      el('date').value = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    });
  });

  var filter = form.querySelector('.tagfilter');
  if (filter) {
    filter.addEventListener('input', function () {
      var q = filter.value.trim().toLowerCase();
      form.querySelectorAll('.chipbox').forEach(function (chip) {
        var input = chip.querySelector('input');
        chip.hidden = q !== '' && !input.checked && input.value.toLowerCase().indexOf(q) < 0;
      });
    });
  }

  // Stop a double tap from saving twice.
  form.addEventListener('submit', function (event) {
    if (form.dataset.sent) { event.preventDefault(); return; }
    form.dataset.sent = '1';
  });

  // A page restored from the back/forward cache keeps that mark.
  window.addEventListener('pageshow', function () { delete form.dataset.sent; });

  refresh();
})();

// Accounts page: collapsible type groups, remembered per device. "Expand
// all" opens the groups but leaves each "Others" (hidden accounts) shut;
// "Collapse all" shuts both.
(function () {
  'use strict';
  var groups = Array.prototype.slice.call(document.querySelectorAll('details.group'));
  var button = document.getElementById('toggle-all');
  if (!groups.length || !button) return;
  var KEY = 'mmex_web.collapsed_groups';

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { return []; }
  }
  function save() {
    var shut = groups.filter(function (g) { return !g.open; }).map(function (g) { return g.dataset.group; });
    try { localStorage.setItem(KEY, JSON.stringify(shut)); } catch (e) { /* private mode: not remembered */ }
  }
  function label() {
    button.textContent = groups.every(function (g) { return g.open; }) ? 'Collapse all' : 'Expand all';
  }

  var shut = load();
  groups.forEach(function (g) {
    if (shut.indexOf(g.dataset.group) >= 0) g.open = false;
    g.addEventListener('toggle', function () { save(); label(); });
  });
  button.hidden = false;
  button.addEventListener('click', function () {
    var expand = button.textContent === 'Expand all';
    groups.forEach(function (g) { g.open = expand; });
    if (!expand) {
      document.querySelectorAll('details.others').forEach(function (o) { o.open = false; });
    }
    save();
    label();
  });
  label();
})();

// The eye in the top bar: hide or show balances at once, and save the
// choice (so phone and PC agree). Without this script the button's form
// posts and the page reloads.
(function () {
  'use strict';
  var form = document.getElementById('eye-form');
  if (!form || !window.fetch) return;
  var button = form.querySelector('button.eye');
  var root = document.documentElement;

  function show(hidden) {
    root.classList.toggle('masked', hidden);
    form.elements.namedItem('hide').value = hidden ? '0' : '1';
    var label = hidden ? 'Show balances' : 'Hide balances';
    button.setAttribute('aria-pressed', hidden ? 'true' : 'false');
    button.title = label;
    button.setAttribute('aria-label', label);
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var hide = form.elements.namedItem('hide').value === '1';
    show(hide);                                   // at once, then save
    var body = new FormData(form);
    body.set('hide', hide ? '1' : '0');
    fetch(form.action, {method: 'POST', body: body, credentials: 'same-origin',
                        headers: {'X-Requested-With': 'fetch'}})
      .then(function (r) { if (!r.ok) throw new Error(r.status); })
      .catch(function () {
        show(!hide);                              // not saved: put it back
        button.classList.add('failed');
        button.title = 'Could not save. Reload the page and try again.';
        setTimeout(function () { button.classList.remove('failed'); }, 3000);
      });
  });
})();

// Rates list: Download fills that row in place, so the page doesn't reload
// and jump to the row. Without this script the form posts and the page
// reloads at the row (#d<date>).
(function () {
  'use strict';
  var table = document.querySelector('table.rates');
  if (!table || !window.fetch) return;

  function note(cell, text, error, title) {
    var span = document.createElement('span');
    span.className = 'rowmsg' + (error ? ' err' : '');
    span.textContent = text;
    if (title) span.title = title;
    cell.appendChild(span);
  }

  table.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.classList.contains('rate-download')) return;
    event.preventDefault();
    var button = form.querySelector('button');
    if (button.disabled) return;
    var cell = form.parentNode, row = form.closest('tr');
    cell.querySelectorAll('.rowmsg').forEach(function (n) { n.remove(); });
    button.disabled = true;
    button.textContent = button.textContent === 'Fill' ? 'Filling…' : 'Downloading…';
    var body = new FormData(form);
    body.set('action', 'download');                 // the clicked button isn't in FormData
    // getAttribute: the form's button is named "action", which shadows form.action.
    fetch(form.getAttribute('action'), {method: 'POST', body: body, credentials: 'same-origin',
                        headers: {'X-Requested-With': 'fetch'}})
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (res) {
        Object.keys(res.values).forEach(function (cid) {
          var td = row.querySelector('td[data-cid="' + cid + '"]');
          if (!td) return;
          var v = res.values[cid], span = document.createElement('span');
          span.title = v.type + ': ' + v.value;
          span.textContent = v.shown;
          td.replaceChildren(span);
        });
        var text = res.messages.map(function (m) { return m.text; }).join(' ');
        var failed = !res.ok;
        if (res.missing || failed) {
          button.disabled = false;
          button.textContent = failed ? 'Retry' : 'Download';
        } else {
          form.remove();
        }
        note(cell, failed ? 'Failed' : (res.messages.some(function (m) { return m.level === 'error'; }) ? 'Partly' : 'Saved'),
             failed || res.missing, text);
      })
      .catch(function (err) {
        button.disabled = false;
        button.textContent = 'Retry';
        note(cell, 'Failed', true, 'Not saved (' + err.message + '). Reload the page and try again.');
      });
  });
})();

// Light/dark button: switch at once, then save it for this device (a
// cookie, so the next page is drawn in it). Before the first choice the
// page follows the system setting. Without this script the form posts.
(function () {
  'use strict';
  var form = document.getElementById('theme-form');
  if (!form || !window.fetch) return;
  var root = document.documentElement;
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var now = root.dataset.theme ||
      (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    var next = now === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    var body = new FormData(form);
    body.set('theme', next);
    fetch(form.getAttribute('action'), {method: 'POST', body: body, credentials: 'same-origin',
                                        headers: {'X-Requested-With': 'fetch'}})
      .catch(function () { /* shown now; it just won't be remembered */ });
  });
})();

// Flag buttons on transaction rows: one tap moves the row to the next
// status (unflagged -> R -> F -> unflagged) without reloading, and the
// register's totals follow. Taps close together are one History entry.
// Without this script each tap is a plain post that reloads the page.
(function () {
  'use strict';
  if (!window.fetch) return;
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.classList || !form.classList.contains('flagform')) return;
    event.preventDefault();
    var button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    fetch(form.getAttribute('action'), {method: 'POST', body: new FormData(form), credentials: 'same-origin',
                                        headers: {'X-Requested-With': 'fetch'}})
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (res) {
        var b = res.button;
        if (['', 'R', 'F'].indexOf(b.status) < 0) {
          // Now V or similar (changed elsewhere): not tappable any more.
          var chip = document.createElement('span');
          chip.className = 'flag ' + b.cls;
          chip.textContent = b.label;
          form.replaceWith(chip);
        } else {
          form.querySelector('input[name="status"]').value = b.status;
          button.className = 'flag ' + b.cls;
          button.textContent = b.label;
          button.title = b.title;
          button.setAttribute('aria-label', 'Status: ' + b.title);
          button.disabled = false;
        }
        Object.keys(res.totals || {}).forEach(function (key) {
          var el = document.querySelector('[data-total="' + key + '"]');
          if (!el) return;
          var v = el.querySelector('.v') || el;
          v.textContent = res.totals[key].text;
          el.classList.toggle('neg', key !== 'unreconciled' && res.totals[key].neg);
        });
        if (res.error) window.alert(res.error);
      })
      .catch(function (err) {
        button.disabled = false;
        button.title = 'Not changed (' + err.message + '). Tap to try again, or reload the page.';
      });
  });
})();

// Queue review: filter chips show one kind of entry (twins, transfers, own
// rows, changes); balance checks always stay, as the timeline's dividers.
(function () {
  'use strict';
  var list = document.querySelector('.rv-list');
  var chips = document.querySelectorAll('.rvchip');
  if (!list || !chips.length) return;
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      var want = chip.dataset.filter;
      chips.forEach(function (c) { c.setAttribute('aria-pressed', String(c === chip)); });
      list.querySelectorAll('li[data-filter]').forEach(function (li) {
        var kind = li.dataset.filter;
        li.hidden = !(want === 'all' || kind === 'checks' || kind === want);
      });
    });
  });
})();

// Report tables: tap a group's name (an account type, a top category) to
// show or hide its rows. Tables marked data-start-shut start collapsed;
// without this script every row shows.
(function () {
  'use strict';
  var tables = document.querySelectorAll('table[data-groups]');
  if (!tables.length) return;
  var all = document.querySelector('[data-expand-all]');
  function set(tbody, open) {
    tbody.classList.toggle('shut', !open);
    var b = tbody.querySelector('.gtoggle');
    if (b) b.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  function label() {
    if (!all) return;
    var shut = document.querySelector('table[data-groups] tbody.grp.shut');
    all.textContent = shut ? 'Expand all' : 'Collapse all';
  }
  tables.forEach(function (t) {
    var start = t.hasAttribute('data-start-shut');
    t.querySelectorAll('tbody.grp').forEach(function (tb) {
      if (start && tb.querySelector('tr.sub')) set(tb, false);
    });
    t.addEventListener('click', function (ev) {
      var b = ev.target.closest('.gtoggle');
      if (!b) return;
      var tb = b.closest('tbody');
      set(tb, tb.classList.contains('shut'));
      label();
    });
  });
  if (all) {
    all.hidden = false;
    all.addEventListener('click', function () {
      var open = all.textContent === 'Expand all';
      document.querySelectorAll('table[data-groups] tbody.grp').forEach(function (tb) { set(tb, open); });
      label();
    });
    label();
  }
})();
