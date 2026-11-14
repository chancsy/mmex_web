"""Plan ops against an open MMEX DB connection: name lookups, row shapes,
add/update/delete/flag/check, balances, and undo.

Rows are shaped the way the MMEX GUI writes them (checked against rows
MMEX itself wrote). The column layout comes from the DB's checked Schema
(see schema.py), so legacy columns are written only when the DB has them.

Standard library only. Nothing here opens, backs up or commits a DB; that
is batch.py's job.

Plan format: {"ops": [op, ...]}, applied in order, all or nothing.

    {"op": "add", "date": "YYYY-MM-DD", "type": "Withdrawal" | "Deposit" | "Transfer",
     "account": NAME, "amount": 12.5,            # always positive; the type gives the direction
     "payee": NAME, "category": "Parent:Child",  # Withdrawal/Deposit
     "to_account": NAME, "to_amount": 40.1,      # Transfer; to_amount only across currencies
     "notes": "", "number": "", "status": "" | "F" | "R", "tags": [NAME, ...],
     "create_payee": false,      # add the payee if it doesn't exist
     "create_tags": false,       # add any tag that doesn't exist
     "allow_duplicate": false}   # same account, type, day, amount and payee/to_account
    {"op": "update", "id": TRANSID, "expect": {FIELD: VALUE, ...},
     "set": {FIELD: VALUE, ...}, "create_payee": false, "create_tags": false}
        # FIELD: date, account, to_account, payee, category, amount, to_amount,
        # notes, status ("", F, R, V), number, tags, type. Changing the type
        # to Transfer needs to_account; away from Transfer needs payee (and
        # usually a category).
    {"op": "delete", "id": TRANSID, "expect": {...}}   # soft delete, to MMEX's trash
    {"op": "flag", "account": NAME, "from": DATE, "to": DATE, "status": "R" | "F",
     "only": ["", ...]}          # which current statuses to change (default: unflagged)
    {"op": "check", "account": NAME, "as_of": DATE, "balance": 123.45}
    {"op": "absent", "account": NAME, "from": DATE, "to": DATE,
     "type": "Withdrawal", "payee": NAME}   # type and payee optional
        # A check: fails when the account already has a live row in that
        # range (e.g. this month's bill). Put it before the add it guards.

Any op may carry "why": TEXT, the sender's reason for it, shown to a
reviewer next to what the op does; it changes nothing in the DB.

`expect` lists fields the row must still have (plan-level names, as
friendly() returns them); otherwise the op is refused with ExpectError.
"""

import datetime
import time

CATEG_DELIM = ':'
TRANSFER_CATEGORY = 'Transfer'
TYPES = ('Withdrawal', 'Deposit', 'Transfer')
ADD_STATUSES = ('', 'R', 'F')
SET_STATUSES = ('', 'R', 'F', 'V')
UPDATABLE = {'type', 'date', 'account', 'to_account', 'payee', 'category', 'amount', 'to_amount',
             'notes', 'status', 'number', 'tags'}
TRANS = 'CHECKINGACCOUNT_V1'
LIVE_ROWS = "STATUS <> 'V' AND (DELETEDTIME IS NULL OR DELETEDTIME = '')"


class PlanError(Exception):
    pass


class ExpectError(PlanError):
    """An update/delete's `expect` no longer matches the row: someone changed
    it since the plan (or the edit form) was made."""


def utc_now_str() -> str:
    # MMEX stores LASTUPDATEDTIME/DELETEDTIME in UTC, ISO with a 'T'.
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def parse_date(value, what) -> str:
    try:
        return datetime.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise PlanError(f'{what}: "{value}" is not a YYYY-MM-DD date')


def parse_amount(value, what) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f'{what}: amount must be a number, got {value!r}')
    amount = round(float(value), 2)
    if amount <= 0:
        raise PlanError(f'{what}: amount must be positive (the type gives the direction), got {value}')
    return amount


class Ids:
    """MMEX 1.8+ IDs are the creation time in UTC microseconds. Hand them out
    the same way, strictly increasing so two rows in one run never collide."""

    def __init__(self):
        self.last = 0

    def next(self, conn, table, col) -> int:
        while True:
            self.last = max(time.time_ns() // 1000, self.last + 1)
            if conn.execute(f'SELECT 1 FROM {table} WHERE {col} = ?', (self.last,)).fetchone() is None:
                return self.last


class Ref:
    """Name <-> id lookups, loaded once per run (and refreshed when a payee
    is created)."""

    def __init__(self, conn):
        self.conn = conn
        self.accounts = {r[0]: {'id': r[0], 'name': r[1], 'status': r[2], 'currency': r[3], 'initial': r[4] or 0}
                         for r in conn.execute('SELECT ACCOUNTID, ACCOUNTNAME, STATUS, CURRENCYID, INITIALBAL FROM ACCOUNTLIST_V1')}
        self.categories = {r[0]: {'id': r[0], 'name': r[1], 'active': r[2], 'parent': r[3]}
                           for r in conn.execute('SELECT CATEGID, CATEGNAME, ACTIVE, PARENTID FROM CATEGORY_V1')}
        self.tags = {r[0]: {'id': r[0], 'name': r[1], 'active': r[2]}
                     for r in conn.execute('SELECT TAGID, TAGNAME, ACTIVE FROM TAG_V1')}
        self.load_payees()

    def load_payees(self):
        self.payees = {r[0]: {'id': r[0], 'name': r[1], 'active': r[2]}
                       for r in self.conn.execute('SELECT PAYEEID, PAYEENAME, ACTIVE FROM PAYEE_V1')}

    def category_path(self, categ_id) -> str:
        parts, seen = [], set()
        while categ_id is not None and categ_id in self.categories and categ_id not in seen:
            seen.add(categ_id)
            parts.append(self.categories[categ_id]['name'])
            categ_id = self.categories[categ_id]['parent']
        return CATEG_DELIM.join(reversed(parts)) if parts else ''

    def account_name(self, account_id) -> str:
        return self.accounts[account_id]['name'] if account_id in self.accounts else ''

    def payee_name(self, payee_id) -> str:
        return self.payees[payee_id]['name'] if payee_id in self.payees else ''

    @staticmethod
    def _near(names, wanted, limit=5):
        w = wanted.lower()
        hits = [n for n in names if w in n.lower() or n.lower() in w]
        return sorted(hits)[:limit]

    def account(self, name, what, allow_closed=False) -> dict:
        if not isinstance(name, str) or not name:
            raise PlanError(f'{what}: account name missing')
        hits = [a for a in self.accounts.values() if a['name'] == name]
        if not hits:
            raise PlanError(f'{what}: no account named "{name}". Close matches: '
                            f'{self._near([a["name"] for a in self.accounts.values()], name)}')
        if hits[0]['status'] != 'Open' and not allow_closed:
            raise PlanError(f'{what}: account "{name}" is {hits[0]["status"]}')
        return hits[0]

    def payee(self, name, what):
        # PAYEENAME is UNIQUE COLLATE NOCASE, so a case-insensitive match is
        # exactly how MMEX itself identifies a payee.
        hits = [p for p in self.payees.values() if p['name'].lower() == str(name).lower()]
        return hits[0] if hits else None

    def category(self, path, what) -> dict:
        if not isinstance(path, str) or not path:
            raise PlanError(f'{what}: category path missing')
        hits = [c for c in self.categories.values() if self.category_path(c['id']) == path]
        if not hits:
            leaf = path.split(CATEG_DELIM)[-1]
            near = [self.category_path(c['id']) for c in self.categories.values()
                    if leaf.lower() in c['name'].lower()]
            raise PlanError(f'{what}: no category "{path}". Similar: {sorted(near)[:8]}')
        return hits[0]

    def tag(self, name, what) -> dict:
        hits = [t for t in self.tags.values() if t['name'].lower() == str(name).lower()]
        if not hits:
            raise PlanError(f'{what}: no tag "{name}" (set "create_tags": true to add it). Similar: '
                            f'{self._near([t["name"] for t in self.tags.values()], name)}')
        return hits[0]


def check_tag_name(name, what) -> str:
    """MMEX's own tag dialog refuses spaces, and a name starting with & or |
    (they're operators in its tag filter)."""
    name = str(name).strip()
    if not name or len(name) > 100 or any(ch.isspace() for ch in name) or name[0] in '&|' or ',' in name:
        raise PlanError(f'{what}: "{name}" is not a valid tag name (no spaces or commas, not starting with & or |)')
    return name


def fetch_row(conn, schema, trans_id) -> dict:
    cols = schema.cols(TRANS)
    row = conn.execute(f'SELECT {", ".join(cols)} FROM {TRANS} WHERE TRANSID = ?', (trans_id,)).fetchone()
    return dict(zip(cols, row)) if row else None


def row_tag_ids(conn, trans_id) -> list:
    return sorted(r[0] for r in conn.execute(
        "SELECT TAGID FROM TAGLINK_V1 WHERE REFTYPE = 'Transaction' AND REFID = ?", (trans_id,)))


def is_deleted(row) -> bool:
    return bool(row['DELETEDTIME'])


def friendly(ref, conn, row) -> dict:
    """The row as plan-level names, used for `expect` and for previews."""
    out = {
        'id': row['TRANSID'],
        'date': (row['TRANSDATE'] or '')[:10],
        'type': row['TRANSCODE'],
        'account': ref.account_name(row['ACCOUNTID']),
        'amount': round(row['TRANSAMOUNT'] or 0, 2),
        'category': ref.category_path(row['CATEGID']),
        'notes': row['NOTES'] or '',
        'status': row['STATUS'] or '',
        'number': row['TRANSACTIONNUMBER'] or '',
        'tags': sorted(ref.tags[t]['name'] for t in row_tag_ids(conn, row['TRANSID']) if t in ref.tags),
    }
    if row['TRANSCODE'] == 'Transfer':
        out['to_account'] = ref.account_name(row['TOACCOUNTID'])
        out['to_amount'] = round(row['TOTRANSAMOUNT'] or 0, 2)
    else:
        out['payee'] = ref.payee_name(row['PAYEEID'])
    return out


def describe(f) -> str:
    who = f'-> {f["to_account"]}' if f['type'] == 'Transfer' else f.get('payee', '')
    amt = f'{f["amount"]:.2f}'
    if f['type'] == 'Transfer' and abs(f.get('to_amount', f['amount']) - f['amount']) > 0.001:
        amt += f' (to {f["to_amount"]:.2f})'
    tags = f' #{",".join(f["tags"])}' if f.get('tags') else ''
    status = f' [{f["status"]}]' if f['status'] else ''
    notes = f' "{f["notes"]}"' if f['notes'] else ''
    return f'{f["date"]} {f["type"]:<10} {f["account"]} {amt} {who} {f["category"]}{notes}{status}{tags}'


def balance(conn, account_id, as_of=None) -> float:
    """MMEX's balance: INITIALBAL plus every non-void, non-deleted row up to
    the end of as_of (verified to the cent against bank statements)."""
    date_cond, args = '', []
    if as_of:
        date_cond = ' AND substr(TRANSDATE, 1, 10) <= ?'
        args = [as_of]
    out_sum = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN TRANSCODE = 'Deposit' THEN TRANSAMOUNT ELSE -TRANSAMOUNT END), 0) "
        f'FROM {TRANS} WHERE ACCOUNTID = ? AND {LIVE_ROWS}{date_cond}', [account_id] + args).fetchone()[0]
    in_sum = conn.execute(
        f"SELECT COALESCE(SUM(TOTRANSAMOUNT), 0) FROM {TRANS} "
        f"WHERE TRANSCODE = 'Transfer' AND TOACCOUNTID = ? AND {LIVE_ROWS}{date_cond}", [account_id] + args).fetchone()[0]
    initial = conn.execute('SELECT COALESCE(INITIALBAL, 0) FROM ACCOUNTLIST_V1 WHERE ACCOUNTID = ?', (account_id,)).fetchone()[0]
    return round(initial + out_sum + in_sum, 2)


def find_duplicates(conn, cols) -> list:
    if cols['TRANSCODE'] == 'Transfer':
        match_col, match_val = 'TOACCOUNTID', cols['TOACCOUNTID']
    else:
        match_col, match_val = 'PAYEEID', cols['PAYEEID']
    return [r[0] for r in conn.execute(
        f'SELECT TRANSID FROM {TRANS} WHERE ACCOUNTID = ? AND TRANSCODE = ? '
        f'AND substr(TRANSDATE, 1, 10) = ? AND ABS(TRANSAMOUNT - ?) < 0.005 AND {match_col} = ? '
        f'AND {LIVE_ROWS}',
        (cols['ACCOUNTID'], cols['TRANSCODE'], cols['TRANSDATE'][:10], cols['TRANSAMOUNT'], match_val))]


class Applier:
    """Runs plan ops on an open connection inside the caller's transaction.

    Outputs, filled as it goes:
    - preview: human-readable text lines
    - events: the same information as dicts, for the web UI and the API
    - journal: what undo needs to reverse each op
    - check_results: one bool per check op
    """

    def __init__(self, conn, schema):
        self.conn = conn
        self.schema = schema
        self.ref = Ref(conn)
        self.ids = Ids()
        self.journal = []
        self.preview = []
        self.events = []
        self.touched = set()
        self.check_results = []

    def warn(self, text):
        self.preview.append(f'  warning: {text}')
        self.events.append({'kind': 'warning', 'text': text})

    def row(self, trans_id):
        return fetch_row(self.conn, self.schema, trans_id)

    def view(self, row):
        return friendly(self.ref, self.conn, row)

    # -- shared field resolution ------------------------------------------

    def resolve_fields(self, spec, what, base=None, create_payee=False) -> dict:
        """Turn plan-level fields into CHECKINGACCOUNT_V1 columns. `base` is
        the existing row for an update (only fields present in spec change)."""
        cols = dict(base) if base else {}
        trans_type = spec.get('type') or cols.get('TRANSCODE')
        type_changed = base is not None and 'type' in spec and spec['type'] != base['TRANSCODE']
        if trans_type not in TYPES:
            raise PlanError(f'{what}: type must be one of {TYPES}, got {spec.get("type")!r}')
        cols['TRANSCODE'] = trans_type
        if base is None:
            missing = [k for k in ('date', 'account', 'amount') if k not in spec]
            if missing:
                raise PlanError(f'{what}: missing {missing}')

        if 'date' in spec:
            cols['TRANSDATE'] = parse_date(spec['date'], what) + 'T00:00:00'
        if 'account' in spec:
            cols['ACCOUNTID'] = self.ref.account(spec['account'], what)['id']
        if 'amount' in spec:
            cols['TRANSAMOUNT'] = parse_amount(spec['amount'], what)
        if 'notes' in spec:
            cols['NOTES'] = str(spec['notes'])
        if 'number' in spec:
            cols['TRANSACTIONNUMBER'] = str(spec['number'])

        if trans_type == 'Transfer':
            if 'payee' in spec:
                raise PlanError(f'{what}: a Transfer has to_account, not payee')
            if 'to_account' in spec:
                cols['TOACCOUNTID'] = self.ref.account(spec['to_account'], what)['id']
            if cols.get('TOACCOUNTID') in (None, -1):
                raise PlanError(f'{what}: Transfer needs to_account')
            if cols['TOACCOUNTID'] == cols.get('ACCOUNTID'):
                raise PlanError(f'{what}: Transfer to the same account')
            cols['PAYEEID'] = -1
            same_currency = (self.ref.accounts[cols['ACCOUNTID']]['currency']
                             == self.ref.accounts[cols['TOACCOUNTID']]['currency'])
            amount_moved = base is None or type_changed or any(k in spec for k in ('amount', 'account', 'to_account'))
            if 'to_amount' in spec:
                to_amount = parse_amount(spec['to_amount'], what)
                if same_currency and abs(to_amount - cols['TRANSAMOUNT']) > 0.001:
                    raise PlanError(f'{what}: same-currency transfer but to_amount {to_amount} != amount {cols["TRANSAMOUNT"]}')
                cols['TOTRANSAMOUNT'] = to_amount
            elif amount_moved and same_currency:
                cols['TOTRANSAMOUNT'] = cols['TRANSAMOUNT']
            elif amount_moved:
                raise PlanError(f'{what}: {self.ref.account_name(cols["ACCOUNTID"])} -> '
                                f'{self.ref.account_name(cols["TOACCOUNTID"])} changes currency; give to_amount')
            if 'category' in spec or base is None:
                cols['CATEGID'] = self.ref.category(spec.get('category', TRANSFER_CATEGORY), what)['id']
        else:
            if 'to_account' in spec or 'to_amount' in spec:
                raise PlanError(f'{what}: to_account/to_amount only apply to a Transfer')
            cols['TOACCOUNTID'] = -1
            if 'payee' in spec:
                payee = self.ref.payee(spec['payee'], what)
                if payee is None:
                    if not create_payee:
                        raise PlanError(f'{what}: no payee "{spec["payee"]}" (set "create_payee": true to add it). '
                                        f'Close matches: {Ref._near([p["name"] for p in self.ref.payees.values()], spec["payee"])}')
                    payee = self.create_payee(spec['payee'], spec.get('category'), what)
                elif not payee['active']:
                    self.warn(f'payee "{payee["name"]}" is marked inactive in MMEX')
                cols['PAYEEID'] = payee['id']
            if cols.get('PAYEEID') in (None, -1):
                raise PlanError(f'{what}: {trans_type} needs a payee')
            if 'category' in spec:
                cols['CATEGID'] = self.ref.category(spec['category'], what)['id']
            if cols.get('CATEGID') in (None, -1):
                raise PlanError(f'{what}: {trans_type} needs a category')
            # A former transfer has PAYEEID -1, so the check above already
            # demands a payee; its TOTRANSAMOUNT was the received amount.
            if base is None or 'amount' in spec or type_changed:
                cols['TOTRANSAMOUNT'] = cols['TRANSAMOUNT']

        if 'CATEGID' in cols and cols['CATEGID'] in self.ref.categories and not self.ref.categories[cols['CATEGID']]['active']:
            self.warn(f'category "{self.ref.category_path(cols["CATEGID"])}" is marked inactive in MMEX')
        return cols

    def resolve_status(self, value, allowed, what) -> str:
        if value not in allowed:
            raise PlanError(f'{what}: status must be one of {allowed}, got {value!r}')
        return value

    def resolve_tags(self, names, what, create=False) -> list:
        if not isinstance(names, list):
            raise PlanError(f'{what}: tags must be a list of names')
        ids = set()
        for name in names:
            if create and not any(t['name'].lower() == str(name).lower() for t in self.ref.tags.values()):
                ids.add(self.create_tag(name, what)['id'])
            else:
                ids.add(self.ref.tag(name, what)['id'])
        return sorted(ids)

    def create_tag(self, name, what) -> dict:
        name = check_tag_name(name, what)
        tag_id = self.ids.next(self.conn, 'TAG_V1', 'TAGID')
        self.conn.execute('INSERT INTO TAG_V1 (TAGID, TAGNAME, ACTIVE) VALUES (?, ?, 1)', (tag_id, name))
        self.journal.append({'op': 'create_tag', 'tag_id': tag_id, 'name': name})
        self.preview.append(f'+ new tag "{name}"')
        self.events.append({'kind': 'new_tag', 'name': name, 'id': tag_id})
        self.ref.tags[tag_id] = {'id': tag_id, 'name': name, 'active': 1}
        return self.ref.tags[tag_id]

    def create_payee(self, name, category_path, what) -> dict:
        categ_id = self.ref.category(category_path, what)['id'] if category_path else -1
        payee_id = self.ids.next(self.conn, 'PAYEE_V1', 'PAYEEID')
        # Same shape as a GUI-created payee row.
        cols = {'PAYEEID': payee_id, 'PAYEENAME': name, 'CATEGID': categ_id, 'NUMBER': '', 'WEBSITE': '',
                'NOTES': '', 'ACTIVE': 1, 'PATTERN': ''}
        cols.update({c: None for c in self.schema.legacy('PAYEE_V1')})
        names = self.schema.cols('PAYEE_V1')
        self.conn.execute(f'INSERT INTO PAYEE_V1 ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})',
                          [cols[c] for c in names])
        self.journal.append({'op': 'create_payee', 'payee_id': payee_id, 'name': name})
        self.preview.append(f'+ new payee "{name}"')
        self.events.append({'kind': 'new_payee', 'name': name, 'id': payee_id})
        self.ref.load_payees()
        return self.ref.payees[payee_id]

    def set_tags(self, trans_id, tag_ids) -> list:
        self.conn.execute("DELETE FROM TAGLINK_V1 WHERE REFTYPE = 'Transaction' AND REFID = ?", (trans_id,))
        link_ids = []
        for tag_id in tag_ids:
            link_id = self.ids.next(self.conn, 'TAGLINK_V1', 'TAGLINKID')
            self.conn.execute("INSERT INTO TAGLINK_V1 (TAGLINKID, REFTYPE, REFID, TAGID) VALUES (?, 'Transaction', ?, ?)",
                              (link_id, trans_id, tag_id))
            link_ids.append(link_id)
        return link_ids

    def check_expect(self, row, expect, what):
        if not expect:
            return
        if not isinstance(expect, dict):
            raise PlanError(f'{what}: expect must be an object')
        current = self.view(row)
        for key, want in expect.items():
            have = current.get(key)
            if key in ('amount', 'to_amount'):
                ok = have is not None and abs(have - round(float(want), 2)) < 0.005
            elif key == 'tags':
                ok = sorted(t.lower() for t in have) == sorted(str(t).lower() for t in want)
            else:
                ok = have == want
            if not ok:
                raise ExpectError(f'{what}: expected {key}={want!r}, row has {have!r}')

    def live_row(self, trans_id, what) -> dict:
        row = self.row(trans_id)
        if row is None:
            raise PlanError(f'{what}: no transaction with id {trans_id}')
        if is_deleted(row):
            raise PlanError(f'{what}: transaction {trans_id} is already deleted')
        return row

    def touch(self, cols):
        self.touched.add(cols['ACCOUNTID'])
        if cols.get('TOACCOUNTID') not in (None, -1):
            self.touched.add(cols['TOACCOUNTID'])

    # -- ops ---------------------------------------------------------------

    def op_add(self, spec, what):
        cols = self.resolve_fields(spec, what, create_payee=bool(spec.get('create_payee')))
        cols['STATUS'] = self.resolve_status(spec.get('status', ''), ADD_STATUSES, what)
        tag_ids = self.resolve_tags(spec.get('tags', []), what, create=bool(spec.get('create_tags')))
        dups = find_duplicates(self.conn, cols)
        if dups and not spec.get('allow_duplicate'):
            shown = '; '.join(describe(self.view(self.row(d))) + f' (id {d})' for d in dups)
            raise PlanError(f'{what}: looks like a duplicate of {shown}. Set "allow_duplicate": true if it really is a second one.')
        cols['TRANSID'] = self.ids.next(self.conn, TRANS, 'TRANSID')
        cols.setdefault('NOTES', '')
        cols.setdefault('TRANSACTIONNUMBER', '')
        # GUI defaults, as seen on every recent MMEX-written row.
        cols.update({'FOLLOWUPID': -1, 'LASTUPDATEDTIME': utc_now_str(), 'DELETEDTIME': '', 'COLOR': -1})
        cols.update({c: None for c in self.schema.legacy(TRANS)})
        names = self.schema.cols(TRANS)
        self.conn.execute(f'INSERT INTO {TRANS} ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})',
                          [cols[c] for c in names])
        link_ids = self.set_tags(cols['TRANSID'], tag_ids)
        row = self.row(cols['TRANSID'])
        self.journal.append({'op': 'add', 'row': row, 'taglink_ids': link_ids})
        view = self.view(row)
        self.preview.append(f'+ {describe(view)}  (id {cols["TRANSID"]})')
        self.events.append({'kind': 'add', 'row': view})
        self.touch(cols)

    def op_update(self, spec, what):
        row = self.live_row(spec.get('id'), what)
        self.check_expect(row, spec.get('expect'), what)
        changes = spec.get('set')
        if not isinstance(changes, dict) or not changes:
            raise PlanError(f'{what}: update needs a non-empty "set"')
        unknown = set(changes) - UPDATABLE
        if unknown:
            raise PlanError(f'{what}: cannot set {sorted(unknown)}')
        before = dict(row)
        before_tags = row_tag_ids(self.conn, row['TRANSID'])
        before_view = self.view(row)
        self.touch(row)
        cols = self.resolve_fields(changes, what, base=row, create_payee=bool(spec.get('create_payee')))
        if 'status' in changes:
            cols['STATUS'] = self.resolve_status(changes['status'], SET_STATUSES, what)
        cols['LASTUPDATEDTIME'] = utc_now_str()
        names = self.schema.cols(TRANS)[1:]
        self.conn.execute(f'UPDATE {TRANS} SET {", ".join(c + " = ?" for c in names)} WHERE TRANSID = ?',
                          [cols[c] for c in names] + [row['TRANSID']])
        if 'tags' in changes:
            self.set_tags(row['TRANSID'], self.resolve_tags(changes['tags'], what, create=bool(spec.get('create_tags'))))
        after = self.row(row['TRANSID'])
        self.journal.append({'op': 'update', 'before': before, 'before_tags': before_tags,
                             'after': after, 'after_tags': row_tag_ids(self.conn, row['TRANSID'])})
        after_view = self.view(after)
        changed = [k for k in after_view if before_view.get(k) != after_view.get(k)]
        diff = ', '.join(f'{k}: {before_view.get(k)!r} -> {after_view.get(k)!r}' for k in changed)
        self.preview.append(f'~ {describe(before_view)}  (id {row["TRANSID"]})\n    {diff or "no change"}')
        self.events.append({'kind': 'update', 'before': before_view, 'after': after_view, 'changed': changed})
        self.touch(after)

    def op_delete(self, spec, what):
        row = self.live_row(spec.get('id'), what)
        self.check_expect(row, spec.get('expect'), what)
        # MMEX's own delete only stamps DELETEDTIME; LASTUPDATEDTIME is left as is.
        self.conn.execute(f'UPDATE {TRANS} SET DELETEDTIME = ? WHERE TRANSID = ?', (utc_now_str(), row['TRANSID']))
        self.journal.append({'op': 'delete', 'before': row, 'after': self.row(row['TRANSID'])})
        view = self.view(row)
        self.preview.append(f'- {describe(view)}  (id {row["TRANSID"]})')
        self.events.append({'kind': 'delete', 'row': view})
        self.touch(row)

    def op_flag(self, spec, what):
        account = self.ref.account(spec.get('account'), what)
        start, end = parse_date(spec.get('from'), what), parse_date(spec.get('to'), what)
        status = self.resolve_status(spec.get('status'), ('R', 'F'), what)
        only = spec.get('only', [''])
        if not isinstance(only, list) or any(s not in SET_STATUSES for s in only):
            raise PlanError(f'{what}: only must be a list drawn from {SET_STATUSES}')
        rows = self.conn.execute(
            f'SELECT TRANSID, STATUS, LASTUPDATEDTIME FROM {TRANS} WHERE (ACCOUNTID = ? OR TOACCOUNTID = ?) '
            "AND substr(TRANSDATE, 1, 10) BETWEEN ? AND ? AND (DELETEDTIME IS NULL OR DELETEDTIME = '') "
            'ORDER BY TRANSDATE, TRANSID', (account['id'], account['id'], start, end)).fetchall()
        targets = [r for r in rows if (r[1] or '') in only and (r[1] or '') != status]
        now = utc_now_str()
        for trans_id, _, _ in targets:
            self.conn.execute(f'UPDATE {TRANS} SET STATUS = ?, LASTUPDATEDTIME = ? WHERE TRANSID = ?', (status, now, trans_id))
        self.journal.append({'op': 'flag', 'status': status, 'updated_at': now,
                             'changes': [{'id': t, 'old_status': s, 'old_updated': u} for t, s, u in targets]})
        counts = {}
        for r in rows:
            counts[r[1] or 'blank'] = counts.get(r[1] or 'blank', 0) + 1
        self.preview.append(f'* flag {account["name"]} {start}..{end}: {len(targets)} -> {status} '
                            f'(rows in range by status before: {counts})')
        self.events.append({'kind': 'flag', 'account': account['name'], 'from': start, 'to': end, 'status': status,
                            'ids': [t for t, _, _ in targets], 'before_counts': counts})

    def op_check(self, spec, what):
        account = self.ref.account(spec.get('account'), what, allow_closed=True)
        as_of = parse_date(spec.get('as_of'), what)
        if isinstance(spec.get('balance'), bool) or not isinstance(spec.get('balance'), (int, float)):
            raise PlanError(f'{what}: balance must be a number')
        want = round(float(spec['balance']), 2)
        have = balance(self.conn, account['id'], as_of)
        ok = abs(have - want) < 0.005
        self.check_results.append(ok)
        self.preview.append(f'{"PASS" if ok else "FAIL"} check {account["name"]} at {as_of}: MMEX {have:,.2f} vs statement {want:,.2f}'
                            + ('' if ok else f'  (diff {have - want:+,.2f})'))
        self.events.append({'kind': 'check', 'account': account['name'], 'as_of': as_of,
                            'mmex': have, 'statement': want, 'ok': ok})

    def op_absent(self, spec, what):
        """A check that the account has no live row (not void, not deleted)
        dated from..to, of `type` and with `payee` when given: e.g. "this
        month's bill isn't in MMEX yet". Put it before the add it guards."""
        account = self.ref.account(spec.get('account'), what, allow_closed=True)
        start, end = parse_date(spec.get('from'), what), parse_date(spec.get('to'), what)
        where, args = ['ACCOUNTID = ?', 'substr(TRANSDATE, 1, 10) BETWEEN ? AND ?', LIVE_ROWS], [account['id'], start, end]
        trans_type = spec.get('type')
        if trans_type is not None:
            if trans_type not in TYPES:
                raise PlanError(f'{what}: type must be one of {TYPES}')
            where.append('TRANSCODE = ?')
            args.append(trans_type)
        label = f'{trans_type or "row"} in {account["name"]} {start}..{end}'
        if spec.get('payee') is not None:
            payee = self.ref.payee(spec['payee'], what)
            where.append('PAYEEID = ?')
            args.append(payee['id'] if payee else -2)
            label += f' to {payee["name"] if payee else spec["payee"]}'
        ids = [r[0] for r in self.conn.execute(
            f'SELECT TRANSID FROM {TRANS} WHERE {" AND ".join(where)} ORDER BY TRANSDATE, TRANSID', args)]
        found = [dict(self.view(self.row(i)), id=i) for i in ids]
        ok = not found
        self.check_results.append(ok)
        self.preview.append(f'{"PASS" if ok else "FAIL"} absent: no {label}' if ok else
                            f'FAIL absent: MMEX already has {label}: '
                            + '; '.join(f'{describe(f)} (id {f["id"]})' for f in found))
        self.events.append({'kind': 'absent', 'account': account['name'], 'from': start, 'to': end,
                            'type': trans_type, 'payee': spec.get('payee'), 'ok': ok, 'found': found})

    def run(self, plan) -> dict:
        """Apply every op in order. Returns {account_id: balance before} for
        each account touched (None when first touched by a created row)."""
        ops = plan.get('ops') if isinstance(plan, dict) else None
        if not isinstance(ops, list) or not ops:
            raise PlanError('plan must be {"ops": [...]} with at least one op')
        before = {}
        handlers = {'add': self.op_add, 'update': self.op_update, 'delete': self.op_delete,
                    'flag': self.op_flag, 'check': self.op_check, 'absent': self.op_absent}
        for i, spec in enumerate(ops, 1):
            what = f'op {i} ({spec.get("op") if isinstance(spec, dict) else "?"})'
            if not isinstance(spec, dict) or spec.get('op') not in handlers:
                raise PlanError(f'{what}: op must be one of {sorted(handlers)}')
            # Snapshot balances of an account the first time any op touches it.
            for acct in self.accounts_for(spec):
                before.setdefault(acct, balance(self.conn, acct))
            first_event = len(self.events)
            handlers[spec['op']](spec, what)
            # Which op each event came from, and the sender's reason for it
            # (an optional "why" on any op), for a reviewer's display.
            why = spec.get('why')
            for event in self.events[first_event:]:
                event['op'] = i
                if isinstance(why, str) and why.strip():
                    event['why'] = why.strip()[:500]
            for acct in self.touched:
                before.setdefault(acct, None)
        return before

    def accounts_for(self, spec) -> list:
        ids = []
        for key in ('account', 'to_account'):
            for a in self.ref.accounts.values():
                if a['name'] == spec.get(key) or (spec.get('set') or {}).get(key) == a['name']:
                    ids.append(a['id'])
        if spec.get('op') in ('update', 'delete'):
            row = self.row(spec.get('id')) if isinstance(spec.get('id'), int) else None
            if row:
                ids += [row['ACCOUNTID']] + ([row['TOACCOUNTID']] if row['TOACCOUNTID'] not in (None, -1) else [])
        return ids


def _taglinks(conn, trans_id) -> list:
    return [list(r) for r in conn.execute(
        "SELECT TAGLINKID, TAGID FROM TAGLINK_V1 WHERE REFTYPE = 'Transaction' AND REFID = ? ORDER BY TAGLINKID", (trans_id,))]


def _set_tags(conn, trans_id, tag_ids):
    conn.execute("DELETE FROM TAGLINK_V1 WHERE REFTYPE = 'Transaction' AND REFID = ?", (trans_id,))
    for tag_id in tag_ids:
        conn.execute("INSERT INTO TAGLINK_V1 (TAGLINKID, REFTYPE, REFID, TAGID) VALUES (?, 'Transaction', ?, ?)",
                     (Ids().next(conn, 'TAGLINK_V1', 'TAGLINKID'), trans_id, tag_id))


def _insert(conn, table, row):
    cols = list(row)
    conn.execute(f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})', [row[c] for c in cols])


def undo_entries(conn, schema, entries, force) -> tuple:
    """Reverse a journal, newest first. Each step first checks the row is
    still exactly as the engine left it; if someone changed it since, stop
    (unless force) rather than overwrite their edit.

    Returns (lines, journal): `journal` lists what this undo did, in the
    same entry format, so the undo can itself be undone (a redo):
    - an added row that undo removed becomes a `remove` entry (the full row
      and its tag links); undoing it puts the row back with the same id
    - a restored row becomes an `update` entry (from the state undo found
      to the state it restored)
    - a removed payee or tag becomes `remove_payee` / `remove_tag`"""
    lines, journal = [], []
    names = schema.cols(TRANS)

    def same(current, expected, what):
        if current != expected and not force:
            raise PlanError(f'{what}: row changed since it was written (now {current}); not undoing. Use --force to override.')

    def restore(trans_id, target, target_tags=None):
        """Set the row (and optionally its tags) to `target`; journal it."""
        found, found_tags = fetch_row(conn, schema, trans_id), row_tag_ids(conn, trans_id)
        conn.execute(f'UPDATE {TRANS} SET {", ".join(c + " = ?" for c in names[1:])} WHERE TRANSID = ?',
                     [target[c] for c in names[1:]] + [trans_id])
        if target_tags is not None and target_tags != found_tags:
            _set_tags(conn, trans_id, target_tags)
        journal.append({'op': 'update', 'before': found, 'before_tags': found_tags,
                        'after': fetch_row(conn, schema, trans_id), 'after_tags': row_tag_ids(conn, trans_id)})

    for entry in reversed(entries):
        op = entry['op']
        if op == 'add':
            row = entry['row']
            same(fetch_row(conn, schema, row['TRANSID']), row, f'add {row["TRANSID"]}')
            links = _taglinks(conn, row['TRANSID'])
            conn.execute("DELETE FROM TAGLINK_V1 WHERE REFTYPE = 'Transaction' AND REFID = ?", (row['TRANSID'],))
            conn.execute(f'DELETE FROM {TRANS} WHERE TRANSID = ?', (row['TRANSID'],))
            journal.append({'op': 'remove', 'row': row, 'taglinks': links})
            lines.append(f'removed added row {row["TRANSID"]} ({row["TRANSDATE"][:10]} {row["TRANSCODE"]} {row["TRANSAMOUNT"]})')
        elif op == 'remove':
            row = entry['row']
            if fetch_row(conn, schema, row['TRANSID']) is not None:
                raise PlanError(f'restore {row["TRANSID"]}: a row with that id exists again; not restoring.')
            _insert(conn, TRANS, {c: row.get(c) for c in names})
            for link_id, tag_id in entry.get('taglinks', []):
                if conn.execute('SELECT 1 FROM TAGLINK_V1 WHERE TAGLINKID = ?', (link_id,)).fetchone():
                    link_id = Ids().next(conn, 'TAGLINK_V1', 'TAGLINKID')
                conn.execute("INSERT INTO TAGLINK_V1 (TAGLINKID, REFTYPE, REFID, TAGID) VALUES (?, 'Transaction', ?, ?)",
                             (link_id, row['TRANSID'], tag_id))
            journal.append({'op': 'add', 'row': fetch_row(conn, schema, row['TRANSID']),
                            'taglink_ids': [l[0] for l in _taglinks(conn, row['TRANSID'])]})
            lines.append(f'put back row {row["TRANSID"]} ({row["TRANSDATE"][:10]} {row["TRANSCODE"]} {row["TRANSAMOUNT"]})')
        elif op in ('update', 'delete'):
            before, after = entry['before'], entry['after']
            same(fetch_row(conn, schema, before['TRANSID']), after, f'{op} {before["TRANSID"]}')
            target_tags = None
            if op == 'update' and entry['before_tags'] != entry['after_tags']:
                same(row_tag_ids(conn, before['TRANSID']), entry['after_tags'], f'tags of {before["TRANSID"]}')
                target_tags = entry['before_tags']
            restore(before['TRANSID'], before, target_tags)
            lines.append(f'restored row {before["TRANSID"]} to its state before the {op}')
        elif op == 'flag':
            for change in entry['changes']:
                cur = conn.execute(f'SELECT STATUS, LASTUPDATEDTIME FROM {TRANS} WHERE TRANSID = ?', (change['id'],)).fetchone()
                same(list(cur) if cur else None, [entry['status'], entry['updated_at']], f'flag {change["id"]}')
                target = dict(fetch_row(conn, schema, change['id']), STATUS=change['old_status'],
                              LASTUPDATEDTIME=change['old_updated'])
                restore(change['id'], target)
            lines.append(f'restored status on {len(entry["changes"])} flagged row(s)')
        elif op == 'create_payee':
            used = conn.execute(f'SELECT COUNT(*) FROM {TRANS} WHERE PAYEEID = ?', (entry['payee_id'],)).fetchone()[0]
            if used:
                lines.append(f'kept payee "{entry["name"]}" (now used by {used} row(s))')
            else:
                cols = schema.cols('PAYEE_V1')
                found = conn.execute(f'SELECT {", ".join(cols)} FROM PAYEE_V1 WHERE PAYEEID = ?', (entry['payee_id'],)).fetchone()
                conn.execute('DELETE FROM PAYEE_V1 WHERE PAYEEID = ?', (entry['payee_id'],))
                if found:
                    journal.append({'op': 'remove_payee', 'row': dict(zip(cols, found))})
                lines.append(f'removed created payee "{entry["name"]}"')
        elif op == 'remove_payee':
            row = entry['row']
            if conn.execute('SELECT 1 FROM PAYEE_V1 WHERE PAYEEID = ? OR PAYEENAME = ?',
                            (row['PAYEEID'], row['PAYEENAME'])).fetchone():
                raise PlanError(f'payee "{row["PAYEENAME"]}" exists again; not restoring.')
            _insert(conn, 'PAYEE_V1', {c: row.get(c) for c in schema.cols('PAYEE_V1')})
            journal.append({'op': 'create_payee', 'payee_id': row['PAYEEID'], 'name': row['PAYEENAME']})
            lines.append(f'put back payee "{row["PAYEENAME"]}"')
        elif op == 'create_tag':
            used = conn.execute('SELECT COUNT(*) FROM TAGLINK_V1 WHERE TAGID = ?', (entry['tag_id'],)).fetchone()[0]
            if used:
                lines.append(f'kept tag "{entry["name"]}" (now used by {used} row(s))')
            else:
                found = conn.execute('SELECT TAGID, TAGNAME, ACTIVE FROM TAG_V1 WHERE TAGID = ?', (entry['tag_id'],)).fetchone()
                conn.execute('DELETE FROM TAG_V1 WHERE TAGID = ?', (entry['tag_id'],))
                if found:
                    journal.append({'op': 'remove_tag', 'row': dict(zip(('TAGID', 'TAGNAME', 'ACTIVE'), found))})
                lines.append(f'removed created tag "{entry["name"]}"')
        elif op == 'remove_tag':
            row = entry['row']
            if conn.execute('SELECT 1 FROM TAG_V1 WHERE TAGID = ? OR TAGNAME = ?', (row['TAGID'], row['TAGNAME'])).fetchone():
                raise PlanError(f'tag "{row["TAGNAME"]}" exists again; not restoring.')
            _insert(conn, 'TAG_V1', row)
            journal.append({'op': 'create_tag', 'tag_id': row['TAGID'], 'name': row['TAGNAME']})
            lines.append(f'put back tag "{row["TAGNAME"]}"')
        else:
            raise PlanError(f'unknown journal entry {op!r}')
    return lines, journal


# ---------------------------------------------------------------------------
# read-only queries
# ---------------------------------------------------------------------------

def find(conn, schema, ref, id=None, account=None, date_from=None, date_to=None, amount=None,
         payee=None, category=None, notes=None, include_deleted=False, limit=200) -> list:
    """Transactions as plan-level dicts, newest first."""
    cols = schema.cols(TRANS)
    where, params = [], []
    if id:
        where.append('TRANSID = ?')
        params.append(id)
    if account:
        acct = ref.account(account, '--account', allow_closed=True)
        where.append('(ACCOUNTID = ? OR TOACCOUNTID = ?)')
        params += [acct['id'], acct['id']]
    if date_from:
        where.append('substr(TRANSDATE, 1, 10) >= ?')
        params.append(date_from)
    if date_to:
        where.append('substr(TRANSDATE, 1, 10) <= ?')
        params.append(date_to)
    if amount is not None:
        where.append('(ABS(TRANSAMOUNT - ?) < 0.005 OR ABS(TOTRANSAMOUNT - ?) < 0.005)')
        params += [amount, amount]
    if notes:
        where.append('NOTES LIKE ?')
        params.append(f'%{notes}%')
    if not include_deleted:
        where.append("(DELETEDTIME IS NULL OR DELETEDTIME = '')")
    sql = (f'SELECT {", ".join(cols)} FROM {TRANS}'
           + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY TRANSDATE DESC, TRANSID DESC')
    out = []
    for raw in conn.execute(sql, params):
        row = dict(zip(cols, raw))
        view = friendly(ref, conn, row)
        if payee and payee.lower() not in (view.get('payee') or view.get('to_account') or '').lower():
            continue
        if category and category.lower() not in view['category'].lower():
            continue
        if include_deleted and row['DELETEDTIME']:
            view['deleted'] = row['DELETEDTIME']
        out.append(view)
        if len(out) >= limit:
            break
    return out


def lookup(ref, what, like=None) -> list:
    like = (like or '').lower()
    if what == 'accounts':
        rows = [f'{a["id"]:>4}  {a["status"]:<6}  cur={a["currency"]:<4} {a["name"]}' for a in ref.accounts.values()]
    elif what == 'payees':
        rows = [f'{"" if p["active"] else "(inactive) "}{p["name"]}' for p in ref.payees.values()]
    elif what == 'categories':
        rows = [f'{"" if c["active"] else "(inactive) "}{ref.category_path(c["id"])}' for c in ref.categories.values()]
    elif what == 'tags':
        rows = [f'{"" if t["active"] else "(inactive) "}{t["name"]}' for t in ref.tags.values()]
    else:
        raise PlanError(f'lookup: unknown kind {what!r}')
    return sorted(r for r in rows if like in r.lower())
