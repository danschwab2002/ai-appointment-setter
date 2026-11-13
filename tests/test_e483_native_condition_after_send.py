"""E-483 (C3 del mapa «el AE maneja todos los flujos», 2026-11-12; H3/P9): el pago rechazado completo.

El legado de ATT1 manda B1 en una de cuatro variantes y, a las 24 h, B2 si B1 salio sin cupon o Z si salio
con uno (``payment_failure_followups.py``). En el nativo la cadena elegia una plantilla por rama. Desde
20261112210300 cada rama es un camino: su B1, la espera y su B2 o Z. «El toque anterior llevo cupon» no hace
falta como dato (sumarlo exigia ``flow_definition.py``): el cupon de B1 es fijo y esta escrito en la rama.

La base lo mide ``validate_e483_native_condition_after_send.mjs`` (PGlite, los rechazos capturados de
Maribel). Aca: la regla 14 nativa (``native_not_supported`` y ``_native_multitouch_not_supported``), los
textos del AE, el validador del ciclo con el catalogo, y el bridge con las filas que contesta la base
(``tests/fixtures/flow_engine/native_condition_after_send_rows.json``, escrito por esa prueba con ``--emit``).
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_flow_change_api_app as app_tests
import test_flow_engine_native_template_shape as shape
from test_flow_engine_native_dispatch import _dispatcher, _NativeAuthority, _NativeChatwoot, _run, _templates
from test_flow_payment_failure_branches_schema import MARIBEL, TEMPLATES, _send

from bridge.flow_engine import native_not_supported, native_payment_failure_branches, native_payment_failure_paths
from bridge.flow_validator import _native_shape_for_the_owner
from bridge.supabase import FlowEngineNativeBranch, SupabaseClient
from bridge.worker import FLOW_ENGINE_STEP_UNSUPPORTED

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
MIGRATION = ROOT / "supabase" / "migrations" / "20261112210300_e483_native_condition_after_send_v1.sql"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_condition_after_send_rows.json").read_text(encoding="utf-8"))
FOLLOWUPS_ATT1 = {template["name"]: template for template in json.loads((FIXTURES / (
    "chatwoot_message_templates_inbox_11_pago_rechazado_seguimiento_20261019.json")).read_text(
        encoding="utf-8"))["message_templates"]}
MARIBEL_CATALOG = {template["name"]: template for template in json.loads((FIXTURES / (
    "chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json")).read_text(
        encoding="utf-8"))["message_templates"]}
FLOW_ID = "0b3c5f00-0e30-4000-8000-0000000e4833"
B2_ATT1, Z_ATT1 = "att1_pago_rechazado_descuento_01", "att1_ultima_renovacion_01"
# El seguimiento que hoy entra en un toque siguiente nativo: una plantilla de marketing con nombre y producto y
# sin boton de URL variable (la de Maribel, reconstruida).
FOLLOW = "maribel_retomar_proceso_01"


def _follow(template: str = FOLLOW, **extra: Any) -> dict[str, Any]:
    return {"plantilla": template, "idioma": "es_MX", "variables": ["nombre", "producto"], **extra}


def _b1(branch: str, coupon: bool) -> dict[str, Any]:
    step = _send(branch)
    if not coupon:
        step["boton"] = {"link": "checkout"}
    return step


def _path(branch: str, coupon: bool, *, wait: str = "24h", follow: str = FOLLOW) -> list[dict[str, Any]]:
    return [_b1(branch, coupon), {"esperar": wait}, _follow(follow)]


def _chain(paths: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """La cadena de ATT1 (el orden de choose_variant), con su camino en cada rama."""
    return {
        "condicion": {"dato": "descuento_previo", "es": ["si"]}, "entonces": paths[0],
        "si_no": [{"condicion": {"dato": "rechazos", "es": ["varios"]}, "entonces": paths[1],
                   "si_no": [{"condicion": {"dato": "tipo_de_rechazo", "es": ["sin_saldo"]}, "entonces": paths[2],
                              "si_no": paths[3]}]}],
    }


def _paths(**changes: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    paths = {"cupon": _path("cupon", True), "dos_veces": _path("dos_veces", False),
             "saldo": _path("saldo", True), "banco": _path("banco", False)}
    paths.update(changes)
    return [paths[name] for name in ("cupon", "dos_veces", "saldo", "banco")]


def _full(**changes: Any) -> dict[str, Any]:
    definition = copy.deepcopy(MARIBEL)
    definition["pasos"] = [{"esperar": "15m"}, _chain(_paths())]
    definition["termina"] = {"si": ["compra", "contesta"], "vence": "3d"}
    definition.update(changes)
    return definition


FULL = _full()


# -- La regla 14 nativa ---------------------------------------------------------------------------


def test_each_branch_carries_its_b1_its_wait_and_its_b2_or_z() -> None:
    assert native_not_supported(FULL) == ()
    paths = native_payment_failure_paths(FULL)
    assert paths is not None
    assert [(condition and condition["dato"], [step.get("plantilla", step.get("esperar")) for step in path])
            for condition, path in paths] == [
        ("descuento_previo", ["maribel_pago_rechazado_cupon_01", "24h", FOLLOW]),
        ("rechazos", ["maribel_pago_rechazado_dos_veces_01", "24h", FOLLOW]),
        ("tipo_de_rechazo", ["maribel_pago_rechazado_saldo_01", "24h", FOLLOW]),
        (None, ["maribel_pago_rechazado_banco_01", "24h", FOLLOW]),
    ]
    # Las ramas que elige el bridge siguen siendo el primer envio de cada una.
    branches = native_payment_failure_branches(FULL)
    assert branches is not None
    assert [step["plantilla"] for _, step in branches] == [
        f"maribel_pago_rechazado_{name}_01" for name in ("cupon", "dos_veces", "saldo", "banco")]


def test_the_published_chain_of_one_send_per_branch_does_not_change() -> None:
    assert native_not_supported(MARIBEL) == ()
    branches = native_payment_failure_branches(MARIBEL)
    paths = native_payment_failure_paths(MARIBEL)
    assert branches is not None and paths is not None
    assert [path for _, path in paths] == [(step,) for _, step in branches]


@pytest.mark.parametrize(("definition", "code"), [
    # Con mas de un envio por rama, la respuesta corta el siguiente: el flujo lo tiene que decir.
    (_full(termina={"si": ["compra"], "vence": "3d"}), "varios_envios_sin_contesta"),
    # La politica es una sola: todas las ramas esperan lo mismo.
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(dos_veces=_path("dos_veces", False, wait="48h")))]),
     "ramas_con_otras_esperas"),
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(banco=[_b1("banco", False)]))]), "ramas_con_otras_esperas"),
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(banco=[
        _b1("banco", False), {"esperar": "24h"}, _follow(boton={"link": "checkout"})]))]),
     "boton_despues_del_primer_envio"),
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(cupon=[*_path("cupon", True), {"esperar": "1h"}]))]),
     "espera_despues_del_envio"),
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(cupon=[_b1("cupon", True), _follow()]))]),
     "espera_entre_envios"),
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(cupon=[_b1("cupon", True), {"esperar": "0m"}, _follow()]))]),
     "espera_entre_envios"),
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(saldo=[
        *_path("saldo", True), {"esperar": "1h"}, _follow(), {"esperar": "1h"}, _follow()]))]),
     "demasiados_envios"),
    # Elegir otra vez despues de mandar: dentro de una rama, o con una plantilla antes de la cadena.
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(banco=[
        _b1("banco", False), {"esperar": "24h"},
        {"condicion": {"dato": "rechazos", "es": ["varios"]}, "entonces": [_follow()], "si_no": [_follow()]}]))]),
     "condicion_despues_de_un_envio"),
    (_full(pasos=[_b1("banco", False), {"esperar": "24h"}, _chain(_paths())]), "condicion_despues_de_un_envio"),
    # La gracia es una sola, antes de elegir: una rama empieza con su mensaje.
    (_full(pasos=[{"esperar": "15m"}, _chain(_paths(cupon=[{"esperar": "1h"}, *_path("cupon", True)]))]),
     "rama_de_un_envio"),
])
def test_what_the_base_does_not_run_says_why(definition: dict[str, Any], code: str) -> None:
    assert f"engine_not_supported:{code}" in native_not_supported(definition)


@pytest.mark.parametrize("code", ["condicion_despues_de_un_envio", "rama_de_un_envio", "ramas_con_otras_esperas"])
def test_the_ae_has_a_text_for_each_code(code: str) -> None:
    general = _native_shape_for_the_owner("algo_que_no_existe")
    reason, alternative = _native_shape_for_the_owner(code)
    assert (reason, alternative) != general
    assert "cada variante" in reason or "variantes" in reason


def test_the_base_mirrors_the_python_rule() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'e483_native_condition_after_send_already_applied'" in sql
    for needed in ("c_max_sends constant integer := 3", "v_waited < 1", "v_step ? 'boton'", '["contesta"]',
                   "attempt.outcome = ''accepted_by_chatwoot''", "position_row.ordinal = 1"):
        assert needed in sql, needed


# -- El validador del ciclo, con el catalogo --------------------------------------------------------


@pytest.fixture
def full_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.28.0")
    monkeypatch.setenv("GIT_SHA", "bc1e63b0bc1e63b0bc1e63b0bc1e63b0bc1e63b0")
    added = {**TEMPLATES, FOLLOW: MARIBEL_CATALOG[FOLLOW], **FOLLOWUPS_ATT1}
    templates = [t for t in app_tests.CATALOG["message_templates"] if t.get("name") not in added]
    monkeypatch.setitem(app_tests.CATALOG, "message_templates", [*templates, *copy.deepcopy(list(added.values()))])


def _validate(pasos: list, *, multitouch: bool = True, stage: str = "validate") -> tuple[dict, dict]:
    """El AE propone el pago rechazado nuevo y lo valida (el molde de test_flow_engine_native_template_shape),
    con una instancia que manda (o no) los toques siguientes."""
    from test_flow_change_api_app import AUTH, PREFIX, SLACK, PostgrestEmulator, _client

    from bridge.flow_validator import NativeRuntime

    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(shape.COUNTS)
    runtime = NativeRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                            cart_touches=True, main_product="att1", native_multitouch=multitouch)
    client, _ = _client(emulator, runtime=runtime)
    definition = {key: copy.deepcopy(FULL[key]) for key in ("nombre", "dispara", "entra", "termina", "horario")}
    saved = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json={
        "items": [{"producto": "att1", "flujo": "pago_rechazado", "tipo": "nuevo", "prender": "cohorte",
                   "definicion": {**definition, "pasos": copy.deepcopy(pasos)}}],
        "base": {}, "purpose": "el pago rechazado completo, B1 por motivo y B2 o Z", "requested_by": SLACK})
    if stage == "draft":
        return {}, saved.json()
    assert saved.status_code == 201, saved.json()
    data = saved.json()["data"]
    validated = client.post(f"{PREFIX}/flow-changes/{data['proposal_id']}/validate", headers=AUTH,
                            json={"revision": data["revision"], "requested_by": SLACK})
    assert validated.status_code == 200, validated.json()
    return emulator.proposals[data["proposal_id"]]["items"][0]["definicion"], validated.json()["data"]


def test_without_the_flag_the_ae_cannot_save_the_complete_refused_payment(full_catalog: None) -> None:
    # Con FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED apagado saldria solo B1: el borrador vuelve frenado.
    _, answer = _validate(FULL["pasos"], multitouch=False, stage="draft")
    assert answer["detail"] == "flow_change_draft_blocked"
    assert [block["code"] for block in answer["data"]["blocks"]] == ["native_multitouch_not_running"]


def test_the_cycle_validates_the_complete_refused_payment(full_catalog: None) -> None:
    definition, result = _validate(FULL["pasos"])

    assert definition["pasos"] == FULL["pasos"]
    assert result["blocks"] == [], result["blocks"]
    assert result["status"] == "validada"


def test_the_approved_b2_and_z_of_att1_do_not_fit_a_following_native_touch_yet(full_catalog: None) -> None:
    # B2 y Z de ATT1 llevan el boton del checkout con el cupon 2 y una variable mas: un toque siguiente nativo
    # no lleva boton (20261112210000) ni cupon personal (C2). El validador lo frena; no sale a medias.
    pasos = [{"esperar": "15m"}, _chain(_paths(
        cupon=_path("cupon", True, follow=Z_ATT1), dos_veces=_path("dos_veces", False, follow=B2_ATT1),
        saldo=_path("saldo", True, follow=Z_ATT1), banco=_path("banco", False, follow=B2_ATT1)))]
    _, result = _validate(pasos)

    assert result["status"] == "borrador"
    codes = {code for code, _ in shape._blocks(result)}
    assert codes & {"template_button_mismatch", "template_variable_count_mismatch"}, shape._blocks(result)


# -- El bridge, con las filas de la base -------------------------------------------------------------


def _client(rows: dict[str, Any]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows[request.url.path.removeprefix("/rest/v1/rpc/")])

    return SupabaseClient(base_url="https://supabase.example.test", service_role_key="clave-de-prueba",
                          transport=httpx.MockTransport(handler))


def _parsed(row: dict[str, Any]) -> Any:
    step = asyncio.run(_client({"flow_engine_execution_context_v1": [{**row, "flow_id": FLOW_ID}]})
                       .get_flow_engine_step_context("action-att1"))
    assert step is not None
    return step


def test_the_fixture_is_the_real_chain_with_its_follow_up() -> None:
    assert ROWS["_capture"]["encargo"] == "E-483 C3"
    assert [branch["paso"]["plantilla"] for branch in ROWS["touch1_context_banco"]["template_branches"]] == [
        f"maribel_pago_rechazado_{name}_01" for name in ("cupon", "dos_veces", "saldo", "banco")]
    assert (ROWS["touch2_context_banco"]["template_name"], ROWS["touch2_context_saldo"]["template_name"]) == (
        B2_ATT1, Z_ATT1)


def test_the_client_reads_the_following_touch_of_the_chosen_branch_without_branches() -> None:
    first = _parsed(ROWS["touch1_context_banco"])
    assert first.send_ordinal == 1 and first.template_branches is not None and len(first.template_branches) == 4

    for key, template in (("banco", B2_ATT1), ("saldo", Z_ATT1)):
        second = _parsed(ROWS[f"touch2_context_{key}"])
        assert (second.step_key, second.send_ordinal, second.template_name) == ("native_touch_2", 2, template)
        assert second.template_branches is None and second.template_button is None
        assert second.template_variables == ("nombre", "producto")


def _following_branch(authority: _NativeAuthority, **changes: Any) -> _NativeAuthority:
    """El toque siguiente de una rama, como lo da la base (el molde de test_e483_native_multitouch)."""
    second = _parsed(ROWS["touch2_context_saldo"])
    authority.action = replace(authority.action, action_type="no_reply_review", step_key="native_touch_2",
                               anchor_type="message")
    authority.context = replace(authority.context, action_type="no_reply_review", step_key="native_touch_2")
    # La plantilla del catalogo de la prueba (el inbox 11 de ATT1 del despachador nativo): Z tal como esta
    # aprobada no entra en un toque siguiente (lo frena el validador, arriba).
    authority.step = replace(second, template_name=authority.step.template_name,
                             template_variables=authority.step.template_variables, **changes)
    return authority


def test_with_the_flag_the_following_touch_of_a_branch_goes_out_without_picking_again(tmp_path: Path) -> None:
    authority = _following_branch(_NativeAuthority())
    chatwoot = _NativeChatwoot()
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_multitouch_enabled = True
    composition = asyncio.run(dispatcher._compose_approved_template_proposal(
        action=authority.action, execution_context=authority.context, attempt=None, step_template=authority.step))

    assert composition.failure_reason is None
    assert composition.template_name == authority.step.template_name
    # El doble de Supabase no sabe dar los datos del rechazo ni registrar una rama: el bridge no los pidio
    # (la rama es la del primer envio).
    assert not hasattr(authority, "get_flow_engine_native_payment_failure_context")


def test_a_following_touch_with_branches_closes_for_good(tmp_path: Path) -> None:
    # La base nunca da ramas en el toque siguiente (20261112210300); si las diera, el bridge no elige otra vez.
    branch = FlowEngineNativeBranch(datum=None, values=None, template_name=FOLLOW, template_language="es_MX",
                                    template_variables=("nombre", "producto"), template_button=None)
    authority = _following_branch(_NativeAuthority(), template_branches=(branch, branch))
    chatwoot = _NativeChatwoot()
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_multitouch_enabled = True

    _run(dispatcher)

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED
