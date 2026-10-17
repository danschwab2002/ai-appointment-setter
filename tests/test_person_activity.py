"""Lo que hizo la persona, en la entrada del agente (varios productos, paso 2b).

Decision de Dan del 2026-10-16 «el agente recibe todo el conocimiento y decide
el producto»: el agente infiere de que producto se habla con la conversacion y
con lo que hizo la persona, que le pasa el bridge.

Fixture: tests/fixtures/att1_person_activity_20261016.json, el cuerpo de
get_portable_person_activity_v1 corrido en solo lectura contra att1-db sobre una
persona real por cada combinacion de actividades. La consulta en si se prueba
contra la base en tests/sql/followup_engine/validate_portable_person_activity.mjs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from bridge.app import agent_products_context, person_activity_context
from bridge.instance_manifest import Product
from bridge.supabase import (
    PERSON_ACTIVITY_KINDS,
    PersonActivity,
    SupabaseClient,
    SupabaseError,
)
from test_hotmart import _config
from test_product_label import _by_product
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    WA_ID,
    _manifest_settings,
    _process,
    _Supabase,
    _text_webhook,
)

CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "att1_person_activity_20261016.json").read_text(
        encoding="utf-8"
    )
)


def _person(number: int) -> dict:
    [person] = [item for item in CAPTURE["people"] if item["person"] == number]
    return person


def _rows(number: int) -> list[dict[str, str]]:
    return [dict(row) for row in _person(number)["rows"]]


def _activities(number: int) -> tuple[PersonActivity, ...]:
    return tuple(
        PersonActivity(
            activity=row["activity"],
            occurred_at=datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")),
        )
        for row in _rows(number)
    )


def test_the_capture_covers_every_activity_the_function_returns() -> None:
    seen = {row["activity"] for person in CAPTURE["people"] for row in person["rows"]}

    assert seen == PERSON_ACTIVITY_KINDS
    # Medido: nadie paso de 7 cosas en 30 dias, lejos del tope de 20.
    assert max(len(person["rows"]) for person in CAPTURE["people"]) == 7


# ------------------------------------------------------------- el cliente


def _client(handler) -> SupabaseClient:
    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )


def test_the_lookup_sends_the_binding_and_the_wa_id() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json=_rows(2))

    config = _config()
    activities = _run_lookup(_client(handler), config=config, whatsapp_phone=WA_ID)

    assert observed["path"] == "/rest/v1/rpc/get_portable_person_activity_v1"
    assert observed["body"] == {
        "p_tenant_ref": config.tenant_ref,
        "p_funnel_ref": config.funnel_ref,
        "p_whatsapp_phone": WA_ID,
    }
    assert activities == _activities(2)


def _run_lookup(client: SupabaseClient, **kwargs: object):
    import asyncio

    return asyncio.run(client.get_portable_person_activity(**kwargs))


def test_no_rows_means_no_activity() -> None:
    activities = _run_lookup(
        _client(lambda request: httpx.Response(200, json=[])),
        config=_config(),
        whatsapp_phone=WA_ID,
    )

    assert activities == ()


@pytest.mark.parametrize(
    "row",
    [
        {"activity": "form", "occurred_at": "2026-10-16T15:08:47Z", "phone": "1"},
        {"activity": "refund", "occurred_at": "2026-10-16T15:08:47Z"},
        {"activity": ["form"], "occurred_at": "2026-10-16T15:08:47Z"},
        {"activity": "form", "occurred_at": None},
        {"activity": "form", "occurred_at": "ayer"},
        {"activity": "form", "occurred_at": "2026-10-16T15:08:47"},
    ],
)
def test_an_unexpected_row_is_an_error_not_a_context(row: dict[str, object]) -> None:
    with pytest.raises(SupabaseError):
        _run_lookup(
            _client(lambda request: httpx.Response(200, json=[row])),
            config=_config(),
            whatsapp_phone=WA_ID,
        )


# ------------------------------------------------------------- el contexto


def test_the_agent_gets_each_activity_with_its_product_and_time() -> None:
    assert person_activity_context(_activities(4), product="att1") == [
        {"activity": "form", "product": "att1", "at": "2026-10-11T23:59:19Z"},
        {"activity": "cart", "product": "att1", "at": "2026-10-12T00:41:36Z"},
        {"activity": "payment_rejected", "product": "att1", "at": "2026-10-14T19:47:49Z"},
        {"activity": "purchase", "product": "att1", "at": "2026-10-14T19:48:18Z"},
    ]


def test_the_activity_goes_oldest_first_whatever_the_order_it_came_in() -> None:
    shuffled = tuple(reversed(_activities(1)))

    context = person_activity_context(shuffled, product="att1")

    assert [item["at"] for item in context] == sorted(item["at"] for item in context)
    assert len(context) == 7


def test_the_activity_carries_nothing_but_what_when_and_which_product() -> None:
    for person in CAPTURE["people"]:
        for item in person_activity_context(_activities(person["person"]), product="att1"):
            assert set(item) == {"activity", "product", "at"}


# ------------------------------------------------------ el turno del agente


class _ActivitySupabase(_Supabase):
    def __init__(
        self,
        *,
        activities: tuple[PersonActivity, ...] = (),
        activity_error: bool = False,
        identities: tuple[str, ...] = (),
    ) -> None:
        super().__init__(identities=identities)
        self.activities = activities
        self.activity_error = activity_error
        self.activity_lookups: list[dict[str, object]] = []

    async def get_portable_person_activity(
        self, **kwargs: object
    ) -> tuple[PersonActivity, ...]:
        self.activity_lookups.append(kwargs)
        if self.activity_error:
            raise SupabaseError("portable_person_activity_get_failed: HTTP 503")
        return self.activities


def _products_settings(tmp_path: Path):
    """El manifiesto de prueba de ATT1 con [[productos]] y el conocimiento por producto."""
    settings = _manifest_settings(tmp_path)
    manifest = settings.instance_manifest
    assert manifest is not None
    return replace(
        settings,
        instance_manifest=replace(manifest, products=(Product(key="att1"),)),
        commercial_knowledge=_by_product("att1"),
    )


def _turn(tmp_path: Path, supabase: _ActivitySupabase, *, settings=None):
    webhook, history = _text_webhook("Hola, quiero saber más del programa")
    return _process(tmp_path, supabase, settings=settings, webhook=webhook, history=history)


def test_the_agent_turn_carries_the_products_and_what_the_person_did(
    tmp_path: Path,
) -> None:
    supabase = _ActivitySupabase(activities=_activities(2))
    settings = _products_settings(tmp_path)

    _, shadow, _ = _turn(tmp_path, supabase, settings=settings)

    [lookup] = supabase.activity_lookups
    assert lookup["whatsapp_phone"] == WA_ID
    assert lookup["config"] == settings.commercial_ally_config
    context = shadow.calls[0][1]
    assert context["products"] == [
        {"key": "att1", "name": settings.commercial_knowledge.products[0].offer_name}
    ]
    assert context["person_activity"] == person_activity_context(
        _activities(2), product="att1"
    )


def test_the_product_name_is_the_one_the_knowledge_renders(tmp_path: Path) -> None:
    settings = _products_settings(tmp_path)

    [product] = agent_products_context(settings)

    assert f"# Producto: {product['name']}" in settings.commercial_knowledge.render()


def test_the_lookup_uses_the_identity_the_form_stored(tmp_path: Path) -> None:
    # 52 + 10 del formulario; la base compara las dos formas.
    supabase = _ActivitySupabase(activities=_activities(8), identities=(FORM_ID,))

    _turn(tmp_path, supabase, settings=_products_settings(tmp_path))

    assert [lookup["whatsapp_phone"] for lookup in supabase.activity_lookups] == [FORM_ID]


def test_without_activity_the_agent_still_gets_the_products(tmp_path: Path) -> None:
    supabase = _ActivitySupabase(activities=())

    _, shadow, _ = _turn(tmp_path, supabase, settings=_products_settings(tmp_path))

    assert len(supabase.activity_lookups) == 1
    assert "products" in shadow.calls[0][1]
    assert "person_activity" not in shadow.calls[0][1]


def test_a_failed_lookup_does_not_stop_the_reply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _ActivitySupabase(activity_error=True)

    with caplog.at_level(logging.WARNING):
        chatwoot, shadow, _ = _turn(
            tmp_path, supabase, settings=_products_settings(tmp_path)
        )

    assert len(shadow.calls) == 1
    assert "person_activity" not in shadow.calls[0][1]
    assert chatwoot.reply_calls
    assert "person_activity_lookup_failed" in caplog.text
    assert WA_ID not in caplog.text


def test_without_productos_the_context_is_as_before(tmp_path: Path) -> None:
    supabase = _ActivitySupabase(activities=_activities(2))

    _, shadow, _ = _turn(tmp_path, supabase)

    assert supabase.activity_lookups == []
    assert "products" not in shadow.calls[0][1]
    assert "person_activity" not in shadow.calls[0][1]


def test_the_time_goes_in_utc_whatever_zone_it_came_in() -> None:
    from datetime import timedelta, timezone

    cdmx = timezone(timedelta(hours=-6))
    [item] = person_activity_context(
        (PersonActivity(activity="cart", occurred_at=datetime(2026, 10, 16, 9, 51, 1, 500, tzinfo=cdmx)),),
        product="att1",
    )

    assert item["at"] == "2026-10-16T15:51:01Z"
    assert datetime.fromisoformat(item["at"].replace("Z", "+00:00")).tzinfo == UTC


def test_the_link_purchase_migration_keeps_the_shape_the_bridge_reads() -> None:
    # 20261016000300 cambia solo el cuerpo: la misma firma, las mismas dos
    # columnas que valida get_portable_person_activity y ninguna funcion nueva.
    migrations = Path(__file__).parents[1] / "supabase" / "migrations"
    sql = (migrations / "20261016000300_portable_person_activity_link_purchase_v1.sql").read_text(
        encoding="utf-8"
    )

    assert "create function" not in sql
    assert sql.count("create or replace function public.get_portable_person_activity_v1(") == 1
    assert (
        "    p_tenant_ref text,\n    p_funnel_ref text,\n    p_whatsapp_phone text\n"
        ") returns table (\n    activity text,\n    occurred_at timestamptz\n)"
    ) in sql
    assert "-- person_activity: link purchase" in sql
    assert "where issuance.status = 'purchase_matched'" in sql
    assert "and intent.funnel_ref = p_funnel_ref" in sql
    # La compra que tambien paso por el correlador portable no se cuenta dos veces.
    assert "from public.portable_hotmart_purchase_correlations correlation\n" in sql
    assert "where correlation.webhook_event_id = event.id" in sql
