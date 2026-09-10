#!/usr/bin/env python3
"""Record privacy review for one exact daily-feedback HTML bundle."""

from __future__ import annotations

import json
import sys

from bridge.daily_feedback_export import run_approval_cli


if __name__ == "__main__":
    try:
        result = run_approval_cli(sys.argv[1:])
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, sort_keys=True))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
