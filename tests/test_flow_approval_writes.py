"""Aprobar desde la pagina (src/bridge/flow_approval.py, A′ PR 3b, contrato flow-change-cycle-v1 1.9.0).

Los mismos dobles que tests/test_flow_approval_page.py: la app con los clientes
reales (``ApprovalStore`` y ``SlackIdTokenClient``), PostgREST y Slack emulados
con ``httpx.MockTransport``, el almacen de ATT1 del 2026-10-22 y el catalogo de
la bandeja 11 del 2026-10-21, ambos capturados. La revalidacion al publicar
corre el validador real (``validate_proposal``) sobre esos datos.

**Regla 2.** Sigue sin haber captura real de Slack (el ``id_token``) ni de
PostgREST contestando las funciones del aprobador: salen de la documentacion y
de las reglas de 20261016000100 y 20261028000100 (lo que la base hace de verdad
lo prueba PGlite, ``validate_flow_change_slack_approver.mjs``). Tampoco hay
captura del aviso al canal de alertas: se prueba el comando que se le da al
productor del conector, no lo que Slack muestra. Las tres capturas quedan para
el E2E del paso 6 del plan de A′.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Iterator
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_flow_approval_page import (
    ATT1_STORE,
    BRAYAN,
    DAN,
    ORIGIN,
    PROPOSAL,
    FakeChatwoot,
    FakeProducer,
    PostgrestEmulator,
    _client,
    _settings,
    _sign_in,
)

from bridge import flow_approval
from bridge.app import create_app

FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
PHONE = "+52 55 1234 5678"
WRITES = {"approver_approve_flow_change_v1", "approver_publish_flow_change_v1", "approver_pause_flow_v1",
          "approver_discard_flow_change_v1", "approver_flow_cohort_v1"}


@pytest.fixture(autouse=True)
def image(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # La identidad del validador sale de la imagen (el Dockerfile pone SETTER_VERSION y GIT_SHA).
    monkeypatch.setenv("SETTER_VERSION", "1.22.0")
    monkeypatch.setenv("GIT_SHA", "a7bffb7000000000000000000000000000000000")
    yield


def _signed(db: PostgrestEmulator | None = None, *, user: str = DAN, **kwargs: object):
    client, db, slack, clock = _client(db, **kwargs)
    _sign_in(client, slack, clock, user=user)
    return client, db


def _page(client: TestClient, path: str = f"/flow-approval/p/{PROPOSAL}") -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.text


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', page)
    assert match is not None, name
    return match.group(1)


def _post(client: TestClient, path: str, fields: dict[str, str], *, origin: str = ORIGIN):
    from urllib.parse import urlencode

    return client.post(path, content=urlencode(fields), headers={**FORM, "Origin": origin}, follow_redirects=False)


def _publish_fields(page: str) -> dict[str, str]:
    return {"csrf_token": _field(page, "csrf_token"), "fingerprint": _field(page, "fingerprint")}


def _done(response) -> dict[str, str]:
    """El resultado del PRG, sin su firma (que tiene que estar, y ser un HMAC)."""
    assert response.status_code == 303, response.text
    target = urlsplit(response.headers["location"])
    query = {key: values[0] for key, values in parse_qs(target.query).items()}
    if query:
        assert re.fullmatch(r"[0-9a-f]{64}", query.pop("f", "")), "el resultado tiene que ir firmado"
    return query


def _writes(db: PostgrestEmulator) -> list[str]:
    return [name for name, _ in db.calls if name in WRITES]


def _cart_version(db: PostgrestEmulator) -> None:
    """La propuesta pasa a ser una version del carrito de ATT1, que esta prendido: llega a todos."""
    [cart] = [flow for flow in db.flows if flow["flow_key"] == "carrito"]
    definition = copy.deepcopy(cart["versions"]["1"])
    definition.pop("version", None)
    definition["estado"] = "borrador"
    definition["pasos"] = [{"esperar": "2h"}, *definition["pasos"]]
    db.proposal.update(items=[{"producto": "att1", "flujo": "carrito", "flow_id": cart["flow_id"],
                               "tipo": "version", "definicion": definition, "en_curso": "seguir"}],
                       draft_base={cart["flow_id"]: {"active_version": 1}}, purpose="que el carrito espere 2 h")


# ----------------------------------------------------------------- aprobar y publicar, un clic


def test_publishing_a_new_flow_for_the_cohort_is_one_click_with_the_session_approver() -> None:
    producer = FakeProducer()
    chatwoot = FakeChatwoot()
    client, db = _signed(producer=producer, chatwoot=chatwoot)
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "publicada", "aviso": "enviado"}
    assert response.headers["location"].startswith(f"/flow-approval/p/{PROPOSAL}?")
    # Aprueba y publica, en ese orden, con el ID de Slack de la sesion: el pedido no lo lleva.
    assert db.writes == [("approver_approve_flow_change_v1", DAN), ("approver_publish_flow_change_v1", DAN)]
    for name, body in db.calls:
        if name in WRITES:
            assert DAN not in str(body)
            assert set(body) >= {"p_session_hash"}
    # La revalidacion: fase publish, con la clase de quien pidio, sin bloqueos y con el catalogo de la bandeja.
    [publish] = [body for name, body in db.calls if name == "approver_publish_flow_change_v1"]
    revalidation = publish["p_revalidation"]
    assert revalidation["validator"]["phase"] == "publish"
    assert revalidation["validator"]["actor"] == "automation_expert"
    assert revalidation["validator"]["product_version"] == "1.22.0"
    assert revalidation["result"]["blocks"] == [] and len(revalidation["catalog_sha256"]) == 64
    assert chatwoot.inboxes == [11]
    # El aviso al canal de alertas: el flujo, que paso y quien. Ni telefono ni para que.
    [notice] = producer.commands
    assert (notice.event_code, notice.component, notice.reason_code, notice.state) == (
        "OPS-005", "flow_approval:bienvenida", "publicada_cohorte", f"por:{DAN.lower()}")
    assert db.proposal["purpose"] not in repr(notice)
    # La pagina, despues de la redireccion, dice lo que paso y ya no ofrece publicar.
    page = _page(client, response.headers["location"])
    assert "Publicada." in page and "Se avisó en el canal de alertas." in page
    assert "Aprobar y publicar" not in page and "ya está publicada" in page


def test_brayan_approves_with_his_own_slack_id() -> None:
    client, db = _signed(user=BRAYAN)
    _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert db.proposal["approved_by_ref"] == BRAYAN
    assert db.writes[0] == ("approver_approve_flow_change_v1", BRAYAN)


def test_a_double_click_does_not_publish_twice() -> None:
    client, db = _signed()
    fields = _publish_fields(_page(client))
    first = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)
    second = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)
    assert _done(first)["hecho"] == "publicada"
    assert _done(second) == {"hecho": "ya_publicada"}
    assert [name for name, _ in db.writes] == ["approver_approve_flow_change_v1", "approver_publish_flow_change_v1"]
    assert "Ya estaba publicada" in _page(client, second.headers["location"])


def test_an_approved_proposal_that_did_not_publish_is_retried_without_approving_again() -> None:
    client, db = _signed()
    db.proposal.update(status="aprobada", effective_status="aprobada")
    page = _page(client)
    assert "Reintentar publicar" in page
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(page))
    assert _done(response)["hecho"] == "publicada"
    assert [name for name, _ in db.writes] == ["approver_publish_flow_change_v1"]


def test_after_eight_minutes_it_validates_again_before_publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = flow_approval.validate_proposal

    def counted(payload: object, *, environ: object) -> dict:
        calls.append("validar")
        return real(payload, environ=environ)  # type: ignore[arg-type]

    monkeypatch.setattr(flow_approval, "validate_proposal", counted)
    monkeypatch.setattr(flow_approval, "REVALIDATE_AFTER", timedelta(seconds=-1))
    client, db = _signed()
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response)["hecho"] == "publicada"
    assert calls == ["validar", "validar"]
    # La segunda revalidacion relee la propuesta y el almacen: no valida contra lo de antes (E-197, M-4).
    names = [name for name, _ in db.calls]
    publish_at = names.index("approver_publish_flow_change_v1")
    approve_at = names.index("approver_approve_flow_change_v1")
    assert "approver_get_flow_change_proposal_v1" in names[approve_at:publish_at]
    assert "approver_read_flow_store_v1" in names[approve_at:publish_at]


def test_if_the_proposal_changed_before_the_second_revalidation_it_does_not_publish(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow_approval, "REVALIDATE_AFTER", timedelta(seconds=-1))
    client, db = _signed()
    fields = _publish_fields(_page(client))
    real = db._write

    def approve_then_change(name: str, body: dict, user: str):
        response = real(name, body, user)
        if name == "approver_approve_flow_change_v1":
            db.proposal["fingerprint"] = "e" * 64
        return response

    db._write = approve_then_change  # type: ignore[method-assign]
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)
    assert response.status_code == 303 and response.headers["location"] == f"/flow-approval/p/{PROPOSAL}"
    assert "approver_publish_flow_change_v1" not in [name for name, _ in db.calls]


# ----------------------------------------------------------------- dos pasos


def test_what_reaches_everyone_asks_for_a_second_step_that_only_the_bridge_signs() -> None:
    producer = FakeProducer()
    client, db = _signed(producer=producer)
    _cart_version(db)
    fields = _publish_fields(_page(client))
    first = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)
    # El primer clic no aprueba ni publica: muestra el segundo paso, que dice que llega a todos.
    assert first.status_code == 200
    assert "Confirmá antes de publicar" in first.text and "att1/carrito" in first.text
    assert "llega a todos" in first.text
    assert _writes(db) == []
    confirm = _field(first.text, "confirmar")
    # Un segundo paso inventado, o el de otra huella, vuelve a la pantalla del segundo paso.
    forged = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", {**fields, "confirmar": "0" * 64})
    assert forged.status_code == 200 and "Confirmá antes de publicar" in forged.text
    assert _writes(db) == []
    # Con el del bridge, aprueba y publica.
    done = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", {**fields, "confirmar": confirm})
    assert _done(done) == {"hecho": "publicada", "aviso": "enviado"}
    assert [name for name, _ in db.writes] == ["approver_approve_flow_change_v1", "approver_publish_flow_change_v1"]
    assert producer.commands[0].reason_code == "publicada_todos"
    assert producer.commands[0].component == "flow_approval:carrito"


def test_the_second_step_is_tied_to_the_session() -> None:
    client, db = _signed()
    _cart_version(db)
    fields = _publish_fields(_page(client))
    confirm = _field(_post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields).text, "confirmar")
    other, _ = _signed(db, user=BRAYAN)
    other_fields = _publish_fields(_page(other))
    response = _post(other, f"/flow-approval/p/{PROPOSAL}/publish", {**other_fields, "confirmar": confirm})
    assert response.status_code == 200 and "Confirmá antes de publicar" in response.text
    assert _writes(db) == []


@pytest.mark.parametrize(("items", "flows", "expected"), [
    ([{"producto": "att1", "flujo": "b", "tipo": "nuevo", "prender": "cohorte"}], [], []),
    ([{"producto": "att1", "flujo": "b", "tipo": "prender", "flow_id": "x", "a": "cohorte"}], [], []),
    ([{"producto": "att1", "flujo": "b", "tipo": "prender", "flow_id": "x"}], [], [("att1/b", "llega_a_todos")]),
    ([{"producto": "att1", "flujo": "b", "tipo": "apagar", "flow_id": "x"}], [], [("att1/b", "apaga")]),
    ([{"producto": "att1", "flujo": "b", "tipo": "version", "flow_id": "x", "en_curso": "seguir"}],
     [{"flow_id": "x", "state": "cohorte"}], []),
    ([{"producto": "att1", "flujo": "b", "tipo": "volver", "flow_id": "x", "en_curso": "seguir"}],
     [{"flow_id": "x", "state": "prendido"}], [("att1/b", "llega_a_todos")]),
    ([{"producto": "att1", "flujo": "b", "tipo": "version", "flow_id": "x", "en_curso": "cancelar"}],
     [{"flow_id": "x", "state": "cohorte"}], [("att1/b", "corta_en_curso")]),
    # Ante la duda, dos pasos: una version de un flujo pausado, un tipo que no se conoce, sin items.
    ([{"producto": "att1", "flujo": "b", "tipo": "version", "flow_id": "x"}],
     [{"flow_id": "x", "state": "pausado"}], [("att1/b", "sin_clasificar")]),
    ([{"producto": "att1", "flujo": "b", "tipo": "mover", "flow_id": "x"}], [], [("att1/b", "sin_clasificar")]),
    ([], [], [("?", "sin_clasificar")]),
])
def test_one_click_or_two_steps(items: list, flows: list, expected: list) -> None:
    assert flow_approval.second_step_reasons(items, flows) == expected


# ----------------------------------------------------------------- lo que no publica


def test_a_post_without_the_origin_the_csrf_or_a_session_writes_nothing() -> None:
    client, db = _signed()
    fields = _publish_fields(_page(client))
    path = f"/flow-approval/p/{PROPOSAL}/publish"
    assert _post(client, path, fields, origin="https://otro.example").status_code == 403
    assert _post(client, path, {**fields, "csrf_token": "0" * 64}).status_code == 403
    assert _post(client, path, {**fields, "extra": "x"}).status_code == 422
    anonymous = TestClient(client.app, base_url=ORIGIN)
    assert _post(anonymous, path, fields).status_code == 401
    assert _writes(db) == []


def test_someone_taken_off_the_list_cannot_publish_with_a_live_cookie() -> None:
    client, db = _signed(user=BRAYAN)
    fields = _publish_fields(_page(client))
    shortened, _, _, _ = _client(db, flow_approval_slack_approvers=DAN)
    shortened.cookies = client.cookies
    assert _post(shortened, f"/flow-approval/p/{PROPOSAL}/publish", fields).status_code == 401
    assert _writes(db) == []


def test_another_fingerprint_reloads_and_does_not_publish() -> None:
    client, db = _signed()
    fields = _publish_fields(_page(client))
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", {**fields, "fingerprint": "e" * 64})
    assert response.status_code == 303 and response.headers["location"] == f"/flow-approval/p/{PROPOSAL}"
    assert _writes(db) == []


@pytest.mark.parametrize(("status", "code"), [("vencida", "flow_change_proposal_expired"),
                                              ("descartada", "flow_change_not_approved")])
def test_an_expired_or_closed_proposal_does_not_publish(status: str, code: str) -> None:
    client, db = _signed()
    fields = _publish_fields(_page(client))
    db.proposal["effective_status"] = status
    assert _done(_post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)) == {"hecho": code}
    assert _writes(db) == []


def test_without_a_built_simulation_the_page_does_not_publish() -> None:
    client, db = _signed()
    fields = _publish_fields(_page(client))
    db.proposal["validation"]["preview"]["simulacion"] = {"status": "failed", "reason_code": "flow_engine_unavailable"}
    assert _done(_post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)) == {"hecho": "simulation_not_built"}
    assert _writes(db) == []
    assert "Aprobar y publicar" not in _page(client)


def _cart_off(db: PostgrestEmulator, *, failed_simulation: bool = True) -> None:
    """La propuesta pasa a ser apagar el carrito de ATT1 (prendido), con la simulacion que fallo en Maribel."""
    [cart] = [flow for flow in db.flows if flow["flow_key"] == "carrito"]
    db.proposal.update(items=[{"producto": "att1", "flujo": "carrito", "flow_id": cart["flow_id"], "tipo": "apagar"}],
                       draft_base={cart["flow_id"]: {"active_version": cart["active_version"]}},
                       purpose="apagar el carrito")
    if failed_simulation:
        db.proposal["validation"]["preview"]["simulacion"] = {"status": "failed",
                                                              "reason_code": "flow_engine_unavailable"}


def test_turning_a_flow_off_publishes_without_a_simulation_in_two_steps() -> None:
    # E-235, 3: el ensayo del 31/10 en Maribel. Apagar solo saca envios: no espera a la simulacion.
    producer = FakeProducer()
    client, db = _signed(producer=producer)
    _cart_off(db)
    page = _page(client)
    assert "Aprobar y apagar el flujo" in page and "--sin-simulacion" not in page
    assert "Apagar solo saca envíos" in page
    fields = _publish_fields(page)
    # Sigue pidiendo el segundo paso (apagar, decision 2 del 2026-10-27).
    first = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)
    assert first.status_code == 200 and "Confirmá antes de publicar" in first.text
    assert "apaga el flujo" in first.text and "Sí, aprobar y apagar el flujo" in first.text
    assert _writes(db) == []
    done = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", {**fields, "confirmar": _field(first.text, "confirmar")})
    assert _done(done) == {"hecho": "publicada", "aviso": "enviado"}
    assert [name for name, _ in db.writes] == ["approver_approve_flow_change_v1", "approver_publish_flow_change_v1"]


@pytest.mark.parametrize("extra", [
    {"producto": "att1", "flujo": "carrito", "tipo": "prender", "a": "cohorte"},
    {"producto": "att1", "flujo": "carrito", "tipo": "volver", "version": 1, "en_curso": "seguir"},
    "no es un item",
])
def test_turning_off_mixed_with_anything_else_still_needs_the_simulation(extra: object) -> None:
    client, db = _signed()
    _cart_off(db)
    db.proposal["items"] = [*db.proposal["items"], extra]
    # Sin boton en la pagina: el POST se arma a mano con la huella de la propuesta.
    fields = {"csrf_token": _field(_page(client), "csrf_token"), "fingerprint": db.proposal["fingerprint"]}
    assert _done(_post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)) == {"hecho": "simulation_not_built"}
    assert _writes(db) == []
    assert "Aprobar y apagar el flujo" not in _page(client)


@pytest.mark.parametrize(("items", "expected"), [
    ([{"tipo": "apagar"}], True),
    ([{"tipo": "apagar"}, {"tipo": "apagar"}], True),
    ([], False),
    (None, False),
    ([{"tipo": "prender"}], False),
    ([{"tipo": "nuevo", "prender": "cohorte"}], False),
    ([{"tipo": "apagar"}, {"tipo": "version"}], False),
    (["apagar"], False),
])
def test_only_turns_off(items: object, expected: bool) -> None:
    assert flow_approval.only_turns_off(items) is expected


@pytest.mark.parametrize(("members", "maximum", "text"), [
    (0, 5, "0 de 5 teléfonos cargados · quedan 5 lugares."),
    (1, 5, "1 de 5 teléfonos cargados · quedan 4 lugares."),
    (4, 5, "4 de 5 teléfonos cargados · queda 1 lugar."),
    (5, 5, "5 de 5 teléfonos cargados · no quedan lugares."),
    (1, 1, "1 de 1 teléfono cargado · no quedan lugares."),
])
def test_the_cohort_count_says_how_many_are_loaded_and_how_many_places_are_left(
        members: int, maximum: int, text: str) -> None:
    # E-235, 1: con un telefono cargado la pagina decia «Quedan 1 de 5».
    assert flow_approval.cohort_count_text(members, maximum) == text


def test_without_the_catalog_it_does_not_approve() -> None:
    client, db = _signed(chatwoot=FakeChatwoot(failure=RuntimeError("chatwoot caido")))
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "template_catalog_unavailable"}
    assert _writes(db) == []


def test_a_revalidation_with_blocks_does_not_approve() -> None:
    # Mientras tanto la bienvenida ya existe: el validador de ahora bloquea el nuevo.
    client, db = _signed()
    fields = _publish_fields(_page(client))
    db.add_welcome_in_cohort()
    assert _done(_post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)) == {"hecho": "revalidation_blocked"}
    assert _writes(db) == []


def test_without_the_image_identity_it_does_not_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA")
    client, db = _signed()
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "validator_unavailable"}
    assert _writes(db) == []


def test_a_stale_base_at_approval_does_not_publish_and_offers_no_retry() -> None:
    client, db = _signed()
    db.approve_result = {"ok": False, "reason_code": "flow_change_stale_base"}
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "flow_change_stale_base"}
    assert "approver_publish_flow_change_v1" not in [name for name, _ in db.calls]
    page = _page(client, response.headers["location"])
    assert "pedile al agente una propuesta nueva" in page
    assert "Aprobar y publicar" not in page and "Reintentar publicar" not in page


def test_a_publish_that_fails_after_the_yes_stays_approved_and_offers_to_retry() -> None:
    producer = FakeProducer()
    client, db = _signed(producer=producer)
    db.publish_result = {"ok": False, "reason_code": "flow_change_revalidation_blocked"}
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "flow_change_revalidation_blocked"}
    assert db.proposal["status"] == "aprobada" and producer.commands == []
    page = _page(client, response.headers["location"])
    assert "Reintentar publicar" in page and "aprobada, sin publicar" in page


def test_when_the_notice_does_not_go_out_the_publication_stays_and_the_page_says_so() -> None:
    client, db = _signed(producer=FakeProducer(failure=RuntimeError("conector caido")))
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "publicada", "aviso": "no_salio"}
    assert db.proposal["status"] == "publicada"
    assert "El aviso al canal de alertas no salió" in _page(client, response.headers["location"])


def test_without_a_slack_connector_it_publishes_and_says_it_did_not_notify() -> None:
    client, _ = _signed()
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "publicada", "aviso": "sin_conector"}


# ----------------------------------------------------------------- descartar


def test_discarding_takes_a_one_line_reason() -> None:
    client, db = _signed()
    page = _page(client)
    csrf = _field(page, "csrf_token")
    path = f"/flow-approval/p/{PROPOSAL}/discard"
    assert _done(_post(client, path, {"csrf_token": csrf, "motivo": "   "})) == {"hecho": "flow_store_text_invalid"}
    assert _done(_post(client, path, {"csrf_token": csrf, "motivo": "no\nasi"})) == {"hecho": "flow_store_text_invalid"}
    assert _writes(db) == []
    assert _done(_post(client, path, {"csrf_token": csrf, "motivo": "lo pidio otra cosa"})) == {"hecho": "descartada"}
    assert db.proposal["status"] == "descartada" and db.proposal["close_reason"] == "lo pidio otra cosa"
    assert db.writes == [("approver_discard_flow_change_v1", DAN)]


# ----------------------------------------------------------------- pausar


def test_pausing_uses_the_generation_the_page_showed_and_notifies() -> None:
    producer = FakeProducer()
    client, db = _signed(producer=producer)
    page = _page(client, "/flow-approval/")
    form = re.search(r'<form method="post" action="/flow-approval/pause">(.*?)</form>', page).group(1)
    fields = {"csrf_token": _field(form, "csrf_token"), "producto": _field(form, "producto"),
              "flujo": _field(form, "flujo"), "generacion": _field(form, "generacion")}
    assert (fields["flujo"], fields["generacion"]) == ("carrito", "2")
    assert _done(_post(client, "/flow-approval/pause", fields)) == {"hecho": "pausado", "aviso": "enviado"}
    [cart] = [flow for flow in db.flows if flow["flow_key"] == "carrito"]
    assert (cart["state"], cart["generation"]) == ("pausado", 3)
    [notice] = producer.commands
    assert (notice.event_code, notice.component, notice.reason_code) == ("OPS-003", "flow_approval:carrito", "pausado")
    # La misma pagina vieja: otra generacion, no pausa de nuevo y lo dice.
    assert _done(_post(client, "/flow-approval/pause", fields)) == {"hecho": "generacion"}
    assert _done(_post(client, "/flow-approval/pause", {**fields, "generacion": "3"})) == {"hecho": "ya_pausado"}
    assert db.writes == [("approver_pause_flow_v1", DAN)] and len(producer.commands) == 1


def test_pausing_from_a_proposal_goes_back_to_it_and_refuses_what_has_no_shape() -> None:
    db = PostgrestEmulator()
    _cart_version(db)
    client, db = _signed(db)
    page = _page(client)
    form = re.search(r'<form method="post" action="/flow-approval/pause">(.*?)</form>', page).group(1)
    fields = {key: _field(form, key) for key in ("csrf_token", "producto", "flujo", "generacion", "p")}
    response = _post(client, "/flow-approval/pause", fields)
    assert response.headers["location"].startswith(f"/flow-approval/p/{PROPOSAL}?")
    for bad in ({"flujo": "Carrito'"}, {"generacion": "-1"}, {"p": "no-es-uuid"}):
        assert _post(client, "/flow-approval/pause", {**fields, **bad}).status_code == 422


# ----------------------------------------------------------------- la cohorte


def test_the_cohort_phone_goes_to_the_base_and_nowhere_else(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    db = PostgrestEmulator()
    db.add_welcome_in_cohort()
    producer = FakeProducer()
    client, db = _signed(db, producer=producer)
    page = _page(client, "/flow-approval/")
    assert 'name="telefono" autocomplete="off"' in page
    form = re.search(r'<form method="post" action="/flow-approval/cohort">(.*?)</form>', page).group(1)
    fields = {"csrf_token": _field(form, "csrf_token"), "producto": "att1", "flujo": "bienvenida", "telefono": PHONE}
    response = _post(client, "/flow-approval/cohort", fields)
    assert _done(response) == {"hecho": "cohorte", "miembros": "3", "max": "5"}
    [call] = [body for name, body in db.calls if name == "approver_flow_cohort_v1"]
    assert call["p_phone"] == PHONE and call["p_flow_key"] == "bienvenida"
    assert db.writes == [("approver_flow_cohort_v1", DAN)]
    shown = _page(client, response.headers["location"])
    for text in (response.headers["location"], shown, caplog.text, repr(producer.commands)):
        assert PHONE not in text and "1234 5678" not in text and "12345678" not in text
    assert "3 de 5 teléfonos cargados · quedan 2 lugares." in shown and "Quedan" not in shown
    # Lo que la base no acepta vuelve con su motivo; un telefono sin forma ni llega a la base.
    db.cohort_result = {"ok": False, "reason_code": "flow_cohort_full"}
    assert _done(_post(client, "/flow-approval/cohort", fields)) == {"hecho": "flow_cohort_full"}
    calls = len([name for name, _ in db.calls if name == "approver_flow_cohort_v1"])
    assert _done(_post(client, "/flow-approval/cohort", {**fields, "telefono": "hola"})) == {
        "hecho": "flow_cohort_phone_invalid"}
    assert len([name for name, _ in db.calls if name == "approver_flow_cohort_v1"]) == calls


# ----------------------------------------------------------------- ATT1 y Johanna, sin el flag


def test_with_the_flag_off_the_write_routes_do_not_exist() -> None:
    app = create_app(_settings(flow_approval_enabled=False), supabase_client=object())  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    for path in (f"/flow-approval/p/{PROPOSAL}/publish", f"/flow-approval/p/{PROPOSAL}/discard",
                 "/flow-approval/pause", "/flow-approval/cohort"):
        assert _post(client, path, {"csrf_token": "x"}).status_code == 404


def test_with_the_flag_on_the_bridge_does_not_start_without_the_chatwoot_control_client() -> None:
    with pytest.raises(ValueError, match="Chatwoot control client"):
        create_app(_settings(), supabase_client=object())  # type: ignore[arg-type]


def test_the_fixture_store_is_the_captured_one() -> None:
    db = PostgrestEmulator()
    assert db.flows == ATT1_STORE["flows"]


# ----------------------------------------------------------------- E-197: las fronteras que nada probaba


def _raw_post(client: TestClient, path: str, fields: dict[str, str], headers: dict[str, str]):
    from urllib.parse import urlencode

    return client.post(path, content=urlencode(fields),
                       headers={"Content-Type": "application/x-www-form-urlencoded", **headers}, follow_redirects=False)


def test_an_origin_that_is_null_absent_or_almost_the_same_writes_nothing() -> None:
    client, db = _signed()
    fields = _publish_fields(_page(client))
    path = f"/flow-approval/p/{PROPOSAL}/publish"
    host = urlsplit(ORIGIN).netloc
    for origin in (ORIGIN.upper(), ORIGIN + ":443", ORIGIN + "/", ORIGIN.replace("https://", "http://"),
                   ORIGIN.replace("https://", "https://x.")):
        assert _raw_post(client, path, fields, {"Origin": origin}).status_code == 403, origin
    assert _raw_post(client, path, fields, {"Origin": "null"}).status_code == 403
    assert _raw_post(client, path, fields, {"Origin": "null", "Sec-Fetch-Site": "same-site"}).status_code == 403
    assert _raw_post(client, path, fields, {}).status_code == 403
    assert _raw_post(client, path, fields, {"Sec-Fetch-Site": "same-origin", "Host": "otro.example"}).status_code == 403
    assert _writes(db) == []
    # Sin Origin, solo el navegador que dice same-origin para este host.
    assert _done(_raw_post(client, path, fields, {"Sec-Fetch-Site": "same-origin", "Host": host}))["hecho"] == "publicada"


def test_a_session_of_another_workspace_cannot_write() -> None:
    client, db = _signed()
    fields = _publish_fields(_page(client))
    for session in db.sessions.values():
        session["team"] = "T0OTRO000001"
    assert _post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields).status_code == 401
    assert _writes(db) == []


def test_the_result_in_the_link_is_only_a_closed_code() -> None:
    client, _ = _signed()
    page = _page(client, f"/flow-approval/p/{PROPOSAL}?hecho=%3Cb%3Ex%3C%2Fb%3E&aviso=%3Ci%3E&miembros=1000")
    assert "No se hizo" not in page and "<b>x</b>" not in page and "&lt;b&gt;" not in page


def test_a_discard_reason_longer_than_200_is_refused() -> None:
    client, db = _signed()
    csrf = _field(_page(client), "csrf_token")
    path = f"/flow-approval/p/{PROPOSAL}/discard"
    assert _done(_post(client, path, {"csrf_token": csrf, "motivo": "x" * 201})) == {"hecho": "flow_store_text_invalid"}
    assert _done(_post(client, path, {"csrf_token": csrf, "motivo": "no‮asi"})) == {"hecho": "flow_store_text_invalid"}
    assert _writes(db) == []


def test_two_clicks_at_once_the_second_sees_the_replay_and_does_not_notify_again() -> None:
    # Los dos leyeron «aprobada»; la base ordena con el lock y al segundo le contesta replayed.
    producer = FakeProducer()
    client, db = _signed(producer=producer)
    db.proposal.update(status="aprobada", effective_status="aprobada")
    db.publish_result = {"ok": True, "replayed": True}
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "ya_publicada"}
    assert producer.commands == []


def test_the_second_step_is_tied_to_the_proposal_and_its_fingerprint() -> None:
    config = flow_approval.check_settings(_settings())
    assert config is not None
    reasons = [("att1/carrito", "llega_a_todos")]
    token = config.confirm_token("s" * 43, PROPOSAL, "f" * 64, reasons, 7)
    assert token != config.confirm_token("s" * 43, "22222222-2222-4333-8444-555555555555", "f" * 64, reasons, 7)
    assert token != config.confirm_token("s" * 43, PROPOSAL, "e" * 64, reasons, 7)
    # E-197, M-2: tambien a los motivos que mostro la pantalla y a la ventana.
    assert token != config.confirm_token("s" * 43, PROPOSAL, "f" * 64, [("att1/carrito", "apaga")], 7)
    assert token != config.confirm_token("s" * 43, PROPOSAL, "f" * 64, reasons, 8)
    assert token != config.csrf_token("s" * 43)


def test_an_old_second_step_does_not_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    # E-197, M-2: el campo del segundo paso vale la ventana de 15 minutos en curso y la anterior.
    client, db = _signed()
    _cart_version(db)
    fields = _publish_fields(_page(client))
    first = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", fields)
    confirm = _field(first.text, "confirmar")
    monkeypatch.setattr(flow_approval, "CONFIRM_WINDOW", timedelta(microseconds=1))
    late = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", {**fields, "confirmar": confirm})
    assert late.status_code == 200 and "Confirmá antes de publicar" in late.text
    assert _writes(db) == []


def test_a_result_link_made_by_hand_says_nothing_and_hides_nothing() -> None:
    # E-197, M-1: sin la firma de esta sesion y esta pagina, el resultado no se muestra.
    client, db = _signed()
    path = f"/flow-approval/p/{PROPOSAL}"
    forged = _page(client, f"{path}?hecho=publicada&aviso=enviado")
    assert "Publicada." not in forged and "Se avisó" not in forged and "Aprobar y publicar" in forged
    wrong = _page(client, f"{path}?hecho=flow_change_stale_base&f={'0' * 64}")
    assert "cambió la base" not in wrong and "Aprobar y publicar" in wrong
    # La firma de otra pagina tampoco sirve.
    index = _post(client, "/flow-approval/pause", {
        "csrf_token": _field(_page(client), "csrf_token"), "producto": "att1", "flujo": "carrito",
        "generacion": "1"})
    signature = parse_qs(urlsplit(index.headers["location"]).query)["f"][0]
    moved = _page(client, f"{path}?hecho=generacion&f={signature}")
    assert "El flujo cambió mientras mirabas" not in moved
    # Y la firma de otra sesion, tampoco.
    other, _ = _signed(db, user=BRAYAN)
    done = _post(client, f"{path}/publish", _publish_fields(_page(client)))
    assert "Publicada." in _page(client, done.headers["location"])
    assert "Publicada." not in _page(other, done.headers["location"])


def test_an_unknown_result_code_is_escaped() -> None:
    # E-197, M-6: la regex corta antes, pero si algun dia se afloja queda el escape.
    shown = flow_approval._render_done({"hecho": "<b>x</b>"})
    assert "<b>" not in shown and "&lt;b&gt;x&lt;/b&gt;" in shown


def test_a_signed_result_that_is_not_a_closed_code_is_not_shown() -> None:
    # La firma no alcanza: el codigo tiene que ser ademas uno cerrado (la regex va detras del HMAC).
    client, _ = _signed()
    config = flow_approval.check_settings(_settings())
    assert config is not None
    path = f"/flow-approval/p/{PROPOSAL}"
    secret = client.cookies["__Host-flow_approval_session"]
    result = {"hecho": "<b>x</b>"}
    page = _page(client, f"{path}?hecho=%3Cb%3Ex%3C%2Fb%3E&f={config.done_token(secret, path, result)}")
    assert "No se hizo" not in page and "&lt;b&gt;" not in page


def test_a_body_over_4096_bytes_is_refused_even_without_content_length() -> None:
    # E-197, M-3: se corta leyendo, no despues de leer todo (un cuerpo chunked no trae Content-Length).
    client, db = _signed()
    csrf = _field(_page(client), "csrf_token")
    body = f"csrf_token={csrf}&motivo={'x' * 5000}".encode()
    chunks = iter([body[i:i + 1024] for i in range(0, len(body), 1024)])
    response = client.post(f"/flow-approval/p/{PROPOSAL}/discard", content=chunks, follow_redirects=False,
                           headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN})
    assert response.status_code == 422
    assert _writes(db) == []
    declared = client.post(f"/flow-approval/p/{PROPOSAL}/discard", content=b"csrf_token=x", follow_redirects=False,
                           headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN,
                                    "Content-Length": "999999"})
    assert declared.status_code == 422
