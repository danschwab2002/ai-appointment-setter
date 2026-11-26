"""El conocimiento vivo: las reglas, el servido y el turno (live-knowledge-v1, pieza 5.5).

Dato real: el conocimiento v14 de ATT1 (``instances/att1-b7580bc-20261103``, copia del repo
de la instancia del 2026-11-03) y el v9 de Maribel (``instances/maribel-fbad25e-20261113``,
del 2026-11-13), con sus manifiestos. Los cambios que prueban un bloqueo los arma el test y
lo dice. El 2026-11-26 se corrio lo mismo, en solo lectura y sin guardarlo, contra el v15 de
ATT1 y el v10 de Maribel de los repos de instancia (los vigentes): las dos pasan sus propias
reglas, un cambio de una pregunta frecuente pasa y un monto inventado bloquea.

Las respuestas del api_server de Hermes salen del fixture capturado
``hermes_glm_prose_then_json_att1_20261029.json`` (con ``_proposal`` de test_price_guard_turn).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_price_guard_turn import GOOD_PRICE_REPLY, _Hermes, _proposal, _Recorder

from bridge import live_knowledge as lk
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.hermes import HermesShadowProcessor
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(__file__).parent / "fixtures" / "instances"
ATT1 = FIXTURES / "att1-b7580bc-20261103"
MARIBEL = FIXTURES / "maribel-fbad25e-20261113"
KNOWLEDGE_ID = "6f1c1e9a-55aa-4f6e-9d55-000000000055"


def _instance(folder: Path, name: str) -> tuple[InstanceManifest, lk.KnowledgeContext]:
    raw = tomllib.loads((folder / "conocimiento" / name).read_text(encoding="utf-8"))
    knowledge = CommercialKnowledge.from_mapping(raw)
    manifest = InstanceManifest.from_toml_file(folder / "instancia.toml")
    return manifest, lk.KnowledgeContext.build(manifest, lk.file_knowledge(knowledge, raw))


ATT1_MANIFEST, ATT1_CONTEXT = _instance(ATT1, "knowledge-v14.toml")
MARIBEL_MANIFEST, MARIBEL_CONTEXT = _instance(MARIBEL, "knowledge-v9.toml")
FILE = ATT1_CONTEXT.file


def _faq_change(extra: str = " Te escribimos por aca.") -> dict[str, Any]:
    faq = copy.deepcopy(FILE.sections["faq"])
    faq[0]["respuesta"] += extra  # la mutacion es del test
    return lk.parse_change({"cambios": {"faq": faq}})


def _evaluate(change: dict[str, Any], context: lk.KnowledgeContext = ATT1_CONTEXT) -> lk.Evaluation:
    return lk.evaluate(change, base=context.file, context=context)


def _codes(evaluation: lk.Evaluation) -> list[str]:
    return [block["code"] for block in evaluation.blocks]


# ------------------------------------------------------------------------ las reglas


@pytest.mark.parametrize("context", [ATT1_CONTEXT, MARIBEL_CONTEXT], ids=["att1-v14", "maribel-v9"])
def test_the_file_of_each_instance_passes_its_own_rules(context: lk.KnowledgeContext) -> None:
    assert lk.instance_blocks(context.file.knowledge, context=context) == []


@pytest.mark.parametrize("context", [ATT1_CONTEXT, MARIBEL_CONTEXT], ids=["att1-v14", "maribel-v9"])
def test_rebuilding_the_file_from_its_sections_gives_the_same_text_and_hash(context: lk.KnowledgeContext) -> None:
    # El render no lee la cabecera salvo la version: el hash de lo validado es el de lo servido.
    file = context.file
    rebuilt = lk.build_knowledge(file.sections, file_version=file.file_version, ally_ref=file.knowledge.ally_ref)
    approved = lk.build_knowledge(file.sections, file_version=file.file_version, ally_ref=file.knowledge.ally_ref,
                                  approved_by="U0DANSCHWAB1", approved_on=file.knowledge.approved_on)
    assert rebuilt.render() == approved.render() == file.rendered
    assert rebuilt.rendered_sha256 == file.sha256


def test_a_change_of_one_section_passes_and_the_diff_shows_only_it() -> None:
    evaluation = _evaluate(_faq_change())
    assert evaluation.blocks == []
    # El v14 de ATT1 renderiza 29.091 bytes: pasa el 90 % del tope de 32.000 y se avisa.
    assert [warning["code"] for warning in evaluation.warnings] == ["knowledge_harness_not_run",
                                                                     "knowledge_near_size_limit"]
    assert evaluation.sections is not None
    assert {key for key in FILE.sections if evaluation.sections[key] != FILE.sections[key]} == {"faq"}
    diff = lk.render_diff(FILE.rendered, evaluation.rendered or "")
    assert (diff["agregadas"], diff["sacadas"]) == (1, 1)
    assert any(line["tipo"] == "agrega" and line["texto"].endswith("Te escribimos por aca.") for line in diff["lineas"])


def test_an_amount_that_is_not_in_the_catalog_blocks() -> None:
    evaluation = _evaluate(_faq_change(" Hoy sale USD 97."))  # USD 97 lo inventa el test
    assert evaluation.blocks == [{"code": "knowledge_amount_not_in_catalog", "detail": "USD 97"}]


def test_an_amount_inside_the_forbidden_promises_does_not_block() -> None:
    # Como en la guarda de precio: lo que el agente NO puede decir no es un permitido, ni un monto nuevo.
    forbidden = copy.deepcopy(FILE.sections["promesas_prohibidas"])
    forbidden["items"].append("Decir que cuesta USD 97.")
    assert _evaluate(lk.parse_change({"cambios": {"promesas_prohibidas": forbidden}})).blocks == []


def test_another_price_of_the_offer_blocks() -> None:
    offer = copy.deepcopy(FILE.sections["oferta"])
    offer["precio"] = "97"
    codes = _codes(_evaluate(lk.parse_change({"cambios": {"oferta": offer}})))
    assert "knowledge_price_not_in_catalog" in codes and "knowledge_amount_not_in_catalog" in codes


def test_the_price_of_maribel_in_its_currency_passes_and_another_currency_blocks() -> None:
    offer = copy.deepcopy(MARIBEL_CONTEXT.file.sections["oferta"])
    offer["descripcion"] += " Acceso inmediato."
    assert _evaluate(lk.parse_change({"cambios": {"oferta": offer}}), MARIBEL_CONTEXT).blocks == []
    offer["moneda"] = "USD"
    assert "knowledge_price_not_in_catalog" in _codes(_evaluate(lk.parse_change({"cambios": {"oferta": offer}}),
                                                                MARIBEL_CONTEXT))


def test_renaming_the_offer_blocks() -> None:
    offer = copy.deepcopy(FILE.sections["oferta"])
    offer["nombre"] = "Otro nombre"
    assert _codes(_evaluate(lk.parse_change({"cambios": {"oferta": offer}}))) == ["knowledge_offer_name_changed"]


def test_the_same_text_is_an_empty_change() -> None:
    change = lk.parse_change({"cambios": {"faq": FILE.sections["faq"]}})
    assert _codes(_evaluate(change)) == ["knowledge_change_empty"]


def test_what_the_loader_refuses_blocks_with_its_message() -> None:
    voice = copy.deepcopy(FILE.sections["voz"])
    voice["reglas"] = []
    evaluation = _evaluate(lk.parse_change({"cambios": {"voz": voice}}))
    assert _codes(evaluation) == ["knowledge_invalid"]
    assert "voz.reglas" in evaluation.blocks[0]["detail"]
    faq = copy.deepcopy(FILE.sections["faq"])
    faq[0]["respuesta"] = "la clave es sk-" + "a" * 30  # forma de credencial, inventada por el test
    assert _codes(_evaluate(lk.parse_change({"cambios": {"faq": faq}}))) == ["knowledge_invalid"]


def test_over_the_size_limit_blocks() -> None:
    faq = copy.deepcopy(FILE.sections["faq"])
    faq.append({"pregunta": "Larga", "respuesta": "x " * 3000, "fuente": "test", "vigencia": "2026-11-26"})
    assert _codes(_evaluate(lk.parse_change({"cambios": {"faq": faq}}))) == ["knowledge_invalid"]


def test_an_optional_section_can_be_removed_and_a_mandatory_one_cannot() -> None:
    assert _evaluate(lk.parse_change({"cambios": {"faq": None}})).blocks == []
    with pytest.raises(lk.KnowledgeChangeInvalid) as refused:
        lk.parse_change({"cambios": {"voz": None}})
    assert refused.value.reason == {"field": "cambios.voz", "problem": "seccion_obligatoria"}


@pytest.mark.parametrize("raw, problem", [
    ({"cambios": {"cabecera": {}}}, "seccion_no_editable"),
    ({"cambios": {"otra": {}}}, "seccion_no_editable"),
    ({"cambios": {}}, "cambios_vacio_o_no_objeto"),
    ({"cambios": {"faq": "texto"}}, "seccion_debe_ser_tabla_o_lista"),
    ({"cambios": {"productos.att1": {"clave": "otra"}}}, "la_clave_del_producto_no_cambia"),
    ({"otra": 1}, "debe_ser_un_objeto_con_cambios"),
])
def test_the_shape_of_the_change_is_closed(raw: dict[str, Any], problem: str) -> None:
    with pytest.raises(lk.KnowledgeChangeInvalid) as refused:
        lk.parse_change(raw)
    assert refused.value.reason["problem"] == problem


def test_a_product_section_on_a_single_product_knowledge_blocks() -> None:
    change = lk.parse_change({"cambios": {"productos.att1": {"clave": "att1"}}})
    assert _codes(_evaluate(change)) == ["knowledge_change_invalid"]


def test_changing_the_shape_blocks() -> None:
    sections = copy.deepcopy(dict(FILE.sections))
    product = {"clave": "alimenta", "oferta": sections.pop("oferta"), "contenido": sections.pop("contenido")}
    by_product = lk.build_knowledge({**sections, "productos": [product]}, file_version=14, ally_ref="att1")
    assert [block["code"] for block in lk.instance_blocks(by_product, context=ATT1_CONTEXT)] == \
        ["knowledge_shape_changed"]


# ------------------------------------------------------------------------ el servido


def _row(sections: Mapping[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    sections = sections if sections is not None else _evaluate(_faq_change()).sections
    assert sections is not None
    knowledge = lk.build_knowledge(sections, file_version=FILE.file_version, ally_ref="att1")
    return {"knowledge_id": KNOWLEDGE_ID, "live_version": 1, "file_version": FILE.file_version,
            "file_sha256": FILE.file_sha256, "knowledge": sections, "rendered_sha256": knowledge.rendered_sha256,
            "approved_at": "2026-11-26T15:00:00+00:00", "approved_by_ref": "U0DANSCHWAB1", **overrides}


class _Base:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.down = False
        self.calls = 0

    async def __call__(self) -> Mapping[str, Any]:
        self.calls += 1
        if self.down:
            raise OSError("caida")
        return {"knowledge": self.row}


def _source(base: _Base, segments: list[str] | None = None) -> lk.LiveKnowledgeSource:
    held = segments if segments is not None else []
    return lk.LiveKnowledgeSource(context=ATT1_CONTEXT, fetch=base, segments_in_force=lambda: tuple(held))


def test_without_a_live_version_the_file_is_served_as_today() -> None:
    source = _source(_Base())
    assert source.current() is FILE  # antes de leer
    assert asyncio.run(source.refresh()) is True
    assert source.current() is FILE
    assert source.current().ref() == {"origen": "archivo", "version": 14, "sha256": FILE.sha256}
    assert source.status_line() == "origen=archivo,refresh=ok"


def test_the_published_version_is_served_and_the_last_good_stays_if_the_base_falls() -> None:
    base = _Base(_row())
    source = _source(base)
    asyncio.run(source.refresh())
    served = source.current()
    assert served.origin == "vivo" and served.knowledge_id == KNOWLEDGE_ID and served.live_version == 1
    assert served.rendered.count("Te escribimos por aca.") == 1
    assert source.serving_state(KNOWLEDGE_ID) == "served"
    base.down = True
    assert asyncio.run(source.refresh()) is False
    assert source.current() is served
    assert source.status_line() == "origen=vivo:v1,refresh=failing:1"


def test_if_the_base_never_answered_the_file_is_served() -> None:
    base = _Base(_row())
    base.down = True
    source = _source(base)
    assert asyncio.run(source.refresh()) is False
    assert source.current() is FILE


def test_returning_to_the_file_serves_the_file_on_the_next_read() -> None:
    base = _Base(_row())
    source = _source(base)
    asyncio.run(source.refresh())
    base.row = None
    asyncio.run(source.refresh())
    assert source.current() is FILE and source.serving_state(KNOWLEDGE_ID) is None


@pytest.mark.parametrize("overrides, reason", [
    ({"file_sha256": "0" * 64}, lk.NOT_SERVED_FILE_CHANGED),
    ({"file_version": 15}, lk.NOT_SERVED_FILE_CHANGED),
    ({"rendered_sha256": "1" * 64}, lk.NOT_SERVED_RENDER),
    ({"knowledge": {"voz": {}}}, lk.NOT_SERVED_INVALID),
])
def test_a_published_version_that_cannot_be_served_falls_back_to_the_file(overrides: dict[str, Any],
                                                                           reason: str) -> None:
    source = _source(_Base(_row(**overrides)))
    asyncio.run(source.refresh())
    assert source.current() is FILE
    assert source.serving_state(KNOWLEDGE_ID) == reason
    assert source.status_line() == f"origen=archivo,refresh=ok,no_servida={reason}"


def test_a_published_version_over_a_price_out_of_the_catalog_is_not_served() -> None:
    offer = copy.deepcopy(FILE.sections["oferta"])
    offer["precio"] = "97"
    sections = {**copy.deepcopy(dict(FILE.sections)), "oferta": offer}
    source = _source(_Base(_row(sections)))
    asyncio.run(source.refresh())
    assert source.current() is FILE and source.serving_state(KNOWLEDGE_ID) == lk.NOT_SERVED_INVALID


def test_with_a_segment_in_force_the_file_is_served_and_the_live_one_comes_back_after() -> None:
    # D-55.4: el segmento se firmo contra el archivo; nunca va junto con una version viva.
    segments = ["black-november-2026"]
    source = _source(_Base(_row()), segments)
    asyncio.run(source.refresh())
    assert source.current() is FILE
    assert source.edit_base().origin == "vivo"
    assert source.serving_state(KNOWLEDGE_ID) == lk.NOT_SERVED_SEGMENTS
    assert source.status_line() == f"origen=archivo,refresh=ok,no_servida={lk.NOT_SERVED_SEGMENTS}"
    segments.clear()
    assert source.current().origin == "vivo" and source.serving_state(KNOWLEDGE_ID) == "served"


def test_if_the_segments_cannot_be_read_the_file_is_served() -> None:
    def broken() -> tuple[str, ...]:
        raise RuntimeError("catalogo")

    source = lk.LiveKnowledgeSource(context=ATT1_CONTEXT, fetch=_Base(_row()), segments_in_force=broken)
    asyncio.run(source.refresh())
    assert source.current() is FILE


# ------------------------------------------------------------------------ el turno


class _Fixed:
    def __init__(self, served: lk.ServedKnowledge) -> None:
        self.served = served

    def current(self) -> lk.ServedKnowledge:
        return self.served


CONTEXT: dict[str, object] = {"conversation_ref": "71", "messages": [], "known_fields": {}}


def _turn(tmp_path: Path, name: str, *, source: Any = None, bind: bool = False
          ) -> tuple[list[dict[str, object]], bytes, list[dict[str, object]]]:
    hermes = _Hermes([_proposal(GOOD_PRICE_REPLY)])
    recorder = _Recorder()
    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1", api_key="test-hermes-key", model_name="agente-comercial",
        shadow_dir=tmp_path / name, transport=httpx.MockTransport(hermes), system_prompt=FILE.rendered,
        provenance_recorder=recorder, **({"knowledge_source": source} if source is not None and not bind else {}))
    if bind:
        processor.bind_knowledge_source(source)
    asyncio.run(processor.run(delivery_id="turno-55", context=dict(CONTEXT)))
    digest = hashlib.sha256(b"turno-55").hexdigest()
    return hermes.requests, (tmp_path / name / f"{digest}.json").read_bytes(), \
        [turn.context_added for turn in recorder.turns]


def test_with_the_flag_off_the_request_to_hermes_is_byte_for_byte_the_same(tmp_path: Path) -> None:
    today = _turn(tmp_path, "hoy")
    unbound = _turn(tmp_path, "sin-fuente", source=None, bind=True)
    assert today == unbound
    assert "knowledge" not in today[2][0]


def test_with_the_flag_on_and_the_file_only_the_provenance_says_which_knowledge(tmp_path: Path) -> None:
    today = _turn(tmp_path, "hoy")
    with_file = _turn(tmp_path, "archivo", source=_Fixed(FILE))
    assert json.dumps(with_file[0]) == json.dumps(today[0])  # el pedido, igual
    assert with_file[1] == today[1]  # el resultado guardado, igual
    added = with_file[2][0]
    assert added.pop("knowledge") == {"origen": "archivo", "version": 14, "sha256": FILE.sha256}
    assert added == today[2][0]


def test_a_live_version_goes_as_the_system_and_the_provenance_names_it(tmp_path: Path) -> None:
    source = _source(_Base(_row()))
    asyncio.run(source.refresh())
    requests, _, added = _turn(tmp_path, "vivo", source=source, bind=True)
    system = requests[0]["messages"][0]  # type: ignore[index]
    assert system == {"role": "system", "content": source.current().rendered}
    assert "Te escribimos por aca." in system["content"]
    assert added[0]["knowledge"] == {"origen": "vivo", "knowledge_id": KNOWLEDGE_ID, "live_version": 1,
                                     "version": 14, "sha256": source.current().sha256}
