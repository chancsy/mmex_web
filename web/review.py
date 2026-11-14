"""What a queued batch does, laid out for review: the engine's events in
plan order, with each run of balance checks as a divider, a twin's two
rows (a transfer whose notes follow the Twin pattern, plus the expense
row it pays for) merged into one entry, and badges for the things a
reviewer looks for. Pure functions of a preview; the template draws it."""

import re

PLACEHOLDERS = {'{payee}': 'payee', '{expense_account}': 'expense', '{to_account}': 'to'}


def twin_matcher(pattern):
    """A function notes -> {'payee', 'expense', 'to'} (whichever the
    pattern has) or None, for the Twin notes pattern."""
    parts = re.split(r'(\{payee\}|\{expense_account\}|\{to_account\})', pattern or '')
    names = [PLACEHOLDERS[p] for p in parts if p in PLACEHOLDERS]
    if not names or len(set(names)) != len(names):
        return lambda notes: None
    rx = re.compile('^' + ''.join(f'(?P<{PLACEHOLDERS[p]}>.+?)' if p in PLACEHOLDERS else re.escape(p)
                                  for p in parts) + '$', re.S)

    def match(notes):
        m = rx.match(notes or '')
        return m.groupdict() if m else None
    return match


def _is_twin_transfer(row, match):
    """The twin fields when `row` is a transfer whose notes follow the
    pattern (and name its own To account, if the pattern has one)."""
    if not row or row.get('type') != 'Transfer':
        return None
    found = match(row.get('notes'))
    if found and found.get('to', row.get('to_account')) == row.get('to_account'):
        return found
    return None


def _same_money(a, b):
    return abs(float(a or 0) - float(b or 0)) < 0.005


def label(row, match):
    """(title, route) for a row: a transfer is named after its twin payee
    or its notes, with "From -> To" as the route; anything else after its
    payee, in its account."""
    if row.get('type') != 'Transfer':
        return row.get('payee') or '—', row.get('account')
    route = f"{row.get('account')} → {row.get('to_account')}"
    twin = _is_twin_transfer(row, match)
    if twin and twin.get('payee'):
        return twin['payee'], route
    notes = (row.get('notes') or '').strip().splitlines()
    title = notes[0] if notes else 'Transfer'
    return (title if len(title) <= 60 else title[:57] + '…'), route


def _signed(row):
    """The amount as it moves the row's own account."""
    amount = float(row.get('amount') or 0)
    return amount if row.get('type') == 'Deposit' else -amount


def build(preview, twin_pattern, summary=None) -> dict:
    """{'entries', 'counts', 'balances', 'summary', 'warnings'} for the
    review page. Entries (in plan order) are dicts with 'kind': row, twin,
    change, delete, flag or checks; rows and twins have 'filter' (twin,
    transfer, own), changes and deletes 'change'."""
    events = (preview or {}).get('events') or []
    match = twin_matcher(twin_pattern)
    new_payees = {e['name'] for e in events if e.get('kind') == 'new_payee'}
    new_tags = {e['name'] for e in events if e.get('kind') == 'new_tag'}
    adds = [(i, e['row']) for i, e in enumerate(events) if e.get('kind') == 'add']

    # Twins: a pattern transfer plus the expense row with the same date and
    # amount, in the account and with the payee its notes name.
    partner, used = {}, set()
    for i, row in adds:
        found = _is_twin_transfer(row, match)
        if not found:
            continue
        for j, other in adds:
            if j in used or j == i or other.get('type') != 'Withdrawal':
                continue
            if (other['date'] == row['date'] and _same_money(other['amount'], row['amount'])
                    and found.get('expense', other['account']) == other['account']
                    and found.get('payee', other.get('payee')).lower() == (other.get('payee') or '').lower()):
                partner[i], partner[j] = j, i
                used.update((i, j))
                break

    # Splits: a transfer and an expense row from the same account on the
    # same day, the transfer's notes naming the expense's payee.
    split = set()
    for i, row in adds:
        if row.get('type') != 'Transfer' or i in partner:
            continue
        for j, other in adds:
            payee = (other.get('payee') or '').strip().lower()
            if (j not in partner and other.get('type') == 'Withdrawal' and other['account'] == row['account']
                    and other['date'] == row['date'] and payee and payee in (row.get('notes') or '').lower()):
                split.update((i, j))

    entries, checks = [], None
    counts = {'new': 0, 'twin': 0, 'transfer': 0, 'own': 0, 'change': 0, 'checks': 0, 'checks_ok': 0,
              'new_payees': len(new_payees), 'new_tags': len(new_tags)}

    def badges_for(row, i):
        out = []
        if i in split:
            out.append(('split', 'Split'))
        if row.get('payee') in new_payees:
            out.append(('new', 'New payee'))
        if any(t in new_tags for t in row.get('tags') or []):
            out.append(('new', 'New tag'))
        return out

    for i, event in enumerate(events):
        kind = event.get('kind')
        if kind in ('check', 'absent'):
            if checks is None:
                checks = {'kind': 'checks', 'items': [], 'ok': True}
                entries.append(checks)
            ok = event.get('ok', True) if kind == 'check' else True
            checks['items'].append(event)
            checks['ok'] = checks['ok'] and ok
            counts['checks'] += 1
            counts['checks_ok'] += 1 if ok else 0
            continue
        checks = None
        if kind == 'add':
            row = event['row']
            counts['new'] += 1
            if i in partner:
                j = partner[i]
                if j < i:
                    continue  # drawn with its partner, which came first
                transfer, expense = (row, events[j]['row']) if row['type'] == 'Transfer' else (events[j]['row'], row)
                counts['twin'] += 1
                entries.append({'kind': 'twin', 'filter': 'twin', 'transfer': transfer, 'expense': expense,
                                'amount': _signed(transfer), 'badges': badges_for(expense, j) + badges_for(transfer, i),
                                'why': event.get('why') or events[j].get('why'), 'op': event.get('op')})
                continue
            filt = 'transfer' if row['type'] == 'Transfer' else 'own'
            counts[filt] += 1
            title, route = label(row, match)
            entries.append({'kind': 'row', 'filter': filt, 'row': row, 'amount': _signed(row), 'title': title, 'route': route,
                            'badges': badges_for(row, i), 'why': event.get('why'), 'op': event.get('op')})
        elif kind == 'update':
            counts['change'] += 1
            before, after = event['before'], event['after']
            fields = [{'name': k, 'before': before.get(k), 'after': after.get(k)} for k in event.get('changed') or []]
            badges = [('change', 'Changed')]
            if _is_twin_transfer(after, match) or _is_twin_transfer(before, match):
                badges.append(('twin', 'Twin'))
            title, route = label(after, match)
            entries.append({'kind': 'change', 'filter': 'change', 'row': before, 'after': after, 'fields': fields,
                            'title': title, 'route': route,
                            'amount': _signed(before), 'badges': badges, 'why': event.get('why'), 'op': event.get('op')})
        elif kind == 'delete':
            counts['change'] += 1
            title, route = label(event['row'], match)
            entries.append({'kind': 'delete', 'filter': 'change', 'row': event['row'], 'amount': _signed(event['row']),
                            'title': title, 'route': route, 'badges': [('delete', 'Deleted')],
                            'why': event.get('why'), 'op': event.get('op')})
        elif kind == 'flag':
            counts['change'] += 1
            entries.append({'kind': 'flag', 'filter': 'change', 'event': event, 'why': event.get('why')})

    balances = [dict(b, change=None if b.get('before') is None else round(b['after'] - b['before'], 2))
                for b in (preview or {}).get('balances') or []]
    warnings = [e['text'] for e in events if e.get('kind') == 'warning']
    return {'entries': entries, 'counts': counts, 'balances': balances, 'warnings': warnings,
            'summary': summary, 'has_events': bool(events)}
