"""The app's one way to write the MMEX DB: engine.apply/undo under a lock,
with a backup per the policy and a history entry in app.db for undo.

gunicorn runs one worker, so this lock (plus SQLite's own BEGIN IMMEDIATE)
makes the app a single writer even with several threads.

Guards (a Syncthing conflict copy, MMEX's ISUSED flag) only warn here: the
pages show them as banners, and refusing would block every edit until a
stale conflict file is cleaned up.
"""

import datetime
import hashlib
import json
import logging
import os
import re
import threading

from engine import BackupPolicy, apply, preview, rates, undo
from engine.core import describe

from .store import transids

log = logging.getLogger(__name__)

# Status taps (the flag button on each row) less than this far apart, with
# no other write between, share one history entry and one undo.
FLAG_GROUP_MIN = 15
STATUS_NAMES = {'': 'unflagged', 'R': 'R', 'F': 'F'}


def flag_summary(flags) -> str:
    """One history line for a run of status taps: {transid: {'account',
    'from' (status before the first tap), 'to' (after the last)}}."""
    changed = [f for f in flags.values() if f['from'] != f['to']]
    accounts = ', '.join(dict.fromkeys(f['account'] for f in (changed or flags.values())))
    if not changed:
        return f'Status taps in {accounts}: no net change'
    counts = {}
    for f in changed:
        counts[f['to']] = counts.get(f['to'], 0) + 1
    order = ['R', 'F', '']
    parts = [f'{n} to {STATUS_NAMES.get(s, s)}' for s, n in sorted(counts.items(), key=lambda x: order.index(x[0]))]
    return f'Set status on {len(changed)} row(s) in {accounts}: ' + ', '.join(parts)


def summarize(events) -> str:
    """One line for the history list, from the engine's events."""
    parts = []
    for e in events:
        if e['kind'] in ('add', 'delete'):
            parts.append(('Added ' if e['kind'] == 'add' else 'Deleted ') + describe(e['row']))
        elif e['kind'] == 'update':
            changed = [k for k in e['changed'] if k != 'id']
            parts.append(f'Edited {describe(e["after"])}' + (f' ({", ".join(changed)})' if changed else ''))
        elif e['kind'] == 'flag':
            parts.append(f'Flagged {len(e["ids"])} row(s) in {e["account"]} as {e["status"]}')
    return '; '.join(parts) or 'No change'


def preview_dict(result) -> dict:
    """What the queue keeps of an engine preview (JSON-safe)."""
    return {'ok': result.ok, 'error': result.error, 'stale': result.stale, 'lines': result.lines,
            'events': result.events, 'balances': result.balances, 'checks_ok': result.checks_ok,
            'warnings': result.warnings}


def preview_digest(p) -> str:
    """A fingerprint of what a preview would do, ignoring the new rows' ids
    (time-based, so different on every run): two previews with the same
    digest write the same rows and leave the same balances."""
    lines = [re.sub(r'\(id \d+\)', '(id new)', line) if line.startswith('+ ') else line for line in p['lines']]
    body = json.dumps([p['ok'], p['error'], p['checks_ok'], lines, p['balances']], sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def added_ids(journal) -> list:
    return [e['row']['TRANSID'] for e in (journal or {}).get('entries', []) if e['op'] == 'add']


def _ui_error(text) -> str:
    """The engine's undo refusals, without the raw row dump and CLI hints."""
    text = (text or '').replace(' Use --force to override.', '')
    return re.sub(r'^\w+ (\d+): row changed since it was written \(now .*\); not undoing\.$',
                  r'transaction \1 has been changed since (by a later edit here, or elsewhere). '
                  r'Undo the later change first, or fix the row by hand.', text, flags=re.S)


class Writer:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.lock = threading.Lock()
        self.backups = (BackupPolicy(settings.backup_dir, keep=settings.backup_keep,
                                     min_interval_min=settings.backup_min_interval_min)
                        if settings.backup_dir else None)

    def unavailable(self):
        """Why the app can't write right now, or None when it can."""
        path = self.settings.db_path
        if self.backups is None:
            return 'Read-only: MMEX_BACKUP_DIR isn\'t set, and the app never writes without backups.'
        if not os.path.isfile(path):
            return None  # the pages report a missing DB themselves
        if not (os.access(path, os.W_OK) and os.access(os.path.dirname(os.path.abspath(path)), os.W_OK)):
            return 'Read-only: the database file or its folder isn\'t writable by the app.'
        return None

    def write(self, plan, action, source='you'):
        """Apply a plan. Returns (result, write_id); write_id is None when
        the write failed, or succeeded but couldn't be recorded."""
        with self.lock:
            result = apply(self.settings.db_path, plan, self.backups, refuse_on=(), source=source, force_backup=False)
        if not result.ok:
            return result, None
        try:
            write_id = self.store.record(source=source, action=action, summary=summarize(result.events),
                                         lines=result.lines, db_path=self.settings.db_path,
                                         db_fingerprint=result.db_fingerprint, backup=result.backup,
                                         journal=result.journal)
        except Exception:
            log.exception('write applied but not recorded in app.db')
            write_id = None
        return result, write_id

    def flag(self, trans_id, old, new, now=None):
        """Change one row's status (expecting it to be `old` still), as a
        status tap: grouped with the taps just before it into one history
        entry. Returns (result, write_id) like write()."""
        plan = {'ops': [{'op': 'update', 'id': trans_id, 'set': {'status': new}, 'expect': {'status': old}}]}
        with self.lock:
            result = apply(self.settings.db_path, plan, self.backups, refuse_on=(), source='you', force_backup=False)
            if not result.ok:
                return result, None
            try:
                return result, self._record_flag(result, trans_id, old, new, now)
            except Exception:
                log.exception('status change applied but not recorded in app.db')
                return result, None

    def _record_flag(self, result, trans_id, old, new, now=None):
        account = result.events[0]['after']['account']
        last = self.store.latest()
        if (last and last['action'] == 'flag' and last['source'] == 'you' and not last['undone_at_utc']
                and last['journal'] and last['db_fingerprint'] == result.db_fingerprint):
            now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            age = now - datetime.datetime.fromisoformat(last['at_utc'])
            if datetime.timedelta(0) <= age < datetime.timedelta(minutes=FLAG_GROUP_MIN):
                journal = last['journal']
                journal['entries'] += result.journal['entries']
                journal['written_at_utc'] = result.journal['written_at_utc']
                flags = journal.setdefault('flags', {})
                prev = flags.get(str(trans_id))
                flags[str(trans_id)] = {'account': account, 'from': prev['from'] if prev else old, 'to': new}
                self.store.extend(last['id'], summary=flag_summary(flags), lines=last['lines'] + result.lines,
                                  journal=journal, rows=[trans_id])
                return last['id']
        journal = dict(result.journal, flags={str(trans_id): {'account': account, 'from': old, 'to': new}})
        return self.store.record(source='you', action='flag', summary=flag_summary(journal['flags']),
                                 lines=result.lines, db_path=self.settings.db_path,
                                 db_fingerprint=result.db_fingerprint, backup=result.backup, journal=journal)

    def undo(self, write, commit=False):
        """Undo a recorded write; undoing an undo is a redo. commit=False
        only previews it."""
        if not write.get('journal'):
            return None, 'This entry has no journal (it was recorded before redo existed), so it can\'t be reversed here.'
        if write.get('undone_at_utc'):
            return None, f'Already reversed at {write["undone_at_utc"].replace("T", " ")} UTC.'
        if not commit:
            result = undo(self.settings.db_path, write['journal'], commit=False, refuse_on=())
            return result, None if result.ok else _ui_error(result.error)
        with self.lock:
            result = undo(self.settings.db_path, write['journal'], self.backups, commit=True, refuse_on=(),
                          force_backup=False)
        if not result.ok:
            return result, _ui_error(result.error)
        action = 'redo' if write['action'] == 'undo' else 'undo'
        base = write['summary'].split(': ', 1)[1] if write['action'] in ('undo', 'redo') else write['summary']
        self.store.record(source='you', action=action, summary=('Redid: ' if action == 'redo' else 'Undid: ') + base,
                          lines=result.lines, db_path=self.settings.db_path, db_fingerprint=result.db_fingerprint,
                          backup=result.backup, journal=result.journal, undo_of=write['id'],
                          rows=transids(write['journal']))
        return result, None

    def preview(self, plan) -> dict:
        """The engine's preview of a plan against the DB as it is now."""
        return preview_dict(preview(self.settings.db_path, plan))

    def approve(self, item, seen_digest):
        """Apply a pending queue batch, if its preview right now is still the
        one the user looked at (`seen_digest`). Always backs up first.
        Returns (result or None, error or None, write_id)."""
        with self.lock:
            current = self.store.item(item['id'])
            if current is None or current['status'] != 'pending':
                return None, f'This batch is no longer pending (it is {current["status"] if current else "gone"}).', None
            now = self.preview(current['plan'])
            if preview_digest(now) != seen_digest:
                return None, 'What this batch would do has changed since the page was loaded. Look at it again.', None
            result = apply(self.settings.db_path, current['plan'], self.backups, refuse_on=(),
                           source=current['source'], force_backup=True)
            if not result.ok:
                return result, result.error or 'a check failed', None
            write_id = None
            try:
                write_id = self.store.record(source=current['source'], action='queue',
                                             summary=f'{current["title"]}: {summarize(result.events)}',
                                             lines=result.lines, db_path=self.settings.db_path,
                                             db_fingerprint=result.db_fingerprint, backup=result.backup,
                                             journal=result.journal)
            except Exception:
                log.exception('queue batch applied but not recorded in app.db')
            self.store.decide(current['id'], 'approved', write_id=write_id, rows=added_ids(result.journal))
        return result, None, write_id

    def rates(self, changes) -> list:
        """Write currency-history changes (engine.rates.apply): backed up,
        single writer, but not journalled; rates have their own page."""
        with self.lock:
            return rates.apply(self.settings.db_path, changes, self.backups)
