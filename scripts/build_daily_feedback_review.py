#!/usr/bin/env python3
"""Build one private daily-feedback review artifact."""

from __future__ import annotations

import json
import os
import sys

from bridge.daily_feedback_export import run_cli


def main() -> int:
    result = run_cli(sys.argv[1:], environ=os.environ)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
