"""Engine CLI, for development and one-off maintenance (the web app and its
queue are the normal way in). Run from the project dir:

    python -m engine preview plan.json --db DB
    python -m engine apply plan.json --db DB --commit --backup-dir DIR --journal-dir DIR
    python -m engine undo JOURNAL.json --db DB [--commit] [--force] [--backup-dir DIR]
    python -m engine balance --db DB --account NAME [--as-of YYYY-MM-DD]
    python -m engine find --db DB [--account NAME] [--from D] [--to D] [--amount X] [--notes S] [--json]
    python -m engine lookup accounts|payees|categories|tags --db DB [--like S]

`apply` without --commit is the same as `preview`. Nothing has a default DB:
every command names the file it works on.
"""

import argparse
import datetime
import json
import os
import sys

from . import batch, core, render
from .backups import BackupPolicy
from .schema import SchemaError


def _load(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def _policy(args):
    if not args.backup_dir:
        sys.exit('--backup-dir is required for a write')
    return BackupPolicy(args.backup_dir, keep=args.backup_keep, min_interval_min=args.backup_interval)


def cmd_preview(args):
    result = batch.preview(args.db, _load(args.plan))
    if not result.ok:
        sys.exit(render.refused_preview_text(result))
    print(render.preview_text(result))


def cmd_apply(args):
    if not args.commit:
        return cmd_preview(args)
    if not args.journal_dir:
        sys.exit('--journal-dir is required with --commit')
    refuse_on = () if args.ignore_guards else ('conflict', 'in_use')
    try:
        result = batch.apply(args.db, _load(args.plan), _policy(args), refuse_on=refuse_on,
                             source=f'cli:{os.path.basename(args.plan)}')
    except batch.GuardError as exc:
        sys.exit(str(exc))
    if not result.ok:
        sys.exit(render.refused_apply_text(result))
    os.makedirs(args.journal_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.db))[0]
    path = os.path.join(args.journal_dir, f'{stem}_{datetime.datetime.now():%Y%m%d_%H%M%S_%f}.json')
    result.journal['plan'] = os.path.abspath(args.plan)
    with open(path, 'x', encoding='utf-8') as fh:
        json.dump(result.journal, fh, indent=1)
    print(render.applied_text(result))
    print(f'\nBackup:  {result.backup}{"" if result.backup_taken else " (recent one, no new copy)"}'
          f'\nJournal: {path}\nUndo:    python -m engine undo "{path}" --db "{args.db}" --commit --backup-dir "{args.backup_dir}"')


def cmd_undo(args):
    journal = _load(args.journal)
    if journal.get('db') and os.path.abspath(args.db) != journal['db']:
        sys.exit(f'Journal was written against {journal["db"]}, not {args.db}.')
    refuse_on = () if args.ignore_guards else ('conflict', 'in_use')
    try:
        result = batch.undo(args.db, journal, backups=_policy(args) if args.commit else None,
                            force=args.force, commit=args.commit, refuse_on=refuse_on)
    except batch.GuardError as exc:
        sys.exit(str(exc))
    if not result.ok:
        sys.exit(f'REFUSED: {result.error}')
    if not args.commit:
        print(f'PREVIEW undo against {args.db} (in-memory copy; nothing written)\n  ' + '\n  '.join(result.lines)
              + '\n\nRe-run with --commit to write this.')
        return
    redo_path = os.path.splitext(args.journal)[0] + f'_undo_{datetime.datetime.now():%Y%m%d_%H%M%S_%f}.json'
    with open(redo_path, 'w', encoding='utf-8') as fh:
        json.dump(result.journal, fh, indent=1)
    journal['undone_at_utc'] = core.utc_now_str()
    journal['undo_backup'] = result.backup
    journal['undo_journal'] = redo_path
    with open(args.journal, 'w', encoding='utf-8') as fh:
        json.dump(journal, fh, indent=1)
    print(f'UNDONE on {args.db}\n  ' + '\n  '.join(result.lines) + f'\nBackup before undo: {result.backup}'
          f'\nRedo:    python -m engine undo "{redo_path}" --db "{args.db}" --commit --backup-dir "{args.backup_dir}"')


def cmd_balance(args):
    conn, schema, ref = batch.read(args.db)
    acct = ref.account(args.account, '--account', allow_closed=True)
    print(f'{acct["name"]} at {args.as_of or "all dates"}: {core.balance(conn, acct["id"], args.as_of):,.2f}')


def cmd_find(args):
    conn, schema, ref = batch.read(args.db)
    out = core.find(conn, schema, ref, id=args.id, account=args.account, date_from=args.date_from,
                    date_to=args.date_to, amount=args.amount, payee=args.payee, category=args.category,
                    notes=args.notes, include_deleted=args.include_deleted, limit=args.limit)
    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    for view in out:
        print(f'{view["id"]:>17}  {core.describe(view)}' + (f'  DELETED {view["deleted"]}' if view.get('deleted') else ''))
    print(f'({len(out)} row(s){", limit reached" if len(out) >= args.limit else ""})')


def cmd_lookup(args):
    conn, schema, ref = batch.read(args.db)
    for line in core.lookup(ref, args.what, args.like):
        print(line)


def main():
    parser = argparse.ArgumentParser(prog='python -m engine', description='mmex_web write engine')
    sub = parser.add_subparsers(dest='cmd', required=True)

    def write_args(p):
        p.add_argument('--backup-dir')
        p.add_argument('--backup-keep', type=int, default=20)
        p.add_argument('--backup-interval', type=int, default=30, help='minutes; 0 = back up before every write')
        p.add_argument('--ignore-guards', action='store_true', help='write despite a conflict file or ISUSED')

    p = sub.add_parser('preview')
    p.add_argument('plan')
    p.add_argument('--db', required=True)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser('apply')
    p.add_argument('plan')
    p.add_argument('--db', required=True)
    p.add_argument('--commit', action='store_true')
    p.add_argument('--journal-dir')
    write_args(p)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser('undo')
    p.add_argument('journal')
    p.add_argument('--db', required=True)
    p.add_argument('--commit', action='store_true')
    p.add_argument('--force', action='store_true')
    write_args(p)
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser('balance')
    p.add_argument('--db', required=True)
    p.add_argument('--account', required=True)
    p.add_argument('--as-of')
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser('find')
    p.add_argument('--db', required=True)
    p.add_argument('--id', type=int)
    p.add_argument('--account')
    p.add_argument('--from', dest='date_from')
    p.add_argument('--to', dest='date_to')
    p.add_argument('--amount', type=float)
    p.add_argument('--payee')
    p.add_argument('--category')
    p.add_argument('--notes')
    p.add_argument('--include-deleted', action='store_true')
    p.add_argument('--limit', type=int, default=200)
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_find)

    p = sub.add_parser('lookup')
    p.add_argument('what', choices=['accounts', 'payees', 'categories', 'tags'])
    p.add_argument('--db', required=True)
    p.add_argument('--like')
    p.set_defaults(func=cmd_lookup)

    args = parser.parse_args()
    try:
        args.func(args)
    except (core.PlanError, SchemaError, FileNotFoundError) as exc:
        sys.exit(f'ERROR: {exc}')


if __name__ == '__main__':
    main()
