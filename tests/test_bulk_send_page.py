"""La pagina de un masivo manejado por el AE (encargo 5 del mapa del 2026-11-25, piezas 3 y 6).

Los dobles de tests/test_flow_approval_page.py (la sesion de «Entrar con Slack», PostgREST y
Slack emulados) con las cuatro funciones de la pagina de 20261125000200 y sus reglas, copiadas
de la migracion (lo que hace la base de verdad lo prueba PGlite: validate_bulk_send_store.mjs).

Las entradas:

* Las listas son las sinteticas de ``tests/fixtures/bulk_import/`` (ver su README): ninguna sale
  de una persona.
* El catalogo de plantillas es el de la bandeja 11 de ATT1, capturado el 2026-10-10
  (``tests/fixtures/chatwoot_inbox_11_message_templates_20261010.json``). La plantilla con imagen
  es esa misma con un ``HEADER`` de formato ``IMAGE``, la forma que documenta Meta para un
  encabezado con imagen (no hay una capturada con imagen en el repo).
* La salud es la forma de ``estado.json`` de ``meta_salud.py`` (``calidad``, ``limite``,
  ``puede_enviar``, ``leido_en``), la misma que lee el barredor de audiencia.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_flow_approval_page import (
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    OUTSIDER,
    PostgrestEmulator,
    SlackEmulator,
    _Clock,
    _settings,
    _sign_in,
)

from bridge import bulk_send_page as bp
from bridge import flow_approval, flow_audience_page
from bridge.bulk_import import read_bulk_list
from bridge.bulk_send_report import FailureCount, RecipientOutcome, BulkSendReport

FIXTURES = Path(__file__).parent / "fixtures"
GHL_CSV = FIXTURES / "bulk_import" / "lista_ghl_sintetica_cp1252_punto_y_coma.csv"
OPENPYXL_XLSX = FIXTURES / "bulk_import" / "lista_sintetica_openpyxl.xlsx"
CATALOG = json.loads((FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8"))
SEND = "0000000d-0000-4000-8000-0000000b0001"
OTHER = "0000000d-0000-4000-8000-0000000b0002"
LIST = "0000000e-0000-4000-8000-0000000c0001"
PATH = "/flow-approval/masivos"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
SCHEDULED = "2026-11-29T23:00:00+00:00"  # 17:00 de CDMX


class FakeChatwoot:
    def __init__(self, *, image: bool = False, failure: Exception | None = None) -> None:
        self.catalog = copy.deepcopy(CATALOG)
        self.failure = failure
        if image:
            for template in self.catalog["message_templates"]:
                if template["name"] == "att1_carrito_abandonado_01":
                    template["components"].insert(0, {"type": "HEADER", "format": "IMAGE"})

    async def get_inbox(self, *, inbox_id: int) -> dict:
        assert inbox_id == 11
        if self.failure is not None:
            raise self.failure
        return {"id": inbox_id, "message_templates": self.catalog["message_templates"]}


class BulkDb(PostgrestEmulator):
    """Las funciones approver_*_bulk_send_v1 con las reglas de la migracion."""

    def __init__(self) -> None:
        super().__init__()
        self.send: dict[str, Any] = {
            "send_id": SEND, "state": "borrador", "template_name": "att1_carrito_abandonado_01",
            "template_language": "es_MX", "template_variables": [{"de": "nombre"}, "Alimenta tu Tiroides"],
            "header_image_url": None, "campaign_label": "black-2026", "scheduled_at": SCHEDULED,
            "window_mode": "centrada", "rate_millis": 2500, "recipient_cap": 6000, "created_by": "automation_expert",
            "created_at": "2026-11-25T16:00:00Z", "confirmed_by": None, "confirmed_at": None,
            "confirmed_count": None, "confirmed_health": None, "started_at": None, "finished_at": None,
            "brake_reason": None, "braked_by": None, "braked_at": None, "list": None, "recipients_by_state": {}}
        self.members: list[dict[str, Any]] = []
        self.bulk_calls: list[tuple[str, dict]] = []

    def vet(self, exclusions: dict[int, str]) -> None:
        """Lo que hace apply_bulk_list_vetoes_v1: reemplaza las exclusiones y deja la lista revisada."""
        for position, member in enumerate(self.members):
            member["exclusion_reason"] = exclusions.get(position)
        self.send["list"]["state"] = "revisada"
        self.send["list"]["vetted_at"] = "2026-11-25T16:10:00Z"

    def view(self) -> dict[str, Any]:
        view = copy.deepcopy(self.send)
        if view["list"] is not None:
            eligible = [m for m in self.members if m["exclusion_reason"] is None]
            excluded: dict[str, int] = {}
            for member in self.members:
                if member["exclusion_reason"]:
                    excluded[member["exclusion_reason"]] = excluded.get(member["exclusion_reason"], 0) + 1
            view["list"].update(
                eligible_count=len(eligible), excluded_by_reason=excluded,
                examples=[{"phone": "…" + m["phone"][-4:],
                           "initial": (m["first_name"][0].upper() + ".") if m["first_name"] else None}
                          for m in eligible[:5]])
        return view

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if name == "approver_read_flow_audience_v1":
            # La seccion de audiencia, levantada junto con esta: sin flujos manuales ni envios.
            return httpx.Response(200, json={"flows": [], "occurrences": []})
        if "bulk" not in name:
            return super().handler(request)
        body = json.loads(request.content)
        self.bulk_calls.append((name, body))
        session = self._session(body)
        if session is None:
            return self._raise("28000", "flow_approval_session_invalid", 403)
        if body["p_send_id"] != SEND:
            return self._raise("P0002", "bulk_send_not_found", 404)
        send = self.send
        if name == "approver_read_bulk_send_v1":
            return httpx.Response(200, json=self.view())
        if name == "approver_create_bulk_list_v1":
            if send["state"] != "borrador":
                return httpx.Response(200, json={"ok": False, "reason_code": "not_a_draft"})
            if send["list"] is not None:
                return httpx.Response(200, json={"ok": False, "reason_code": "list_already_attached"})
            data = body["p_list"]
            assert data["notice_declared"] is True
            assert re.fullmatch(r"[0-9a-f]{64}", data["file_sha256"])
            for key, value in data["rejected_counts"].items():
                assert re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) and isinstance(value, int)
            phones = [row["phone"] for row in data["rows"]]
            if len(set(phones)) != len(phones):
                return self._raise("22023", "bulk_list_duplicate_phone")
            assert all(re.fullmatch(r"[1-9][0-9]{7,14}", phone) for phone in phones)
            self.members = [{"phone": row["phone"], "first_name": row["first_name"], "exclusion_reason": None}
                            for row in data["rows"]]
            send["list"] = {"list_id": LIST, "state": "cargada", "uploaded_by": f"slack:{session['user']}",
                            "uploaded_at": "2026-11-25T16:05:00Z", "file_sha256": data["file_sha256"],
                            "source_kind": data["source_kind"], "source_note": data["source_note"],
                            "notice_declared": True, "rejected_counts": data["rejected_counts"],
                            "member_count": len(phones), "retention_days": 30, "vetted_at": None, "purged_at": None}
            return httpx.Response(200, json={"ok": True, "list_id": LIST, "member_count": len(phones)})
        if name == "approver_confirm_bulk_send_v1":
            if send["state"] != "borrador":
                return httpx.Response(200, json={"ok": False, "reason_code": "not_waiting_for_confirmation"})
            if send["list"] is None:
                return httpx.Response(200, json={"ok": False, "reason_code": "list_missing"})
            if send["list"]["state"] != "revisada":
                return httpx.Response(200, json={"ok": False, "reason_code": "list_not_vetted"})
            if body["p_confirmed"] is not True:
                return httpx.Response(200, json={"ok": False, "reason_code": "confirmation_missing"})
            if body["p_health"] not in ("verde", "amarillo"):
                return httpx.Response(200, json={"ok": False, "reason_code": "health_blocks"})
            if body["p_health"] == "amarillo" and body["p_yellow_ack"] is not True:
                return httpx.Response(200, json={"ok": False, "reason_code": "yellow_ack_missing"})
            eligible = sum(1 for m in self.members if m["exclusion_reason"] is None)
            if body["p_expected_count"] != eligible:
                return httpx.Response(200, json={"ok": False, "reason_code": "count_mismatch"})
            send.update(state="confirmado", confirmed_by=f"slack:{session['user']}", confirmed_count=eligible,
                        confirmed_health=body["p_health"], confirmed_at="2026-11-25T16:20:00Z")
            return httpx.Response(200, json={"ok": True, "confirmed_count": eligible})
        if name == "approver_brake_bulk_send_v1":
            if send["state"] not in ("borrador", "confirmado", "preparando", "enviando"):
                return httpx.Response(200, json={"ok": False, "reason_code": "not_brakeable"})
            send.update(state="frenado", brake_reason="a_mano", braked_by=f"slack:{session['user']}",
                        braked_at="2026-11-25T16:30:00Z")
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(name)

    def names(self) -> list[str]:
        return [name for name, _ in self.bulk_calls]


class FakeReviewer:
    """Los vetos del encargo 4 (un doble): saca a Beto por baja y deja la lista revisada."""

    def __init__(self, db: BulkDb, *, failures: int = 0) -> None:
        self.db = db
        self.failures = failures
        self.calls: list[dict[str, Any]] = []

    async def review(self, *, send_id: str, list_id: str, members: Any, ghl_opt_out_phones: Any) -> dict:
        self.calls.append({"send_id": send_id, "list_id": list_id, "members": list(members),
                           "ghl": list(ghl_opt_out_phones)})
        if self.failures:
            self.failures -= 1
            raise RuntimeError("chatwoot_listado_incompleto")
        exclusions = {m.position: "baja" for m in members if m.phone == "573000000002"}
        exclusions.update({m.position: "baja_ghl" for m in members if m.phone in set(ghl_opt_out_phones)})
        self.db.vet(exclusions)
        return {"ok": True, "excluded": len(exclusions)}


def _health(calidad: str = "GREEN", *, att1: str = "GREEN", hours_ago: float = 1.0,
            puede_enviar: str = "AVAILABLE") -> dict[str, Any]:
    when = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {"att1": {"calidad": att1, "limite": "TIER_10K", "puede_enviar": "AVAILABLE", "leido_en": when},
            "maribel": {"calidad": calidad, "limite": "TIER_10K", "puede_enviar": puede_enviar, "leido_en": when},
            "johanna": {"calidad": "GREEN", "limite": "TIER_1K", "puede_enviar": "AVAILABLE", "leido_en": when}}


class Harness:
    def __init__(self, *, reviewer: bool = True, failures: int = 0, image: bool = False,
                 report: Any = None, max_upload_bytes: int = bp.DEFAULT_MAX_BYTES,
                 with_audience: bool = False, campaigns: Any = None, warmup: Any = None) -> None:
        self.db, self.slack, self.clock = BulkDb(), SlackEmulator(), _Clock()
        self.health = _health()
        self.reviewer = FakeReviewer(self.db, failures=failures) if reviewer else None
        self.images: list[str] = []

        async def probe(url: str) -> bp.ImageCheck:
            self.images.append(url)
            return bp.ImageCheck(True, size=180_000, content_type="image/jpeg")

        config = flow_approval.check_settings(_settings())
        assert config is not None
        bulk = bp.routes(chatwoot=FakeChatwoot(image=image), inbox_id=11, country_code="57",
                         sending_key="maribel", portfolio_keys=("maribel", "att1", "johanna"),
                         health=lambda: self.health, reviewer=self.reviewer, report=report,
                         campaigns=campaigns, image_probe=probe, max_upload_bytes=max_upload_bytes,
                         warmup=warmup)
        extra = bulk
        if with_audience:
            from test_flow_audience_page import FakeSweeper
            audience = flow_audience_page.routes(sweeper=FakeSweeper())  # type: ignore[arg-type]

            def extra(app: FastAPI, tools: flow_approval.PageTools) -> None:
                audience(app, tools)
                bulk(app, tools)
        self.app = FastAPI()
        flow_approval.register_flow_approval_routes(
            self.app, config=config,
            store=flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                              transport=httpx.MockTransport(self.handler)),
            oidc=flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                                  transport=httpx.MockTransport(self.slack.handler),
                                                  clock=lambda: self.clock.now),
            time_zone="America/Mexico_City", extra_routes=extra)
        self.client = TestClient(self.app, base_url=ORIGIN)

    def handler(self, request: httpx.Request) -> httpx.Response:
        return self.db.handler(request)

    def sign_in(self, user: str = DAN) -> Harness:
        _sign_in(self.client, self.slack, self.clock, user=user)
        return self

    def page(self, send: str = SEND) -> str:
        response = self.client.get(f"{PATH}/{send}")
        assert response.status_code == 200, response.text
        return response.text

    def csrf(self) -> str:
        match = re.search(r'name="csrf_token" value="([^"]+)"', self.page())
        assert match is not None
        return match.group(1)

    def upload(self, data: bytes | None = None, *, filename: str = "lista.csv", origen: str = "inscritas_ghl",
               aviso: bool = True, nota: str = "", bajas: bytes | None = None, csrf: str | None = None,
               origin: str = ORIGIN, path: str = "subir") -> httpx.Response:
        fields: dict[str, str] = {"csrf_token": csrf if csrf is not None else self.csrf(), "origen": origen}
        if aviso:
            fields["aviso"] = "si"
        if nota:
            fields["nota"] = nota
        if path != "subir":
            fields = {"csrf_token": fields["csrf_token"]}
        files = {"archivo": (filename, GHL_CSV.read_bytes() if data is None else data, "text/csv")}
        if bajas is not None:
            files["bajas"] = ("bajas.csv", bajas, "text/csv")
        return self.client.post(f"{PATH}/{SEND}/{path}", data=fields, files=files, headers={"Origin": origin},
                                follow_redirects=False)

    def post(self, action: str, fields: dict[str, str], *, origin: str = ORIGIN) -> httpx.Response:
        return self.client.post(f"{PATH}/{SEND}/{action}", content=urlencode(fields),
                                headers={**FORM, "Origin": origin}, follow_redirects=False)


def _done(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["hecho"][0]


def _vetted(**kwargs: Any) -> Harness:
    harness = Harness(**kwargs).sign_in()
    assert _done(harness.upload()) == "lista_revisada"
    return harness


# --- la vista previa ----------------------------------------------------------------------------


def test_without_a_list_the_page_shows_the_template_and_the_upload_form_and_no_confirm() -> None:
    harness = Harness().sign_in()
    page = harness.page()
    assert "att1_carrito_abandonado_01" in page and "MARKETING" in page and "APPROVED" in page
    assert "No más mensajes (QUICK_REPLY)" in page
    assert 'enctype="multipart/form-data"' in page and 'name="archivo"' in page and 'name="aviso"' in page
    for source in ("inscritas_ghl", "compradoras", "otra"):
        assert f'value="{source}"' in page
    assert "/confirmar" not in page
    assert "Etiqueta de campaña: black-2026 (no se pudo comprobar si está vigente)" in page
    assert harness.db.names() == ["approver_read_bulk_send_v1"]


def test_upload_normalizes_saves_only_the_rows_and_runs_the_vetoes() -> None:
    harness = Harness().sign_in()
    assert _done(harness.upload(nota="Black de noviembre")) == "lista_revisada"
    (name, body), = [call for call in harness.db.bulk_calls if call[0] == "approver_create_bulk_list_v1"]
    data = body["p_list"]
    assert [row["phone"] for row in data["rows"]] == [
        "573000000001", "573000000002", "5215500000005", "541100000006", "34612000010", "13055550112"]
    assert data["rows"][0] == {"phone": "573000000001", "first_name": "Ana", "email": "ana@example.com"}
    assert data["rejected_counts"] == {"ar_largo": 1, "co_no_movil": 1, "largo": 1, "mx_largo": 1, "no_movil": 1,
                                       "sin_digitos": 1, "sin_pais": 1, "vacio": 1, "duplicado": 1}
    assert data["source_kind"] == "inscritas_ghl" and data["source_note"] == "Black de noviembre"
    import hashlib
    assert data["file_sha256"] == hashlib.sha256(GHL_CSV.read_bytes()).hexdigest()
    # El archivo crudo no viaja a la base: solo las filas normalizadas y la huella.
    assert "Last Name" not in json.dumps(body) and "+57 300" not in json.dumps(body)
    (call,) = harness.reviewer.calls
    assert call["list_id"] == LIST and call["send_id"] == SEND and call["ghl"] == []
    assert [(m.position, m.phone) for m in call["members"]][:2] == [(0, "573000000001"), (1, "573000000002")]


def test_the_vetted_page_shows_counts_masked_examples_the_message_and_the_window() -> None:
    harness = _vetted()
    page = harness.page()
    assert "Van: <strong>5</strong> personas" in page
    assert "Quedaron afuera 1: 1 pidieron no recibir más" in page
    assert "sirvieron <strong>6</strong>" in page and "<strong>9</strong> filas" in page
    assert "1 repetidos (queda la primera fila)" in page and "1 sin código de país" in page
    # Cinco ejemplos enmascarados con el mensaje armado; ningun telefono entero.
    assert page.count("<li><p><strong>…") == 5
    assert "…0001" in page and "Hola, A.. Soy el asistente" in page
    assert "Hola, qué tal. Soy el asistente" in page  # el de +1 305 no tiene nombre
    assert "Alimenta tu Tiroides" in page
    for phone in ("573000000001", "5215500000005", "13055550112"):
        assert phone not in page
    # D = 5 / 2,5 = 2 s, centrada en las 23:00 UTC.
    assert "Dura 2 s" in page
    assert "Arranca: UTC 29/11 22:59 · Argentina 29/11 19:59 · Colombia 29/11 17:59 · CDMX 29/11 16:59" in page
    assert "Termina: UTC 29/11 23:00" in page
    assert "horario permitido" not in page
    # La salud del portfolio y el formulario de confirmar sin la segunda marca.
    assert "maribel (manda este masivo)" in page and "att1" in page and "johanna" in page
    assert "La salud deja arrancar." in page
    assert 'name="conteo"' in page and 'name="confirmo"' in page and 'name="amarillo"' not in page


def test_the_window_math_and_the_country_of_the_instance() -> None:
    center = datetime(2026, 11, 29, 23, 0, tzinfo=UTC)
    window = bp.send_window(4500, 2500, center, window_mode="centrada")
    assert window.seconds == 1800
    assert window.start == center - timedelta(minutes=15)
    assert window.end == center + timedelta(minutes=15) + timedelta(minutes=5)  # mas la tanda piloto
    assert window.inside_hours
    early = bp.send_window(4500, 2500, datetime(2026, 11, 29, 14, 0, tzinfo=UTC), window_mode="desde_la_hora")
    assert early.start.hour == 14 and not early.inside_hours  # 08:00 de CDMX
    assert bp.send_window(10, 1000, center, window_mode="centrada").pilot_wait == timedelta(0)
    assert bp.country_code_for_time_zone("America/Bogota") == "57"
    assert bp.country_code_for_time_zone("America/Mexico_City") == "52"
    assert bp.country_code_for_time_zone("America/Argentina/Buenos_Aires") == "54"
    assert bp.country_code_for_time_zone("Europe/Madrid") is None


def test_the_campaign_label_is_checked_against_the_live_segments() -> None:
    class Segment:
        label = "black-2026"

        def __init__(self, state: str) -> None:
            self.state = state

        def state_at(self, now: datetime) -> str:
            assert now == datetime.fromisoformat(SCHEDULED)
            return self.state

    assert "está vigente a la hora pedida" in Harness(campaigns=lambda: [Segment("vigente")]).sign_in().page()
    assert "no está vigente a la hora pedida" in Harness(campaigns=lambda: [Segment("vencida")]).sign_in().page()
    assert "No hay una campaña con la etiqueta" in Harness(campaigns=lambda: []).sign_in().page()


def test_the_header_image_is_probed_once_and_its_weight_shown() -> None:
    harness = Harness(image=True).sign_in()
    harness.db.send["header_image_url"] = "https://cdn.example.org/black.jpg"
    page = harness.page()
    assert "image/jpeg · 176 KB, se puede bajar" in page
    harness.page()
    assert harness.images == ["https://cdn.example.org/black.jpg"]  # con cache


def test_an_image_template_without_the_link_says_it() -> None:
    page = Harness(image=True).sign_in().page()
    assert "La plantilla lleva imagen y el masivo no tiene el link" in page


def test_probe_image_reads_type_and_weight_without_trusting_the_link() -> None:
    asyncio.run(_probe_image_cases())


async def _probe_image_cases() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok.png":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x" * 2048)
        if request.url.path == "/gif":
            return httpx.Response(200, headers={"content-type": "image/gif"}, content=b"x")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    assert await bp.probe_image("https://cdn.example.org/ok.png", transport=transport) == bp.ImageCheck(
        True, size=2048, content_type="image/png")
    assert "png o jpeg" in (await bp.probe_image("https://cdn.example.org/gif", transport=transport)).problem
    assert "HTTP 404" in (await bp.probe_image("https://cdn.example.org/no", transport=transport)).problem
    assert "https://" in (await bp.probe_image("http://cdn.example.org/ok.png", transport=transport)).problem


# --- la subida ----------------------------------------------------------------------------------


def test_an_xlsx_goes_through_the_raw_multipart_byte_for_byte() -> None:
    harness = Harness().sign_in()
    data = OPENPYXL_XLSX.read_bytes()
    assert _done(harness.upload(data, filename="lista.xlsx")) == "lista_revisada"
    (_, body), = [call for call in harness.db.bulk_calls if call[0] == "approver_create_bulk_list_v1"]
    expected = read_bulk_list(data, country_code="57", filename="lista.xlsx")
    assert [row["phone"] for row in body["p_list"]["rows"]] == [m.phone for m in expected.members]


def test_ghl_opt_outs_go_up_as_a_second_file_and_reach_the_vetoes() -> None:
    harness = Harness().sign_in()
    bajas = "Phone\n+52 55 0000 0005\n".encode()
    assert _done(harness.upload(bajas=bajas)) == "lista_revisada"
    assert harness.reviewer.calls[0]["ghl"] == ["5215500000005"]
    assert "1 están en las bajas de GHL" in harness.page()


@pytest.mark.parametrize("change, expected", [
    ({"aviso": False}, "notice_not_declared"),
])
def test_without_the_notice_nothing_is_saved(change: dict, expected: str) -> None:
    harness = Harness().sign_in()
    assert _done(harness.upload(**change)) == expected
    assert "approver_create_bulk_list_v1" not in harness.db.names()


def test_other_source_needs_a_note_and_a_bad_source_is_refused() -> None:
    harness = Harness().sign_in()
    assert harness.upload(origen="otra").status_code == 422
    assert harness.upload(origen="comprada").status_code == 422
    assert _done(harness.upload(origen="otra", nota="lista del vivo de octubre")) == "lista_revisada"


def test_another_origin_a_bad_csrf_or_no_session_do_not_upload() -> None:
    harness = Harness().sign_in()
    assert harness.upload(origin="https://otro.example.test").status_code == 403
    assert harness.upload(csrf="0" * 64).status_code == 403
    assert "approver_create_bulk_list_v1" not in harness.db.names()
    logged_out = Harness()
    response = logged_out.upload(csrf="0" * 64)
    assert response.status_code == 401 and "Entrar con Slack" in response.text
    assert logged_out.db.bulk_calls == []


def test_an_outsider_does_not_see_the_page() -> None:
    harness = Harness().sign_in(user=OUTSIDER)
    response = harness.client.get(f"{PATH}/{SEND}")
    assert "Entrar con Slack" in response.text and "att1_carrito" not in response.text


def test_a_file_over_the_cap_is_413_and_reads_nothing_more() -> None:
    harness = Harness(max_upload_bytes=1024).sign_in()
    response = harness.upload(b"Phone\n" + b"3000000001\n" * 10_000)
    assert response.status_code == 413
    assert "approver_create_bulk_list_v1" not in harness.db.names()


def test_a_file_without_a_phone_column_says_the_headers_masked() -> None:
    harness = Harness().sign_in()
    response = harness.upload(b"Nombre;Correo\nAna;ana@example.com\n")
    assert response.status_code == 422
    assert "No se encontró la columna del teléfono" in response.text and "Nombre" in response.text


def test_a_list_with_nobody_valid_is_not_saved() -> None:
    harness = Harness().sign_in()
    assert _done(harness.upload(b"Phone\n601 555\n")) == "empty_list"
    assert "approver_create_bulk_list_v1" not in harness.db.names()


def test_without_the_vetoes_installed_the_list_stays_loaded_and_cannot_be_confirmed() -> None:
    harness = Harness(reviewer=False).sign_in()
    assert _done(harness.upload()) == "lista_sin_revisar"
    page = harness.page()
    assert "todavía no pasó la revisión" in page and 'name="conteo"' not in page
    response = harness.post("confirmar", {"csrf_token": harness.csrf(), "conteo": "6", "confirmo": "si"})
    assert _done(response) == "list_not_vetted"


def test_a_failed_review_is_retried_by_uploading_the_same_file_and_another_file_is_refused() -> None:
    harness = Harness(failures=1).sign_in()
    assert _done(harness.upload()) == "revision_fallida"
    assert "Volver a revisar" in harness.page()
    assert _done(harness.upload()) == "lista_revisada"
    assert harness.db.names().count("approver_create_bulk_list_v1") == 1
    assert len(harness.reviewer.calls) == 2
    assert _done(harness.upload(b"Phone\n3000000009\n")) == "list_already_attached"


def test_the_invalid_rows_come_back_as_csv_from_the_same_file_without_saving_anything() -> None:
    harness = _vetted()
    before = harness.db.names()
    response = harness.upload(path="no-validos.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["fila", "telefono_como_estaba", "motivo"]
    assert ["4", "601 555", "sin código de país"] in rows
    assert ["3", "573000000001", "repetidos (queda la primera fila)"] in rows
    assert ["14", "'+999 1", "con un largo que no es el de un teléfono"] in rows  # sin formula
    assert len(rows) == 10
    assert harness.db.names()[len(before):] == ["approver_read_bulk_send_v1", "approver_read_bulk_send_v1"]


# --- confirmar y frenar ---------------------------------------------------------------------------


@pytest.mark.parametrize("fields, code", [
    ({"conteo": "6", "confirmo": "si"}, "count_mismatch"),
    ({"conteo": "5"}, "confirmation_missing"),
    ({"conteo": "cinco", "confirmo": "si"}, "count_mismatch"),
])
def test_a_wrong_count_or_no_mark_does_not_confirm(fields: dict[str, str], code: str) -> None:
    harness = _vetted()
    assert _done(harness.post("confirmar", {"csrf_token": harness.csrf(), **fields})) == code
    assert harness.db.send["state"] == "borrador"


def test_the_exact_count_and_the_mark_confirm_with_the_health_read_by_the_page() -> None:
    harness = _vetted()
    assert _done(harness.post("confirmar", {"csrf_token": harness.csrf(), "conteo": "5", "confirmo": "si"})) \
        == "confirmado"
    (_, body), = [call for call in harness.db.bulk_calls if call[0] == "approver_confirm_bulk_send_v1"]
    assert body["p_expected_count"] == 5 and body["p_health"] == "verde" and body["p_yellow_ack"] is False
    assert harness.db.send["confirmed_by"] == f"slack:{DAN}"
    page = harness.page()
    assert "Confirmado" in page and 'name="conteo"' not in page and "/frenar" in page


def test_yellow_needs_the_second_mark() -> None:
    harness = _vetted()
    harness.health = _health("YELLOW")
    assert 'name="amarillo"' in harness.page()
    csrf = harness.csrf()
    assert _done(harness.post("confirmar", {"csrf_token": csrf, "conteo": "5", "confirmo": "si"})) \
        == "yellow_ack_missing"
    assert "approver_confirm_bulk_send_v1" not in harness.db.names()
    assert _done(harness.post("confirmar", {"csrf_token": csrf, "conteo": "5", "confirmo": "si",
                                            "amarillo": "si"})) == "confirmado"
    (_, body), = [call for call in harness.db.bulk_calls if call[0] == "approver_confirm_bulk_send_v1"]
    assert body["p_health"] == "amarillo" and body["p_yellow_ack"] is True


@pytest.mark.parametrize("health", [
    _health("RED"),
    _health(puede_enviar="BLOCKED"),
    _health(hours_ago=30),
    {"maribel": _health()["maribel"], "att1": _health()["att1"]},  # sin johanna
    None,
])
def test_red_blocked_stale_or_a_missing_number_of_the_portfolio_does_not_confirm(health: Any) -> None:
    harness = _vetted()
    harness.health = health
    assert "La salud no deja arrancar" in harness.page()
    assert _done(harness.post("confirmar", {"csrf_token": harness.csrf(), "conteo": "5", "confirmo": "si",
                                            "amarillo": "si"})) == "health_blocks"
    assert "approver_confirm_bulk_send_v1" not in harness.db.names()


def test_another_number_blocked_in_the_portfolio_stops_the_start() -> None:
    harness = _vetted()
    health = _health()
    health["att1"]["puede_enviar"] = "BLOCKED"
    harness.health = health
    assert "comparte el portfolio" in harness.page()


def test_brake_by_hand_and_an_unknown_send_is_404() -> None:
    harness = _vetted()
    assert _done(harness.post("frenar", {"csrf_token": harness.csrf()})) == "frenado"
    page = harness.page()
    assert "Frenado: lo frenó una persona" in page and "/frenar" not in page
    assert _done(harness.post("frenar", {"csrf_token": harness.csrf()})) == "not_brakeable"
    assert harness.client.get(f"{PATH}/{OTHER}").status_code == 404
    assert harness.client.get(f"{PATH}/no-es-un-uuid").status_code == 404


def test_another_origin_or_a_bad_csrf_do_not_confirm() -> None:
    harness = _vetted()
    fields = {"csrf_token": harness.csrf(), "conteo": "5", "confirmo": "si"}
    assert harness.post("confirmar", fields, origin="https://otro.example.test").status_code == 403
    assert harness.post("confirmar", {**fields, "csrf_token": "0" * 64}).status_code == 403
    assert harness.db.send["state"] == "borrador"


# --- el reporte -----------------------------------------------------------------------------------


class FakeReport:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def report(self, send_id: str) -> BulkSendReport:
        self.asked.append(send_id)
        outcomes = (
            RecipientOutcome(recipient_id="0", phone="573000000001", recipient_state="sent", conversation_id=10,
                             delivery="read", meta_code=None, replied=True, opted_out=False, handed_off=False,
                             purchased=False),
            RecipientOutcome(recipient_id="1", phone="5215500000005", recipient_state="sent", conversation_id=11,
                             delivery="failed", meta_code="131026", replied=False, opted_out=False,
                             handed_off=False, purchased=False),
        )
        return BulkSendReport(sent=2, delivered=1, read=1, failed=1, awaiting_delivery=0, unreadable=0,
                              failed_by_code=(FailureCount("131026", 1, "No tiene WhatsApp."),), replies=1,
                              opt_outs=0, handoffs=0, purchases=0, not_sent={}, outcomes=outcomes)


def test_after_it_started_the_page_shows_the_report_and_the_undelivered_csv() -> None:
    report = FakeReport()
    harness = _vetted(report=report)
    assert report.asked == []  # antes de arrancar no se lee
    assert harness.client.get(f"{PATH}/{SEND}/no-entregados.csv").status_code == 404
    harness.db.send.update(state="terminado", started_at="2026-11-29T22:59:00Z", finished_at="2026-11-29T23:00:00Z",
                           confirmed_at="2026-11-25T16:20:00Z", confirmed_by=f"slack:{DAN}")
    page = harness.page()
    assert "Mandados 2, entregados 1 (leídos 1), fallidos 1" in page and "1 No tiene WhatsApp." in page
    assert "Bajar los 1 que no recibieron" in page
    response = harness.client.get(f"{PATH}/{SEND}/no-entregados.csv")
    assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["telefono", "conversacion", "estado", "codigo_meta", "explicacion"]
    assert rows[1][:2] == ["5215500000005", "11"] and len(rows) == 2


# --- la pagina entera ------------------------------------------------------------------------------


def test_with_the_audience_section_the_whole_page_still_starts_and_answers() -> None:
    """Una ruta que rompe al arrancar tumba la pagina entera: la de aprobacion, la de audiencia y la del
    masivo, juntas, como las registra el bridge con ``_join_extra_routes``."""
    harness = Harness(with_audience=True)
    paths = {route.path for route in harness.app.routes}  # type: ignore[attr-defined]
    assert {f"{PATH}/{{send_id}}", f"{PATH}/{{send_id}}/subir", f"{PATH}/{{send_id}}/confirmar",
            f"{PATH}/{{send_id}}/frenar", f"{PATH}/{{send_id}}/no-validos.csv",
            f"{PATH}/{{send_id}}/no-entregados.csv", "/flow-approval/envios"} <= paths
    assert "Entrar con Slack" in harness.client.get(f"{PATH}/{SEND}").text
    harness.sign_in()
    assert harness.client.get("/flow-approval/envios").status_code == 200
    assert "att1_carrito_abandonado_01" in harness.page()
    response = harness.client.get(f"{PATH}/{SEND}")
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers["cache-control"] == "no-store, max-age=0"


def test_without_registering_the_section_there_are_no_routes() -> None:
    """Sin ``BULK_SEND_ENABLED`` el bridge no pasa estas rutas: la pagina de aprobacion no las tiene."""
    db, slack, clock = BulkDb(), SlackEmulator(), _Clock()
    config = flow_approval.check_settings(_settings())
    assert config is not None
    app = FastAPI()
    flow_approval.register_flow_approval_routes(
        app, config=config,
        store=flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                          transport=httpx.MockTransport(db.handler)),
        oidc=flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                              transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now))
    client = TestClient(app, base_url=ORIGIN)
    assert client.get(f"{PATH}/{SEND}").status_code == 404


def test_a_multipart_with_an_unknown_field_or_without_the_file_is_refused() -> None:
    harness = Harness().sign_in()
    csrf = harness.csrf()
    response = harness.client.post(
        f"{PATH}/{SEND}/subir", data={"csrf_token": csrf, "origen": "compradoras", "aviso": "si", "otro": "x"},
        files={"archivo": ("lista.csv", GHL_CSV.read_bytes(), "text/csv")}, headers={"Origin": ORIGIN},
        follow_redirects=False)
    assert response.status_code == 422
    response = harness.client.post(
        f"{PATH}/{SEND}/subir", data={"csrf_token": csrf, "origen": "compradoras", "aviso": "si"},
        files={"bajas": ("bajas.csv", b"Phone\n3000000001\n", "text/csv")}, headers={"Origin": ORIGIN},
        follow_redirects=False)
    assert response.status_code == 422
    response = harness.client.post(f"{PATH}/{SEND}/subir", content=urlencode({"csrf_token": csrf}),
                                   headers=FORM, follow_redirects=False)
    assert response.status_code == 422
    assert "approver_create_bulk_list_v1" not in harness.db.names()
