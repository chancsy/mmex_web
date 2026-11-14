"""Plain-text rendering of engine results, for the CLI."""


def balances_text(balances) -> str:
    if not balances:
        return ''
    out = ['', 'Balances (all dates):']
    for b in balances:
        out.append(f'  {b["account"]:<32} {b["before"]:>14,.2f} -> {b["after"]:>14,.2f}  ({b["after"] - b["before"]:+,.2f})')
    return '\n'.join(out)


def preview_text(result) -> str:
    """Output of a successful preview."""
    out = [f'PREVIEW against {result.db} (in-memory copy; nothing written)', '']
    out.append('\n'.join(result.lines))
    text = '\n'.join(out)
    bal = balances_text(result.balances)
    if bal:
        text += '\n' + bal
    if not result.checks_ok:
        text += '\n\nA check FAILED: --commit would roll the whole plan back.'
    else:
        text += '\n\nRe-run with --commit to write this.'
    if any('ISUSED' in w for w in result.warnings):
        text += '\nNote: the DB is flagged in use by MMEX (ISUSED=TRUE); --commit will refuse until it is closed.'
    return text


def refused_preview_text(result) -> str:
    """Output of a refused preview."""
    return f'REFUSED (nothing written): {result.error}'


def applied_text(result) -> str:
    out = [f'WROTE {result.db}', '', '\n'.join(result.lines)]
    text = '\n'.join(out)
    bal = balances_text(result.balances)
    if bal:
        text += '\n' + bal
    return text


def refused_apply_text(result) -> str:
    return '\n'.join(result.lines) + f'\nREFUSED, rolled back (nothing written): {result.error}'
