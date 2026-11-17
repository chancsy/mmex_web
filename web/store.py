"""app.db: mmex_web's own SQLite file in the app-data dir, never synced.

It holds the history of every write the app made to the MMEX DB, with the
engine's journal for each, so a write can be undone later; the display
preferences; every xe.com rate table the app fetched, whole, so one
page visit serves every currency for that date; and the queue: batches
other programs submit through the API, which reach the MMEX DB only once
approved on the Queue page.

A connection per call: the file is small and writes are rare, and sqlite3
connections shouldn't be shared between gunicorn's threads.
"""

import json
import os
import sqlite3

from engine.core import utc_now_str

SCHEMA_VERSION = 4
SCHEMA = '''
CREATE TABLE IF NOT EXISTS writes (
    id INTEGER PRIMARY KEY,
    at_utc TEXT NOT NULL,
    source TEXT NOT NULL,          -- 'you' for the UI, else the queue source whose batch it was
    action TEXT NOT NULL,          -- add, edit, delete, flag, undo, redo, queue
    summary TEXT NOT NULL,
    lines TEXT NOT NULL,           -- JSON list of the engine's preview lines
    db_path TEXT NOT NULL,
    db_fingerprint TEXT NOT NULL,
    backup TEXT,
    journal TEXT,                  -- JSON engine journal (an undo's journal redoes it)
    undo_of INTEGER REFERENCES writes(id),
    undone_at_utc TEXT,
    undone_by INTEGER REFERENCES writes(id)
);
CREATE TABLE IF NOT EXISTS write_rows (
    write_id INTEGER NOT NULL REFERENCES writes(id),
    transid INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS write_rows_transid ON write_rows(transid);
CREATE TABLE IF NOT EXISTS prefs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);
CREATE TABLE IF NOT EXISTS xe_rates (
    base TEXT NOT NULL,            -- the table's base currency code (e.g. USD)
    date TEXT NOT NULL,            -- YYYY-MM-DD
    code TEXT NOT NULL,            -- xe.com's currency code
    name TEXT NOT NULL,
    units_per TEXT NOT NULL,       -- units of `code` per one base unit, as xe.com shows it
    per_unit TEXT NOT NULL,        -- base units per one `code` (what MMEX stores), as shown
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY (base, date, code)
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- the API token's source, e.g. check_bills
    key TEXT NOT NULL,             -- the sender's id for the batch, unique among pending ones
    title TEXT NOT NULL,
    plan TEXT NOT NULL,            -- JSON {"ops": [...]}, the engine's plan format
    submitted_at_utc TEXT NOT NULL,
    db_fingerprint TEXT,
    preview TEXT NOT NULL,         -- JSON: the engine's preview when submitted (or last edited)
    status TEXT NOT NULL,          -- pending, approved, rejected, superseded
    edited INTEGER NOT NULL DEFAULT 0,
    decided_at_utc TEXT,
    note TEXT,                     -- why it was rejected
    write_id INTEGER REFERENCES writes(id),
    rows TEXT                      -- JSON list of the TRANSIDs an approved batch added
);
CREATE UNIQUE INDEX IF NOT EXISTS queue_pending_key ON queue(source, key) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS queue_status ON queue(status, id);
'''

# Display preferences (the Settings page). Server-side, so phone and PC
# agree; which groups are collapsed is per device, in the browser.
PREF_DEFAULTS = {
    'favourites_first': False,     # favourite accounts at the top of each group
    'hidden_accounts': [],         # account ids shown under "Others" on the accounts page
    'hide_balances': False,        # the eye in the top bar: balances shown as dots
    'rate_keep_currencies': [],    # MMEX currency ids given rates though no open account uses them
    # Twin entry (one purchase as two rows): a Transfer from the paying account
    # to the To account (e.g. Cash), notes from this pattern, plus a Withdrawal
    # with the real payee in the expense account (e.g. Household). The To and
    # expense accounts used last are remembered and preselected.
    'twin_notes': '{payee}, {expense_account} -> {to_account}',
    'twin_to_account': None,
    'twin_expense_account': None,
    # Reports (see engine/reports.py). nonliquid None = not chosen yet: Asset
    # and Loan accounts count as not liquid until the Settings page is saved.
    'report_ignored_accounts': [],     # a separate "wallet": left out of every report
    'nonliquid_accounts': None,        # in Total, not in Liquid
    'investment_accounts': [],         # in Investment (liquid or not)
    # Extra lines on the Net worth chart: [{name, add_to, accounts}], the
    # chosen accounts' balances added to nothing, Liquid or Total.
    'chart_lines': [],
    # The year the reports' "All" range starts from; None = the first month with data.
    'report_start_year': None,
}

CHART_LINE_MAX = 5
# How a chart line combines its accounts: alone, added to Liquid or Total
# (skipping any already in it), or taken out of Liquid or Total (only those in it).
CHART_LINE_ADD_TO = {'none': 'Just these accounts', 'liquid': 'Liquid plus these', 'total': 'Total plus these',
                     'liquid_minus': 'Liquid minus these', 'total_minus': 'Total minus these'}

NONLIQUID_TYPES = ('Asset', 'Loan')


def report_config(prefs, account_types) -> dict:
    """The engine's report config from the prefs. account_types: {id: type},
    for the not-yet-chosen Not liquid default."""
    nonliquid = prefs['nonliquid_accounts']
    if nonliquid is None:
        nonliquid = [a for a, t in account_types.items() if t in NONLIQUID_TYPES]
    return {'ignored': set(prefs['report_ignored_accounts']), 'nonliquid': set(nonliquid),
            'investment': set(prefs['investment_accounts'])}


def transids(journal) -> list:
    """The MMEX TRANSIDs a journal touched."""
    out = []
    for e in journal.get('entries', []):
        if e['op'] in ('add', 'remove'):
            out.append(e['row']['TRANSID'])
        elif e['op'] in ('update', 'delete'):
            out.append(e['before']['TRANSID'])
        elif e['op'] == 'flag':
            out += [c['id'] for c in e['changes']]
    return out


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._connect() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f'{path} is from a newer mmex_web (schema {version})')
            conn.executescript(SCHEMA)
            conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        os.chmod(path, 0o600)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        out = dict(row)
        out['lines'] = json.loads(out['lines'])
        out['journal'] = json.loads(out['journal']) if out['journal'] else None
        return out

    def record(self, source, action, summary, lines, db_path, db_fingerprint, backup, journal=None,
               undo_of=None, rows=()) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                'INSERT INTO writes (at_utc, source, action, summary, lines, db_path, db_fingerprint, backup, '
                'journal, undo_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (utc_now_str(), source, action, summary, json.dumps(lines), db_path, db_fingerprint, backup,
                 json.dumps(journal) if journal else None, undo_of))
            write_id = cur.lastrowid
            ids = list(rows) or (transids(journal) if journal else [])
            conn.executemany('INSERT INTO write_rows (write_id, transid) VALUES (?, ?)', [(write_id, t) for t in ids])
            if undo_of:
                conn.execute('UPDATE writes SET undone_at_utc = ?, undone_by = ? WHERE id = ?',
                             (utc_now_str(), write_id, undo_of))
        return write_id

    def latest(self):
        """The newest write, or None."""
        with self._connect() as conn:
            return self._decode(conn.execute('SELECT * FROM writes ORDER BY id DESC LIMIT 1').fetchone())

    def extend(self, write_id, summary, lines, journal, rows=()):
        """Fold a later write into an existing entry (status taps grouped
        into one history line): its time moves to now and its journal holds
        both, so one undo reverses them all."""
        with self._connect() as conn:
            conn.execute('UPDATE writes SET at_utc = ?, summary = ?, lines = ?, journal = ? WHERE id = ?',
                         (utc_now_str(), summary, json.dumps(lines), json.dumps(journal), write_id))
            have = {r[0] for r in conn.execute('SELECT transid FROM write_rows WHERE write_id = ?', (write_id,))}
            conn.executemany('INSERT INTO write_rows (write_id, transid) VALUES (?, ?)',
                             [(write_id, t) for t in dict.fromkeys(rows) if t not in have])

    def get(self, write_id):
        with self._connect() as conn:
            return self._decode(conn.execute('SELECT * FROM writes WHERE id = ?', (write_id,)).fetchone())

    def list(self, source=None, limit=50, offset=0) -> list:
        sql, args = 'SELECT * FROM writes', []
        if source:
            sql += ' WHERE source = ?'
            args.append(source)
        sql += ' ORDER BY id DESC LIMIT ? OFFSET ?'
        with self._connect() as conn:
            return [self._decode(r) for r in conn.execute(sql, args + [limit, offset])]

    def count(self, source=None) -> int:
        with self._connect() as conn:
            if source:
                return conn.execute('SELECT COUNT(*) FROM writes WHERE source = ?', (source,)).fetchone()[0]
            return conn.execute('SELECT COUNT(*) FROM writes').fetchone()[0]

    def sources(self) -> list:
        with self._connect() as conn:
            return [r[0] for r in conn.execute('SELECT DISTINCT source FROM writes ORDER BY source')]

    def last_source(self, ids) -> dict:
        """{transid: source of the latest app write that touched it}, for
        rows the app wrote. Rows not in the result came from elsewhere."""
        ids = list(ids)
        out = {}
        with self._connect() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for transid, source in conn.execute(
                        'SELECT r.transid, w.source FROM write_rows r JOIN writes w ON w.id = r.write_id '
                        f'WHERE r.transid IN ({",".join("?" * len(chunk))}) ORDER BY w.id', chunk):
                    out[transid] = source
        return out

    def prefs(self) -> dict:
        out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
               for k, v in PREF_DEFAULTS.items()}
        with self._connect() as conn:
            for key, value in conn.execute('SELECT key, value FROM prefs'):
                if key in PREF_DEFAULTS:
                    out[key] = json.loads(value)
        return out

    def set_prefs(self, **values):
        unknown = set(values) - set(PREF_DEFAULTS)
        if unknown:
            raise KeyError(f'unknown preference(s): {sorted(unknown)}')
        with self._connect() as conn:
            conn.executemany('INSERT INTO prefs (key, value) VALUES (?, ?) '
                             'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                             [(k, json.dumps(v)) for k, v in values.items()])

    def save_xe_table(self, base, date, rows):
        """Keep one fetched xe.com table whole, replacing any earlier one for that date."""
        now = utc_now_str()
        with self._connect() as conn:
            conn.execute('DELETE FROM xe_rates WHERE base = ? AND date = ?', (base, date))
            conn.executemany('INSERT INTO xe_rates (base, date, code, name, units_per, per_unit, fetched_at_utc) '
                             'VALUES (?, ?, ?, ?, ?, ?, ?)',
                             [(base, date, r['code'], r['name'], r['units_per'], r['per_unit'], now) for r in rows])

    def xe_table(self, base, date):
        """The saved table for a date: {'fetched_at_utc', 'rates': {code: per_unit}}, or None."""
        with self._connect() as conn:
            rows = conn.execute('SELECT code, per_unit, fetched_at_utc FROM xe_rates WHERE base = ? AND date = ?',
                                (base, date)).fetchall()
        if not rows:
            return None
        return {'fetched_at_utc': max(r['fetched_at_utc'] for r in rows), 'rates': {r['code']: r['per_unit'] for r in rows}}

    def xe_dates(self, base) -> dict:
        """{date: fetched_at_utc} for every saved table."""
        with self._connect() as conn:
            return {r[0]: r[1] for r in conn.execute(
                'SELECT date, MAX(fetched_at_utc) FROM xe_rates WHERE base = ? GROUP BY date', (base,))}

    # -- queue ---------------------------------------------------------------

    @staticmethod
    def _decode_item(row):
        if row is None:
            return None
        out = dict(row)
        out['plan'] = json.loads(out['plan'])
        out['preview'] = json.loads(out['preview'])
        out['rows'] = json.loads(out['rows']) if out['rows'] else []
        out['edited'] = bool(out['edited'])
        return out

    def enqueue(self, source, key, title, plan, preview, db_fingerprint, replace=False) -> tuple:
        """Add a pending batch. Returns (id, None), or (None, existing id)
        when that key is already pending and `replace` isn't set; with
        `replace` the pending one is superseded."""
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute("SELECT id FROM queue WHERE source = ? AND key = ? AND status = 'pending'",
                               (source, key)).fetchone()
            if old and not replace:
                return None, old[0]
            if old:
                conn.execute("UPDATE queue SET status = 'superseded', decided_at_utc = ? WHERE id = ?",
                             (utc_now_str(), old[0]))
            cur = conn.execute(
                'INSERT INTO queue (source, key, title, plan, submitted_at_utc, db_fingerprint, preview, status) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (source, key, title, json.dumps(plan), utc_now_str(), db_fingerprint, json.dumps(preview)))
            return cur.lastrowid, None

    def item(self, item_id):
        with self._connect() as conn:
            return self._decode_item(conn.execute(
                'SELECT q.*, w.undone_at_utc FROM queue q LEFT JOIN writes w ON w.id = q.write_id WHERE q.id = ?',
                (item_id,)).fetchone())

    def items(self, source=None, status=None, key=None, limit=100) -> list:
        where, args = [], []
        for col, value in (('q.source', source), ('q.status', status), ('q.key', key)):
            if value:
                where.append(f'{col} = ?')
                args.append(value)
        sql = ('SELECT q.*, w.undone_at_utc FROM queue q LEFT JOIN writes w ON w.id = q.write_id'
               + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY q.id DESC LIMIT ?')
        with self._connect() as conn:
            return [self._decode_item(r) for r in conn.execute(sql, args + [limit])]

    def queue_summary(self) -> dict:
        """{'pending': n, 'oldest_utc': submit time of the oldest pending, 'by_source': {source: n}}."""
        with self._connect() as conn:
            rows = conn.execute("SELECT source, COUNT(*), MIN(submitted_at_utc) FROM queue WHERE status = 'pending' "
                                'GROUP BY source').fetchall()
        return {'pending': sum(r[1] for r in rows), 'oldest_utc': min((r[2] for r in rows), default=None),
                'by_source': {r[0]: r[1] for r in rows}}

    def queue_sources(self) -> list:
        with self._connect() as conn:
            return [r[0] for r in conn.execute('SELECT DISTINCT source FROM queue ORDER BY source')]

    def edit_item(self, item_id, plan, preview, db_fingerprint) -> bool:
        """Replace a pending batch's plan (edited before approving)."""
        with self._connect() as conn:
            cur = conn.execute("UPDATE queue SET plan = ?, preview = ?, db_fingerprint = ?, edited = 1 "
                               "WHERE id = ? AND status = 'pending'",
                               (json.dumps(plan), json.dumps(preview), db_fingerprint, item_id))
            return cur.rowcount == 1

    def decide(self, item_id, status, note=None, write_id=None, rows=()) -> bool:
        """Move a pending batch to approved or rejected. False if it was no
        longer pending (decided elsewhere meanwhile)."""
        with self._connect() as conn:
            cur = conn.execute("UPDATE queue SET status = ?, decided_at_utc = ?, note = ?, write_id = ?, rows = ? "
                               "WHERE id = ? AND status = 'pending'",
                               (status, utc_now_str(), note, write_id, json.dumps(list(rows)), item_id))
            return cur.rowcount == 1
