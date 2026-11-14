"""mmex_web's MMEX write engine. Standard library only.

    from engine import preview, apply, undo, BackupPolicy
    result = preview('/path/to/db.mmb', {'ops': [...]})

See batch.py for the entry points, core.py for the plan ops, schema.py for
which DBs may be written.
"""

from .backups import BackupPolicy
from .batch import GuardError, Result, apply, preview, read, undo
from .core import ExpectError, PlanError, balance, find, lookup
from .schema import SUPPORTED_SCHEMA_VERSIONS, SchemaError

__all__ = ['BackupPolicy', 'ExpectError', 'GuardError', 'PlanError', 'Result', 'SUPPORTED_SCHEMA_VERSIONS', 'SchemaError',
           'apply', 'balance', 'find', 'lookup', 'preview', 'read', 'undo']
