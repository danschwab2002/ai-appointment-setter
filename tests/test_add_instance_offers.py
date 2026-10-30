"""scripts/add_instance_offers.py and migration 20261028000300, without a database (E-163).

The end to end run, on a real PostgreSQL, is
tests/sql/followup_engine/real_postgres_add_instance_offers.py; the chain with a
case in flight, a stored event and a new cart, on PGlite, is
tests/sql/followup_engine/validate_pilot_scope_offer_append.mjs. Here: what the
script reads from the manifest, what it refuses before touching the base, and
that its prediction of ``/ready`` is the comparison of the bridge.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from bridge.commercial_ally import CommercialAllyConfig

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("add_instance_offers", ROOT / "scripts/add_instance_offers.py")
assert SPEC and SPEC.loader
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)

MANIFEST = (ROOT / "tests/fixtures/instances/maribel/instancia.toml").read_text(encoding="utf-8")
MIGRATION = ROOT / "supabase/migrations/20261028000300_pilot_scope_offer_append_v1.sql"
ORG = ('[[hotmart.ofertas]]\ncodigo = "mmliigg5"\nsite = "elprotocolo"\nlanding_id = "epma-vsl-org-a"\n'
       'url = "https://www.elprotocolo.net/epma/evg/vsl/org-a"\norigen = "organico"\n\n')


def manifest(tmp_path: Path, extra: str = ORG, text: str = MANIFEST) -> Path:
    path = tmp_path / "instancia.toml"
    path.write_text(text.replace("\n[chatwoot]\n", "\n" + extra + "[chatwoot]\n", 1), encoding="utf-8")
    return path


def row_of(config: CommercialAllyConfig) -> dict[str, object]:
    """The row as resolve_commercial_ally_runtime_binding returns it through to_jsonb."""
    row: dict[str, object] = dataclasses.asdict(config)
    row["additional_offer_codes"] = list(config.additional_offer_codes)
    row["additional_offer_landings"] = [dataclasses.asdict(item) for item in config.additional_offer_landings]
    row["product_price"] = float(config.product_price)
    row.update(status="active", created_at="2026-10-26T00:00:00Z", updated_at="2026-10-26T00:00:00Z")
    return row


def test_the_manifest_gives_the_binding_with_the_offer_and_its_landing(tmp_path: Path) -> None:
    targets = script.load_targets(manifest(tmp_path), ["mmliigg5"])
    assert len(targets) == 1
    config, mine = targets[0]
    assert mine == ["mmliigg5"]
    assert config.offer_code == "u8ikyx78" and config.binding_version == 2
    assert config.additional_offer_codes == ("mmliigg5",)
    assert config.additional_offer_landings[0].page_path == "/epma/evg/vsl/org-a"
    plan = script.build_plan(targets)
    assert plan[0]["offers"] == [{"offer_code": "mmliigg5", "landing_id": "epma-vsl-org-a"}]


@pytest.mark.parametrize(("offers", "reason"), [
    (["zzzz9999"], "no esta en el manifiesto"),
    (["u8ikyx78"], "oferta por defecto"),
])
def test_it_refuses_what_is_not_an_additional_offer_of_the_manifest(tmp_path: Path, offers, reason) -> None:
    with pytest.raises(script.Refused, match=reason):
        script.load_targets(manifest(tmp_path), offers)


def test_it_refuses_a_manifest_that_does_not_validate(tmp_path: Path) -> None:
    broken = ORG.replace('landing_id = "epma-vsl-org-a"', 'landing_id = "epma-vsl-ads-a"')
    with pytest.raises(script.Refused, match="el manifiesto no valida"):
        script.load_targets(manifest(tmp_path, broken), ["mmliigg5"])


def test_the_base_has_to_be_the_new_manifest_without_the_offers(tmp_path: Path) -> None:
    targets = script.load_targets(manifest(tmp_path), ["mmliigg5"])
    config = targets[0][0]
    before = script._without(config, ["mmliigg5"])
    current = {"bindings": [{"funnel_ref": "maribel", "row": row_of(before)}]}
    plan = script.build_plan(targets)
    script.check_current(targets, current, plan)
    assert plan[0]["old_codes"] == [] and plan[0]["old_landings"] == []
    # Ya aplicado: tampoco frena (no hay nada que sumar).
    plan = script.build_plan(targets)
    script.check_current(targets, {"bindings": [{"funnel_ref": "maribel", "row": row_of(config)}]}, plan)
    assert plan[0]["old_codes"] == ["mmliigg5"]
    # Otro precio en la base: no es una oferta nueva.
    drifted = dataclasses.replace(before, product_price=Decimal("1497"))
    with pytest.raises(script.Refused, match="difiere en product_price"):
        script.check_current(targets, {"bindings": [{"funnel_ref": "maribel", "row": row_of(drifted)}]},
                             script.build_plan(targets))
    with pytest.raises(script.Refused, match="no esta activo"):
        script.check_current(targets, {"bindings": [{"funnel_ref": "maribel", "row": None, "status": "retired"}]},
                             script.build_plan(targets))


def test_an_offer_removed_or_reordered_in_the_manifest_is_refused(tmp_path: Path) -> None:
    second = ORG.replace("mmliigg5", "s4y32uzp").replace("epma-vsl-org-a", "epma-ret-ads-a1").replace(
        "vsl/org-a", "ret/ads-a1")
    # El manifiesto pone s4y32uzp ANTES de mmliigg5, que la base ya tiene: lo nuevo no va al final.
    targets = script.load_targets(manifest(tmp_path, second + ORG), ["s4y32uzp"])
    assert script._without(targets[0][0], ["s4y32uzp"]).additional_offer_codes == ("mmliigg5",)
    # La base con otra oferta que el manifiesto ya no declara: no es sumar.
    other = script.load_targets(manifest(tmp_path, ORG.replace("mmliigg5", "o11h7tn3")), ["o11h7tn3"])[0][0]
    with pytest.raises(script.Refused, match="difiere en additional_offer_codes"):
        script.check_current(targets, {"bindings": [{"funnel_ref": "maribel", "row": row_of(other)}]},
                             script.build_plan(targets))


def test_the_prediction_is_the_comparison_of_the_bridge(tmp_path: Path) -> None:
    config = script.load_targets(manifest(tmp_path), ["mmliigg5"])[0][0]
    assert script.predicted_ready([(config, ["mmliigg5"])], [{"funnel_ref": "maribel", "row": row_of(config)}]) == []
    before = script._without(config, ["mmliigg5"])
    failures = script.predicted_ready([(config, ["mmliigg5"])], [{"funnel_ref": "maribel", "row": row_of(before)}])
    assert failures and "additional_offer_codes" in failures[0]
    # El bridge lee la fila con la misma funcion (SupabaseClient.resolve_commercial_ally_runtime_binding).
    supabase = (ROOT / "src/bridge/supabase.py").read_text(encoding="utf-8")
    assert "CommercialAllyConfig.from_binding_row(row)" in supabase
    assert CommercialAllyConfig.from_binding_row(row_of(config)) == config
    with pytest.raises(ValueError):
        CommercialAllyConfig.from_binding_row({key: value for key, value in row_of(config).items()
                                               if key != "inbound_scope_version"})


class FakeOps:
    def __init__(self) -> None:
        self.out = io.StringIO()


def test_usage_errors_do_not_touch_the_base(tmp_path: Path) -> None:
    ops = FakeOps()
    code = script.main(["--manifiesto", str(manifest(tmp_path)), "--ofertas", "mmliigg5"], ops=ops)  # type: ignore[arg-type]
    assert code == script.EXIT_USAGE and "--quien" in ops.out.getvalue()
    with pytest.raises(SystemExit):
        script.main(["--manifiesto", str(manifest(tmp_path)), "--ofertas", "bad code", "--quien", "dan"])
    with pytest.raises(script.Refused, match="CONTENEDOR_DB"):
        script.Config.from_env({})


def test_the_sql_carries_no_value_in_its_text() -> None:
    for sql in (script.SQL_READ, script.SQL_APPLY):
        assert "pg_temp._args" in sql
        for literal in ("lancemos", "maribel", "mmliigg5", "u8ikyx78"):
            assert literal not in sql
    # Cada scope del piloto se edita en su version, pedido para esa fila, y ningun control se toca.
    assert "set_config('pilot_scope.offer_append', v_s.scope_key || '/' || v_s.version, true)" in script.SQL_APPLY
    assert "pilot_runtime_controls set" not in script.SQL_APPLY
    assert "set_lancemos_pilot_runtime_state" not in script.SQL_APPLY
    assert "activate_lancemos_pilot_scope_version" not in script.SQL_APPLY
    assert "insert into public.commercial_ally_runtime_bindings" not in script.SQL_APPLY
    assert "insert into public.pilot_scope_versions" not in script.SQL_APPLY


def test_the_migration_only_opens_the_append_and_is_one_transaction() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    assert code.count("create or replace function") == 1
    assert "create or replace function public.validate_pilot_scope_version()" in code
    assert "current_setting('pilot_scope.offer_append', true)" in code
    assert "(to_jsonb(new) - 'additional_offer_codes' - 'updated_at')" in code
    # Las demas ramas del trigger son las de 20260810000100, sin cambios.
    original = (ROOT / "supabase/migrations/20260810000100_lancemos_pilot_boundary.sql").read_text(encoding="utf-8")
    body = original.split("create or replace function public.validate_pilot_scope_version()", 1)[1].split(
        "$function$;", 1)[0]
    mine = code.split("create or replace function public.validate_pilot_scope_version()", 1)[1].split(
        "$function$;", 1)[0]
    without_append = re.sub(r"\n\s*if coalesce\(current_setting.*?end if;\n", "\n", mine, flags=re.S)
    assert re.sub(r"\s+", " ", without_append) == re.sub(r"\s+", " ", body)
    names = sorted(path.name for path in MIGRATION.parent.glob("*.sql"))
    # Despues va la compra con nuestro link (20261028000400, E-169).
    assert names[-3] == MIGRATION.name
    # El doble caso del carrito (20261030000100, E-204): la admision nativa y el planificador del carrito.
    assert names[-1] == "20261030000100_flow_engine_native_legacy_double_case_v1.sql"
    assert names[-2] == "20261028000400_purchase_by_our_link_counts_v1.sql"


def test_the_read_sql_reads_and_never_writes() -> None:
    lowered = script.SQL_READ.lower()
    for verb in ("insert ", "update ", "delete ", "set_config"):
        assert verb not in lowered
    assert json.dumps(script.REASONS)  # every reason is printable
