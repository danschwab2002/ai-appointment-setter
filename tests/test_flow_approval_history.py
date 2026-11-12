"""El historial de propuestas de la pagina de aprobacion (E-295, migracion 20261103000600).

Los mismos dobles que tests/test_flow_approval_page.py: la app con los clientes
reales, PostgREST y Slack emulados con ``httpx.MockTransport``. Las dos
lecturas nuevas (``approver_list_flow_changes_v1`` y
``approver_read_flow_change_record_v1``) contestan en el emulador con la forma
que fija la migracion; lo que la base hace de verdad (orden, cursor, permisos,
sin el para que en la lista) lo prueba PGlite
(tests/sql/followup_engine/validate_flow_approval_history.mjs). **Regla 2:**
no hay captura de PostgREST contestando estas funciones: no estan desplegadas.
"""

from __future__ import annotations

import re

from test_flow_approval_page import (
    BRAYAN,
    DAN,
    OUTSIDER,
    PROPOSAL,
    RIVAL,
    WELCOME_ID,
    PostgrestEmulator,
    _client,
    _sign_in,
)
from test_flow_approval_writes import _page, _signed

HISTORY = "approver_list_flow_changes_v1"
RECORD = "approver_read_flow_change_record_v1"
VERSION_PATH = f"/flow-approval/p/{PROPOSAL}/v/{WELCOME_ID}/1"
HISTORY_PAGE = "/flow-approval/historial"


def _row(number: int, status: str, **extra: object) -> dict:
    """Una propuesta vieja del historial, con la forma de la lista de la migracion."""
    return {"proposal_id": f"00000000-0000-4000-8000-{number:012d}", "status": status, "effective_status": status,
            "requested_by_kind": "automation_expert", "requested_by_ref": "U0PIDEALAE1",
            "approved_by_kind": None, "approved_by_ref": None, "closed_by_kind": None, "closed_by_ref": None,
            "created_at": f"2026-10-2{number % 7}T12:00:00Z", "validated_at": None, "expires_at": None,
            "approved_at": None, "published_at": None, "closed_at": None, "versions": [],
            "flows": [{"producto": "att1", "flujo": "carrito", "tipo": "version"}], **extra}


def _closed_history(db: PostgrestEmulator) -> None:
    db.extra_history = [
        _row(1, "publicada", approved_by_kind="slack", approved_by_ref=BRAYAN, approved_at="2026-10-21T13:00:00Z",
             published_at="2026-10-21T13:01:00Z",
             versions=[{"flow_id": WELCOME_ID, "producto": "att1", "flujo": "carrito", "version": 2}]),
        _row(2, "descartada", closed_by_kind="slack", closed_by_ref=DAN, closed_at="2026-10-22T13:00:00Z"),
        _row(3, "validada", effective_status="vencida", expires_at="2026-10-24T12:00:00Z"),
    ]


def _published(db: PostgrestEmulator) -> None:
    """La propuesta del link ya publicada: genero la version 1 de la bienvenida."""
    db.add_welcome_in_cohort()
    db.proposal.update(status="publicada", effective_status="publicada", approved_by_kind="slack",
                       approved_by_ref=BRAYAN, approved_at="2026-10-28T02:10:00Z", published_at="2026-10-28T02:11:00Z")
    db.versions = [{"flow_id": WELCOME_ID, "producto": "att1", "flujo": "bienvenida", "version": 1,
                    "published_at": "2026-10-28T02:11:00Z"}]
    db.events += [
        {"event_type": "flow_change_approved", "actor_kind": "slack", "actor_ref": BRAYAN, "reason_code": "ok",
         "generation": None, "created_at": "2026-10-28T02:10:00Z", "producto": None, "flujo": None},
        {"event_type": "flow_change_published", "actor_kind": "slack", "actor_ref": BRAYAN, "reason_code": "ok",
         "generation": 1, "created_at": "2026-10-28T02:11:00Z", "producto": "att1", "flujo": "bienvenida"},
    ]


def _history_section(page: str) -> str:
    match = re.search(r'<section class="panel" id="historial">(.*?)</section>', page, re.S)
    assert match is not None, "la portada no tiene historial"
    return match.group(1)


# ----------------------------------------------------------------- la portada


def test_the_index_has_a_history_button_and_does_not_list_the_history() -> None:
    # Pedido de Dan (2026-11-02): el historial no se ve en la portada; se abre con un boton.
    db = PostgrestEmulator()
    _closed_history(db)
    client, db = _signed(db)
    page = _page(client, "/flow-approval/")
    assert f'<a class="button" href="{HISTORY_PAGE}">Historial</a>' in page
    assert page.index(">Historial</a>") < page.index("Propuestas esperando el sí") < page.index("<h2>Los flujos</h2>")
    assert 'id="historial"' not in page and "descartada por" not in page
    assert not [name for name, _ in db.calls if name == HISTORY]


def test_the_history_page_lists_every_proposal_with_its_state_in_words() -> None:
    db = PostgrestEmulator()
    _closed_history(db)
    client, db = _signed(db)
    page = _page(client, HISTORY_PAGE)
    assert "<h1>Historial de propuestas</h1>" in page and '<a href="/flow-approval/">Aprobación de flujos</a>' in page
    # Solo el historial: ni las que esperan el si como seccion aparte, ni los flujos con sus botones.
    assert "Propuestas esperando el sí" not in page and "<form method=\"post\" action=\"/flow-approval/pause\"" not in page
    history = _history_section(page)
    links = re.findall(r'<a class="row" href="/flow-approval/p/([0-9a-f-]{36})">', history)
    assert links == [PROPOSAL, RIVAL, *[row["proposal_id"] for row in db.extra_history]]
    for words in ("esperando el sí", "publicada", "descartada", "vencida sin publicar"):
        assert f">{words}</span>" in history
    assert "5 propuestas en total." in history
    # Quien la pidio, la aprobo y la descarto, por su ID de Slack; uno mismo, como «vos».
    assert "a pedido de <code>U0PIDEALAE1</code>" in history
    assert f"aprobada por <code>{BRAYAN}</code> por Slack el 21/10/2026 07:00" in history
    assert f"descartada por vos (<code>{DAN}</code>) el 22/10/2026 07:00" in history
    assert "(att1/carrito versión 2)" in history
    # La hora es la de la instancia (America/Mexico_City), no la UTC.
    assert "UTC" not in history
    [call] = [body for name, body in db.calls if name == HISTORY]
    assert call["p_before_proposal_id"] is None and call["p_limit"] == 20


def test_the_history_pages_by_cursor_with_see_more() -> None:
    db = PostgrestEmulator()
    db.extra_history = [_row(index, "descartada", closed_by_kind="operador", closed_by_ref="dan",
                             closed_at="2026-10-22T13:00:00Z") for index in range(1, 24)]
    client, db = _signed(db)
    first = _history_section(_page(client, HISTORY_PAGE))
    shown = re.findall(r'<a class="row" href="/flow-approval/p/([0-9a-f-]{36})">', first)
    assert len(shown) == 20 and "Volver a las más nuevas" not in first
    more = re.search(r'<a class="button" href="(/flow-approval/historial\?desde=([0-9a-f-]{36}))">Ver más</a>', first)
    assert more is not None and more.group(2) == shown[-1]
    second = _history_section(_page(client, more.group(1)))
    rest = re.findall(r'<a class="row" href="/flow-approval/p/([0-9a-f-]{36})">', second)
    assert len(rest) == 5 and not set(rest) & set(shown) and "Ver más" not in second
    assert "Volver a las más nuevas" in second and "el operador <code>dan</code>" in second
    assert [body["p_before_proposal_id"] for name, body in db.calls if name == HISTORY] == [None, shown[-1]]


def test_a_cursor_that_is_not_a_proposal_of_the_instance_is_404() -> None:
    client, db = _signed()
    assert client.get(f"{HISTORY_PAGE}?desde=no-es-un-uuid").status_code == 404
    assert client.get(f"{HISTORY_PAGE}?desde=00000000-0000-4000-8000-000000000999").status_code == 404


def test_if_the_history_does_not_answer_the_page_says_so() -> None:
    # Sin la migracion, PostgREST contesta 404 (PGRST202): la pagina sale y lo dice; la portada no la necesita.
    db = PostgrestEmulator()
    db.history_down = "PGRST202"
    client, db = _signed(db)
    assert "El historial no contestó" in _page(client, HISTORY_PAGE)
    assert "Propuestas esperando el sí" in _page(client, "/flow-approval/")


# ----------------------------------------------------------------- quien lo ve


def test_without_a_session_or_outside_the_list_nobody_reads_the_history() -> None:
    client, db, slack, clock = _client()
    for path in (HISTORY_PAGE, f"{HISTORY_PAGE}?desde=00000000-0000-4000-8000-000000000001", VERSION_PATH):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 401 and "Entrar con Slack" in response.text
    response = _sign_in(client, slack, clock, user=OUTSIDER)
    assert response.status_code == 403
    for path in (HISTORY_PAGE, VERSION_PATH, f"/flow-approval/p/{PROPOSAL}"):
        assert client.get(path, follow_redirects=False).status_code == 401
    assert not [name for name, _ in db.calls if name in (HISTORY, RECORD)]


# ----------------------------------------------------------------- el detalle


def test_an_open_proposal_keeps_its_buttons_and_shows_its_timeline() -> None:
    client, db = _signed()
    page = _page(client)
    assert "Aprobar y publicar" in page and "Descartar este pedido" in page and "Tu decisión" in page
    assert "<h2>Lo que pasó</h2>" in page and "Se guardó el borrador" in page and "Se validó" in page
    assert page.index("Se guardó el borrador") < page.index("Se validó")
    assert "Cómo terminó" not in page
    assert f'<a href="{HISTORY_PAGE}">Volver al historial</a>' in page


def test_a_published_proposal_is_read_only_and_links_to_the_version_it_made() -> None:
    db = PostgrestEmulator()
    _published(db)
    client, db = _signed(db)
    page = _page(client)
    assert "Cómo terminó" in page and "ya está publicada" in page and "Tu decisión" not in page
    assert f'<a href="{HISTORY_PAGE}">Volver al historial</a>' in page
    # Ni aprobar ni descartar la propuesta.
    assert '<form method="post" action="/flow-approval/p/' not in page
    assert "Aprobar y publicar" not in page and "Descartar este pedido" not in page
    # E-486 (8b): el flujo sigue en cohorte: se suma un telefono (y se pausa)
    # desde la propuesta publicada, y el POST vuelve a esta propuesta.
    assert 'action="/flow-approval/cohort"' in page and 'action="/flow-approval/pause"' in page
    assert "Sumar un teléfono a la cohorte de bienvenida" in page
    assert f'name="p" value="{PROPOSAL}"' in page
    assert f'<a href="{VERSION_PATH}">att1/bienvenida, versión 1</a>' in page
    assert f"Aprobada por <code>{BRAYAN}</code> por Slack" in page
    assert "Se publicó · att1/bienvenida" in page
    # La rival abierta del mismo flujo no se avisa en una cerrada.
    assert "Hay otra propuesta abierta" not in page


def test_a_published_proposal_whose_flow_is_on_shows_no_cohort_form() -> None:
    # E-486 (8b): la cohorte solo con el flujo en cohorte.
    db = PostgrestEmulator()
    _published(db)
    for flow in db.flows:
        flow["state"] = "prendido"
    client, db = _signed(db)
    page = _page(client)
    assert 'action="/flow-approval/cohort"' not in page
    assert 'action="/flow-approval/pause"' in page


def test_a_discarded_proposal_with_its_flow_in_cohort_still_has_no_buttons() -> None:
    db = PostgrestEmulator()
    db.add_welcome_in_cohort()
    db.proposal.update(status="descartada", effective_status="descartada", closed_by_kind="slack",
                       closed_by_ref=DAN, closed_at="2026-10-28T03:00:00Z", close_reason="ya no va")
    client, db = _signed(db)
    page = _page(client)
    assert 'action="/flow-approval/cohort"' not in page and 'action="/flow-approval/pause"' not in page


def test_a_discarded_proposal_shows_who_and_why_escaped() -> None:
    db = PostgrestEmulator()
    db.proposal.update(status="descartada", effective_status="descartada", closed_by_kind="slack",
                       closed_by_ref=DAN, closed_at="2026-10-28T03:00:00Z", close_reason="<script>x</script> ya no va")
    db.events.append({"event_type": "flow_change_discarded", "actor_kind": "slack", "actor_ref": DAN,
                      "reason_code": "ok", "generation": None, "created_at": "2026-10-28T03:00:00Z", "producto": None,
                      "flujo": None})
    client, db = _signed(db)
    page = _page(client)
    assert "Esta propuesta está <strong>descartada</strong>: es solo lectura." in page
    assert f"Descartada por vos (<code>{DAN}</code>)" in page
    assert "&lt;script&gt;x&lt;/script&gt; ya no va" in page and "<script>" not in page
    assert '<form method="post" action="/flow-approval/p/' not in page and "Lo que generó" not in page


def test_an_expired_proposal_is_read_only() -> None:
    db = PostgrestEmulator()
    db.proposal["effective_status"] = "vencida"
    client, db = _signed(db)
    page = _page(client)
    assert "<strong>vencida sin publicar</strong>: es solo lectura." in page
    assert '<form method="post" action="/flow-approval/p/' not in page


def test_if_the_record_does_not_answer_the_detail_still_shows_the_proposal() -> None:
    db = PostgrestEmulator()
    db.record_down = "PGRST202"
    client, db = _signed(db)
    page = _page(client)
    assert "Aprobar y publicar" in page and "<h2>Lo que pasó</h2>" not in page


# ----------------------------------------------------------------- la version que genero


def test_the_version_page_shows_the_flow_version_the_proposal_published() -> None:
    db = PostgrestEmulator()
    _published(db)
    client, db = _signed(db)
    page = _page(client, VERSION_PATH)
    assert "versión 1</h1>" in page and "es la versión de hoy" in page
    assert f'<a href="/flow-approval/p/{PROPOSAL}">La propuesta que la publicó</a>' in page
    assert "Lo que recibe la persona" in page and "<form" not in page.split("</div></div>", 1)[1]
    # Una version que esta propuesta no publico, o un link mal armado, no muestra nada.
    for path in (f"/flow-approval/p/{PROPOSAL}/v/{WELCOME_ID}/2", f"/flow-approval/p/{PROPOSAL}/v/{RIVAL}/1",
                 f"/flow-approval/p/{PROPOSAL}/v/{WELCOME_ID}/0", f"/flow-approval/p/{PROPOSAL}/v/{WELCOME_ID}/uno",
                 f"/flow-approval/p/{RIVAL}/v/{WELCOME_ID}/1"):
        assert client.get(path).status_code == 404, path


def test_the_version_page_says_when_the_flow_already_runs_another_version() -> None:
    db = PostgrestEmulator()
    _published(db)
    welcome = next(flow for flow in db.flows if flow["flow_id"] == WELCOME_ID)
    welcome["versions"]["2"] = {**welcome["versions"]["1"], "version": 2}
    welcome["active_version"] = 2
    client, db = _signed(db)
    assert "ya no es la de hoy: hoy corre la versión 2" in _page(client, VERSION_PATH)


def test_with_the_flag_off_the_history_and_the_version_do_not_exist() -> None:
    from test_flow_approval_page import ORIGIN, _NoSupabase, _settings  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from bridge.app import create_app  # noqa: PLC0415

    app = create_app(_settings(flow_approval_enabled=False), supabase_client=_NoSupabase())  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    for path in (HISTORY_PAGE, VERSION_PATH):
        assert client.get(path, follow_redirects=False).status_code == 404
