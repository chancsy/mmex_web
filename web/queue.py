"""The Queue page: batches other programs submitted through the API. Each
one shows what approving it would do to the DB as it is now; Approve
applies it (backup first), Reject files it away with a note. A batch of one
add can be edited first (amount, date, notes)."""

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from .auth import login_required
from .entry import FormError, _amount, _date, _engine_error, _flash_result, _require_writable, writer
from .pages import db
from .review import build as build_review
from .writer import preview_digest

bp = Blueprint('queue', __name__)
TABS = ('pending', 'approved', 'rejected', 'all')


def _store():
    return current_app.config['MMEX_STORE']


def single_add(item):
    """The add op of a one-add batch (an absent check before it allowed),
    or None: only those can be edited here."""
    adds = [op for op in item['plan']['ops'] if op.get('op') == 'add']
    others = [op for op in item['plan']['ops'] if op.get('op') not in ('add', 'absent')]
    return adds[0] if len(adds) == 1 and not others and adds[0].get('type') != 'Transfer' else None


@bp.route('/queue')
@login_required
def index():
    d = db()
    tab = request.args.get('status', 'pending')
    if tab not in TABS:
        abort(400, f'status must be one of {TABS}')
    source = request.args.get('source') or None
    items = _store().items(source=source, status=None if tab == 'all' else tab, limit=200)
    return render_template('queue.html', items=items, tab=tab, tabs=TABS, source=source,
                           sources=_store().queue_sources(), banners=d.banners)


def _render_item(d, item, status=200):
    now = changed = None
    if item['status'] == 'pending':
        now = writer().preview(item['plan'])
        changed = preview_digest(now) != preview_digest(item['preview'])
    pattern = _store().prefs()['twin_notes'] or '{payee}, {expense_account} -> {to_account}'
    review = build_review(now or item['preview'], pattern, summary=(item['plan'] or {}).get('summary'))
    return render_template('queue_item.html', item=item, now=now, changed=changed, review=review,
                           seen=preview_digest(now) if now else '', edit=single_add(item) if now else None,
                           banners=d.banners), status


@bp.route('/queue/<int:item_id>')
@login_required
def item(item_id):
    d = db()
    found = _store().item(item_id)
    if found is None:
        abort(404, 'No such batch.')
    return _render_item(d, found)


def _pending(item_id):
    d = db()
    _require_writable(d)
    found = _store().item(item_id)
    if found is None:
        abort(404, 'No such batch.')
    if found['status'] != 'pending':
        flash(f'This batch is already {found["status"]}.', 'error')
        return d, None
    return d, found


@bp.route('/queue/<int:item_id>/approve', methods=['POST'])
@login_required
def approve(item_id):
    d, found = _pending(item_id)
    if found is None:
        return redirect(url_for('queue.item', item_id=item_id))
    if request.form.get('changed') == '1' and not request.form.get('ack'):
        flash('This batch changed since it was submitted: tick the box to say you\'ve looked, then approve.', 'error')
        return redirect(url_for('queue.item', item_id=item_id))
    result, error, write_id = writer().approve(found, request.form.get('seen', ''))
    if error:
        flash(f'Not approved: {_engine_error(error)}', 'error')
        return redirect(url_for('queue.item', item_id=item_id))
    _flash_result(result)
    flash(f'Approved: {found["title"]}. It\'s in History, where it can be undone.', 'info')
    return redirect(url_for('queue.index'))


@bp.route('/queue/<int:item_id>/reject', methods=['POST'])
@login_required
def reject(item_id):
    d, found = _pending(item_id)
    if found is None:
        return redirect(url_for('queue.item', item_id=item_id))
    note = request.form.get('note', '').strip()[:500] or None
    if _store().decide(item_id, 'rejected', note=note):
        flash(f'Rejected: {found["title"]}.', 'info')
    else:
        flash('This batch was decided elsewhere meanwhile.', 'error')
    return redirect(url_for('queue.index'))


@bp.route('/queue/<int:item_id>/edit', methods=['POST'])
@login_required
def edit(item_id):
    d, found = _pending(item_id)
    if found is None:
        return redirect(url_for('queue.item', item_id=item_id))
    add = single_add(found)
    if add is None:
        abort(400, 'Only a batch of one add can be edited here; reject it and have it sent again.')
    try:
        new = dict(add, amount=_amount(request.form.get('amount'), 'Amount'), date=_date(request.form.get('date')),
                   notes=request.form.get('notes', '').strip())
    except FormError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('queue.item', item_id=item_id))
    plan = dict(found['plan'], ops=[new if op is add else op for op in found['plan']['ops']])
    preview = writer().preview(plan)
    if not preview['ok']:
        flash(f'Not saved: {_engine_error(preview["error"])}', 'error')
        return redirect(url_for('queue.item', item_id=item_id))
    if not _store().edit_item(item_id, plan, preview, db_fingerprint=None):
        flash('This batch was decided elsewhere meanwhile.', 'error')
    else:
        flash('Saved. Check what it does now, then approve.', 'info')
    return redirect(url_for('queue.item', item_id=item_id))
