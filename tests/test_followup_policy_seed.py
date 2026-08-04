"""Contract tests for the temporary fast E2E follow-up policy."""

from __future__ import annotations

import re
from pathlib import Path


POLICY_SEED = (
    Path(__file__).parents[1]
    / "supabase"
    / "seeds"
    / "20260804000100_cart_recovery_e2e_fast_v1.sql"
)


def test_fast_e2e_policy_encodes_the_confirmed_relative_timeline() -> None:
    assert POLICY_SEED.exists(), "missing fast E2E policy seed"
    sql = re.sub(r"\s+", " ", POLICY_SEED.read_text(encoding="utf-8").lower())

    assert "'cart-recovery-e2e-fast', 1, 'published'" in sql
    assert "'america/argentina/buenos_aires'" in sql
    assert "interval '1 minute'" in sql
    assert "interval '1 hour'" in sql
    assert "max_automatic_messages" in sql
    assert '"step_key":"first_contact"' in sql
    assert '"step_key":"followup_1"' in sql and '"delay":"5 minutes"' in sql
    assert '"step_key":"followup_2"' in sql and '"delay":"10 minutes"' in sql
    assert '"days":[1,2,3,4,5,6,7]' in sql
    assert '"start":"00:00"' in sql
    assert '"end":"23:59"' in sql
