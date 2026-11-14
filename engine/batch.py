"""The engine's entry points: preview a batch of plan ops, apply it, undo it.

A batch is a plan, {"ops": [...]} in the plan-file format (see core.py).
- preview(): runs it on an in-memory copy of the DB. The file on disk is
  only read.
- apply(): runs it on the DB in one transaction. Any PlanError, or any
  failing `check` op, rolls the whole batch back. A backup is taken first
  (per the BackupPolicy), and the result carries the journal that undo()
  needs. Storing that journal is the caller's job.
- undo(): reverses a journal, refusing rows that changed since. A committed
  undo returns a journal of its own, so it can be undone too (a redo).

Guards: a Syncthing conflict copy next to the DB, or MMEX's ISUSED flag.
`refuse_on` says which of them stop a write; the rest are reported in
`warnings` only.
"""

import os
from dataclasses import dataclass, field

from . import core, dbfile
from .core import Applier, ExpectError, PlanError, balance, undo_entries, utc_now_str


class GuardError(Exception):
    """A file-level guard (conflict copy, ISUSED) refused a write."""


@dataclass
class Result:
    ok: bool
    db: str
    error: str = None
    stale: bool = False                            # refused because a row's `expect` no longer matches
    lines: list = field(default_factory=list)      # human-readable preview lines
    events: list = field(default_factory=list)     # the same, as dicts
    balances: list = field(default_factory=list)   # [{'account', 'before', 'after'}], by account name
    checks_ok: bool = True
    warnings: list = field(default_factory=list)
    schema_version: int = None
    db_fingerprint: str = None
    journal: dict = None                           # apply(), and undo(commit=True): undo it to redo
    backup: str = None                             # apply()/undo() only: backup covering this write
    backup_taken: bool = False


def _balances(conn, ref, before) -> list:
    out = []
    for acct_id in sorted(before, key=lambda a: ref.account_name(a)):
        after = balance(conn, acct_id)
        was = before[acct_id] if before[acct_id] is not None else after
        out.append({'account': ref.account_name(acct_id), 'before': was, 'after': after})
    return out


def _guard(db_path, refuse_on) -> list:
    found = dbfile.guard_warnings(db_path)
    warnings = []
    if found['conflict']:
        msg = f'Syncthing conflict file(s) next to the DB: {found["conflict"]}'
        if 'conflict' in refuse_on:
            raise GuardError(f'REFUSED: {msg}. Resolve them first.')
        warnings.append(msg)
    if found['in_use']:
        msg = 'the DB is flagged in use by MMEX (ISUSED=TRUE)'
        if 'in_use' in refuse_on:
            raise GuardError(f'REFUSED: {msg}. Close MMEX on every device, let Syncthing settle, and retry.')
        warnings.append(msg)
    return warnings


def preview(db_path, plan) -> Result:
    conn, schema = dbfile.memory_copy(db_path)
    try:
        result = Result(ok=False, db=db_path, schema_version=schema.version, db_fingerprint=dbfile.fingerprint(conn),
                        warnings=_guard(db_path, refuse_on=()))
        conn.execute('BEGIN')
        applier = Applier(conn, schema)
        try:
            before = applier.run(plan)
        except PlanError as exc:
            result.error, result.stale = str(exc), isinstance(exc, ExpectError)
            result.lines, result.events = applier.preview, applier.events
            return result
        result.lines, result.events = applier.preview, applier.events
        result.balances = _balances(conn, applier.ref, before)
        result.checks_ok = all(applier.check_results)
        result.ok = True
        return result
    finally:
        conn.close()


def apply(db_path, plan, backups, refuse_on=('conflict', 'in_use'), source=None, force_backup=True,
          expect_fingerprint=None) -> Result:
    """Write the batch. Returns a Result; result.ok is False (and nothing is
    written) when an op is refused or a check fails. Raises GuardError when
    a guard in refuse_on trips, SchemaError when the DB isn't supported."""
    warnings = _guard(db_path, refuse_on)
    conn, schema = dbfile.open_rw(db_path)
    try:
        fp = dbfile.fingerprint(conn)
        if expect_fingerprint and fp != expect_fingerprint:
            raise GuardError(f'REFUSED: {db_path} is not the DB this batch was prepared for')
        result = Result(ok=False, db=db_path, schema_version=schema.version, db_fingerprint=fp, warnings=warnings)
        if backups is not None:
            result.backup, result.backup_taken = backups.take(db_path, force=force_backup)
        conn.execute('BEGIN IMMEDIATE')
        applier = Applier(conn, schema)
        try:
            before = applier.run(plan)
            if not all(applier.check_results):
                raise PlanError('a check failed')
        except PlanError as exc:
            conn.execute('ROLLBACK')
            result.error, result.stale = str(exc), isinstance(exc, ExpectError)
            result.lines, result.events = applier.preview, applier.events
            result.checks_ok = all(applier.check_results)
            return result
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        conn.execute('COMMIT')
        result.ok = True
        result.lines, result.events = applier.preview, applier.events
        result.balances = _balances(conn, applier.ref, before)
        result.journal = {'db': os.path.abspath(db_path), 'db_fingerprint': fp, 'schema_version': schema.version,
                          'source': source, 'backup': result.backup, 'written_at_utc': utc_now_str(),
                          'entries': applier.journal}
        return result
    finally:
        conn.close()


def undo(db_path, journal, backups=None, force=False, commit=False, refuse_on=('conflict', 'in_use'),
         force_backup=True) -> Result:
    """Reverse a journal from apply(). commit=False previews on an in-memory
    copy. Refuses when the journal belongs to another DB or was already undone.
    force_backup=False lets the backup policy's interval skip the backup, as
    for apply()."""
    if journal.get('undone_at_utc'):
        return Result(ok=False, db=db_path, error=f'Journal already undone at {journal["undone_at_utc"]} UTC.')
    if not commit:
        conn, schema = dbfile.memory_copy(db_path)
    else:
        warnings = _guard(db_path, refuse_on)
        conn, schema = dbfile.open_rw(db_path)
    try:
        fp = dbfile.fingerprint(conn)
        result = Result(ok=False, db=db_path, schema_version=schema.version, db_fingerprint=fp)
        if journal.get('db_fingerprint') and journal['db_fingerprint'] != fp:
            result.error = 'Journal was written against a different DB.'
            return result
        if commit:
            result.warnings = warnings
            if backups is not None:
                result.backup, result.backup_taken = backups.take(db_path, force=force_backup)
            conn.execute('BEGIN IMMEDIATE')
        else:
            conn.execute('BEGIN')
        try:
            result.lines, entries = undo_entries(conn, schema, journal['entries'], force)
        except PlanError as exc:
            conn.execute('ROLLBACK')
            result.error = str(exc)
            return result
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        conn.execute('COMMIT' if commit else 'ROLLBACK')
        result.ok = True
        if commit:
            # What this undo did, as a journal of its own: undo() it to redo.
            result.journal = {'db': os.path.abspath(db_path), 'db_fingerprint': fp, 'schema_version': schema.version,
                              'source': journal.get('source'), 'backup': result.backup, 'written_at_utc': utc_now_str(),
                              'undo_of': journal.get('written_at_utc'), 'entries': entries}
        return result
    finally:
        conn.close()


def read(db_path):
    """(connection, schema, ref) for read-only queries on an in-memory copy."""
    conn, schema = dbfile.memory_copy(db_path)
    return conn, schema, core.Ref(conn)
