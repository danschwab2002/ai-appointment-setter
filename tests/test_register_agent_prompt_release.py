"""El registrador que mira el perfil donde vive.

Dos propiedades se defienden aca. La primera: el digest del release depende de
las RUTAS y sus hashes, no de los mtimes --- si dependiera del mtime, tocar un
archivo sin cambiarlo inventaria un release nuevo, y el cron que corre seguido
llenaria la tabla de ruido. La segunda: del perfil se guarda el texto completo
del SOUL y SOLO EL HASH del resto, porque `config.yaml` puede llevar
credenciales.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "register_agent_prompt_release.py"
_spec = importlib.util.spec_from_file_location("register_agent_prompt_release", _RUTA)
assert _spec is not None and _spec.loader is not None
registrador = importlib.util.module_from_spec(_spec)
sys.modules["register_agent_prompt_release"] = registrador
_spec.loader.exec_module(registrador)


SOUL = "# Agente comercial\n\nTexto con \"comillas\", acentos aeiou y simbolos $.\n"


def _perfil(tmp_path: Path, *, soul: str = SOUL, con_snapshot: bool = True) -> Path:
    directorio = tmp_path / "agente-comercial"
    directorio.mkdir()
    (directorio / "SOUL.md").write_text(soul, encoding="utf-8")
    (directorio / "config.yaml").write_text(
        "model: glm-5.2\napi_key: no-deberia-viajar\n", encoding="utf-8"
    )
    if con_snapshot:
        (directorio / ".skills_prompt_snapshot.json").write_text(
            '{"skills": []}', encoding="utf-8"
        )
    return directorio


def test_it_observes_every_artifact_of_the_profile(tmp_path: Path) -> None:
    vista = registrador.observar(
        _perfil(tmp_path), registrador.ARTEFACTOS_POR_DEFECTO
    )
    assert set(vista["artifacts"]) == {
        "SOUL.md",
        "config.yaml",
        ".skills_prompt_snapshot.json",
    }
    assert vista["missing"] == []
    assert len(vista["release_digest"]) == 64
    for detalle in vista["artifacts"].values():
        assert len(detalle["sha256"]) == 64
        assert detalle["bytes"] > 0
        assert detalle["modified_at"].endswith("+00:00")


def test_the_release_digest_ignores_the_modification_time(tmp_path: Path) -> None:
    """Tocar un archivo sin cambiarlo no inventa un release nuevo.

    El cron corre seguido; si el digest dependiera del mtime, cada `touch` --- o
    cada reescritura identica que haga Hermes --- crearia una fila y el historial
    de versiones del prompt se volveria ilegible.
    """
    directorio = _perfil(tmp_path)
    antes = registrador.observar(directorio, registrador.ARTEFACTOS_POR_DEFECTO)

    os.utime(directorio / "SOUL.md", (1_800_000_000, 1_800_000_000))
    despues = registrador.observar(directorio, registrador.ARTEFACTOS_POR_DEFECTO)

    assert despues["release_digest"] == antes["release_digest"]
    assert despues["artifacts"]["SOUL.md"]["modified_at"] != antes["artifacts"]["SOUL.md"]["modified_at"]


def test_the_release_digest_changes_when_any_artifact_changes(tmp_path: Path) -> None:
    directorio = _perfil(tmp_path)
    antes = registrador.observar(directorio, registrador.ARTEFACTOS_POR_DEFECTO)

    # No el SOUL: el punto es que el digest cubre TODO lo orbital al prompt.
    (directorio / ".skills_prompt_snapshot.json").write_text(
        '{"skills": ["hermes-agent"]}', encoding="utf-8"
    )
    despues = registrador.observar(directorio, registrador.ARTEFACTOS_POR_DEFECTO)

    assert despues["release_digest"] != antes["release_digest"]


def test_a_missing_artifact_is_reported_and_not_hidden(tmp_path: Path) -> None:
    vista = registrador.observar(
        _perfil(tmp_path, con_snapshot=False), registrador.ARTEFACTOS_POR_DEFECTO
    )
    assert vista["missing"] == [".skills_prompt_snapshot.json"]
    assert ".skills_prompt_snapshot.json" not in vista["artifacts"]


def test_without_the_soul_there_is_no_release(tmp_path: Path) -> None:
    """El SOUL es el prompt: sin el, lo que se registraria no seria un release."""
    directorio = _perfil(tmp_path)
    (directorio / "SOUL.md").unlink()
    with pytest.raises(SystemExit, match="SOUL.md"):
        registrador.observar(directorio, registrador.ARTEFACTOS_POR_DEFECTO)


def test_an_empty_soul_is_rejected(tmp_path: Path) -> None:
    directorio = _perfil(tmp_path, soul="   \n\n")
    with pytest.raises(SystemExit, match="vacio"):
        registrador.observar(directorio, registrador.ARTEFACTOS_POR_DEFECTO)


def test_a_symlinked_artifact_is_refused(tmp_path: Path) -> None:
    """No se sigue un symlink: apuntaria a algo que no es el perfil."""
    directorio = _perfil(tmp_path)
    (directorio / "config.yaml").unlink()
    (directorio / "config.yaml").symlink_to(tmp_path / "otro.yaml")
    with pytest.raises(SystemExit, match="symlink"):
        registrador.observar(directorio, registrador.ARTEFACTOS_POR_DEFECTO)


def test_a_missing_profile_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no existe el directorio"):
        registrador.observar(tmp_path / "no-esta", registrador.ARTEFACTOS_POR_DEFECTO)


def test_only_the_soul_text_travels_never_the_other_artifacts(tmp_path: Path) -> None:
    """`config.yaml` puede llevar credenciales: viaja su hash, nunca su contenido.

    Se comprueba sobre el SQL completo que se manda, no sobre la estructura
    intermedia, porque es ahi donde un descuido se convertiria en un secreto
    escrito en la capa durable.
    """
    vista = registrador.observar(_perfil(tmp_path), registrador.ARTEFACTOS_POR_DEFECTO)
    carga = {
        "tenant_ref": "lancemos",
        "scope_ref": "psicologajohanna-agent-bot-19",
        "profile_name": "agente-comercial",
        "release_digest": vista["release_digest"],
        "artifacts": vista["artifacts"],
        "artifacts_modified_at": vista["artifacts_modified_at"],
        "soul_text": vista["soul_text"],
        "observed_at": "2026-09-28T00:00:00+00:00",
        "registered_by": "test",
    }
    enviado = json.dumps(carga, ensure_ascii=False)

    assert "no-deberia-viajar" not in enviado
    assert "model: glm-5.2" not in enviado
    assert "Agente comercial" in enviado
    assert vista["artifacts"]["config.yaml"]["sha256"] in enviado


def test_the_payload_is_quoted_so_the_prompt_cannot_break_the_sql(tmp_path: Path) -> None:
    """El SOUL tiene comillas y acentos: se cita con una etiqueta aleatoria.

    Interpolar el texto del prompt en SQL a mano seria pedir un escape roto. La
    etiqueta se elige al azar y se verifica que no aparezca en el contenido, asi
    que un SOUL que contenga algo parecido a una cita no puede cerrarla.
    """
    soul_hostil = "$prov$ '; drop table public.agent_prompt_releases; -- \n"
    vista = registrador.observar(
        _perfil(tmp_path, soul=soul_hostil), registrador.ARTEFACTOS_POR_DEFECTO
    )
    capturado: dict[str, object] = {}

    def falso(url: str, cuerpo: bytes, cabeceras: dict[str, str]) -> object:
        capturado["sql"] = json.loads(cuerpo)["query"]
        return [{"resultado": {"outcome": "registered", "release_ordinal": 1}}]

    original = registrador._pedir
    registrador._pedir = falso  # type: ignore[assignment]
    try:
        registrador.por_management(
            "ref-de-prueba",
            "token-de-prueba",
            {
                "tenant_ref": "lancemos",
                "scope_ref": "scope",
                "profile_name": "agente-comercial",
                "release_digest": vista["release_digest"],
                "artifacts": vista["artifacts"],
                "artifacts_modified_at": vista["artifacts_modified_at"],
                "soul_text": vista["soul_text"],
                "observed_at": "2026-09-28T00:00:00+00:00",
                "registered_by": "test",
            },
        )
    finally:
        registrador._pedir = original  # type: ignore[assignment]

    sql = str(capturado["sql"])
    # La etiqueta es unica y no aparece dentro del contenido citado.
    etiqueta = sql.split("$", 2)[1]
    assert etiqueta.startswith("prov")
    assert sql.count(f"${etiqueta}$") == 2
    assert "drop table" in sql  # viaja como DATO, dentro de la cita
    assert "register_agent_prompt_release_v1" in sql


def test_the_answer_of_each_transport_is_unwrapped_the_same_way() -> None:
    """La Management API devuelve filas y PostgREST el jsonb pelado."""
    esperado = {"outcome": "registered", "release_ordinal": 2}
    assert registrador._desanidar(esperado) == esperado
    assert registrador._desanidar([{"resultado": esperado}]) == esperado
    assert registrador._desanidar([esperado]) == esperado
    assert registrador._desanidar("raro")["outcome"] == "unknown"


def test_without_credentials_it_says_which_ones_are_missing(monkeypatch) -> None:
    """Un registrador que no puede escribir tiene que decirlo, no fallar mudo."""
    for nombre in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_PROJECT_REF",
        "SUPABASE_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(nombre, raising=False)
    with pytest.raises(SystemExit, match="faltan credenciales"):
        registrador.registrar({})


def test_the_defaults_match_the_daily_review_scope() -> None:
    """Los defaults son los del inbox de Johanna, no placeholders.

    Es lo que hace que el registro arranque con el redeploy sin cargar ninguna
    variable, y que la procedencia se junte con los lotes de la revision diaria,
    que indexa con ese mismo tenant y scope.
    """
    assert registrador.TENANT_POR_DEFECTO == "lancemos"
    assert registrador.SCOPE_POR_DEFECTO == "psicologajohanna-agent-bot-19"
    assert registrador.PERFIL_POR_DEFECTO.as_posix() == "/opt/data/profiles/agente-comercial"
    assert registrador.ARTEFACTO_CON_TEXTO == "SOUL.md"
