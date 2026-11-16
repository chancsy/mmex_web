"""The JSON API, for programs rather than people: submit a batch to the
queue, follow what became of it, and read balances, transactions and names.

Nothing here writes the MMEX DB. A submitted batch is previewed at once and
waits on the Queue page until it's approved there.

- Auth: `Authorization: Bearer <token>`. Each queue source has its own
  token (`python -m web token <source>`); secrets.json keeps only a hash of
  it. The source a batch is filed under comes from the token, never from
  the request, so one sender can't pose as another.
- Reach: requests that came through a reverse proxy (any X-Forwarded-* or
  Forwarded header) get 404 unless MMEX_WEB_API_VIA_PROXY is set, so the
  API answers only callers that reach the app directly (the same host or
  its docker network), not the internet-facing route.

Endpoints:
    POST /api/queue              {"key", "title", "ops": [...], "replace": false}
                                 201 queued (with the preview); 409 that key is
                                 already pending (with its id); 422 the batch is
                                 invalid or a check fails (with the reason and
                                 the preview); 503 the DB can't be previewed
    GET  /api/queue?key=&status= this source's batches, newest first
    GET  /api/queue/<id>         one of this source's batches
    GET  /api/queue/summary      pending count and the oldest, every source
    GET  /api/balance?account=&as_of=
    GET  /api/find?account=&from=&to=&amount=&payee=&category=&notes=&id=&include_deleted=&limit=
    GET  /api/lookup/<accounts|payees|categories|tags>?like=
    GET  /api/reports/month-end?month=YYYY-MM
                                 every account at that month-end (own currency
                                 and base), its change, and Total / Liquid / Investment
"""

import datetime
import hashlib
import hmac

from flask import Blueprint, abort, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from engine import core
from engine.core import PlanError

from . import secrets_file
from .pages import ReadDb

bp = Blueprint('api', __name__, url_prefix='/api')
PROXY_HEADERS = ('X-Forwarded-For', 'X-Forwarded-Host', 'X-Forwarded-Proto', 'Forwarded', 'X-Real-IP')
STATUSES = ('pending', 'approved', 'rejected', 'superseded')


def token_digest(token) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _source():
    """The source whose token the request carries, or None."""
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return None
    digest = token_digest(header[7:].strip())
    settings = current_app.config['MMEX_SETTINGS']
    # Read per request, so a new or revoked token needs no restart.
    tokens = secrets_file.load(settings.secrets_path).get('api_tokens', {})
    for source, want in tokens.items():
        if hmac.compare_digest(want, digest):
            return source
    return None


@bp.before_request
def _gate():
    settings = current_app.config['MMEX_SETTINGS']
    if not settings.api_via_proxy and any(h in request.headers for h in PROXY_HEADERS):
        abort(404)
    source = _source()
    if source is None:
        abort(401, 'missing or unknown API token')
    request.environ['mmex.api_source'] = source


def _json_error(exc):
    return jsonify(error=exc.description if exc.code != 404 else 'not found'), exc.code


# Per status code: Flask prefers the app's code-specific handlers (HTML error
# pages) over a blueprint's catch-all, but a blueprint's code-specific ones
# over the app's.
for _code in (400, 401, 403, 404, 405, 413, 503):
    bp.register_error_handler(_code, _json_error)
bp.register_error_handler(HTTPException, _json_error)


def _src() -> str:
    return request.environ['mmex.api_source']


def item_json(item, with_preview=True) -> dict:
    out = {k: item[k] for k in ('id', 'source', 'key', 'title', 'status', 'edited', 'submitted_at_utc',
                                'decided_at_utc', 'note', 'rows')}
    out['undone'] = bool(item.get('undone_at_utc'))
    if with_preview:
        p = item['preview']
        out['preview'] = {k: p.get(k) for k in ('ok', 'error', 'checks_ok', 'lines', 'balances', 'warnings')}
    return out


def _text(body, name, required=True, limit=200):
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        abort(400, f'"{name}" must be a non-empty string of at most {limit} characters')
    return value.strip()


@bp.post('/queue')
def submit():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, 'send a JSON object')
    key = _text(body, 'key')
    title = _text(body, 'title', required=False) or key
    ops = body.get('ops')
    if not isinstance(ops, list) or not ops:
        abort(400, '"ops" must be a non-empty list of plan ops')
    plan = {'ops': ops}
    summary = body.get('summary')
    if summary is not None and not isinstance(summary, str):
        abort(400, '"summary" must be text')
    if summary and summary.strip():
        plan['summary'] = summary.strip()[:2000]
    try:
        preview = current_app.config['MMEX_WRITER'].preview(plan)
    except FileNotFoundError:
        abort(503, 'the MMEX database file is not available')
    except Exception as exc:  # e.g. SchemaError: the DB can't be written by this engine
        abort(503, f'the batch could not be previewed: {exc}')
    shown = {k: preview[k] for k in ('ok', 'error', 'checks_ok', 'lines', 'events', 'balances', 'warnings')}
    if not preview['ok'] or not preview['checks_ok']:
        return jsonify(error=preview['error'] or 'a check failed', preview=shown), 422
    store = current_app.config['MMEX_STORE']
    item_id, pending = store.enqueue(_src(), key, title, plan, preview, db_fingerprint=None,
                                     replace=bool(body.get('replace')))
    if pending:
        return jsonify(error=f'"{key}" is already pending; send "replace": true to supersede it', id=pending), 409
    return jsonify(id=item_id, status='pending', preview=shown), 201


@bp.get('/queue')
def list_items():
    status = request.args.get('status') or None
    if status and status not in STATUSES:
        abort(400, f'status must be one of {STATUSES}')
    limit = min(max(request.args.get('limit', 100, type=int), 1), 500)
    items = current_app.config['MMEX_STORE'].items(source=_src(), status=status,
                                                   key=request.args.get('key') or None, limit=limit)
    return jsonify(items=[item_json(i, with_preview=False) for i in items])


@bp.get('/queue/summary')
def summary():
    return jsonify(current_app.config['MMEX_STORE'].queue_summary())


@bp.get('/queue/<int:item_id>')
def get_item(item_id):
    item = current_app.config['MMEX_STORE'].item(item_id)
    if item is None or item['source'] != _src():
        abort(404)
    return jsonify(item_json(item))


# -- reads --------------------------------------------------------------------

def _read():
    try:
        return ReadDb(current_app.config['MMEX_SETTINGS'].db_path)
    except FileNotFoundError:
        abort(503, 'the MMEX database file is not available')


def _date(name):
    value = request.args.get(name, '').strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        abort(400, f'{name} must be YYYY-MM-DD')


@bp.get('/balance')
def balance():
    d = _read()
    try:
        account = d.ref.account(request.args.get('account'), 'account', allow_closed=True)
        as_of = _date('as_of')
        return jsonify(account=account['name'], as_of=as_of, balance=round(core.balance(d.conn, account['id'], as_of), 2))
    except PlanError as exc:
        abort(400, str(exc))
    finally:
        d.close()


@bp.get('/find')
def find():
    d = _read()
    args = request.args
    if d.schema is None:
        d.close()
        abort(503, 'this database\'s schema isn\'t supported')
    try:
        amount = args.get('amount', type=float)
        rows = core.find(d.conn, d.schema, d.ref, id=args.get('id', type=int), account=args.get('account') or None,
                         date_from=_date('from'), date_to=_date('to'), amount=amount,
                         payee=args.get('payee') or None, category=args.get('category') or None,
                         notes=args.get('notes') or None,
                         include_deleted=args.get('include_deleted', '') in ('1', 'true'),
                         limit=min(max(args.get('limit', 200, type=int), 1), 2000))
        return jsonify(rows=rows)
    except PlanError as exc:
        abort(400, str(exc))
    finally:
        d.close()


@bp.get('/lookup/<kind>')
def lookup(kind):
    d = _read()
    try:
        ref, like = d.ref, request.args.get('like', '').lower()
        if kind == 'accounts':
            rows = [{'id': a['id'], 'name': a['name'], 'status': a['status'], 'currency': a['currency']}
                    for a in ref.accounts.values()]
        elif kind == 'payees':
            rows = [{'id': p['id'], 'name': p['name'], 'active': bool(p['active'])} for p in ref.payees.values()]
        elif kind == 'categories':
            rows = [{'id': c['id'], 'name': ref.category_path(c['id']), 'active': bool(c['active'])}
                    for c in ref.categories.values()]
        elif kind == 'tags':
            rows = [{'id': t['id'], 'name': t['name'], 'active': bool(t['active'])} for t in ref.tags.values()]
        else:
            abort(404)
        return jsonify(rows=sorted((r for r in rows if like in r['name'].lower()), key=lambda r: r['name'].lower()))
    finally:
        d.close()


@bp.get('/reports/month-end')
def month_end():
    """Every account's balance at a month's end and its change from the month
    before (own currency, plus the base-currency value), with Total, Liquid
    and Investment as the Reports pages count them. Ignored accounts are
    listed (marked) but left out of the totals."""
    from engine import reports as R
    from .reports import config
    month = request.args.get('month', '').strip()
    if not R.valid_month(month):
        abort(400, 'month must be YYYY-MM')
    d = _read()
    try:
        prev = R.shift(month, -1)
        snap = R.snapshot(d.conn, [prev, month], config(d))
        symbols = {cid: c['symbol'] for cid, c in d.currencies.items()}
        rows, totals = [], {'total': 0.0, 'liquid': 0.0, 'investment': 0.0}
        for a in sorted(snap['accounts'].values(), key=lambda a: a['name'].lower()):
            rows.append({'id': a['id'], 'name': a['name'], 'type': a['type'], 'status': a['status'],
                         'currency': symbols.get(a['currency']), 'balance': a['own'][month],
                         'change': round(a['own'][month] - a['own'][prev], 2), 'rate': a['rate'][month],
                         'base_balance': round(a['base'][month], 2), 'ignored': a['ignored'], 'liquid': a['liquid'],
                         'investment': a['investment'], 'cash': a['cash'][month],
                         'assets': [x['name'] for x in a['assets']]})
            if not a['ignored']:
                totals['total'] += a['base'][month]
                totals['liquid'] += a['base'][month] if a['liquid'] else 0
                totals['investment'] += a['base'][month] if a['investment'] else 0
        return jsonify(month=month, as_of=R.month_end(month), base=symbols.get(snap['base']), accounts=rows,
                       **{k: round(v, 2) for k, v in totals.items()})
    finally:
        d.close()
