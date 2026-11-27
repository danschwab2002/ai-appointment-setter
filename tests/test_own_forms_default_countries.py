"""Formularios propios: la lista de paises por defecto del selector (own-forms-v1).

El bug (medido el 27/11/2026 en el embed.js publicado de ATT1, /f/att1-org-b/embed.js):
el formulario no declara ``paises``, el embed llegaba con ``"paises": []``, el selector
no se dibujaba y un telefono de Mexico escrito con sus 10 digitos salia como ``+55`` y
los mismos digitos: un numero de Brasil invalido que nunca llegaba al checkout.

Caso: tests/fixtures/own_forms/att1_org_b_formulario_propio_0858954_20261127.toml, la
copia textual del bloque publicado en setter-instancia-att1 (origin/main 0858954). El
telefono es sintetico (un numero de la Ciudad de Mexico que no es de nadie conocido).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import phonenumbers
import pytest

from bridge.ghl_precheckout_adapter import GhlAdapterRejection
from bridge.ghl_precheckout_adapter import _phone as parse_phone
from bridge.own_forms import DEFAULT_COUNTRIES, render_embed_js
from test_own_forms import att1_with_forms

FIXTURES = Path(__file__).parent / "fixtures"
ORG_B_TOML = FIXTURES / "own_forms" / "att1_org_b_formulario_propio_0858954_20261127.toml"
EMBED_JS = Path(__file__).parents[1] / "src" / "bridge" / "own_forms_assets" / "embed.js"
# Diez digitos de un celular de la Ciudad de Mexico, sin indicativo: como lo escribe
# una persona de Mexico. Sintetico.
MX_TEN_DIGITS = "5512345678"


def published_forms() -> list[dict]:
    return tomllib.loads(ORG_B_TOML.read_text(encoding="utf-8"))["formularios_propios"]


def embed_config(forms: list[dict]) -> dict:
    form = att1_with_forms(forms).own_forms[0]
    script = render_embed_js(form, submit_url="https://bridge.example/f/att1-org-b/enviar")
    match = re.search(r"var CONFIG = (\{.*?\});\n", script, re.S)
    assert match, "embed.js sin la configuracion inyectada"
    return json.loads(match.group(1))


def selected_prefix(config: dict) -> str:
    """El prefijo que manda el embed: el del pais por defecto, o el primero del selector.

    Igual que ``dibujar`` y ``prefijoElegido`` de embed.js: sin selector, ``''``.
    """

    paises = config["paises"]
    if not paises:
        return ""
    chosen = next((p for p in paises if p["iso"] == config["pais_por_defecto"]), paises[0])
    return chosen["prefijo"]


def normalize_with_embed_js(raw: str, prefix: str) -> str:
    """Corre ``normalizarTelefono`` de embed.js, tal cual, en node."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("sin node: la normalizacion de embed.js se prueba solo con node")
    source = EMBED_JS.read_text(encoding="utf-8")
    match = re.search(r"  function normalizarTelefono\(crudo, prefijo\) \{.*?\n  \}\n", source, re.S)
    assert match, "embed.js ya no tiene normalizarTelefono"
    program = match.group(0) + f"process.stdout.write(normalizarTelefono({json.dumps(raw)}, {json.dumps(prefix)}));"
    return subprocess.run([node, "-e", program], capture_output=True, text=True, check=True).stdout


def test_default_countries_start_with_mexico() -> None:
    assert DEFAULT_COUNTRIES[0] == "MX"
    assert len(DEFAULT_COUNTRIES) == len(set(DEFAULT_COUNTRIES)) == 21


def test_published_att1_org_b_embed_carries_the_default_countries() -> None:
    config = embed_config(published_forms())
    assert config["pais_por_defecto"] == "MX"
    assert [p["iso"] for p in config["paises"]] == list(DEFAULT_COUNTRIES)
    assert config["paises"][0] == {"iso": "MX", "nombre": "México", "prefijo": "52"}


def test_empty_paises_list_also_gets_the_default_countries() -> None:
    forms = published_forms()
    forms[0]["paises"] = []
    assert [p["iso"] for p in embed_config(forms)["paises"]] == list(DEFAULT_COUNTRIES)


def test_declared_paises_are_kept_as_declared() -> None:
    forms = published_forms()
    forms[0]["pais"] = "US"
    forms[0]["paises"] = ["US", "MX"]
    config = embed_config(forms)
    assert [p["iso"] for p in config["paises"]] == ["US", "MX"]
    assert config["pais_por_defecto"] == "US"


def test_default_country_outside_the_list_goes_first() -> None:
    forms = published_forms()
    forms[0]["pais"] = "BR"
    config = embed_config(forms)
    assert [p["iso"] for p in config["paises"]] == ["BR", *DEFAULT_COUNTRIES]
    assert selected_prefix(config) == "55"


def test_mexican_ten_digits_leave_the_published_form_as_mexico() -> None:
    config = embed_config(published_forms())
    sent = normalize_with_embed_js(MX_TEN_DIGITS, selected_prefix(config))
    assert sent == "+52" + MX_TEN_DIGITS
    e164, country_code, national, region = parse_phone(sent)
    assert (e164, country_code, national, region) == ("+52" + MX_TEN_DIGITS, "52", MX_TEN_DIGITS, "MX")


def test_the_bug_without_selector_is_an_invalid_brazilian_number() -> None:
    # El control: lo que mandaba el embed sin selector. Documenta por que el arreglo importa.
    sent = normalize_with_embed_js(MX_TEN_DIGITS, "")
    assert sent == "+" + MX_TEN_DIGITS
    assert phonenumbers.region_code_for_country_code(phonenumbers.parse(sent).country_code) == "BR"
    with pytest.raises(GhlAdapterRejection):
        parse_phone(sent)
