"""Segmentos por etiqueta: el archivo, el catalogo y la seleccion por turno (E-311, PR-A).

Diseno: ``documentacion/2026-11-03-e310-diseno-segmentos-conocimiento-por-etiqueta.md``
(E-310) con los defaults D1 a D15. Contrato: ``docs/contracts/segment-knowledge-v1.md``.

Datos: el segmento es ``tests/fixtures/segments/campana-de-prueba-v1.toml``,
FICTICIO a proposito (no es la oferta de ninguna campana real). El conocimiento
base es el de ``tests/fixtures/instances/att1``. Toda hora es fija: ningun test
mira el reloj (el segmento tiene vigencia de noviembre de 2026).
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge import segment_check, segment_knowledge
from bridge.agent_provenance import AgentTurn
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.hermes import HermesShadowProcessor
from bridge.instance_cli import validate_instance
from bridge.instance_manifest import AgentSegment, InstanceManifest, ManifestError
from bridge.segment_knowledge import (
    MAX_SEGMENT_RENDERED_BYTES,
    MAX_TURN_SYSTEM_BYTES,
    PREAMBLE,
    LoadedSegment,
    SegmentCatalog,
    SegmentError,
    SegmentKnowledge,
    TurnSegment,
    build_catalog,
    load_declared_segment,
    load_segment_catalog,
)

FIXTURES = Path(__file__).parent / "fixtures"
SEGMENT = FIXTURES / "segments" / "campana-de-prueba-v1.toml"
ATT1 = FIXTURES / "instances" / "att1"
TZ = "America/Mexico_City"
CDMX = timezone(timedelta(hours=-6))
INSIDE = datetime(2026, 11, 25, 12, 0, tzinfo=CDMX)
DECLARATION = """
[[agente.segmentos]]
clave = "campana-de-prueba"
etiqueta = "campana-de-prueba"
conocimiento = "conocimiento/segmentos/campana-de-prueba-v1.toml"
prioridad = 10
"""

EXPECTED_RENDER = """# Segmento: Campaña de prueba (vigente hasta el 30/11 23:59, hora de la aliada)

Esta persona está en esta campaña. Lo de este bloque vale para ella y, donde dice algo distinto del conocimiento general, manda este bloque. No cambia cómo respondes, el link de pago ni cuándo derivar.

## Cómo usar este bloque

Bloque de prueba: si la persona pregunta por la campaña de prueba, usa estos datos.

## Oferta de la campaña

- Precio en esta campaña: USD 11.
- Precio de antes: USD 22.
- Cupón de la campaña: PRUEBA11.
- Garantía en esta campaña: La misma del programa, sin cambios.
- Cambia a propósito respecto del conocimiento general: precio.

Incluye en esta campaña:

- Un bono ficticio de prueba.

## Asesoramiento

- Si pregunta qué le conviene, recomienda el programa completo (texto de prueba).

## No confirmado

Si preguntan por esto, deriva:

- Si la campaña de prueba se extiende.

## Preguntas frecuentes de la campaña

- P: ¿Hasta cuándo dura la campaña de prueba?
  R: Hasta el 30/11 a las 23:59, hora de México (dato de prueba).
"""


def _payload() -> dict[str, Any]:
    return tomllib.loads(SEGMENT.read_text(encoding="utf-8"))


def _load(payload: dict[str, Any] | None = None, **kwargs: Any) -> SegmentKnowledge:
    return SegmentKnowledge.from_mapping(payload if payload is not None else _payload(), time_zone=TZ, **kwargs)


def _approve(path: Path) -> None:
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'estado = "borrador"',
            'estado = "aprobado"\naprobado_por = "test"\naprobado_el = 2026-09-28',
        ),
        encoding="utf-8",
    )


def sellar_chequeo(text: str, base: CommercialKnowledge, otros: tuple[str, ...] = ()) -> str:
    """El segmento con el ``chequeo_sha`` que da el calculo real (E-337).

    Es lo que escribe ``setter segmento chequear`` con 0 abiertos: el cargador lo
    exige a un ``aprobado``. Se calcula con ``segment_check.calcular_chequeo_sha``
    sobre el contenido actual y el base de la prueba, nunca a mano. ``otros`` son
    los textos de los otros segmentos declarados.
    """

    raw = tomllib.loads(text)
    sha = segment_check.calcular_chequeo_sha(
        raw,
        base_sha256=base.rendered_sha256,
        otros=[tomllib.loads(other) for other in otros],
    )
    lines = [line for line in text.splitlines(keepends=True) if not line.startswith("chequeo_sha")]
    anchor = next(index for index, line in enumerate(lines) if line.startswith("aprobado_el"))
    lines.insert(anchor + 1, f'chequeo_sha = "{sha}"\n')
    return "".join(lines)


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    """La instancia de prueba de ATT1 con el conocimiento aprobado y el segmento declarado."""

    target = tmp_path / "instancia"
    shutil.copytree(ATT1, target)
    _approve(target / "conocimiento" / "knowledge-v1.toml")
    (target / "conocimiento" / "segmentos").mkdir()
    base = CommercialKnowledge.from_toml_file(target / "conocimiento" / "knowledge-v1.toml")
    (target / "conocimiento" / "segmentos" / SEGMENT.name).write_text(
        sellar_chequeo(SEGMENT.read_text(encoding="utf-8"), base), encoding="utf-8"
    )
    manifest = target / "instancia.toml"
    text = manifest.read_text(encoding="utf-8")
    anchor = 'conocimiento = "conocimiento/knowledge-v1.toml"\n'
    assert text.count(anchor) == 1
    manifest.write_text(text.replace(anchor, anchor + DECLARATION), encoding="utf-8")
    return target


def _base(instance: Path) -> CommercialKnowledge:
    return CommercialKnowledge.from_toml_file(instance / "conocimiento" / "knowledge-v1.toml")


def _manifest(instance: Path) -> InstanceManifest:
    return InstanceManifest.from_toml_file(instance / "instancia.toml")


def _loaded(
    key: str = "a",
    *,
    priority: int = 10,
    label: str | None = None,
    start: datetime = datetime(2026, 11, 20, tzinfo=CDMX),
    end: datetime = datetime(2026, 12, 1, tzinfo=CDMX),
    filler: int = 0,
) -> LoadedSegment:
    payload = _payload()
    payload["cabecera"]["clave"] = key
    payload["vigencia"] = {"desde": start, "hasta": end}
    if filler:
        payload["asesoramiento"]["items"] = ["x" * filler]
    knowledge = _load(payload)
    rendered = knowledge.render()
    return LoadedSegment(
        key=key,
        label=label or key,
        priority=priority,
        knowledge_path=f"conocimiento/segmentos/{key}-v1.toml",
        label_macro_id=None,
        knowledge=knowledge,
        rendered=rendered,
        sha256=knowledge.rendered_sha256,
    )


# ------------------------------------------------------------------ el archivo


def test_the_fixture_renders_byte_for_byte_with_the_product_preamble() -> None:
    segment = SegmentKnowledge.from_toml_file(SEGMENT, time_zone=TZ)

    assert segment.render() == EXPECTED_RENDER
    assert PREAMBLE in segment.render()
    assert segment.rendered_sha256 == "3dfe3b54f2e5b6d1633bd27873041c05df1239f9d11acaa2a2a7b94c7100dc4b"
    assert segment.key == "campana-de-prueba" and segment.version == 1 and segment.approved


def test_the_render_is_deterministic_and_leaves_out_the_intake_answers() -> None:
    first, second = _load(), _load()

    assert first.render() == second.render()
    assert first.rendered_sha256 == second.rendered_sha256
    assert first.intake and first.intake[0].question == "¿Se combina con el cupón del carrito?"
    assert "ficticia" not in first.render()  # la respuesta del alta es registro, no va al agente


def test_without_a_name_the_block_is_titled_with_the_key() -> None:
    payload = _payload()
    del payload["cabecera"]["nombre"]

    assert _load(payload).render().startswith("# Segmento: campana-de-prueba (vigente hasta el 30/11 23:59")


def test_the_end_is_shown_in_the_time_zone_of_the_ally() -> None:
    assert "hasta el 30/11 23:59" in _load().render()
    # La misma vigencia leida en Buenos Aires (UTC-3) dice otra hora.
    assert "hasta el 01/12 02:59" in SegmentKnowledge.from_mapping(
        _payload(), time_zone="America/Argentina/Buenos_Aires"
    ).render()


def test_a_draft_does_not_load_for_the_bridge_but_does_for_validation() -> None:
    payload = _payload()
    payload["cabecera"]["estado"] = "borrador"
    with pytest.raises(SegmentError, match="borrador"):
        _load(payload)
    assert _load(payload, require_approved=False).approved is False


def test_an_approved_segment_says_who_and_when() -> None:
    payload = _payload()
    del payload["cabecera"]["aprobado_por"]
    with pytest.raises(SegmentError, match="aprobado_por y aprobado_el"):
        _load(payload)


def test_the_check_sha_is_parsed_with_its_shape() -> None:
    payload = _payload()
    payload["cabecera"]["chequeo_sha"] = "a" * 64
    assert _load(payload).check_sha == "a" * 64
    assert _load().check_sha is None
    payload["cabecera"]["chequeo_sha"] = "no-es-un-sha"
    with pytest.raises(SegmentError, match="chequeo_sha"):
        _load(payload)


@pytest.mark.parametrize(
    ("desde", "hasta", "message"),
    [
        (datetime(2026, 11, 20), datetime(2026, 12, 1, tzinfo=CDMX), "zona"),
        ("2026-11-20", datetime(2026, 12, 1, tzinfo=CDMX), "con zona"),
        (datetime(2026, 12, 1, tzinfo=CDMX), datetime(2026, 12, 1, tzinfo=CDMX), "anterior"),
    ],
)
def test_the_window_needs_a_zone_and_an_order(desde: object, hasta: object, message: str) -> None:
    payload = _payload()
    payload["vigencia"] = {"desde": desde, "hasta": hasta}
    with pytest.raises(SegmentError, match=message):
        _load(payload)


def test_the_window_borders() -> None:
    segment = _load()
    start = datetime(2026, 11, 20, 0, 0, tzinfo=CDMX)
    end = datetime(2026, 12, 1, 0, 0, tzinfo=CDMX)

    assert segment.state_at(start - timedelta(microseconds=1)) == "por_empezar"
    assert segment.state_at(start) == "vigente"  # desde exacto: va
    assert segment.state_at(end - timedelta(microseconds=1)) == "vigente"
    assert segment.state_at(end) == "vencido"  # hasta exacto: ya no va
    assert segment.state_at(end.astimezone(UTC)) == "vencido"
    with pytest.raises(ValueError):
        segment.state_at(datetime(2026, 11, 25))


def test_a_segment_over_8000_bytes_is_refused() -> None:
    payload = _payload()
    payload["asesoramiento"]["items"] = ["x" * MAX_SEGMENT_RENDERED_BYTES]
    with pytest.raises(SegmentError, match="8000"):
        _load(payload)


@pytest.mark.parametrize("secret", ["sk-" + "a" * 24, "xoxb-" + "1" * 20, "f" * 40])
def test_secret_shaped_values_are_refused(secret: str) -> None:
    payload = _payload()
    payload["asesoramiento"]["items"] = [f"usa {secret}"]
    with pytest.raises(SegmentError, match="credencial"):
        _load(payload)


def test_the_instruction_has_a_cap() -> None:
    payload = _payload()
    payload["instruccion"]["texto"] = "y" * 601
    with pytest.raises(SegmentError, match="600"):
        _load(payload)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda o: o.pop("moneda"), "precio y oferta.moneda van juntos"),
        (lambda o: (o.pop("precio"), o.pop("moneda")), "precio_antes va con"),
        (lambda o: o.update(reemplaza=["nombre"]), "no es un campo que se pueda reemplazar"),
        (lambda o: o.update(reemplaza=["medios"]), "la oferta no lo trae"),
        (lambda o: o.update(reemplaza=["precio", "precio"]), "repite"),
        (lambda o: o.update(cupon="CON ESPACIO"), "oferta.cupon"),
        (lambda o: o.update(otra="x"), "claves no soportadas otra"),
        (lambda o: o.update(producto="Con Mayus"), "oferta.producto debe ser un slug"),
    ],
)
def test_the_offer_rules(change: Any, message: str) -> None:
    payload = _payload()
    change(payload["oferta"])
    with pytest.raises(SegmentError, match=message):
        _load(payload)


def test_an_empty_offer_is_refused() -> None:
    payload = _payload()
    payload["oferta"] = {}
    with pytest.raises(SegmentError, match="vacia"):
        _load(payload)


def test_the_product_names_the_offer_of_the_base_with_several_products() -> None:
    payload = _payload()
    payload["oferta"]["producto"] = "att2"
    names = {"att1": "Alimenta tu Tiroides", "att2": "Programa dos"}

    assert "- Producto: Programa dos." in _load(payload, product_names=names).render()
    payload["oferta"]["producto"] = "otro"
    with pytest.raises(SegmentError, match="no es un producto del conocimiento base"):
        _load(payload, product_names=names)


def test_with_one_product_the_product_is_the_principal() -> None:
    payload = _payload()
    payload["oferta"]["producto"] = "alimenta-tu-tiroides"

    assert "- Producto: Alimenta tu Tiroides." in _load(payload, principal_name="Alimenta tu Tiroides").render()
    assert "- Producto: alimenta-tu-tiroides." in _load(payload).render()


def test_without_an_offer_or_the_optional_lists_it_still_renders() -> None:
    payload = _payload()
    for key in ("oferta", "instruccion", "no_confirmado", "faq", "preguntas_del_alta"):
        payload.pop(key)
    render = _load(payload).render()

    assert "## Oferta" not in render and "## No confirmado" not in render
    assert render.endswith("(texto de prueba).\n")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.update(otra={}), "claves no soportadas otra"),
        (lambda p: p.pop("asesoramiento"), "faltan asesoramiento"),
        (lambda p: p["asesoramiento"].update(items=[]), "no puede estar vacia"),
        (lambda p: p.update(se_puede_decir={"items": []}), "no puede estar vacia"),
        (lambda p: p["cabecera"].update(nombre=" "), "cabecera.nombre"),
        (lambda p: p["cabecera"].update(clave="Con Mayus"), "slug"),
        (lambda p: p["cabecera"].update(segment_version=0), "entero positivo"),
        (lambda p: p["faq"][0].pop("fuente"), "faltan fuente"),
    ],
)
def test_the_schema_is_closed(change: Any, message: str) -> None:
    payload = _payload()
    change(payload)
    with pytest.raises(SegmentError, match=message):
        _load(payload)


# ------------------------------------------------------------------ el catalogo y el turno


def test_the_turn_takes_the_labeled_segments_in_force() -> None:
    catalog = build_catalog([_loaded("a"), _loaded("b", priority=20, start=datetime(2027, 1, 1, tzinfo=CDMX),
                                                    end=datetime(2027, 2, 1, tzinfo=CDMX))], base_bytes=29_091)

    selection = catalog.select(labels=("a", "b", "otra"), now=INSIDE)
    assert [segment.key for segment in selection.active] == ["a"]
    assert selection.skipped == (("b", 1, "not_started"),)
    assert selection.context_entries() == [{"key": "a", "version": 1}]

    assert catalog.select(labels=("otra",), now=INSIDE).active == ()  # sin la etiqueta, nada
    expired = catalog.select(labels=("a",), now=datetime(2026, 12, 1, tzinfo=CDMX))
    assert expired.active == () and expired.skipped == (("a", 1, "expired"),)
    with pytest.raises(ValueError):
        catalog.select(labels=("a",), now=datetime(2026, 11, 25))


def test_two_segments_go_by_priority_and_never_more_than_two() -> None:
    catalog = build_catalog(
        [_loaded("c", priority=30), _loaded("a", priority=10), _loaded("b", priority=20)], base_bytes=1_000
    )

    assert [segment.key for segment in catalog.segments] == ["a", "b", "c"]
    selection = catalog.select(labels=("c", "b", "a"), now=INSIDE)
    assert [segment.key for segment in selection.active] == ["a", "b"]
    assert selection.skipped == (("c", 1, "max_per_turn"),)
    assert [block.key for block in catalog.blocks_for(selection.context_entries())] == ["a", "b"]


def test_the_turn_drops_by_size_what_does_not_fit_with_the_base() -> None:
    a, b = _loaded("a", priority=10, filler=4_000), _loaded("b", priority=20, filler=4_000)
    # El cargador no deja armar esto (ver abajo): el recorte del turno es la ultima red.
    catalog = SegmentCatalog(segments=(a, b), base_bytes=MAX_TURN_SYSTEM_BYTES - a.rendered_bytes)

    selection = catalog.select(labels=("a", "b"), now=INSIDE)
    assert [segment.key for segment in selection.active] == ["a"]
    assert selection.skipped == (("b", 1, "size"),)


def test_same_priority_with_overlapping_windows_does_not_load() -> None:
    with pytest.raises(SegmentError, match="misma prioridad"):
        build_catalog([_loaded("a"), _loaded("b")], base_bytes=1_000)
    # Sin pisarse, la misma prioridad vale.
    later = _loaded("b", start=datetime(2026, 12, 1, tzinfo=CDMX), end=datetime(2026, 12, 5, tzinfo=CDMX))
    assert len(build_catalog([_loaded("a"), later], base_bytes=1_000).segments) == 2


def test_overlapping_segments_that_do_not_fit_with_the_base_do_not_load() -> None:
    a, b = _loaded("a", priority=10, filler=4_000), _loaded("b", priority=20, filler=4_000)
    with pytest.raises(SegmentError, match="suman"):
        build_catalog([a, b], base_bytes=MAX_TURN_SYSTEM_BYTES - a.rendered_bytes - 10)
    with pytest.raises(SegmentError, match="pasa los 40000"):
        build_catalog([a], base_bytes=MAX_TURN_SYSTEM_BYTES - a.rendered_bytes + 1)


def test_blocks_for_reads_only_what_the_turn_named() -> None:
    catalog = build_catalog([_loaded("a")], base_bytes=1_000)

    assert catalog.blocks_for(None) == ()
    assert catalog.blocks_for([{"key": "a", "version": 2}]) == ()  # otra version: no
    assert catalog.blocks_for(["a", {"key": "z", "version": 1}]) == ()


def test_readiness_says_key_version_sha_and_state() -> None:
    a = _loaded("a")
    catalog = build_catalog([a], base_bytes=1_000)

    assert catalog.readiness(INSIDE) == f"a:v1:{a.sha256}:vigente"
    assert catalog.readiness(datetime(2026, 1, 1, tzinfo=UTC)).endswith(":por_empezar")
    assert catalog.readiness(datetime(2027, 1, 1, tzinfo=UTC)).endswith(":vencido")
    assert build_catalog([], base_bytes=1).readiness(INSIDE) == "none"


# ------------------------------------------------------------------ la instancia


def test_the_bridge_loads_the_declared_segment_of_the_instance(instance: Path) -> None:
    manifest = _manifest(instance)
    base = _base(instance)

    assert manifest.agent_segments == (
        AgentSegment(
            key="campana-de-prueba",
            label="campana-de-prueba",
            knowledge_path="conocimiento/segmentos/campana-de-prueba-v1.toml",
            priority=10,
        ),
    )
    catalog = load_segment_catalog(manifest, instance, base)
    [segment] = catalog.segments
    assert segment.rendered == EXPECTED_RENDER
    assert catalog.base_bytes == len(base.render().encode("utf-8"))


def test_the_check_hook_gets_each_segment_with_everything_the_check_compares(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El enchufe de PR-B (firma de segment_check.verificar_chequeo) recibe todo lo que compara."""

    calls: list[dict[str, Any]] = []

    def record(segmento: dict[str, Any], **kwargs: Any) -> None:
        calls.append({"segmento": segmento, **kwargs})

    monkeypatch.setattr(segment_knowledge, "verificar_chequeo", record)
    base = _base(instance)
    load_segment_catalog(_manifest(instance), instance, base)

    [call] = calls
    on_disk = instance / "conocimiento" / "segmentos" / SEGMENT.name
    assert call["segmento"] == tomllib.loads(on_disk.read_text(encoding="utf-8"))  # el dict de tomllib, tal cual
    assert call["base"] is base and call["otros"] == []


# ------------------------------------------------------------------ el chequeo del alta (E-337)


def _segment_file(instance: Path) -> Path:
    return instance / "conocimiento" / "segmentos" / SEGMENT.name


def test_an_approved_segment_with_the_sha_of_its_content_loads(instance: Path) -> None:
    base = _base(instance)
    [segment] = load_segment_catalog(_manifest(instance), instance, base).segments
    raw = tomllib.loads(_segment_file(instance).read_text(encoding="utf-8"))
    assert segment.knowledge.check_sha == segment_check.calcular_chequeo_sha(
        raw, base_sha256=base.rendered_sha256
    )


def test_an_approved_segment_without_the_sha_does_not_load(instance: Path) -> None:
    path = _segment_file(instance)
    path.write_text(SEGMENT.read_text(encoding="utf-8"), encoding="utf-8")  # aprobado, sin chequeo_sha
    with pytest.raises(SegmentError, match="aprobado sin chequeo_sha"):
        load_segment_catalog(_manifest(instance), instance, _base(instance))


def test_a_sha_from_another_content_does_not_load(instance: Path) -> None:
    """El sha que dio el chequeo sobre una version anterior del segmento no vale para esta."""

    path = _segment_file(instance)
    text = path.read_text(encoding="utf-8")
    old = '"Un bono ficticio de prueba."'
    assert text.count(old) == 1
    path.write_text(text.replace(old, '"Un bono ficticio de prueba, cambiado."'), encoding="utf-8")
    with pytest.raises(SegmentError, match="no es el del contenido actual"):
        load_segment_catalog(_manifest(instance), instance, _base(instance))


def test_a_sha_measured_against_another_base_does_not_load(instance: Path) -> None:
    """Cambiar el conocimiento base vuelve al chequeo: el sha cubre su render."""

    base = _base(instance)
    other = CommercialKnowledge.from_toml_file(
        FIXTURES / "instances" / "att1-b7580bc-20261103" / "conocimiento" / "knowledge-v14.toml"
    )
    assert other.rendered_sha256 != base.rendered_sha256
    with pytest.raises(SegmentError, match="no es el del contenido actual"):
        segment_knowledge.verificar_chequeo(
            tomllib.loads(_segment_file(instance).read_text(encoding="utf-8")), base=other
        )


def test_approving_and_writing_the_sha_do_not_change_it(instance: Path) -> None:
    """El sha no cubre la aprobacion: el que dio el chequeo sobre el borrador vale aprobado."""

    base = _base(instance)
    draft = SEGMENT.read_text(encoding="utf-8").replace('estado = "aprobado"', 'estado = "borrador"')
    sealed_draft = sellar_chequeo(draft, base)
    sha_line = next(line for line in sealed_draft.splitlines() if line.startswith("chequeo_sha"))
    assert sha_line in _segment_file(instance).read_text(encoding="utf-8")


def test_a_draft_passes_the_hook_and_the_loader_stops_it_apart(instance: Path) -> None:
    draft = _payload()
    draft["cabecera"]["estado"] = "borrador"
    segment_knowledge.verificar_chequeo(draft, base=_base(instance))  # sin chequeo_sha, no lanza


def test_another_segment_the_check_cannot_read_is_a_segment_error(instance: Path) -> None:
    raw = tomllib.loads(_segment_file(instance).read_text(encoding="utf-8"))
    with pytest.raises(SegmentError, match="chequeo del alta"):
        segment_knowledge.verificar_chequeo(raw, base=_base(instance), otros=[{"cabecera": {}}])


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('clave = "campana-de-prueba"\nnombre', 'clave = "otra-clave"\nnombre', "cabecera.clave 'otra-clave'"),
        ('ally_ref = "att1"', 'ally_ref = "maribel"', "es de 'maribel'"),
        ("segment_version = 1", "segment_version = 2", "se llama campana-de-prueba-v2.toml"),
    ],
)
def test_the_file_has_to_be_the_one_the_manifest_declares(
    instance: Path, old: str, new: str, message: str
) -> None:
    path = instance / "conocimiento" / "segmentos" / SEGMENT.name
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding="utf-8")
    manifest = _manifest(instance)
    with pytest.raises(SegmentError, match=message):
        load_declared_segment(manifest.agent_segments[0], instance_dir=instance, manifest=manifest, base=_base(instance))


# ------------------------------------------------------------------ el manifiesto


def _with_segments(instance: Path, block: str) -> Path:
    manifest = instance / "instancia.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(DECLARATION, block), encoding="utf-8")
    return manifest


def test_without_segments_the_manifest_is_as_before() -> None:
    assert InstanceManifest.from_toml_file(ATT1 / "instancia.toml").agent_segments == ()


def test_the_macro_is_optional_and_positive(instance: Path) -> None:
    manifest = _with_segments(instance, DECLARATION + "macro_etiqueta = 7\n")
    assert InstanceManifest.from_toml_file(manifest).agent_segments[0].label_macro_id == 7


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (DECLARATION.replace('etiqueta = "campana-de-prueba"', 'etiqueta = "cliente"'), "no puede abrir un segmento"),
        (DECLARATION.replace('etiqueta = "campana-de-prueba"', 'etiqueta = "automation_paused"'),
         "no puede abrir un segmento"),
        (DECLARATION.replace('etiqueta = "campana-de-prueba"', 'etiqueta = "Con Mayus"'), "minusculas"),
        (DECLARATION.replace("prioridad = 10", "prioridad = -1"), "prioridad"),
        (DECLARATION.replace("prioridad = 10", 'prioridad = "10"'), "prioridad"),
        (DECLARATION.replace('conocimiento = "conocimiento/segmentos/campana-de-prueba-v1.toml"',
                             'conocimiento = "/etc/x.toml"'), "ruta relativa"),
        (DECLARATION.replace('conocimiento = "conocimiento/segmentos/campana-de-prueba-v1.toml"',
                             'conocimiento = "conocimiento/knowledge-v1.toml"'), "conocimiento base"),
        (DECLARATION + DECLARATION, "misma clave"),
        (DECLARATION + DECLARATION.replace('clave = "campana-de-prueba"', 'clave = "otra"'), "misma etiqueta"),
        (DECLARATION.replace("prioridad = 10", "prioridad = 10\notra = 1"), "claves no soportadas otra"),
        (DECLARATION + "macro_etiqueta = 0\n", "entero positivo"),
    ],
)
def test_the_declaration_rules(instance: Path, block: str, message: str) -> None:
    manifest = _with_segments(instance, block)
    with pytest.raises(ManifestError, match=message):
        InstanceManifest.from_toml_file(manifest)


# ------------------------------------------------------------------ setter validate


def test_validate_reports_each_segment_and_its_state(instance: Path) -> None:
    report = validate_instance(instance, now=INSIDE)

    assert report["valida"] is True, report["errores"]
    [segment] = report["segmentos"]
    assert segment["clave"] == "campana-de-prueba" and segment["estado"] == "aprobado"
    assert segment["vigencia"]["ahora"] == "vigente"
    assert segment["bytes"] == len(EXPECTED_RENDER.encode("utf-8"))
    assert isinstance(segment["chequeo_sha"], str) and len(segment["chequeo_sha"]) == 64


def test_validate_flags_an_approved_segment_whose_sha_is_not_its_own(instance: Path) -> None:
    """Lo que no dejaria arrancar al bridge sale antes, en ``setter validate`` (E-337)."""

    _segment_file(instance).write_text(SEGMENT.read_text(encoding="utf-8"), encoding="utf-8")
    report = validate_instance(instance, now=INSIDE)
    assert report["valida"] is False
    assert any(
        error.startswith("segmento campana-de-prueba:") and "chequeo_sha" in error
        for error in report["errores"]
    )


# Dos segmentos con vigencias que se pisan (E-340). El sha de cada uno cubre su contenido,
# el base y el contenido de los otros que se pisan con el (segment_check.calcular_chequeo_sha):
# se cambia uno y el otro tambien vuelve al chequeo. El fixture va de noviembre al 01/12.
SECOND_KEY = "campana-dos"
SECOND_DECLARATION = DECLARATION.replace("campana-de-prueba", SECOND_KEY).replace(
    "prioridad = 10", "prioridad = 20"
)


def _second_file(instance: Path) -> Path:
    return instance / "conocimiento" / "segmentos" / f"{SECOND_KEY}-v1.toml"


def _second_text(*, desde: str = "2026-11-20T00:00:00-06:00", hasta: str = "2026-12-01T00:00:00-06:00") -> str:
    """El fixture con otra clave, otro cupon y la vigencia que se pida."""

    text = SEGMENT.read_text(encoding="utf-8")
    for old, new in (
        ('clave = "campana-de-prueba"\n', f'clave = "{SECOND_KEY}"\n'),
        ('cupon = "PRUEBA11"\n', 'cupon = "PRUEBA22"\n'),
        ("desde = 2026-11-20T00:00:00-06:00\n", f"desde = {desde}\n"),
        ("hasta = 2026-12-01T00:00:00-06:00\n", f"hasta = {hasta}\n"),
    ):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def _declare_second(instance: Path, second: str) -> None:
    _second_file(instance).write_text(second, encoding="utf-8")
    _with_segments(instance, DECLARATION + SECOND_DECLARATION)


def _seal_both(instance: Path) -> None:
    """Lo que deja ``setter segmento chequear`` corrido sobre cada uno con el otro declarado."""

    _seal_first(instance)
    _seal_second(instance)


def _seal_first(instance: Path) -> None:
    path = _segment_file(instance)
    other = _second_file(instance).read_text(encoding="utf-8")
    path.write_text(sellar_chequeo(path.read_text(encoding="utf-8"), _base(instance), (other,)), encoding="utf-8")


def _seal_second(instance: Path) -> None:
    path = _second_file(instance)
    other = _segment_file(instance).read_text(encoding="utf-8")
    path.write_text(sellar_chequeo(path.read_text(encoding="utf-8"), _base(instance), (other,)), encoding="utf-8")


def _change_a_bonus(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    old = '"Un bono ficticio de prueba."'
    assert text.count(old) == 1
    path.write_text(text.replace(old, '"Un bono ficticio de prueba, cambiado."'), encoding="utf-8")


def _sha_out_of_date(instance: Path) -> set[str]:
    """Las claves de los segmentos que validate marca por el chequeo_sha, y nada mas."""

    report = validate_instance(instance, now=INSIDE)
    flagged = {
        key
        for key in ("campana-de-prueba", SECOND_KEY)
        if any(
            error.startswith(f"segmento {key}:") and "no es el del contenido actual" in error
            for error in report["errores"]
        )
    }
    assert report["valida"] is (not flagged), report["errores"]
    assert len(report["errores"]) == len(flagged), report["errores"]
    return flagged


def test_validate_passes_two_overlapping_segments_sealed_with_each_other(instance: Path) -> None:
    _declare_second(instance, _second_text())
    _seal_both(instance)

    report = validate_instance(instance, now=INSIDE)

    assert report["valida"] is True, report["errores"]
    assert [(entry["clave"], entry["vigencia"]["ahora"]) for entry in report["segmentos"]] == [
        ("campana-de-prueba", "vigente"),
        (SECOND_KEY, "vigente"),
    ]
    base = _base(instance)
    for path in (_segment_file(instance), _second_file(instance)):
        # La huella de cada uno cuenta al otro: no es la que daria el segmento solo.
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        assert raw["cabecera"]["chequeo_sha"] != segment_check.calcular_chequeo_sha(
            raw, base_sha256=base.rendered_sha256
        )
    # El cargador del bridge dice lo mismo que validate.
    assert len(load_segment_catalog(_manifest(instance), instance, base).segments) == 2


def test_validate_changing_one_of_two_overlapping_segments_flags_both(instance: Path) -> None:
    """Volver a chequear solo el que cambio no alcanza: el otro se midio contra su version vieja."""

    _declare_second(instance, _second_text())
    _seal_both(instance)
    _change_a_bonus(_second_file(instance))
    assert _sha_out_of_date(instance) == {"campana-de-prueba", SECOND_KEY}
    with pytest.raises(SegmentError, match="no es el del contenido actual"):
        load_segment_catalog(_manifest(instance), instance, _base(instance))

    _seal_second(instance)
    assert _sha_out_of_date(instance) == {"campana-de-prueba"}

    _seal_first(instance)
    assert _sha_out_of_date(instance) == set()


def test_validate_a_new_overlapping_segment_sends_the_first_back_to_the_check(instance: Path) -> None:
    """El primero sigue sellado solo, como antes de que existiera el segundo."""

    _declare_second(instance, _second_text())
    _seal_second(instance)
    assert _sha_out_of_date(instance) == {"campana-de-prueba"}


def test_validate_segments_that_do_not_overlap_do_not_depend_on_each_other(instance: Path) -> None:
    """La vigencia termina en el borde exacto donde empieza la otra: no se pisan."""

    alone = _segment_file(instance).read_text(encoding="utf-8")
    _declare_second(instance, _second_text(desde="2026-12-01T00:00:00-06:00", hasta="2026-12-05T00:00:00-06:00"))
    _seal_both(instance)
    assert _segment_file(instance).read_text(encoding="utf-8") == alone
    assert _sha_out_of_date(instance) == set()

    _change_a_bonus(_second_file(instance))
    assert _sha_out_of_date(instance) == {SECOND_KEY}


def test_validate_a_window_that_moves_onto_another_segment_flags_it(instance: Path) -> None:
    """La cuenta de quien se pisa se hace con las vigencias de hoy, no con las del chequeo."""

    _declare_second(instance, _second_text(desde="2026-12-01T00:00:00-06:00", hasta="2026-12-05T00:00:00-06:00"))
    _seal_both(instance)
    path = _second_file(instance)
    old = "desde = 2026-12-01T00:00:00-06:00\n"
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, "desde = 2026-11-30T23:59:00-06:00\n"), encoding="utf-8")
    _seal_second(instance)  # el que se movio se chequea de nuevo; el primero no se toco

    assert _sha_out_of_date(instance) == {"campana-de-prueba"}


def test_validate_approving_or_sealing_the_other_does_not_move_the_sha(instance: Path) -> None:
    """Sin esto, dos segmentos que se pisan no se podrian sellar nunca: el sha de uno no ve el del otro."""

    _declare_second(instance, _second_text())
    first = SEGMENT.read_text(encoding="utf-8")
    assert sellar_chequeo(first, _base(instance), (_second_text(),)) == sellar_chequeo(
        first, _base(instance), (sellar_chequeo(_second_text(), _base(instance)),)
    )
    _seal_both(instance)

    path = _second_file(instance)
    text = path.read_text(encoding="utf-8")
    old = 'aprobado_por = "test"\n'
    assert text.count(old) == 1
    path.write_text(text.replace(old, 'aprobado_por = "otra persona"\n'), encoding="utf-8")
    assert _sha_out_of_date(instance) == set()


def test_validate_without_segments_has_no_segment_key() -> None:
    assert "segmentos" not in validate_instance(ATT1)


def test_validate_reads_drafts_with_a_warning_and_flags_broken_ones(instance: Path) -> None:
    path = instance / "conocimiento" / "segmentos" / SEGMENT.name
    path.write_text(path.read_text(encoding="utf-8").replace('estado = "aprobado"', 'estado = "borrador"'),
                    encoding="utf-8")
    report = validate_instance(instance, now=datetime(2027, 1, 1, tzinfo=UTC))
    assert report["valida"] is True
    assert any("borrador" in warning and "campana-de-prueba" in warning for warning in report["avisos"])
    assert any("ya vencio" in warning for warning in report["avisos"])

    path.write_text("[cabecera]\n", encoding="utf-8")
    report = validate_instance(instance, now=INSIDE)
    assert report["valida"] is False
    assert any(error.startswith("segmento campana-de-prueba:") for error in report["errores"])


# ------------------------------------------------------------------ el pedido a Hermes


def _capturing(tmp_path: Path, system_prompt: str | None, recorder: Any = None) -> tuple[HermesShadowProcessor, list[bytes]]:
    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(503, json={"detail": "unavailable"})

    return (
        HermesShadowProcessor(
            base_url="https://hermes.example.test/v1",
            api_key="test-hermes-key",
            model_name="att1-agente-comercial",
            shadow_dir=tmp_path,
            transport=httpx.MockTransport(handler),
            system_prompt=system_prompt,
            provenance_recorder=recorder,
        ),
        requests,
    )


def test_the_segments_travel_as_systems_after_the_base_in_their_order(tmp_path: Path) -> None:
    a, b = _loaded("a", priority=10), _loaded("b", priority=20)
    processor, requests = _capturing(tmp_path, "BASE")

    asyncio.run(processor.run(delivery_id="d1", context={"messages": []}, extra_system=(a, b)))

    messages = json.loads(requests[0])["messages"]
    assert [message["role"] for message in messages] == ["system", "system", "system", "user"]
    assert [message["content"] for message in messages[:3]] == ["BASE", a.rendered, b.rendered]


def test_without_segments_the_request_is_byte_for_byte_the_same(tmp_path: Path) -> None:
    before, before_requests = _capturing(tmp_path / "a", "BASE")
    after, after_requests = _capturing(tmp_path / "b", "BASE")
    context = {"conversation_ref": "200", "messages": [{"actor": "prospect", "text": "hola"}]}

    asyncio.run(before.run(delivery_id="d1", context=copy.deepcopy(context)))
    asyncio.run(after.run(delivery_id="d1", context=copy.deepcopy(context), extra_system=()))

    assert before_requests == after_requests
    assert [m["role"] for m in json.loads(before_requests[0])["messages"]] == ["system", "user"]


def test_the_turn_provenance_names_each_segment_with_its_sha(tmp_path: Path) -> None:
    turns: list[AgentTurn] = []

    class _Recorder:
        async def record_agent_turn(self, *, turn: AgentTurn) -> None:
            turns.append(turn)

    a = _loaded("a")
    processor, _ = _capturing(tmp_path / "con", "BASE", _Recorder())
    asyncio.run(processor.run(delivery_id="d1", context={"messages": []}, extra_system=(a,)))
    plain, _ = _capturing(tmp_path / "sin", "BASE", _Recorder())
    asyncio.run(plain.run(delivery_id="d2", context={"messages": []}))

    with_segment, without = turns
    assert with_segment.context_added["segments"] == [{"key": "a", "version": 1, "sha256": a.sha256}]
    assert "segments" not in without.context_added


def test_the_provenance_says_how_many_agent_replies_came_before(tmp_path: Path) -> None:
    turns: list[AgentTurn] = []

    class _Recorder:
        async def record_agent_turn(self, *, turn: AgentTurn) -> None:
            turns.append(turn)

    a = _loaded("a")
    processor, _ = _capturing(tmp_path, "BASE", _Recorder())
    asyncio.run(processor.run(delivery_id="d1", context={"messages": []},
                              extra_system=(TurnSegment(segment=a, agent_replies_before=3),)))

    [turn] = turns
    assert turn.context_added["segments"] == [
        {"key": "a", "version": 1, "sha256": a.sha256, "agent_replies_before": 3}
    ]
    assert TurnSegment(segment=a, agent_replies_before=3).activated_late is True
    assert TurnSegment(segment=a, agent_replies_before=0).activated_late is False
