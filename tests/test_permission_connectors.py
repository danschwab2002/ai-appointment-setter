"""Los conectores del aviso de permiso de WhatsApp (E-628, M2 de E-624).

Contrato: docs/contracts/permission-registry-v1.md §8. Lo que fijan:

* ``[[permisos.avisos]]`` del manifiesto: lo que carga y lo que no (la oferta
  exclusiva y ``requiere_src`` solo de la landing, la vigencia nunca antes de la
  medicion, solo ``recuperacion``, ofertas declaradas, ventana acotada);
* la forma que el bridge le manda a la base es exactamente la de las RPC de la
  migracion ``20261116000200_permission_registry_v1.sql`` (las claves se leen
  del SQL, no se copian);
* el conector, con los eventos capturados de Hotmart: el carrito con telefono
  (panel de Hotmart, 2026-09-21) da un permiso; el carrito sin telefono de ATT1
  (2026-11-10) no escribe nada; el pago rechazado de Maribel (2026-10-28) con y
  sin ``requiere_src``; un evento anterior al aviso no escribe; un ``rejected``
  o una falla de la base nunca levantan;
* el resolver escribe el permiso **antes** de la admision nativa, con el id del
  evento como evidencia, y sin el flag no hay ninguna llamada nueva;
* el flag esta apagado por defecto, es portable, y ``/ready`` dice cuantos
  avisos estan vigentes solo con el flag.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import tomllib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import app as app_module
from bridge import permission_connectors as pc
from bridge.event_log import LOGGED_INFO_EVENTS
from bridge.hotmart import (
    EVENT_CART_ABANDONMENT,
    EVENT_PURCHASE_CANCELED,
    parse_hotmart_payload,
    parse_hotmart_payment_failure_buyer_payload,
)
from bridge.instance_manifest import InstanceManifest, ManifestError
from bridge.supabase import SupabaseClient, SupabaseError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
MIGRATION = ROOT / "supabase" / "migrations" / "20261116000200_permission_registry_v1.sql"
MARIBEL_TOML = FIXTURES / "instances" / "maribel" / "instancia.toml"

# Los eventos capturados (con su origen en el _capture de cada archivo).
CART_WITH_PHONE = json.loads(
    (FIXTURES / "hotmart_cart_abandonment_rejected_v1.json").read_text(encoding="utf-8")
)["payload"]
CART_WITHOUT_PHONE = json.loads(
    (FIXTURES / "hotmart_cart_abandonment_without_phone_att1_20261110.json").read_text(encoding="utf-8")
)["payload"]
MARIBEL_REFUSALS = json.loads(
    (FIXTURES / "hotmart_purchase_canceled_maribel_20261028.json").read_text(encoding="utf-8")
)["eventos"]

EVENT_ID = "00000000-0000-4000-8000-0000000e6281"
CONTACT_ID = "00000000-0000-4000-8000-0000000e6282"
# El texto del checkout de E-624 §5 (version texto, sin casilla).
CHECKOUT_TEXT = (
    "Al continuar, aceptas que El Protocolo te contacte por WhatsApp al número que ingresaste, "
    "con información sobre tu compra y sobre este programa, incluidos recordatorios si no la "
    "terminas. Puedes pedir que no te escribamos más en cualquier momento. Consulta la Política "
    "de Privacidad y los Términos y Condiciones."
)
LANDING_TEXT = (
    "Al hacer clic en «Comprar», aceptas que El Protocolo te contacte por WhatsApp al número que "
    "registres en el pago, con información sobre tu compra y sobre este programa, incluidos "
    "recordatorios si no la terminas. Puedes pedir que no te escribamos más en cualquier momento. "
    "Consulta la Política de Privacidad y los Términos y Condiciones."
)
MEASURED = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _payload() -> dict[str, Any]:
    return tomllib.loads(MARIBEL_TOML.read_text(encoding="utf-8"))


OFFER = _payload()["hotmart"]["ofertas"][0]["codigo"]


def _checkout(**overrides: Any) -> dict[str, Any]:
    notice = {
        "ref": "maribel-checkout-v1",
        "conector": "hotmart_checkout",
        "ofertas": [OFFER],
        "url": "https://pay.hotmart.com/F107441770M?off=u8ikyx78",
        "copy_version": "maribel-whatsapp-checkout-v1",
        "texto": CHECKOUT_TEXT,
        "casilla": "ninguna",
        "medido_en": MEASURED,
        "medido_por": "E-628 prueba",
        "vigente_desde": MEASURED,
        "ventana_horas": 24,
    }
    notice.update(overrides)
    return notice


def _landing(**overrides: Any) -> dict[str, Any]:
    notice = {
        "ref": "maribel-landing-v1",
        "conector": "landing",
        "ofertas": [OFFER],
        "url": "https://www.elprotocolo.net/black-pro",
        "copy_version": "maribel-whatsapp-landing-v1",
        "texto": LANDING_TEXT,
        "casilla": "ninguna",
        "medido_en": MEASURED,
        "medido_por": "E-628 prueba",
        "vigente_desde": MEASURED,
        "ventana_horas": 24,
        "oferta_exclusiva": True,
    }
    notice.update(overrides)
    return notice


def _manifest(*notices: dict[str, Any]) -> InstanceManifest:
    payload = _payload()
    if notices:
        payload["permisos"] = {"avisos": list(notices)}
    return InstanceManifest.from_mapping(payload)


# ---------------------------------------------------------------- el manifiesto


def test_without_permisos_there_is_no_notice() -> None:
    assert _manifest().permission_notices == ()


def test_a_checkout_notice_loads_with_the_funnel_of_the_principal() -> None:
    manifest = _manifest(_checkout())
    (notice,) = manifest.permission_notices
    assert notice.product_ref == manifest.funnel_ref == "maribel"
    assert notice.offer_refs == (OFFER,)
    assert notice.uses == ("recuperacion",)
    assert notice.window == timedelta(hours=24)
    assert notice.requires_src is None
    assert notice.text_sha256 == pc.notice_text_sha256(CHECKOUT_TEXT)
    # TOML: una fecha con zona escrita a mano carga igual que el datetime.
    text = MARIBEL_TOML.read_text(encoding="utf-8") + (
        '\n[[permisos.avisos]]\nref = "maribel-checkout-v1"\nconector = "hotmart_checkout"\n'
        f'ofertas = ["{OFFER}"]\nurl = "https://pay.hotmart.com/F107441770M"\n'
        'copy_version = "maribel-whatsapp-checkout-v1"\n'
        f'texto = "{CHECKOUT_TEXT}"\ncasilla = "ninguna"\n'
        'medido_en = 2026-01-01T12:00:00Z\nmedido_por = "E-628 prueba"\n'
        'vigente_desde = 2026-01-01T12:00:00Z\nventana_horas = 24\n'
    )
    loaded = InstanceManifest.from_mapping(tomllib.loads(text)).permission_notices[0]
    assert (loaded.valid_from, loaded.text_sha256) == (notice.valid_from, notice.text_sha256)


def test_a_checkout_notice_without_offers_covers_the_whole_product() -> None:
    notice = _manifest(_checkout(ofertas=[])).permission_notices[0]
    assert notice.offer_refs == ()
    assert notice.covers("maribel", "cualquiera")


def test_a_landing_notice_loads_with_its_src() -> None:
    notice = _manifest(_landing(requiere_src="blackpro")).permission_notices[0]
    assert (notice.connector, notice.requires_src, notice.exclusive_offer) == ("landing", "blackpro", True)


@pytest.mark.parametrize(
    ("notice", "message"),
    [
        pytest.param(_landing(oferta_exclusiva=False), "oferta_exclusiva = true", id="landing sin exclusiva"),
        pytest.param({k: v for k, v in _landing().items() if k != "oferta_exclusiva"},
                     "oferta_exclusiva = true", id="landing sin la afirmacion"),
        pytest.param(_landing(ofertas=[]), "nombra sus ofertas exclusivas", id="landing sin ofertas"),
        pytest.param(_checkout(requiere_src="x"), "solo de un aviso de landing", id="checkout con src"),
        pytest.param(_checkout(oferta_exclusiva=True), "solo de un aviso de landing", id="checkout exclusiva"),
        pytest.param(_checkout(vigente_desde=MEASURED - timedelta(seconds=1)), "anterior a medido_en",
                     id="vigente antes de medir"),
        pytest.param(_checkout(vigente_hasta=MEASURED - timedelta(seconds=1)), "anterior a vigente_desde",
                     id="cierra antes de empezar"),
        pytest.param(_checkout(ofertas=["noexiste1"]), "no es una oferta declarada", id="oferta ajena"),
        pytest.param(_checkout(usos=["recuperacion", "difusion"]), "solo recuperacion", id="difusion"),
        pytest.param(_checkout(ventana_horas=0), "ventana_horas", id="ventana cero"),
        pytest.param(_checkout(ventana_horas=721), "ventana_horas", id="ventana de mas"),
        pytest.param(_checkout(conector="ghl_form"), "conector debe ser", id="conector ajeno"),
        pytest.param(_checkout(casilla="premarcada"), "casilla", id="casilla premarcada"),
        pytest.param(_checkout(url="http://pay.hotmart.com/x"), "https://", id="url sin https"),
        pytest.param(_checkout(medido_en=datetime(2026, 1, 1, 12)), "con zona", id="fecha sin zona"),
        pytest.param(_checkout(producto="otro"), "no es un producto", id="producto ajeno"),
        pytest.param(_checkout(extra=1), "claves no soportadas", id="clave de mas"),
        pytest.param({k: v for k, v in _checkout().items() if k != "ventana_horas"}, "faltan ventana_horas",
                     id="sin ventana"),
    ],
)
def test_what_a_notice_cannot_say(notice: dict[str, Any], message: str) -> None:
    with pytest.raises(ManifestError, match=re.escape(message)):
        _manifest(notice)


def test_two_notices_with_the_same_ref_do_not_load() -> None:
    with pytest.raises(ManifestError, match="mismo ref"):
        _manifest(_checkout(), _checkout(copy_version="otra-v1"))


def test_an_offer_in_two_landings_is_exclusive_of_none() -> None:
    with pytest.raises(ManifestError, match="dos landings"):
        _manifest(_landing(), _landing(ref="otra-landing-v1", url="https://www.elprotocolo.net/otra"))
    # La misma pagina con un texto nuevo (otro ref) si: es el cambio de version.
    assert len(_manifest(_landing(), _landing(ref="maribel-landing-v2", copy_version="v2")).permission_notices) == 2


# --------------------------------------------------- la forma contra la base


def _rpc_keys(function: str) -> set[str]:
    sql = MIGRATION.read_text(encoding="utf-8")
    body = sql[sql.index(f"create function public.{function}"):]
    match = re.search(r"key not in \(([^)]*)\)", body)
    assert match is not None
    return set(re.findall(r"'([a-z0-9_]+)'", match.group(1)))


def test_the_notice_sent_to_the_base_has_only_keys_the_base_takes() -> None:
    notice = _manifest(_checkout(vigente_hasta=MEASURED + timedelta(days=3))).permission_notices[0]
    body = notice.to_rpc("lancemos")
    assert set(body) <= _rpc_keys("register_permission_notice_v1")
    assert body["notice_text_sha256"] == pc.notice_text_sha256(body["notice_text"])
    assert body["valid_from"] == "2026-01-01T12:00:00+00:00"
    assert body["product_ref"] == "maribel"


def test_the_grant_sent_to_the_base_has_exactly_the_keys_of_the_base() -> None:
    store = _FakeStore()
    connectors = _connectors(store, _checkout())
    asyncio.run(_record_cart(connectors, _maribel_cart()))
    assert set(store.grants[0]) == _rpc_keys("record_permission_grant_v1")


# ---------------------------------------------------------------- el conector


class _FakeStore:
    def __init__(
        self,
        *,
        outcome: str = "inserted",
        reason: str | None = None,
        fail_record: bool = False,
        fail_register: bool = False,
    ) -> None:
        self.outcome = outcome
        self.reason = reason
        self.fail_record = fail_record
        self.fail_register = fail_register
        self.notices: list[dict[str, Any]] = []
        self.closed: list[tuple[str, str]] = []
        self.grants: list[dict[str, Any]] = []

    async def register_permission_notice(self, notice: dict[str, Any]) -> str:
        if self.fail_register:
            raise SupabaseError("permission_notice_register_failed: HTTP 404")
        self.notices.append(notice)
        return "inserted"

    async def close_permission_notice(self, *, tenant_ref: str, notice_ref: str, valid_until: str) -> str:
        self.closed.append((notice_ref, valid_until))
        return "closed"

    async def record_permission_grant(self, grant: dict[str, Any]) -> tuple[str | None, str, str | None]:
        if self.fail_record:
            raise SupabaseError("permission_grant_record_failed: HTTP 500")
        self.grants.append(grant)
        return ("00000000-0000-4000-8000-0000000e6283", self.outcome, self.reason)


def _connectors(store: _FakeStore, *notices: dict[str, Any]) -> pc.PermissionNoticeConnectors:
    manifest = _manifest(*notices)
    return pc.PermissionNoticeConnectors(
        store, tenant_ref=manifest.tenant_ref, notices=manifest.permission_notices
    )


def _maribel_config():
    return _manifest().to_commercial_ally_config()


def _maribel_cart(source: dict[str, Any] = CART_WITH_PHONE) -> dict[str, Any]:
    """El carrito capturado, con el producto y la oferta de Maribel."""
    config = _maribel_config()
    payload = copy.deepcopy(source)
    payload["data"]["product"] = {"id": config.hotmart_product_id, "name": config.product_name}
    payload["data"]["offer"] = {"code": OFFER}
    return payload


def _maribel_refusal(index: int = 0) -> dict[str, Any]:
    payload = copy.deepcopy(MARIBEL_REFUSALS[index]["payload"])
    payload["data"]["product"]["name"] = _maribel_config().product_name
    return payload


async def _record_cart(
    connectors: pc.PermissionNoticeConnectors, payload: dict[str, Any]
) -> pc.GrantResult:
    buyer = parse_hotmart_payload(payload, config=_maribel_config())
    assert buyer is not None
    return await connectors.record_for_event(
        webhook_event_id=EVENT_ID, event_type=EVENT_CART_ABANDONMENT, payload=payload,
        product_ref="maribel", offer_code=buyer.offer_code, phone=buyer.buyer_phone,
        email=buyer.buyer_email, contact_id=CONTACT_ID, creation_date_ms=buyer.creation_date_ms,
    )


async def _record_refusal(
    connectors: pc.PermissionNoticeConnectors, payload: dict[str, Any]
) -> pc.GrantResult:
    buyer = parse_hotmart_payment_failure_buyer_payload(payload, config=_maribel_config())
    assert buyer is not None
    return await connectors.record_for_event(
        webhook_event_id=EVENT_ID, event_type=EVENT_PURCHASE_CANCELED, payload=payload,
        product_ref="maribel", offer_code=buyer.offer_code, phone=buyer.buyer_phone,
        email=buyer.buyer_email, contact_id=CONTACT_ID, creation_date_ms=buyer.creation_date_ms,
    )


def test_a_cart_with_phone_and_a_checkout_notice_records_the_permission() -> None:
    store = _FakeStore()
    connectors = _connectors(store, _checkout())
    result = asyncio.run(_record_cart(connectors, _maribel_cart()))

    assert (result.outcome, result.notice_ref) == ("inserted", "maribel-checkout-v1")
    # Los avisos se cargan antes del primer permiso, aunque no se haya llamado al arrancar.
    assert [notice["notice_ref"] for notice in store.notices] == ["maribel-checkout-v1"]
    (grant,) = store.grants
    created = datetime.fromtimestamp(CART_WITH_PHONE["creation_date"] / 1000, tz=timezone.utc)
    assert grant == {
        "tenant_ref": "lancemos",
        "phone": "525555555555",
        "contact_id": CONTACT_ID,
        "normalized_email": "compradora@example.invalid",
        "product_ref": "maribel",
        "offer_ref": OFFER,
        "uses": ["recuperacion"],
        "connector": "hotmart_checkout",
        "connector_version": "v1",
        "evidence_kind": "aviso_en_el_paso",
        "evidence_ref": EVENT_ID,
        "notice_ref": "maribel-checkout-v1",
        "copy_version": "maribel-whatsapp-checkout-v1",
        "granted_at": created.isoformat(),
        "expires_at": (created + timedelta(hours=24)).isoformat(),
    }


def test_a_cart_without_phone_records_nothing() -> None:
    store = _FakeStore()
    connectors = _connectors(store, _checkout())
    result = asyncio.run(_record_cart(connectors, _maribel_cart(CART_WITHOUT_PHONE)))
    assert (result.outcome, result.reason) == (None, pc.SKIP_WITHOUT_PHONE)
    assert store.grants == [] and store.notices == []


def test_a_cart_before_the_notice_records_nothing() -> None:
    created = datetime.fromtimestamp(CART_WITH_PHONE["creation_date"] / 1000, tz=timezone.utc)
    store = _FakeStore()
    connectors = _connectors(
        store, _checkout(medido_en=created + timedelta(seconds=1), vigente_desde=created + timedelta(seconds=1))
    )
    result = asyncio.run(_record_cart(connectors, _maribel_cart()))
    assert (result.outcome, result.reason) == (None, pc.SKIP_WITHOUT_NOTICE)
    assert store.grants == []


def test_a_cart_after_the_notice_closed_records_nothing() -> None:
    created = datetime.fromtimestamp(CART_WITH_PHONE["creation_date"] / 1000, tz=timezone.utc)
    store = _FakeStore()
    connectors = _connectors(store, _checkout(vigente_hasta=created))
    assert asyncio.run(_record_cart(connectors, _maribel_cart())).reason == pc.SKIP_WITHOUT_NOTICE


def test_a_cart_of_an_offer_the_notice_does_not_cover_records_nothing() -> None:
    store = _FakeStore()
    connectors = _connectors(store, _checkout())
    payload = _maribel_cart()
    payload["data"]["offer"] = {"code": "otraoferta"}
    result = asyncio.run(connectors.record_for_event(
        webhook_event_id=EVENT_ID, event_type=EVENT_CART_ABANDONMENT, payload=payload,
        product_ref="maribel", offer_code="otraoferta", phone="525555555555", email=None,
        contact_id=None, creation_date_ms=CART_WITH_PHONE["creation_date"],
    ))
    assert result.reason == pc.SKIP_WITHOUT_NOTICE and store.grants == []


def test_the_payment_slip_and_the_purchase_record_nothing() -> None:
    store = _FakeStore()
    connectors = _connectors(store, _checkout())
    for event_type in ("PURCHASE_BILLET_PRINTED", "PURCHASE_APPROVED"):
        result = asyncio.run(connectors.record_for_event(
            webhook_event_id=EVENT_ID, event_type=event_type, payload={}, product_ref="maribel",
            offer_code=OFFER, phone="525555555555", email=None, contact_id=None,
            creation_date_ms=CART_WITH_PHONE["creation_date"],
        ))
        assert result.reason == pc.SKIP_EVENT_TYPE
    assert store.grants == []


def test_the_maribel_refusal_with_a_checkout_notice_records_the_permission() -> None:
    store = _FakeStore()
    connectors = _connectors(store, _checkout())
    result = asyncio.run(_record_refusal(connectors, _maribel_refusal()))
    assert result.outcome == "inserted"
    assert store.grants[0]["phone"] == "520000000001"  # checkout_phone del rechazo capturado


@pytest.mark.parametrize(
    ("requires_src", "granted"),
    [(None, True), ("fixture", True), ("blackpro", False)],
    ids=["sin requiere_src", "con el src del evento", "con otro src"],
)
def test_the_maribel_refusal_with_a_landing_notice_and_its_src(requires_src: str | None, granted: bool) -> None:
    store = _FakeStore()
    landing = _landing() if requires_src is None else _landing(requiere_src=requires_src)
    connectors = _connectors(store, landing)
    result = asyncio.run(_record_refusal(connectors, _maribel_refusal()))
    assert (result.outcome == "inserted") is granted
    if not granted:
        assert result.reason == pc.SKIP_SRC and store.grants == []


def test_a_refusal_without_origin_does_not_meet_requiere_src() -> None:
    payload = _maribel_refusal()
    del payload["data"]["purchase"]["origin"]
    store = _FakeStore()
    result = asyncio.run(_record_refusal(_connectors(store, _landing(requiere_src="fixture")), payload))
    assert result.reason == pc.SKIP_SRC


def test_a_cart_with_a_landing_notice_with_src_records_the_permission() -> None:
    # El carrito no trae src: no se le puede exigir (E-624 §4.3).
    store = _FakeStore()
    result = asyncio.run(_record_cart(_connectors(store, _landing(requiere_src="blackpro")), _maribel_cart()))
    assert result.outcome == "inserted"
    assert store.grants[0]["connector"] == "landing"


def test_the_checkout_notice_wins_over_the_landing() -> None:
    store = _FakeStore()
    result = asyncio.run(_record_cart(_connectors(store, _landing(), _checkout()), _maribel_cart()))
    assert result.notice_ref == "maribel-checkout-v1"
    assert len(store.grants) == 1


def test_among_two_notices_of_the_same_page_the_newer_wins() -> None:
    store = _FakeStore()
    newer = _checkout(ref="maribel-checkout-v2", copy_version="maribel-whatsapp-checkout-v2",
                      medido_en=MEASURED + timedelta(days=1), vigente_desde=MEASURED + timedelta(days=1))
    result = asyncio.run(_record_cart(_connectors(store, _checkout(), newer), _maribel_cart()))
    assert result.notice_ref == "maribel-checkout-v2"


@pytest.mark.parametrize("outcome", ["duplicate", "rejected"])
def test_what_the_base_answers_is_returned_and_never_raises(outcome: str, caplog: pytest.LogCaptureFixture) -> None:
    store = _FakeStore(outcome=outcome, reason="aviso_no_vigente" if outcome == "rejected" else None)
    with caplog.at_level(logging.INFO, logger="bridge.permission_connectors"):
        result = asyncio.run(_record_cart(_connectors(store, _checkout()), _maribel_cart()))
    assert result.outcome == outcome
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("permission_grant_")][-1]
    expected = "permission_grant_rejected" if outcome == "rejected" else "permission_grant_recorded"
    assert line.startswith(expected)
    # Nada personal en el log.
    for value in ("525555555555", "compradora@example.invalid", "Compradora"):
        assert value not in caplog.text


def test_a_failing_record_never_raises() -> None:
    store = _FakeStore(fail_record=True)
    result = asyncio.run(_record_cart(_connectors(store, _checkout()), _maribel_cart()))
    assert (result.outcome, result.reason) == ("failed", "SupabaseError")


def test_without_the_notices_in_the_base_no_permission_is_written() -> None:
    store = _FakeStore(fail_register=True)
    connectors = _connectors(store, _checkout())
    assert asyncio.run(connectors.register_notices()) is False
    assert connectors.readiness(datetime.now(timezone.utc)) == "sin_cargar"
    result = asyncio.run(_record_cart(connectors, _maribel_cart()))
    assert result.reason == "avisos_sin_cargar"
    assert store.grants == []
    # Cuando la base vuelve, el proximo evento carga los avisos y escribe.
    store.fail_register = False
    assert asyncio.run(_record_cart(connectors, _maribel_cart())).outcome == "inserted"
    assert connectors.registered


def test_the_load_closes_what_the_manifest_closes_and_ready_counts_the_valid_ones() -> None:
    store = _FakeStore()
    closed_at = MEASURED + timedelta(days=1)
    connectors = _connectors(
        store, _checkout(vigente_hasta=closed_at),
        _landing(medido_en=MEASURED + timedelta(days=2), vigente_desde=MEASURED + timedelta(days=2)),
    )
    assert asyncio.run(connectors.register_notices()) is True
    assert [notice["notice_ref"] for notice in store.notices] == ["maribel-checkout-v1", "maribel-landing-v1"]
    assert store.closed == [("maribel-checkout-v1", closed_at.isoformat())]
    assert connectors.readiness(MEASURED + timedelta(hours=1)) == "1-activos"
    assert connectors.readiness(MEASURED + timedelta(days=3)) == "1-activos"
    assert connectors.readiness(MEASURED - timedelta(days=1)) == "0-activos"


def test_the_info_lines_are_reviewed_for_the_container_log() -> None:
    for name in ("permission_notice_registered", "permission_notice_closed",
                 "permission_grant_recorded", "permission_grant_skipped"):
        assert name in LOGGED_INFO_EVENTS


# ----------------------------------------------------------- el cliente de la base


def _client(answer: httpx.Response, seen: list[tuple[str, Any]]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return answer

    return SupabaseClient(base_url="https://supabase.example.test", service_role_key="test-key",
                          transport=httpx.MockTransport(handler))


def test_the_client_sends_the_grant_and_reads_the_answer() -> None:
    seen: list[tuple[str, Any]] = []
    client = _client(httpx.Response(200, json=[{"grant_id": None, "outcome": "rejected",
                                                "reason": "oferta_fuera_del_aviso"}]), seen)
    assert asyncio.run(client.record_permission_grant({"a": 1})) == (None, "rejected", "oferta_fuera_del_aviso")
    assert seen == [("/rest/v1/rpc/record_permission_grant_v1", {"p_grant": {"a": 1}})]


def test_the_client_sends_the_notice_and_raises_on_a_conflict() -> None:
    seen: list[tuple[str, Any]] = []
    ok = _client(httpx.Response(200, json=[{"notice_id": "x", "outcome": "unchanged"}]), seen)
    assert asyncio.run(ok.register_permission_notice({"b": 2})) == "unchanged"
    assert seen == [("/rest/v1/rpc/register_permission_notice_v1", {"p_notice": {"b": 2}})]
    conflict = _client(httpx.Response(400, json={"code": "55000", "message": "permission_notice_conflict"}), [])
    with pytest.raises(SupabaseError):
        asyncio.run(conflict.register_permission_notice({"b": 2}))
    closed = _client(httpx.Response(200, json=[{"notice_id": "x", "outcome": "already_closed"}]), seen)
    assert asyncio.run(closed.close_permission_notice(
        tenant_ref="lancemos", notice_ref="n", valid_until="2026-01-02T00:00:00+00:00")) == "already_closed"


# ------------------------------------------------------------------ el resolver


def _grant_answer(outcome: str = "inserted") -> httpx.Response:
    return httpx.Response(200, json=[{"grant_id": "00000000-0000-4000-8000-0000000e6284",
                                      "outcome": outcome, "reason": None}])


def _resolver_connectors(client: SupabaseClient, config: Any, **overrides: Any) -> pc.PermissionNoticeConnectors:
    notice = pc.PermissionNotice(
        notice_ref="aviso-checkout-v1", connector="hotmart_checkout", product_ref=config.funnel_ref,
        offer_refs=(config.offer_code,), uses=("recuperacion",), surface_url="https://pay.hotmart.com/x",
        copy_version="aviso-checkout-v1", text=CHECKOUT_TEXT, checkbox="ninguna", measured_at=MEASURED,
        measured_by="E-628 prueba", valid_from=MEASURED, valid_until=None, window=timedelta(hours=24),
    )
    return pc.PermissionNoticeConnectors(client, tenant_ref=config.tenant_ref,
                                         notices=(replace(notice, **overrides),))


def test_the_resolver_records_the_permission_before_the_native_cart_admission() -> None:
    import test_flow_engine_native_cart as native_cart
    from test_flow_engine_resolution import POLICY_KEY, _runtime
    from test_resolution_pilot_plan_rejection import BOUNDARY, CONFIG, EVENT_ID as CART_EVENT, _cart

    from bridge.resolution import resolve_event

    class _Base(native_cart._NativeBase):
        def handler(self, request: httpx.Request) -> httpx.Response:
            name = request.url.path.removeprefix("/rest/v1/rpc/")
            if name in {"register_permission_notice_v1", "record_permission_grant_v1"}:
                self.calls.append((name, json.loads(request.content)))
                if name == "register_permission_notice_v1":
                    return httpx.Response(200, json=[{"notice_id": "n", "outcome": "inserted"}])
                return _grant_answer()
            return super().handler(request)

    base = _Base([native_cart._answer(native_cart._row("motor_arranco"))], routes=native_cart._routes("motor"))
    client = base.client()
    payload = _cart(CONFIG.offer_code)
    asyncio.run(resolve_event(
        webhook_event_id=CART_EVENT, payload=payload, supabase=client, policy_key=POLICY_KEY,
        policy_version=3, allowed_jid=None, chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id, pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG,
        event_type=EVENT_CART_ABANDONMENT, flow_engine=_runtime(base, client),
        native_cart_product_ref=CONFIG.funnel_ref,
        permission_connectors=_resolver_connectors(client, CONFIG),
    ))

    names = base.names()
    assert names.index("record_permission_grant_v1") < names.index("admit_flow_engine_event_v1")
    grant = base.body_of("record_permission_grant_v1")["p_grant"]
    assert (grant["evidence_ref"], grant["product_ref"], grant["offer_ref"], grant["contact_id"]) == (
        CART_EVENT, CONFIG.funnel_ref, CONFIG.offer_code, native_cart.CONTACT_ID)
    assert grant["granted_at"] == datetime.fromtimestamp(
        payload["creation_date"] / 1000, tz=timezone.utc).isoformat()


def test_a_rejected_permission_leaves_the_native_cart_admission_as_today() -> None:
    import test_flow_engine_native_cart as native_cart
    from test_flow_engine_resolution import POLICY_KEY, _runtime
    from test_resolution_pilot_plan_rejection import BOUNDARY, CONFIG, EVENT_ID as CART_EVENT, _cart

    from bridge.resolution import resolve_event

    def run(with_connector: bool) -> native_cart._NativeBase:
        class _Base(native_cart._NativeBase):
            def handler(self, request: httpx.Request) -> httpx.Response:
                name = request.url.path.removeprefix("/rest/v1/rpc/")
                if name == "register_permission_notice_v1":
                    return httpx.Response(200, json=[{"notice_id": "n", "outcome": "unchanged"}])
                if name == "record_permission_grant_v1":
                    self.calls.append((name, json.loads(request.content)))
                    return httpx.Response(200, json=[{"grant_id": None, "outcome": "rejected",
                                                      "reason": "aviso_no_vigente"}])
                return super().handler(request)

        base = _Base([native_cart._answer(native_cart._row("motor_arranco"))], routes=native_cart._routes("motor"))
        client = base.client()
        asyncio.run(resolve_event(
            webhook_event_id=CART_EVENT, payload=_cart(CONFIG.offer_code), supabase=client,
            policy_key=POLICY_KEY, policy_version=3, allowed_jid=None,
            chatwoot_account_id=CONFIG.chatwoot_account_id, chatwoot_inbox_id=CONFIG.chatwoot_inbox_id,
            pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG, event_type=EVENT_CART_ABANDONMENT,
            flow_engine=_runtime(base, client), native_cart_product_ref=CONFIG.funnel_ref,
            permission_connectors=_resolver_connectors(client, CONFIG) if with_connector else None,
        ))
        return base

    today, rejected = run(False), run(True)
    assert "record_permission_grant_v1" not in today.names()
    assert [n for n in rejected.names() if n != "record_permission_grant_v1"] == today.names()
    assert rejected.body_of("admit_flow_engine_event_v1") == today.body_of("admit_flow_engine_event_v1")
    assert rejected.status_patches == today.status_patches


def test_the_resolver_records_the_permission_of_the_maribel_refusal_before_its_admission() -> None:
    import test_flow_engine_native_payment_failure as native_failure
    from test_flow_engine_resolution import _runtime
    from test_resolution_pilot_plan_rejection import BOUNDARY, CONFIG, EVENT_ID as FAILURE_EVENT

    from bridge.resolution import resolve_event

    class _Base(native_failure._NativeBase):
        def handler(self, request: httpx.Request) -> httpx.Response:
            name = request.url.path.removeprefix("/rest/v1/rpc/")
            if name in {"register_permission_notice_v1", "record_permission_grant_v1"}:
                self.calls.append((name, json.loads(request.content)))
                if name == "register_permission_notice_v1":
                    return httpx.Response(200, json=[{"notice_id": "n", "outcome": "inserted"}])
                return _grant_answer()
            return super().handler(request)

    base = _Base([native_failure._answer(native_failure._row("motor_arranco"))])
    client = base.client()
    payload = native_failure._refusal()
    asyncio.run(resolve_event(
        webhook_event_id=FAILURE_EVENT, payload=payload, supabase=client, policy_key=native_failure.POLICY_KEY,
        policy_version=1, allowed_jid=None, chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id, pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG,
        event_type=EVENT_PURCHASE_CANCELED, flow_engine=_runtime(base, client),
        native_payment_failure_product_ref=CONFIG.funnel_ref,
        permission_connectors=_resolver_connectors(
            client, CONFIG, connector="landing", requires_src="fixture", exclusive_offer=True),
    ))

    names = base.names()
    assert names.index("record_permission_grant_v1") < names.index("admit_flow_engine_event_v1")
    grant = base.body_of("record_permission_grant_v1")["p_grant"]
    assert (grant["connector"], grant["evidence_ref"], grant["phone"]) == ("landing", FAILURE_EVENT, "520000000001")


# ------------------------------------------------------------- el flag y /ready


def test_the_flag_is_off_by_default_and_portable() -> None:
    assert app_module.Settings.__dataclass_fields__["permission_notice_connector_enabled"].default is False
    assert "permission_notice_connector_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    assert pc.PERMISSION_NOTICE_CONNECTOR_ENV == "PERMISSION_NOTICE_CONNECTOR_ENABLED"


def _ready_app(tmp_path: Path, *, enabled: bool, fail_register: bool = False) -> tuple[object, Any]:
    from test_hotmart_unknown_offers import _Base as UnknownOffersBase
    from test_product_routing import _settings

    class _Base(UnknownOffersBase):
        def __init__(self) -> None:
            super().__init__()
            self.permission_store = _FakeStore(fail_register=fail_register)

        async def register_permission_notice(self, notice: dict[str, Any]) -> str:
            return await self.permission_store.register_permission_notice(notice)

        async def close_permission_notice(self, **kwargs: Any) -> str:
            return await self.permission_store.close_permission_notice(**kwargs)

    base = _Base()
    manifest = _manifest(_checkout())
    manifest = replace(manifest, flows={**manifest.flows, "carrito": True})
    settings = replace(_settings(manifest, tmp_path), permission_notice_connector_enabled=enabled)
    return app_module.create_app(settings, supabase_client=base), base  # type: ignore[arg-type]


def _ready(app: object) -> Any:
    with TestClient(app) as client:  # type: ignore[arg-type]
        return client.get("/ready")


def test_off_ready_does_not_change_and_nothing_is_loaded(tmp_path: Path) -> None:
    app, base = _ready_app(tmp_path, enabled=False)
    body = _ready(app).json()
    assert "permission_notices" not in body
    assert base.permission_store.notices == []


def test_on_the_notices_load_at_start_and_ready_counts_them(tmp_path: Path) -> None:
    app, base = _ready_app(tmp_path, enabled=True)
    response = _ready(app)
    assert response.status_code == 200
    assert response.json()["permission_notices"] == "1-activos"
    assert [notice["notice_ref"] for notice in base.permission_store.notices] == ["maribel-checkout-v1"]


def test_on_a_failing_load_ready_says_so_and_does_not_fall(tmp_path: Path) -> None:
    app, _ = _ready_app(tmp_path, enabled=True, fail_register=True)
    response = _ready(app)
    assert response.status_code == 200
    assert response.json()["permission_notices"] == "sin_cargar"


def test_the_example_of_the_manifest_reference_loads() -> None:
    text = (ROOT / "docs" / "referencia-manifiesto.md").read_text(encoding="utf-8")
    (block,) = [b for b in re.findall(r"```toml\n(.*?)```", text, flags=re.DOTALL) if "[[permisos.avisos]]" in b]
    block = block.replace("<oferta-del-combo>", OFFER).replace("<hotlink>", "F107441770M")
    payload = _payload()
    payload["permisos"] = tomllib.loads(block)["permisos"]
    (notice,) = InstanceManifest.from_mapping(payload).permission_notices
    assert (notice.connector, notice.offer_refs, notice.window) == ("hotmart_checkout", (OFFER,), timedelta(hours=24))
