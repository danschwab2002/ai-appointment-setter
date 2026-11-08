"""El cliente cerrado de Meta (src/bridge/meta_templates.py, F-146 parte 3).

Contra un servidor falso (``httpx.MockTransport``) con la forma de la Graph API.
**Regla 2:** la plantilla de la lista es captura real (el catalogo que Chatwoot
copia de Meta, bandeja 11, 2026-10-10) y los valores de ``create_ok`` son los que
Meta contesto a ``meta_plantillas.py`` el 2026-11-03. Los errores son la forma
documentada, sin captura: ``tests/fixtures/meta_graph_templates_documented_20261108.json``
lo declara y queda como deuda para el primer envio real.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bridge import meta_templates
from bridge.meta_templates import MetaTemplatesClient, MetaTemplatesError, body_of, content_of

FIXTURES = Path(__file__).parent / "fixtures"
DOCUMENTED = json.loads((FIXTURES / "meta_graph_templates_documented_20261108.json").read_text(encoding="utf-8"))
CAPTURED = json.loads((FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8"))
TOKEN = "EAAtoken-solo-de-plantillas-" + "x" * 40
WABA = "1234567890123"
TEMPLATES = f"/v23.0/{WABA}/message_templates"


def _client(handler) -> MetaTemplatesClient:
    return MetaTemplatesClient(access_token=TOKEN, waba_id=WABA, graph_version="v23.0",
                               transport=httpx.MockTransport(handler))


def _run(coroutine):
    return asyncio.run(coroutine)


def test_create_is_a_post_with_the_token_in_the_header_and_never_in_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DOCUMENTED["create_ok"])

    template = _run(_client(handler).create({"name": "x_01", "category": "MARKETING"}))
    [request] = seen
    assert request.method == "POST" and request.url.path == TEMPLATES and request.url.host == "graph.facebook.com"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert json.loads(request.content) == {"name": "x_01", "category": "MARKETING"}
    assert (template.template_id, template.status, template.category) == ("4596630157247420", "PENDING", "MARKETING")


def test_find_matches_exact_name_and_language_on_a_real_template_from_the_catalog() -> None:
    real = CAPTURED["message_templates"][0]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fields"] == meta_templates.FIELDS and request.url.params["limit"] == "100"
        return httpx.Response(200, json={"data": [real], "paging": {"cursors": {"before": "a", "after": "b"}}})

    client = _client(handler)
    found = _run(client.find(real["name"], real["language"]))
    assert found is not None and found.template_id == real["id"] and found.status == real["status"]
    assert found.body == body_of(real["components"]) and found.body is not None
    # Otro nombre u otro idioma: no es esa. Meta compara nombre con idioma.
    assert _run(client.find(real["name"][:-1], real["language"])) is None
    assert _run(client.find(real["name"], "pt_BR")) is None


def test_find_walks_the_pages_with_the_cursor_and_never_follows_the_url_meta_sends() -> None:
    real = CAPTURED["message_templates"][0]
    pages = [
        {"data": [], "paging": {"cursors": {"after": "cursor-2"}, "next": "https://evil.example/robar"}},
        {"data": [], "paging": {"cursors": {"after": "cursor-3"}, "next": "https://evil.example/robar"}},
        {"data": [real], "paging": {"cursors": {"after": "fin"}}},
    ]
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=pages[len(seen) - 1])

    assert _run(_client(handler).find(real["name"], real["language"])) is not None
    assert [url.host for url in seen] == ["graph.facebook.com"] * 3
    assert [url.params.get("after") for url in seen] == [None, "cursor-2", "cursor-3"]


def test_a_list_that_cannot_be_read_to_the_end_does_not_say_the_name_is_free() -> None:
    def broken(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": 2, "message": "fallo"}})

    def endless(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "paging": {"cursors": {"after": "x"}, "next": "https://z"}})

    def no_cursor(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "paging": {"next": "https://z"}})

    for handler, code in ((broken, "meta:2"), (endless, "meta_list_too_long"), (no_cursor, "meta_unexpected_answer")):
        with pytest.raises(MetaTemplatesError) as error:
            _run(_client(handler).find("x_01", "es_MX"))
        assert error.value.code == code


@pytest.mark.parametrize(("key", "status", "code"), [
    ("error_expired_token", 401, "meta:190"),
    ("error_permission", 403, "meta:10"),
    ("error_rate_limit", 429, "meta:4"),
    ("error_limit_templates", 400, "meta:2388019"),
    ("error_body_too_long", 400, "meta:100:2388040"),
])
def test_an_error_is_only_its_code_nothing_of_what_meta_wrote_travels(key: str, status: int, code: str) -> None:
    with pytest.raises(MetaTemplatesError) as error:
        _run(_client(lambda _: httpx.Response(status, json=DOCUMENTED[key])).create({"name": "x_01"}))
    assert error.value.code == code
    assert str(error.value) == code
    assert TOKEN not in repr(error.value) and DOCUMENTED[key]["error"]["message"] not in repr(error.value)


def test_a_non_json_error_a_network_failure_and_an_unknown_answer_are_closed_codes() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("sin red", request=None)

    for handler, code in (
            (lambda _: httpx.Response(502, text="<html>puerta</html>"), "meta_http:502"),
            (down, "meta_unreachable"),
            (lambda _: httpx.Response(200, json={"status": "PENDING"}), "meta_unexpected_answer"),
            (lambda _: httpx.Response(200, text="no-json"), "meta_unexpected_answer")):
        with pytest.raises(MetaTemplatesError) as error:
            _run(_client(handler).create({"name": "x_01"}))
        assert error.value.code == code


def test_read_asks_for_one_template_by_id_and_nothing_else() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "1234", "status": "APPROVED", "category": "UTILITY"})

    template = _run(_client(handler).read("1234"))
    assert (seen[0].method, seen[0].url.path) == ("GET", "/v23.0/1234")
    assert template.status == "APPROVED"
    with pytest.raises(MetaTemplatesError):
        _run(_client(handler).read("1234/subscribed_apps"))
    assert len(seen) == 1


def test_the_client_does_not_start_with_a_bad_configuration() -> None:
    with pytest.raises(ValueError):
        MetaTemplatesClient(access_token="", waba_id=WABA)
    with pytest.raises(ValueError):
        MetaTemplatesClient(access_token=TOKEN, waba_id="no-numero")
    with pytest.raises(ValueError):
        MetaTemplatesClient(access_token=TOKEN, waba_id=WABA, graph_version="latest")
    assert TOKEN not in repr(MetaTemplatesClient(access_token=TOKEN, waba_id=WABA))


def test_the_content_is_header_body_footer_and_buttons_without_the_examples() -> None:
    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Aviso"},
        {"type": "BODY", "text": "Hola, {{1}}. Ya está.", "example": {"body_text": [["María"]]}},
        {"type": "FOOTER", "text": "Equipo"},
        {"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": "Pagar", "url": "https://pay.hotmart.com/{{1}}", "example": ["x"]},
            {"type": "QUICK_REPLY", "text": "No más mensajes"}]},
    ]
    assert content_of(components) == (
        ("TEXT", "Aviso"), "Hola, {{1}}. Ya está.", "Equipo",
        (("URL", "Pagar", "https://pay.hotmart.com/{{1}}"), ("QUICK_REPLY", "No más mensajes", None)))
    for other in ("no-lista", [], ["no-mapa"], [{"type": "BODY"}]):
        assert content_of(other) is None


FORBIDDEN_VERBS = {"post", "delete", "put", "patch", "request", "stream", "send"}


def calls_in(source: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str], list[str]]:
    """Las llamadas de ``source``: verbos con cualquier receptor, ``get`` del cliente, rutas y usos de ``getattr``."""
    verbs: list[tuple[str, str]] = []
    http_gets: list[tuple[str, str]] = []
    paths: list[str] = []
    indirect: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function = ""

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.function = node.name
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_VERBS:
                    verbs.append((self.function, node.func.attr))
                elif node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "client":
                    http_gets.append((self.function, node.func.attr))
            elif isinstance(node.func, ast.Name) and node.func.id == "getattr":
                indirect.append(self.function)
            self.generic_visit(node)

        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            text = "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
            if text.startswith("/{}"):
                paths.append(text)
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return verbs, http_gets, paths, indirect


def test_the_module_has_three_calls_one_write_and_no_edit_or_delete() -> None:
    # El guardrail de §4: si alguien suma una ruta o un verbo, esto falla, se llame como se llame el cliente.
    verbs, http_gets, paths, indirect = calls_in(Path(meta_templates.__file__).read_text(encoding="utf-8"))
    assert verbs == [("create", "post")]
    assert sorted(http_gets) == [("find", "get"), ("read", "get")]
    assert sorted(set(paths)) == ["/{}/{}", "/{}/{}/message_templates"]
    assert indirect == []


@pytest.mark.parametrize("code", [
    "async def x(self):\n    async with self._http() as http:\n        await http.delete('/a')\n",
    "async def x(self):\n    await self._http().put('/a')\n",
    "async def x(self, c):\n    await c.request('DELETE', '/a')\n",
    "async def x(self, c):\n    await c.post('/a')\n",
])
def test_the_guardrail_sees_a_forbidden_verb_with_any_receiver(code: str) -> None:
    verbs, _, _, _ = calls_in(code)
    assert verbs == [("x", verbs[0][1])] and verbs[0][1] in FORBIDDEN_VERBS


def test_the_guardrail_sees_a_verb_reached_through_getattr() -> None:
    _, _, _, indirect = calls_in("async def x(self, c):\n    await getattr(c, 'delete')('/a')\n")
    assert indirect == ["x"]
