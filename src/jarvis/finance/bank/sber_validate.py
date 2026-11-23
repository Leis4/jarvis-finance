"""Offline Sber statement validator: counts, invariants, verdict, no writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jarvis.finance.bank.sber_statement import read_sber_export
from jarvis.finance.bank.statement_validation import validate_statement


def main() -> int:
    parser = argparse.ArgumentParser(description='Validate one Sber statement without importing it')
    parser.add_argument('path', type=Path)
    parser.add_argument('--account', default='sber')
    args = parser.parse_args()
    parsed = read_sber_export(args.path, account=args.account)
    result = validate_statement(parsed.text, parsed.transactions,
                                source_format=parsed.source_format)
    result['parser_warnings'] = len(parsed.warnings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'safe': 0, 'review': 2, 'unsafe': 3}[result['verdict']]


if __name__ == '__main__':
    raise SystemExit(main())
