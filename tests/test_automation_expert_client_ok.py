"""automation-expert-read-v1 1.4.0 (F-151): el permiso de WhatsApp de la aliada, declarado una vez.

El AE le preguntaba el ``ok_del_cliente`` (quien, medio, fecha) al dueño en cada flujo con
permiso de compra. Ahora se declara en el manifiesto de la instancia
(``[consentimiento].ok_del_cliente``) y ``list_flows`` lo trae en ``data.client_ok``, con la
forma exacta del ``ok_del_cliente`` de un item, para que el agente lo copie sin preguntar.
El valor de Maribel es el que dio Dan el 01/11.
"""

from __future__ import annotations

from dataclasses import replace
import datetime
from pathlib import Path
import tomllib

import pytest

from bridge.flow_definition import check_client_ok
from bridge.instance_manifest import InstanceManifest, ManifestError
from test_automation_expert_native_reads import _flows_body, _reader
from test_automation_expert_read_app import AUTH, _client, _manifest

INSTANCES = Path(__file__).parent / "fixtures" / "instances"
MARIBEL_OK = {
    "quien": "los clientes de Maribel al llenar el formulario de la landing",
    "medio": "checkbox de consentimiento de WhatsApp del formulario de la landing",
    "fecha": "2026-09-01",
}


def _maribel(ok: object | None) -> dict[str, object]:
    payload = tomllib.loads((INSTANCES / "maribel" / "instancia.toml").read_text())
    if ok is not None:
        payload["consentimiento"]["ok_del_cliente"] = ok
    return payload


def test_without_the_field_the_manifest_loads_as_before() -> None:
    for name in ("att1", "maribel", "johanna"):
        manifest = InstanceManifest.from_toml_file(INSTANCES / name / "instancia.toml")
        assert manifest.client_ok is None


def test_the_declared_ok_loads_with_the_shape_of_an_item() -> None:
    manifest = InstanceManifest.from_mapping(_maribel(MARIBEL_OK))
    assert dict(manifest.client_ok or {}) == MARIBEL_OK
    # La misma forma que mira la base en un item: copiado tal cual, no lo frena nada.
    assert check_client_ok(dict(manifest.client_ok or {}), path=(0, "ok_del_cliente")) == ()


def test_a_toml_date_without_quotes_is_also_accepted() -> None:
    manifest = InstanceManifest.from_mapping(_maribel({**MARIBEL_OK, "fecha": datetime.date(2026, 9, 1)}))
    assert (manifest.client_ok or {})["fecha"] == "2026-09-01"


@pytest.mark.parametrize("ok", [
    {"quien": "Maribel", "medio": "WhatsApp"},
    {**MARIBEL_OK, "fecha": "2026-13-01"},
    {**MARIBEL_OK, "quien": "q" * 121},
    {**MARIBEL_OK, "otro": "x"},
    "Maribel por WhatsApp",
])
def test_an_ok_the_database_would_reject_does_not_load(ok: object) -> None:
    with pytest.raises(ManifestError, match="consentimiento.ok_del_cliente"):
        InstanceManifest.from_mapping(_maribel(ok))


def test_list_flows_says_null_when_it_is_not_declared() -> None:
    body = _flows_body(_reader())
    assert body["contract_version"] == "1.4.0"
    assert body["data"]["client_ok"] is None


def test_list_flows_brings_the_declared_ok_also_for_one_flow() -> None:
    manifest = replace(_manifest(), client_ok=MARIBEL_OK)
    client = _client(_reader(), instance_manifest=manifest)
    for path in ("/internal/automation-expert/v1/flows", "/internal/automation-expert/v1/flows/carrito"):
        response = client.get(path, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["client_ok"] == MARIBEL_OK
