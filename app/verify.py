#!/usr/bin/env python3
"""Check that every metric cell in the DB matches a full recompute from facts.

This is the same check exposed by GET /api/verify, but runs directly against
an on-disk database without starting a server. Useful in CI:

    python3 -m app.verify --db data/metrics.db
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import Database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/metrics.db")
    args = parser.parse_args()
    result = Database(args.db).verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
