"""Pages that change the MMEX DB: add, edit and delete a transaction, and
the history of the app's writes with undo.

Every write goes through Writer (writer.py). An edit or delete carries the
row as the form saw it (`orig`); the engine refuses the write if the row
has changed since (ExpectError), so a stale form never overwrites a newer
edit.
"""

import datetime
import json
import logging
import re

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from engine import views

from .auth import _safe_next, login_required
from .pages import db

bp = Blueprint('entry', __name__)
log = logging.getLogger(__name__)
TYPES = ('Withdrawal', 'Deposit', 'Transfer')
# Twin: one purchase entered as two rows (see _new_twin). Add only; once
# saved, each row is an ordinary transaction.
TWIN = 'Twin'
ADD_TYPES = TYPES + (TWIN,)
ADD_STATUSES = [('', 'Unflagged'), ('F', 'Follow up (F)'), ('R', 'Reconciled (R)')]
EDIT_STATUSES = ADD_STATUSES + [('V', 'Void (V)')]
EXPECT_FIELDS = ('date', 'type', 'account', 'amount', 'category', 'notes', 'status', 'number', 'tags',
                 'payee', 'to_account', 'to_amount')
HISTORY_PAGE = 50


class FormError(Exception):
    pass


def writer():
    return current_app.config['MMEX_WRITER']


def _require_writable(d):
    reason = d.write_block
    if reason:
        abort(403, reason)


def _amount(text, label) -> float:
    cleaned = (text or '').replace(',', '').replace(' ', '').strip()
    if not re.fullmatch(r'\d+(\.\d{1,2})?|\.\d{1,2}', cleaned):
        raise FormError(f'{label}: enter a positive amount with up to 2 decimals, e.g. 12.50')
    value = round(float(cleaned), 2)
    if value <= 0:
        raise FormError(f'{label} must be more than zero')
    return value


def _date(text) -> str:
    try:
        return datetime.date.fromisoformat((text or '').strip()).isoformat()
    except ValueError:
        raise FormError('Date: pick a date')


def _account_name(d, value, label) -> str:
    try:
        account_id = int(value)
    except (TypeError, ValueError):
        raise FormError(f'{label}: pick an account')
    if account_id not in d.ref.accounts:
        raise FormError(f'{label}: pick an account')
    return d.ref.accounts[account_id]['name']


def _tags(form):
    """(names, any new) from the ticked tags plus the "new tags" box."""
    names = [t for t in form.getlist('tags') if t]
    new = [t for t in re.split(r'[,\s]+', form.get('new_tags', '')) if t]
    seen, out = set(), []
    for t in names + new:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out, bool(new)


def form_values(form, trans_type=None) -> dict:
    """The form as typed, for re-rendering it after an error."""
    names, _ = _tags(form)
    return {
        'type': trans_type or form.get('type', 'Withdrawal'),
        'account_id': form.get('account', ''), 'to_account_id': form.get('to_account', ''),
        'expense_account_id': form.get('expense_account', ''), 'transfer_notes': form.get('transfer_notes', ''),
        'payee': form.get('payee', '').strip(), 'create_payee': bool(form.get('create_payee')),
        'category': form.get('category', '').strip(),
        'amount': form.get('amount', '').strip(), 'to_amount': form.get('to_amount', '').strip(),
        'date': form.get('date', '').strip(), 'notes': form.get('notes', ''),
        'number': form.get('number', '').strip(), 'status': form.get('status', ''),
        'tags': names, 'new_tags': form.get('new_tags', ''),
    }


def build_fields(d, form, trans_type) -> dict:
    """Plan-level fields from the form (what an add op takes), plus
    '_create_tags'. Raises FormError for anything the user must fix."""
    if trans_type not in TYPES:
        raise FormError('Pick Withdrawal, Deposit or Transfer')
    f = {'type': trans_type, 'date': _date(form.get('date')),
         'account': _account_name(d, form.get('account'), 'Account'),
         'amount': _amount(form.get('amount'), 'Amount'),
         'notes': form.get('notes', '').replace('\r\n', '\n').strip(),
         'number': form.get('number', '').strip(), 'status': form.get('status', '')}
    if trans_type == 'Transfer':
        f['to_account'] = _account_name(d, form.get('to_account'), 'To account')
        if f['to_account'] == f['account']:
            raise FormError('A transfer needs two different accounts')
        f['category'] = form.get('category', '').strip() or 'Transfer'
        a = d.ref.account(f['account'], 'Account', allow_closed=True)
        b = d.ref.account(f['to_account'], 'To account', allow_closed=True)
        if a['currency'] != b['currency']:
            f['to_amount'] = _amount(form.get('to_amount'), 'Amount received')
    else:
        f['payee'] = form.get('payee', '').strip()
        if not f['payee']:
            raise FormError('Payee: enter a payee')
        f['category'] = form.get('category', '').strip()
        if not f['category']:
            raise FormError('Category: pick a category')
    f['tags'], f['_create_tags'] = _tags(form)
    return f


def _engine_error(text) -> str:
    # "op 1 (add): no payee ..." -> "no payee ...", capitalised.
    text = re.sub(r'^op \d+ \(\w+\): ', '', text or 'The write was refused.')
    text = text.replace(' (set "create_payee": true to add it)', ' (tick "Create new payee" to add it)')
    text = text.replace(' (set "create_tags": true to add it)', '')
    text = re.sub(r' Set "allow_duplicate": true if it really is a second one\.$', '', text)
    return text[:1].upper() + text[1:]


def _flash_result(result):
    for e in result.events:
        if e['kind'] == 'warning':
            flash(f'Note: {e["text"]}.', 'info')
    for w in result.warnings:
        flash(f'Warning: {w}.', 'info')


def _picker_context(d, values):
    data = views.picker(d.conn, d.ref)
    accounts = list(data['accounts'])
    # An edited row may sit in a closed account; keep it pickable.
    for key in ('account_id', 'to_account_id'):
        try:
            aid = int(values.get(key) or 0)
        except ValueError:
            aid = 0
        if aid in d.ref.accounts and all(a['id'] != aid for a in accounts):
            a = d.ref.accounts[aid]
            accounts.append({'id': aid, 'name': f'{a["name"]} ({a["status"]})', 'currency': a['currency'],
                             'type': 'Closed', 'favorite': False})
    groups = {}
    favorites = [a for a in accounts if a['favorite']]
    if favorites:
        groups['Favourites'] = favorites
    for a in accounts:
        if not a['favorite']:
            groups.setdefault(a['type'], []).append(a)
    script = {'currency': {str(a['id']): a['currency'] for a in accounts},
              'payees': {p['name'].lower(): p['category'] for p in data['payees']}}
    tags = list(data['tags'])
    for t in values.get('tags', []):
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    twin = twin_settings(d)
    script['names'] = {str(a['id']): a['name'] for a in accounts}
    script['twin'] = {'pattern': twin['pattern'], 'last_to': twin['to_id'], 'last_expense': twin['expense_id']}
    return {'account_groups': groups, 'payees': data['payees'], 'categories': data['categories'],
            'tags': tags, 'script_data': script, 'last_account': data['last_account'], 'twin': twin}


def twin_settings(d) -> dict:
    """The Twin type's notes pattern and the To / expense accounts used last."""
    prefs = current_app.config['MMEX_STORE'].prefs()
    known = lambda i: i if i in d.ref.accounts else None
    return {'pattern': prefs['twin_notes'] or '{payee}, {expense_account} -> {to_account}',
            'to_id': known(prefs['twin_to_account']), 'expense_id': known(prefs['twin_expense_account'])}


def twin_notes(pattern, payee, expense_account, to_account) -> str:
    # Plain replace, not str.format: a payee name may hold braces.
    return (pattern.replace('{payee}', payee).replace('{expense_account}', expense_account)
            .replace('{to_account}', to_account))


def _render(d, mode, values, error=None, duplicates=None, trans=None, status=200, notice=None):
    ctx = _picker_context(d, values)
    return render_template('entry.html', mode=mode, v=values, error=error, notice=notice, duplicates=duplicates, trans=trans,
                           statuses=EDIT_STATUSES if mode == 'edit' else ADD_STATUSES,
                           types=TYPES if mode == 'edit' else ADD_TYPES,
                           next=request.values.get('next', ''), banners=d.banners, **ctx), status


def _after_save(d, fields, next_):
    if request.form.get('save_new'):
        account_id = d.ref.account(fields['account'], 'a', allow_closed=True)['id']
        return redirect(url_for('entry.new', account=account_id, type=fields['type'], date=fields['date'],
                                next=next_ or None))
    if next_:
        return redirect(_safe_next(next_))
    return redirect(url_for('pages.register', account_id=d.ref.account(fields['account'], 'a', allow_closed=True)['id']))


def _saved_message(write_id, text):
    flash(f'{text}' + ('' if write_id else ' (but it couldn\'t be added to History, so it can\'t be undone here).'),
          'info' if write_id else 'error')


@bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    d = db()
    _require_writable(d)
    if request.method == 'GET':
        trans_type = request.args.get('type', 'Withdrawal')
        account = request.args.get('account', type=int)
        values = form_values(MultiDict(), trans_type if trans_type in ADD_TYPES else 'Withdrawal')
        if _copy_of(d, values):
            return _render(d, 'add', values)
        values['date'] = request.args.get('date') or datetime.date.today().isoformat()
        values['account_id'] = str(account) if account in d.ref.accounts else ''
        if not values['account_id']:
            last = views.picker(d.conn, d.ref)['last_account']
            values['account_id'] = str(last) if last else ''
        if values['type'] == TWIN:
            twin = twin_settings(d)
            values['to_account_id'] = str(twin['to_id'] or '')
            values['expense_account_id'] = str(twin['expense_id'] or '')
        return _render(d, 'add', values)

    trans_type = request.form.get('type', '')
    values = form_values(request.form)
    if trans_type == TWIN:
        return _new_twin(d, values)
    try:
        fields = build_fields(d, request.form, trans_type)
    except FormError as exc:
        return _render(d, 'add', values, error=str(exc), status=422)
    if fields['status'] not in {s for s, _ in ADD_STATUSES}:
        return _render(d, 'add', values, error='Status: pick Unflagged, Follow up or Reconciled', status=422)
    create_tags = fields.pop('_create_tags')
    op = {'op': 'add', **fields, 'create_payee': bool(request.form.get('create_payee')), 'create_tags': create_tags}
    if fields['type'] != 'Transfer':
        op.pop('to_account', None)
    else:
        op.pop('create_payee')

    if request.form.get('confirm_duplicate'):
        op['allow_duplicate'] = True
    else:
        account_id = d.ref.account(fields['account'], 'a', allow_closed=True)['id']
        dups = views.near_duplicates(d.conn, d.ref, account_id, fields['amount'], fields['date'])
        if dups:
            return _render(d, 'add', values, duplicates=dups)

    result, write_id = writer().write({'ops': [op]}, action='add')
    if not result.ok:
        return _render(d, 'add', values, error=_engine_error(result.error), status=422)
    _flash_result(result)
    _saved_message(write_id, f'Saved: {fields["date"]} {fields.get("payee") or "→ " + fields["to_account"]} {fields["amount"]:.2f}.')
    return _after_save(d, fields, request.form.get('next', ''))


def _new_twin(d, values):
    """Save one purchase as two rows, as many MMEX books record spending
    paid on behalf of another account (e.g. household spending on a card):

      1. Transfer: paying account -> To account (e.g. Cash), no payee, notes
         from the pattern ("<payee>, <expense account> -> <To account>").
      2. Withdrawal: expense account (e.g. Household), the real payee,
         category, notes and tags.

    Same date and amount, one write: one History entry, one Undo for both.
    The To and expense accounts are remembered for next time."""
    form = request.form
    twin = twin_settings(d)
    try:
        date = _date(form.get('date'))
        amount = _amount(form.get('amount'), 'Amount')
        paid_from = _account_name(d, form.get('account'), 'Paid from')
        to_account = _account_name(d, form.get('to_account'), 'To account')
        expense = _account_name(d, form.get('expense_account'), 'Expense account')
        if len({paid_from, to_account, expense}) < 3:
            raise FormError('Paid from, To account and Expense account must be three different accounts')
        currencies = {d.ref.account(n, 'a', allow_closed=True)['currency'] for n in (paid_from, to_account, expense)}
        if len(currencies) > 1:
            raise FormError('Paid from, To account and Expense account must use the same currency for a twin')
        payee = form.get('payee', '').strip()
        if not payee:
            raise FormError('Payee: enter a payee')
        category = form.get('category', '').strip()
        if not category:
            raise FormError('Category: pick a category')
        status = form.get('status', '')
        if status not in {s for s, _ in ADD_STATUSES}:
            raise FormError('Status: pick Unflagged, Follow up or Reconciled')
        tags, create_tags = _tags(form)
    except FormError as exc:
        return _render(d, 'add', values, error=str(exc), status=422)
    transfer = {'op': 'add', 'type': 'Transfer', 'date': date, 'account': paid_from, 'to_account': to_account,
                'amount': amount, 'category': 'Transfer', 'status': status, 'number': form.get('number', '').strip(),
                # As shown in the form (filled from the pattern, maybe changed); the
                # pattern again when it came empty (e.g. no script).
                'notes': (form.get('transfer_notes', '').replace('\r\n', '\n').strip()
                          or twin_notes(twin['pattern'], payee, expense, to_account))}
    spend = {'op': 'add', 'type': 'Withdrawal', 'date': date, 'account': expense, 'payee': payee,
             'category': category, 'amount': amount, 'status': status, 'number': '',
             'notes': form.get('notes', '').replace('\r\n', '\n').strip(), 'tags': tags,
             'create_payee': bool(form.get('create_payee')), 'create_tags': create_tags}

    if form.get('confirm_duplicate'):
        transfer['allow_duplicate'] = spend['allow_duplicate'] = True
    else:
        dups = []
        for name in (paid_from, expense):
            account_id = d.ref.account(name, 'a', allow_closed=True)['id']
            dups += [dict(r, where=name) for r in views.near_duplicates(d.conn, d.ref, account_id, amount, date)]
        if dups:
            return _render(d, 'add', values, duplicates=dups)

    # Expense row first: the paying account's transfer is then the newest row,
    # so it's the "last used account" the next New starts in.
    result, write_id = writer().write({'ops': [spend, transfer]}, action='add')
    if not result.ok:
        return _render(d, 'add', values, error=_engine_error(result.error), status=422)
    acct_id = lambda name: d.ref.account(name, 'a', allow_closed=True)['id']
    current_app.config['MMEX_STORE'].set_prefs(twin_to_account=acct_id(to_account), twin_expense_account=acct_id(expense))
    _flash_result(result)
    _saved_message(write_id, f'Saved twin: {date} {payee} {amount:.2f}, {paid_from} → {to_account}, and in {expense}.')
    next_ = form.get('next', '')
    if form.get('save_new'):
        return redirect(url_for('entry.new', account=acct_id(paid_from), type=TWIN, date=date, next=next_ or None))
    if next_:
        return redirect(_safe_next(next_))
    return redirect(url_for('pages.register', account_id=acct_id(paid_from)))


def _values_of(t) -> dict:
    """A saved row as form values (the edit form, or a copy's starting point)."""
    return {'type': t['type'], 'account_id': str(t['account_id']),
            'to_account_id': str(t['to_account_id'] or ''), 'payee': t.get('payee', ''), 'create_payee': False,
            'category': t['category'], 'amount': f'{t["amount"]:.2f}',
            'to_amount': f'{t["to_amount"]:.2f}' if t['type'] == 'Transfer' else '',
            'date': t['date'], 'notes': t['notes'], 'number': t['number'], 'status': t['status'],
            'tags': t['tags'], 'new_tags': ''}


def _copy_of(d, values) -> bool:
    """Fill a New form from the row in ?copy=<id>: everything but the
    number and status, which a new row starts without. False when there's
    no ?copy."""
    trans_id = request.args.get('copy', type=int)
    if not trans_id:
        return False
    t = views.transaction(d.conn, d.schema, d.ref, trans_id)
    if t is None or t['deleted']:
        abort(404, 'No such transaction to duplicate.')
    values.update(_values_of(t), number='', status='')
    return True


def _load(d, trans_id):
    if d.schema is None:
        abort(403, 'This database can\'t be written by mmex_web.')
    t = views.transaction(d.conn, d.schema, d.ref, trans_id)
    if t is None:
        abort(404, 'No such transaction.')
    return t


def _orig(t) -> dict:
    return {k: t[k] for k in EXPECT_FIELDS if k in t}


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 0.005
    if isinstance(a, list):
        return sorted(x.lower() for x in a) == sorted(x.lower() for x in b or [])
    return a == b


def _read_orig(form):
    try:
        orig = json.loads(form.get('orig', ''))
    except ValueError:
        abort(400, 'The form is missing the original row. Reload the page and try again.')
    if not isinstance(orig, dict):
        abort(400, 'The form is missing the original row. Reload the page and try again.')
    return orig


@bp.route('/txn/<int:trans_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(trans_id):
    d = db()
    t = _load(d, trans_id)
    if t['deleted']:
        return render_template('deleted.html', t=t, banners=d.banners)
    _require_writable(d)
    if request.method == 'GET':
        return _render(d, 'edit', _values_of(t), trans=dict(t, orig=json.dumps(_orig(t))))

    orig = _read_orig(request.form)
    new_type = request.form.get('type') or t['type']
    values = form_values(request.form, new_type)
    trans_view = dict(t, orig=request.form.get('orig'))
    try:
        fields = build_fields(d, request.form, new_type)
    except FormError as exc:
        return _render(d, 'edit', values, error=str(exc), trans=trans_view, status=422)
    create_tags = fields.pop('_create_tags')
    changes = {k: v for k, v in fields.items() if k != 'to_amount' and not _same(v, orig.get(k))}
    if new_type == 'Transfer' and 'to_amount' in fields:
        moved = any(k in changes for k in ('amount', 'account', 'to_account', 'type'))
        if moved or not _same(fields['to_amount'], orig.get('to_amount')):
            changes['to_amount'] = fields['to_amount']
    if 'status' in changes and changes['status'] not in {s for s, _ in EDIT_STATUSES}:
        return _render(d, 'edit', values, error='Status: pick Unflagged, Follow up, Reconciled or Void',
                       trans=trans_view, status=422)
    if not changes:
        # Stay on the form, showing what arrived, so a change that didn't
        # make it into the submitted form is visible rather than silently lost.
        log.warning('edit %s: submitted form matches the row, nothing saved (fields: %s)',
                 trans_id, ', '.join(sorted(k for k in request.form if k not in ('csrf_token', 'orig'))))
        return _render(d, 'edit', values, trans=trans_view, notice='Nothing changed, so nothing was saved. '
                       'These are the values the app received.')
    op = {'op': 'update', 'id': trans_id, 'expect': orig, 'set': changes, 'create_tags': create_tags}
    if new_type != 'Transfer' and request.form.get('create_payee'):
        op['create_payee'] = True
    result, write_id = writer().write({'ops': [op]}, action='edit')
    if not result.ok:
        if result.stale:
            return _render(d, 'edit', values, trans=trans_view, status=409,
                           error='This transaction changed since you opened it. Reload the page to see the '
                                 'current version, then make your edit again.')
        return _render(d, 'edit', values, error=_engine_error(result.error), trans=trans_view, status=422)
    _flash_result(result)
    _saved_message(write_id, f'Saved changes to {", ".join(sorted(changes))}.')
    return _after_save(d, fields, request.form.get('next', ''))


@bp.route('/txn/<int:trans_id>/delete', methods=['GET', 'POST'])
@login_required
def delete(trans_id):
    d = db()
    t = _load(d, trans_id)
    if t['deleted']:
        return render_template('deleted.html', t=t, banners=d.banners)
    _require_writable(d)
    next_ = request.values.get('next', '')
    if request.method == 'GET':
        return render_template('delete.html', t=t, orig=json.dumps(_orig(t)), next=next_, banners=d.banners,
                               currency=d.currencies.get(d.ref.accounts[t['account_id']]['currency']))
    orig = _read_orig(request.form)
    result, write_id = writer().write({'ops': [{'op': 'delete', 'id': trans_id, 'expect': orig}]}, action='delete')
    if not result.ok:
        flash('This transaction changed since you opened it; nothing was deleted. Check it and try again.'
              if result.stale else _engine_error(result.error), 'error')
        return redirect(url_for('entry.delete', trans_id=trans_id, next=next_ or None))
    _flash_result(result)
    _saved_message(write_id, 'Deleted. It\'s in MMEX\'s trash, and History can undo it.')
    if next_ and not next_.startswith(f'/txn/{trans_id}/'):
        return redirect(_safe_next(next_))
    return redirect(url_for('pages.register', account_id=t['account_id']))


# The flag button on each row: one tap moves a row to the next status.
# V (void) and D (duplicate) aren't in the cycle; the edit form sets V.
FLAG_CYCLE = {'': 'R', 'R': 'F', 'F': ''}
FLAG_TITLES = {'': 'Unflagged', 'R': 'Reconciled', 'F': 'Follow up'}


def flag_button(status) -> dict:
    """What a row's flag button shows for `status`: label, title, class."""
    nxt = FLAG_CYCLE[status]
    return {'status': status, 'label': status or '·', 'cls': f'flag-{status or "none"}',
            'title': f'{FLAG_TITLES[status]}. Tap for {FLAG_TITLES[nxt].lower()}.'}


@bp.route('/txn/<int:trans_id>/flag', methods=['POST'])
@login_required
def flag(trans_id):
    """Move a row to the next status (unflagged -> R -> F -> unflagged).
    A fetch() (X-Requested-With: fetch) gets JSON to update the row and the
    page's totals in place; a plain post goes back to `next`."""
    d = db()
    _require_writable(d)
    old = request.form.get('status', '')
    if old not in FLAG_CYCLE:
        abort(400, 'status must be unflagged, R or F')
    new = FLAG_CYCLE[old]
    result, _ = writer().flag(trans_id, old, new)
    status, error = new, None
    if not result.ok:
        found = d.conn.execute('SELECT STATUS FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', (trans_id,)).fetchone()
        status = (found[0] or '') if found else old
        error = ('This row changed since the page was loaded; nothing was changed. It now shows its current status.'
                 if result.stale else _engine_error(result.error))
    if request.headers.get('X-Requested-With') != 'fetch':
        if error:
            flash(error, 'error')
        return redirect(_safe_next(request.form.get('next')))
    out = {'ok': error is None, 'error': error,
           'button': flag_button(status) if status in FLAG_CYCLE else {'status': status, 'label': status,
                                                                     'cls': f'flag-{status}', 'title': status}}
    try:
        account_id = int(request.form.get('account', ''))
    except ValueError:
        account_id = None
    if account_id in d.ref.accounts:
        reg = views.register(d.conn, d.ref, account_id, limit=0)    # read-only, not immutable: sees the write
        money = current_app.jinja_env.filters['money']
        cur = d.currencies.get(d.ref.accounts[account_id]['currency'])
        values = {'balance': reg['balance'], 'reconciled': reg['reconciled'],
                  'unreconciled': reg['balance'] - reg['reconciled']}
        out['totals'] = {k: {'text': str(money(v, cur)), 'neg': v < 0} for k, v in values.items()}
    return jsonify(out)


@bp.route('/history')
@login_required
def history():
    d = db()
    store = current_app.config['MMEX_STORE']
    source = request.args.get('source') or None
    page = max(1, request.args.get('page', 1, type=int))
    total = store.count(source)
    writes = store.list(source=source, limit=HISTORY_PAGE, offset=(page - 1) * HISTORY_PAGE)
    return render_template('history.html', writes=writes, source=source, sources=store.sources(), page=page,
                           pages=max(1, -(-total // HISTORY_PAGE)), banners=d.banners)


@bp.route('/history/<int:write_id>/undo', methods=['GET', 'POST'])
@login_required
def undo(write_id):
    d = db()
    _require_writable(d)
    store = current_app.config['MMEX_STORE']
    write = store.get(write_id)
    if write is None:
        abort(404, 'No such history entry.')
    if request.method == 'GET':
        result, error = writer().undo(write, commit=False)
        return render_template('undo.html', write=write, result=result, error=error, banners=d.banners)
    result, error = writer().undo(write, commit=True)
    redo = write['action'] == 'undo'
    if error:
        flash(f'Not {"redone" if redo else "undone"}: {error}', 'error')
        return redirect(url_for('entry.undo', write_id=write_id))
    _flash_result(result)
    flash(f'{"Redone" if redo else "Undone"}: {write["summary"].split(": ", 1)[-1] if redo else write["summary"]}', 'info')
    return redirect(url_for('entry.history'))
