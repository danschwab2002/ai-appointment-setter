"""Tests de scripts/hermes_instancia.py, el instalador y chequeo del Hermes de una instancia (ADR-0025).

Cubren lo que corre afuera del contenedor y las funciones puras de lo que corre adentro. Lo que
necesita un Hermes de verdad (crear el profile, instalar la distribucion, arrancar el gateway,
hablarle) lo prueba hermes/prueba/probar_en_contenedor.sh en el CI, contra la imagen declarada.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hermes_instancia.py"
SPEC = importlib.util.spec_from_file_location("hermes_instancia", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
hi = importlib.util.module_from_spec(SPEC)
sys.modules["hermes_instancia"] = hi
SPEC.loader.exec_module(hi)

MENTIRA_SPEC = importlib.util.spec_from_file_location("modelo_de_mentira", ROOT / "hermes" / "prueba" / "modelo_de_mentira.py")
assert MENTIRA_SPEC is not None and MENTIRA_SPEC.loader is not None
mentira = importlib.util.module_from_spec(MENTIRA_SPEC)
MENTIRA_SPEC.loader.exec_module(mentira)

FIXTURE = ROOT / "tests" / "fixtures" / "hermes" / "instancia-prueba.toml"
DISTRIBUCION = ROOT / "profiles" / "agente-comercial-comun"

# Las diez claves del config.yaml del agente de ATT1, medidas el 2026-10-14 en infra_hermes
# (comparar_config_vivo.py, sin imprimir valores). El molde completado tiene que tener exactamente
# estas: cualquier otra cambia el comportamiento del agente respecto de produccion.
CLAVES_DEL_VIVO = {
    "_config_version",
    "fallback_providers[0].model",
    "fallback_providers[0].provider",
    "known_plugin_toolsets.api_server",
    "model.default",
    "model.provider",
    "platform_toolsets.api_server",
    "platforms.api_server.enabled",
    "platforms.api_server.host",
    "platforms.api_server.port",
}


def _instancia(tmp_path: Path, texto: str) -> Path:
    ruta = tmp_path / "hermes.toml"
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def _plan_profile(**cambios) -> dict:
    base = {
        "modelo": "z-ai/glm-5.3-flash",
        "proveedor": "openrouter",
        "base_url": None,
        "respaldo": [{"modelo": "otro/modelo", "proveedor": "openrouter"}],
        "puerto": 8642,
    }
    base.update(cambios)
    return base


INSTANCIA_MINIMA = """
schema = 1
stack = "setter-x"

[profiles.agente-comercial]
modelo = "m"
proveedor = "openrouter"

[profiles.agente-comercial.variables]
API_SERVER_KEY = { archivo = "secretos/bridge.env", variable = "HERMES_API_KEY" }
"""


# --- la declaracion del producto y la distribucion -------------------------------------------


def test_la_declaracion_del_producto_trae_el_comercial_y_el_automation_expert_con_la_imagen_fija():
    d = hi.cargar_declaracion()
    assert list(d.profiles) == ["agente-comercial", "automation-expert"]
    assert d.imagen.referencia == (
        "nousresearch/hermes-agent@sha256:64923faeae267792bf9bf87fe3b4c4869e35004e360c7df01730ad801b74d524"
    )
    assert d.imagen.version_hermes == "0.21.0"
    comercial = d.profiles["agente-comercial"]
    assert comercial.distribucion == DISTRIBUCION
    assert comercial.puerto == 8642
    assert comercial.le_habla == "bridge"
    assert comercial.herramientas == ()
    assert comercial.obligatorio is True
    ae = d.profiles["automation-expert"]
    assert ae.distribucion == ROOT / "profiles" / "automation-expert"
    assert (ae.puerto, ae.le_habla, ae.herramientas, ae.obligatorio) == (None, "slack", ("automation_expert_read",), False)


def test_la_distribucion_es_nativa_de_hermes_y_solo_trae_el_soul_y_el_config():
    archivos = sorted(p.name for p in DISTRIBUCION.iterdir())
    assert archivos == ["SOUL.md", "config.yaml", "distribution.yaml"]
    assert not any(p.is_symlink() for p in DISTRIBUCION.rglob("*"))
    dist = yaml.safe_load((DISTRIBUCION / "distribution.yaml").read_text(encoding="utf-8"))
    assert dist["name"] == "agente-comercial"
    assert dist["author"] == hi.AUTOR
    assert dist["hermes_requires"] == ">=0.21.0"
    assert dist["distribution_owned"] == ["SOUL.md", "config.yaml"]
    requeridas = {e["name"] for e in dist["env_requires"] if e.get("required")}
    assert requeridas == {"API_SERVER_KEY"}


def test_el_molde_completado_tiene_exactamente_las_claves_del_agente_vivo():
    molde = yaml.safe_load((DISTRIBUCION / "config.yaml").read_text(encoding="utf-8"))
    config = hi.renderizar_config(molde, _plan_profile())
    assert set(hi.aplanar(config)) == CLAVES_DEL_VIVO


def test_el_molde_deja_al_agente_sin_herramientas_ni_otras_plataformas():
    molde = yaml.safe_load((DISTRIBUCION / "config.yaml").read_text(encoding="utf-8"))
    config = hi.renderizar_config(molde, _plan_profile())
    assert config["platform_toolsets"] == {"api_server": []}
    assert config["known_plugin_toolsets"] == {"api_server": ["spotify"]}
    assert config["platforms"] == {"api_server": {"enabled": True, "host": "0.0.0.0", "port": 8642}}
    assert config["model"] == {"default": "z-ai/glm-5.3-flash", "provider": "openrouter"}
    assert config["fallback_providers"] == [{"provider": "openrouter", "model": "otro/modelo"}]
    assert config["_config_version"] == 39


def test_el_molde_solo_lleva_base_url_si_la_instancia_la_declara():
    molde = yaml.safe_load((DISTRIBUCION / "config.yaml").read_text(encoding="utf-8"))
    con = hi.renderizar_config(molde, _plan_profile(proveedor="custom", base_url="http://127.0.0.1:9999/v1", respaldo=[]))
    assert con["model"]["base_url"] == "http://127.0.0.1:9999/v1"
    assert con["fallback_providers"] == []
    sin = hi.renderizar_config({**molde, "model": {**molde["model"], "base_url": "vieja"}}, _plan_profile())
    assert "base_url" not in sin["model"]


def test_renderizar_frena_si_el_molde_no_tiene_lo_que_hay_que_completar():
    with pytest.raises(hi.Freno, match="seccion model"):
        hi.renderizar_config({"platforms": {"api_server": {}}}, _plan_profile())
    with pytest.raises(hi.Freno, match="platforms.api_server"):
        hi.renderizar_config({"model": {}}, _plan_profile())
    with pytest.raises(hi.Freno, match="sin valor: otra"):
        hi.renderizar_config({"model": {}, "platforms": {"api_server": {}}, "otra": None}, _plan_profile())


def test_la_declaracion_rechaza_claves_desconocidas_y_puertos_repetidos(tmp_path):
    (tmp_path / "d").mkdir()
    for archivo in hi.ARCHIVOS_DISTRIBUCION:
        (tmp_path / "d" / archivo).write_text("x", encoding="utf-8")
    base = """
schema = 1
[imagen]
nombre = "n"
etiqueta = "e"
digest = "sha256:%s"
version_hermes = "0.21.0"
[profiles.a-b]
distribucion = "d"
puerto = 8642
le_habla = "bridge"
""" % ("0" * 64)
    ruta = tmp_path / "declaracion.toml"
    ruta.write_text(base + "[profiles.c-d]\ndistribucion = \"d\"\npuerto = 8642\nle_habla = \"bridge\"\n", encoding="utf-8")
    with pytest.raises(hi.Freno, match="ya lo usa otro profile"):
        hi.cargar_declaracion(ruta, tmp_path)
    ruta.write_text(base.replace('le_habla = "bridge"', 'le_habla = "bridge"\nextra = 1'), encoding="utf-8")
    with pytest.raises(hi.Freno, match="claves no soportadas extra"):
        hi.cargar_declaracion(ruta, tmp_path)
    ruta.write_text(base.replace('"d"', '"../d"'), encoding="utf-8")
    with pytest.raises(hi.Freno, match="adentro del repo"):
        hi.cargar_declaracion(ruta, tmp_path)


# --- la declaracion de la instancia y sus secretos -------------------------------------------


def test_la_instancia_de_prueba_carga_y_resuelve_los_secretos_relativos_a_su_archivo():
    inst = hi.cargar_instancia(FIXTURE, hi.cargar_declaracion())
    assert inst.stack == "prueba"
    assert inst.servicio == "hermes"
    p = inst.profiles["agente-comercial"]
    assert (p.modelo, p.proveedor, p.base_url, p.respaldo) == ("prueba/modelo", "custom", "http://127.0.0.1:9999/v1", ())
    assert p.variables["API_SERVER_KEY"] == hi.Secreto(FIXTURE.parent / "secretos" / "bridge.env", "HERMES_API_KEY")


@pytest.mark.parametrize(
    "cambio, mensaje",
    [
        (lambda t: t.replace('proveedor = "openrouter"', 'proveedor = "openrouter"\ntemperatura = 1'), "claves no soportadas temperatura"),
        (lambda t: t.replace("[profiles.agente-comercial]", "[profiles.otro]").replace("profiles.agente-comercial.variables", "profiles.otro.variables"), "falta agente-comercial"),
        (lambda t: t + '\n[profiles.copiloto]\nmodelo = "m"\nproveedor = "p"\n[profiles.copiloto.variables]\n', "copiloto no esta en la declaracion"),
        (lambda t: t.replace("API_SERVER_KEY =", "api_server_key ="), "nombre de variable invalido"),
        (lambda t: t.replace('stack = "setter-x"', 'stack = "Setter X"'), "stack invalido"),
        (lambda t: t.replace("schema = 1", "schema = 2"), "entiende el 1"),
    ],
)
def test_la_instancia_se_valida_estricta(tmp_path, cambio, mensaje):
    ruta = _instancia(tmp_path, cambio(INSTANCIA_MINIMA))
    with pytest.raises(hi.Freno, match=mensaje):
        hi.cargar_instancia(ruta, hi.cargar_declaracion())


def test_los_secretos_se_leen_del_archivo_y_los_errores_no_muestran_valores(tmp_path):
    ruta = _instancia(tmp_path, INSTANCIA_MINIMA)
    inst = hi.cargar_instancia(ruta, hi.cargar_declaracion())
    with pytest.raises(hi.Freno, match="no existe el archivo de secretos"):
        hi.leer_secretos(inst)
    (tmp_path / "secretos").mkdir()
    archivo = tmp_path / "secretos" / "bridge.env"
    archivo.write_text("OTRA=valor-que-no-tiene-que-salir\n", encoding="utf-8")
    with pytest.raises(hi.Freno) as exc:
        hi.leer_secretos(inst)
    assert "no tiene HERMES_API_KEY" in str(exc.value)
    assert "valor-que-no-tiene-que-salir" not in str(exc.value)
    archivo.write_text("# comentario\nexport HERMES_API_KEY='clave-de-prueba'\n", encoding="utf-8")
    assert hi.leer_secretos(inst) == {"agente-comercial": {"API_SERVER_KEY": "clave-de-prueba"}}


def test_tapar_saca_cualquier_secreto_de_un_texto():
    secretos = {"p": {"A": "s3cr3t0", "B": ""}}
    assert hi.tapar("error: s3cr3t0 rechazada", secretos) == "error: *** rechazada"


# --- el archivo de variables del profile -------------------------------------------------------


def test_fusionar_variables_conserva_lo_ajeno_y_dice_solo_los_nombres():
    actual = "# comentario de hermes\nOTRA=queda\nAPI_SERVER_KEY=vieja\nAPI_SERVER_KEY=duplicada\n"
    nuevo, cambian = hi.fusionar_variables(actual, {"API_SERVER_KEY": "nueva", "OPENROUTER_API_KEY": "or"})
    assert cambian == ["API_SERVER_KEY", "OPENROUTER_API_KEY"]
    assert nuevo == "# comentario de hermes\nOTRA=queda\nAPI_SERVER_KEY=nueva\nOPENROUTER_API_KEY=or\n"
    otra_vez, cambian = hi.fusionar_variables(nuevo, {"API_SERVER_KEY": "nueva", "OPENROUTER_API_KEY": "or"})
    assert (otra_vez, cambian) == (nuevo, [])


def test_fusionar_variables_sobre_el_archivo_que_deja_profile_create():
    creado = "# Hermes profile .env\n# Add API keys here\n# or it will inherit keys\n"
    nuevo, cambian = hi.fusionar_variables(creado, {"API_SERVER_KEY": "k"})
    assert cambian == ["API_SERVER_KEY"]
    assert nuevo.startswith(creado)
    assert hi.leer_variables_de_texto(nuevo) == {"API_SERVER_KEY": "k"}


# --- comparar y decidir de quien es un Hermes --------------------------------------------------


def test_diferencias_de_config_dice_rutas_y_nunca_valores():
    vivo = {"model": {"default": "valor-vivo", "extra": 1}, "platforms": {"api_server": {"port": 1}}}
    esperado = {"model": {"default": "valor-esperado"}, "platforms": {"api_server": {"port": 1, "host": "h"}}}
    difs = hi.diferencias_de_config(vivo, esperado)
    assert difs == ["falta platforms.api_server.host", "sobra model.extra", "distinto model.default"]
    assert not any("valor-" in d for d in difs)
    assert hi.diferencias_de_config(esperado, esperado) == []


def test_profiles_presentes_cuenta_como_hermes_y_saltea_default(tmp_path):
    for nombre, con_soul in (("agente-comercial", True), ("default", True), ("sin-soul", False)):
        (tmp_path / nombre).mkdir()
        if con_soul:
            (tmp_path / nombre / "SOUL.md").write_text("x", encoding="utf-8")
    assert hi.profiles_presentes(tmp_path) == ["agente-comercial"]
    assert hi.profiles_presentes(tmp_path / "no-existe") == []


NUESTRA = {"name": "agente-comercial", "author": hi.AUTOR}


@pytest.mark.parametrize(
    "marca, presentes, distribuciones, mensaje",
    [
        ({"instancia": "otra"}, [], {}, "marca de la instancia 'otra'"),
        (None, ["agente-comercial", "client-copilot"], {"agente-comercial": NUESTRA}, "no conoce \\(client-copilot\\)"),
        (None, ["agente-comercial"], {"agente-comercial": None}, "no lo piso"),
        (None, ["agente-comercial"], {"agente-comercial": {"name": "agente-comercial", "author": "otro"}}, "no lo piso"),
    ],
)
def test_verificar_duenio_frena_ante_un_hermes_ajeno(marca, presentes, distribuciones, mensaje):
    with pytest.raises(hi.Freno, match=mensaje):
        hi.verificar_duenio("setter-x", marca, presentes, ["agente-comercial"], distribuciones)


@pytest.mark.parametrize(
    "marca, presentes, distribuciones",
    [
        (None, [], {}),  # un Hermes recien nacido
        (None, ["agente-comercial"], {"agente-comercial": NUESTRA}),  # un aplicar anterior se corto
        ({"instancia": "setter-x"}, ["agente-comercial", "intruso"], {"agente-comercial": None}),  # es nuestro: chequear reporta lo que sobra
    ],
)
def test_verificar_duenio_deja_pasar_un_hermes_propio(marca, presentes, distribuciones):
    hi.verificar_duenio("setter-x", marca, presentes, ["agente-comercial"], distribuciones)


# --- esperar al gateway despues de arrancarlo o reiniciarlo ------------------------------------

# Lineas reales de s6-svstat. Las dos primeras, de un reinicio del gateway en el VPS el 2026-10-14
# (hermes/prueba, modelo de mentira). La tercera, del CI del PR #231 (run 38577495522): el SIGTERM
# del reinicio le llego al gateway mientras terminaba de arrancar, salio con 0 y s6 lo dejo abajo.
S6_ARRIBA = "up (pid 712 pgid 712) 1 seconds, normally down"
S6_REINICIANDO = "down (exitcode 1) 0 seconds, want up, ready 0 seconds"
S6_ABAJO_CON_0 = "down (exitcode 0) 143 seconds, ready 143 seconds"


def test_leer_s6_con_las_lineas_medidas():
    assert hi.leer_s6(S6_ARRIBA) == (712, False, None)
    assert hi.leer_s6(S6_REINICIANDO) == (None, False, "exitcode 1")
    assert hi.leer_s6(S6_ABAJO_CON_0) == (None, True, "exitcode 0")
    assert hi.leer_s6("down (exitcode 78) 2 seconds, ready 2 seconds") == (None, True, "exitcode 78")
    assert hi.leer_s6("") == (None, False, None)


class RelojDeMentira:
    def __init__(self):
        self.ahora = 0.0

    def monotonic(self) -> float:
        return self.ahora

    def sleep(self, segundos: float) -> None:
        self.ahora += segundos


def _gateway_de_mentira(monkeypatch, lineas_s6: list[str], estados: list[dict] | None = None, api=(200, 401)):
    """Un Dentro que ve, en orden, las lineas de s6 y los gateway_state.json dados (el ultimo se repite)."""
    monkeypatch.setattr(hi, "time", RelojDeMentira())
    d = hi.Dentro({"modo": "aplicar", "profiles": {}}, {})
    d.esperado = {"agente-comercial": {"platforms": {"api_server": {"port": 8642}}}}
    lineas, pendientes, llamadas = list(lineas_s6), list(estados or [{}]), []

    def siguiente(lista: list):
        return lista.pop(0) if len(lista) > 1 else lista[0]

    d._estado_s6 = lambda servicio: siguiente(lineas)
    d._estado_gateway = lambda carpeta: siguiente(pendientes)
    d._probar_api = lambda puerto, clave: api
    d._variables = lambda nombre: {"API_SERVER_KEY": "clave-de-prueba"}
    d._hermes = lambda *args: llamadas.append(args)
    return d, llamadas


def test_la_espera_no_se_fia_del_estado_que_dejo_el_proceso_anterior(monkeypatch):
    d, llamadas = _gateway_de_mentira(
        monkeypatch,
        ["up (pid 251 pgid 251) 30 seconds, normally down", S6_REINICIANDO, S6_ARRIBA],
        [
            {"pid": 251, "gateway_state": "running"},  # lo que escribio el proceso anterior
            {"pid": 712, "gateway_state": "starting"},  # el nuevo ya atiende, pero no termino de arrancar
            {"pid": 712, "gateway_state": "running"},
        ],
    )
    d._esperar_gateway("agente-comercial", pid_anterior=251)
    # Cuatro esperas de 2 s: el proceso viejo, el reinicio, el estado que dejo el viejo y el "starting".
    assert hi.time.ahora == 8
    assert llamadas == [] and d.reporte.acciones == []


def test_si_sale_con_0_y_s6_no_lo_levanta_lo_arranca_una_vez(monkeypatch):
    d, llamadas = _gateway_de_mentira(
        monkeypatch,
        ["up (pid 251 pgid 251) 18 seconds, normally down", S6_ABAJO_CON_0, S6_ARRIBA],
        [{"pid": 712, "gateway_state": "running"}],
    )
    d._esperar_gateway("agente-comercial", pid_anterior=251)
    assert llamadas == [("--profile", "agente-comercial", "gateway", "start")]
    assert d.reporte.acciones == [
        "arrancar de nuevo el gateway de agente-comercial: salio con 0 y s6 no lo levanto (down (exitcode 0) 143 seconds)"
    ]


def test_si_vuelve_a_salir_con_0_frena_sin_insistir(monkeypatch):
    d, llamadas = _gateway_de_mentira(monkeypatch, ["up (pid 251 pgid 251) 18 seconds, normally down", S6_ABAJO_CON_0])
    with pytest.raises(hi.Freno, match="quedo apagado y s6 no lo vuelve a levantar"):
        d._esperar_gateway("agente-comercial", pid_anterior=251)
    assert len(llamadas) == 1


def test_un_error_de_configuracion_frena_enseguida_sin_arrancarlo(monkeypatch):
    d, llamadas = _gateway_de_mentira(monkeypatch, ["down (exitcode 78) 1 seconds, ready 1 seconds"])
    with pytest.raises(hi.Freno, match=r"exitcode 78"):
        d._esperar_gateway("agente-comercial")
    assert llamadas == [] and hi.time.ahora == 0


def test_si_se_vence_el_tiempo_el_freno_dice_lo_que_vio(monkeypatch):
    d, _ = _gateway_de_mentira(monkeypatch, ["up (pid 251 pgid 251) 300 seconds, normally down"], [{"pid": 251, "gateway_state": "running"}])
    with pytest.raises(hi.Freno) as freno:
        d._esperar_gateway("agente-comercial", pid_anterior=251, segundos=10)
    assert "s6: up (pid 251" in str(freno.value)
    assert "sin probar, s6 no mostro un proceso nuevo" in str(freno.value)


# --- afuera: el contenedor y la corrida --------------------------------------------------------


class DockerDeMentira:
    def __init__(self, stack="setter-x", ids="abc123def456789", digests=None, reporte=None):
        self.stack = stack
        self.ids = ids
        self.digests = digests if digests is not None else [hi.cargar_declaracion().imagen.referencia]
        self.reporte = reporte or {"hallazgos": [], "acciones": []}
        self.llamadas: list[tuple] = []
        self.entradas: list[str] = []

    def __call__(self, *args, entrada=None, chequear=True):
        self.llamadas.append(args)
        salida = ""
        if args[0] == "ps":
            salida = self.ids
        elif args[0] == "inspect" and "{{.Image}}" in args:
            salida = "sha256:imagen"
        elif args[0] == "inspect":
            salida = f"abc123def456789000|{self.stack}"
        elif args[:2] == ("image", "inspect"):
            salida = json.dumps(self.digests)
        elif args[0] == "exec" and "--dentro" in args:
            self.entradas.append(entrada)
            salida = "ruido de hermes\n" + json.dumps(self.reporte)
        return subprocess.CompletedProcess(args, 0, salida, "")


def _preparar_corrida(tmp_path, monkeypatch, **kwargs):
    ruta = _instancia(tmp_path, INSTANCIA_MINIMA)
    (tmp_path / "secretos").mkdir()
    (tmp_path / "secretos" / "bridge.env").write_text("HERMES_API_KEY=clave-secreta-de-prueba\n", encoding="utf-8")
    falso = DockerDeMentira(**kwargs)
    monkeypatch.setattr(hi, "docker", falso)
    return ruta, falso


def test_encontrar_contenedor_frena_si_no_es_del_stack(tmp_path, monkeypatch):
    ruta, _ = _preparar_corrida(tmp_path, monkeypatch, stack="infra")
    inst = hi.cargar_instancia(ruta, hi.cargar_declaracion())
    with pytest.raises(hi.Freno, match="es del stack 'infra', no de 'setter-x'"):
        hi.encontrar_contenedor(inst)
    with pytest.raises(hi.Freno, match="es del stack 'infra'"):
        hi.encontrar_contenedor(inst, "infra_hermes.1.xyz")


@pytest.mark.parametrize("ids", ["", "uno\ndos"])
def test_encontrar_contenedor_exige_exactamente_uno(tmp_path, monkeypatch, ids):
    ruta, _ = _preparar_corrida(tmp_path, monkeypatch, ids=ids)
    inst = hi.cargar_instancia(ruta, hi.cargar_declaracion())
    with pytest.raises(hi.Freno, match="esperaba un contenedor del servicio setter-x_hermes"):
        hi.encontrar_contenedor(inst)


def test_aplicar_frena_con_otra_imagen_antes_de_copiar_nada(tmp_path, monkeypatch):
    ruta, falso = _preparar_corrida(tmp_path, monkeypatch, digests=["nousresearch/hermes-agent@sha256:" + "1" * 64])
    with pytest.raises(hi.Freno, match="no instalo sobre otra version de Hermes"):
        hi.correr("aplicar", ruta)
    assert not any(a[0] in ("cp", "exec") for a in falso.llamadas)


def test_chequear_reporta_otra_imagen_como_diferencia(tmp_path, monkeypatch, capsys):
    ruta, _ = _preparar_corrida(tmp_path, monkeypatch, digests=[])
    assert hi.correr("chequear", ruta) == hi.SALIDA_DIFERENCIA
    assert "DIFERENCIA la imagen del contenedor no es" in capsys.readouterr().out


def test_las_keys_entran_por_stdin_solo_con_aplicar_y_nunca_se_imprimen(tmp_path, monkeypatch, capsys):
    reporte = {"hallazgos": [{"nivel": "ok", "texto": "eco: clave-secreta-de-prueba"}], "acciones": []}
    ruta, falso = _preparar_corrida(tmp_path, monkeypatch, reporte=reporte)
    assert hi.correr("chequear", ruta) == 0
    assert hi.correr("plan", ruta) == 0
    assert hi.correr("aplicar", ruta) == 0
    chequear, plan, aplicar = (json.loads(e) for e in falso.entradas)
    for entrada in (chequear, plan):
        assert "valores" not in entrada
        assert "clave-secreta-de-prueba" not in json.dumps(entrada)
        assert entrada["huellas"] == {"agente-comercial": {"API_SERVER_KEY": hi.huella("clave-secreta-de-prueba")}}
    assert aplicar["valores"] == {"agente-comercial": {"API_SERVER_KEY": "clave-secreta-de-prueba"}}
    salida = capsys.readouterr().out
    assert "clave-secreta-de-prueba" not in salida
    assert "eco: ***" in salida
    for args in falso.llamadas:
        assert not any("clave-secreta-de-prueba" in str(a) for a in args)


def test_la_corrida_limpia_lo_que_copio_aunque_falle_adentro(tmp_path, monkeypatch):
    ruta, falso = _preparar_corrida(tmp_path, monkeypatch)
    falso.reporte = None
    original = falso.__call__

    def roto(*args, entrada=None, chequear=True):
        if args[0] == "exec" and "--dentro" in args:
            falso.llamadas.append(args)
            return subprocess.CompletedProcess(args, 1, "", "Traceback: algo con clave-secreta-de-prueba")
        return original(*args, entrada=entrada, chequear=chequear)

    monkeypatch.setattr(hi, "docker", roto)
    with pytest.raises(hi.Freno) as exc:
        hi.correr("chequear", ruta)
    assert "se rompio" in str(exc.value)
    assert "clave-secreta-de-prueba" not in str(exc.value)
    assert falso.llamadas[-1][:2] == ("exec", "abc123def456")
    assert falso.llamadas[-1][2:4] == ("rm", "-rf")


@pytest.mark.parametrize(
    "modo, hallazgos, acciones, veredicto, codigo",
    [
        ("plan", [], ["crear"], "VEREDICTO: aplicar haria 1 cambio(s)", 0),
        ("plan", [], [], "VEREDICTO: nada que aplicar", 0),
        ("aplicar", [], [], "VEREDICTO: igual a la declaracion", 0),
        ("aplicar", [{"nivel": "diferencia", "texto": "sobra x"}], [], "VEREDICTO: 1 diferencia(s)", 1),
        ("aplicar", [{"nivel": "freno", "texto": "f"}], ["crear"], "FRENO a mitad de camino", 2),
        ("chequear", [{"nivel": "freno", "texto": "f"}], [], "VEREDICTO: FRENO, no se toco nada", 2),
    ],
)
def test_veredictos_y_codigos_de_salida(tmp_path, monkeypatch, capsys, modo, hallazgos, acciones, veredicto, codigo):
    ruta, _ = _preparar_corrida(tmp_path, monkeypatch, reporte={"hallazgos": hallazgos, "acciones": acciones})
    assert hi.correr(modo, ruta) == codigo
    assert veredicto in capsys.readouterr().out


def test_main_convierte_un_freno_en_salida_2(tmp_path, capsys):
    assert hi.main(["chequear", "--instancia", str(tmp_path / "no-existe.toml")]) == hi.SALIDA_FRENO
    assert "FRENO: no existe" in capsys.readouterr().err


def test_lo_que_corre_afuera_usa_solo_la_libreria_estandar():
    arbol = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    modulos = {
        (n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
        for n in arbol.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert modulos - {"__future__"} <= set(sys.stdlib_module_names)


# --- el modelo de mentira ----------------------------------------------------------------------


def test_el_modelo_de_mentira_cuenta_herramientas_y_reconoce_el_soul():
    soul = "Sos el agente comercial.\n" + "regla " * 100
    entero = {"model": "m", "tools": [{}, {}], "messages": [{"role": "system", "content": "antes\n" + soul + "\ndespues"}]}
    assert mentira.resumir_pedido(entero, soul) == {"modelo": "m", "herramientas": 2, "soul": "entero", "stream": False}
    cortado = {"messages": [{"role": "system", "content": soul[:300]}], "stream": True}
    assert mentira.resumir_pedido(cortado, soul)["soul"] == "cortado"
    sin = {"messages": [{"role": "user", "content": soul}]}
    assert mentira.resumir_pedido(sin, soul)["soul"] == "ausente"
    assert mentira.respuesta({"herramientas": 0, "soul": "entero", "modelo": "m"}) == "MENTIRA herramientas=0 soul=entero modelo=m"


# --- el CI ---------------------------------------------------------------------------------------


def test_el_workflow_corre_la_prueba_de_punta_a_punta_cuando_cambia_hermes():
    flujo = yaml.safe_load((ROOT / ".github" / "workflows" / "hermes.yml").read_text(encoding="utf-8"))
    disparo = flujo.get("on") or flujo.get(True)
    caminos = set(disparo["pull_request"]["paths"])
    assert {"hermes/**", "profiles/**", "scripts/hermes_instancia.py", "tests/fixtures/hermes/**"} <= caminos
    pasos = [p.get("run", "") for p in flujo["jobs"]["instalador"]["steps"]]
    assert any("hermes/prueba/probar_en_contenedor.sh" in p for p in pasos)


# --- el profile de Slack (el Automation Expert) ----------------------------------------------

DISTRIBUCION_AE = ROOT / "profiles" / "automation-expert"
ADMIN = "U0123ABCD"

INSTANCIA_CON_AE = INSTANCIA_MINIMA + """
[profiles.automation-expert]
modelo = "m"
proveedor = "openrouter"
administran = ["%s"]

[profiles.automation-expert.variables]
SLACK_BOT_TOKEN = { archivo = "secretos/automation-expert.variables", variable = "SLACK_BOT_TOKEN" }
""" % ADMIN


def _plan_slack(**cambios) -> dict:
    base = {
        "le_habla": "slack",
        "modelo": "z-ai/glm-5.3-flash",
        "proveedor": "openrouter",
        "base_url": None,
        "respaldo": [],
        "puerto": None,
        "herramientas": ["automation_expert_read"],
        "administran": [ADMIN],
    }
    base.update(cambios)
    return base


def _declaracion_con(tmp_path: Path, extra: str) -> Path:
    (tmp_path / "d").mkdir(exist_ok=True)
    for archivo in hi.ARCHIVOS_DISTRIBUCION:
        (tmp_path / "d" / archivo).write_text("x", encoding="utf-8")
    ruta = tmp_path / "declaracion.toml"
    ruta.write_text(
        """
schema = 1
[imagen]
nombre = "n"
etiqueta = "e"
digest = "sha256:%s"
version_hermes = "0.21.0"
[profiles.a-b]
distribucion = "d"
puerto = 8642
le_habla = "bridge"
""" % ("0" * 64) + extra,
        encoding="utf-8",
    )
    return ruta


@pytest.mark.parametrize(
    "extra, mensaje",
    [
        ('[profiles.c-d]\ndistribucion = "d"\nle_habla = "x"\n', "tiene que ser bridge o slack"),
        ('[profiles.c-d]\ndistribucion = "d"\nle_habla = "slack"\npuerto = 8650\nherramientas = ["t"]\n', "no tiene API server ni puerto"),
        ('[profiles.c-d]\ndistribucion = "d"\nle_habla = "slack"\n', "lista no vacia de toolsets"),
        ('[profiles.c-d]\ndistribucion = "d"\nle_habla = "slack"\nherramientas = []\n', "lista no vacia de toolsets"),
        ('[profiles.c-d]\ndistribucion = "d"\nle_habla = "bridge"\npuerto = 8650\nherramientas = ["t"]\n', "va sin herramientas"),
        ('[profiles.c-d]\ndistribucion = "d"\nle_habla = "slack"\nherramientas = ["t"]\nobligatorio = "no"\n', "true o false"),
    ],
)
def test_la_declaracion_valida_cada_tipo_de_profile(tmp_path, extra, mensaje):
    with pytest.raises(hi.Freno, match=mensaje):
        hi.cargar_declaracion(_declaracion_con(tmp_path, extra), tmp_path)


def test_un_profile_opcional_puede_faltar_en_la_instancia():
    inst = hi.cargar_instancia(FIXTURE, hi.cargar_declaracion())
    assert list(inst.profiles) == ["agente-comercial"]


def test_la_instancia_que_declara_el_automation_expert_tiene_que_decir_quien_administra(tmp_path):
    d = hi.cargar_declaracion()
    inst = hi.cargar_instancia(_instancia(tmp_path, INSTANCIA_CON_AE), d)
    assert inst.profiles["automation-expert"].administran == (ADMIN,)
    sin = INSTANCIA_CON_AE.replace('administran = ["%s"]\n' % ADMIN, "")
    with pytest.raises(hi.Freno, match="faltan administran"):
        hi.cargar_instancia(_instancia(tmp_path, sin), d)
    with pytest.raises(hi.Freno, match="IDs de usuario de Slack"):
        hi.cargar_instancia(_instancia(tmp_path, INSTANCIA_CON_AE.replace(ADMIN, "dan")), d)
    en_comercial = INSTANCIA_MINIMA.replace('proveedor = "openrouter"', 'proveedor = "openrouter"\nadministran = ["%s"]' % ADMIN)
    with pytest.raises(hi.Freno, match="claves no soportadas administran"):
        hi.cargar_instancia(_instancia(tmp_path, en_comercial), d)


def test_el_molde_del_automation_expert_completado_no_deja_nada_en_null():
    molde = yaml.safe_load((DISTRIBUCION_AE / "config.yaml").read_text(encoding="utf-8"))
    config = hi.renderizar_config(molde, _plan_slack())
    assert config["model"] == {"default": "z-ai/glm-5.3-flash", "provider": "openrouter"}
    assert config["slack"]["allow_admin_from"] == [ADMIN]
    assert config["slack"]["group_allow_admin_from"] == [ADMIN]
    assert "platforms" not in config
    assert config["platform_toolsets"]["slack"] == ["automation_expert_read", "no_mcp"]
    assert not [r for r, v in hi.aplanar(config).items() if v is None]
    # El molde en Git no cambia: el instalador completa una copia.
    assert yaml.safe_load((DISTRIBUCION_AE / "config.yaml").read_text(encoding="utf-8"))["slack"]["allow_admin_from"] is None


def test_renderizar_un_profile_de_slack_frena_sin_admins_sin_slack_o_con_api_server():
    molde = yaml.safe_load((DISTRIBUCION_AE / "config.yaml").read_text(encoding="utf-8"))
    with pytest.raises(hi.Freno, match="quien administra"):
        hi.renderizar_config(molde, _plan_slack(administran=[]))
    with pytest.raises(hi.Freno, match="no puede prender platforms.api_server"):
        hi.renderizar_config({**molde, "platforms": {"api_server": {"enabled": True}}}, _plan_slack())
    with pytest.raises(hi.Freno, match="seccion slack"):
        hi.renderizar_config({k: v for k, v in molde.items() if k != "slack"}, _plan_slack())


def test_huella_de_carpeta_ve_el_contenido_y_saltea_los_caches(tmp_path):
    assert hi.huella_de_carpeta(tmp_path / "no-existe") is None
    (tmp_path / "p" / "x").mkdir(parents=True)
    (tmp_path / "p" / "x" / "a.py").write_text("uno", encoding="utf-8")
    antes = hi.huella_de_carpeta(tmp_path / "p")
    (tmp_path / "p" / "x" / "__pycache__").mkdir()
    (tmp_path / "p" / "x" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\0")
    assert hi.huella_de_carpeta(tmp_path / "p") == antes
    (tmp_path / "p" / "x" / "a.py").write_text("dos", encoding="utf-8")
    assert hi.huella_de_carpeta(tmp_path / "p") != antes


def test_inicio_de_proceso_cuenta_los_campos_despues_del_nombre():
    # Un nombre con espacios y parentesis, como los que acepta el kernel: starttime es el campo 22.
    stat = "4242 (hermes (gw) x) S 1 4242 4242 0 -1 4194560 " + " ".join(["0"] * 12) + " 500 0"
    assert hi.inicio_de_proceso(stat, "cpu 1 2 3\nbtime 1000\n", 100) == 1005.0


def test_cambiado_despues():
    assert hi.cambiado_despues(None, [1.0]) is True
    assert hi.cambiado_despues(10.0, [5.0, None, 9.9]) is False
    assert hi.cambiado_despues(10.0, [5.0, 11.5]) is False  # adentro del margen
    assert hi.cambiado_despues(10.0, [5.0, 12.5]) is True


def test_el_paquete_lleva_los_plugins_y_solo_los_profiles_que_la_instancia_declara(tmp_path):
    d = hi.cargar_declaracion()
    solo = tmp_path / "solo"
    solo.mkdir()
    hi.armar_paquete(solo, "/tmp/x", "plan", d, hi.cargar_instancia(FIXTURE, d), False)
    assert sorted(p.name for p in (solo / "profiles").iterdir()) == ["agente-comercial"]

    con = tmp_path / "con"
    con.mkdir()
    inst = hi.cargar_instancia(_instancia(tmp_path, INSTANCIA_CON_AE), d)
    hi.armar_paquete(con, "/tmp/x", "plan", d, inst, False)
    ae = con / "profiles" / "automation-expert"
    assert (ae / "plugins" / "automation-expert-read" / "tools.py").is_file()
    assert not list(ae.rglob("__pycache__"))
    assert hi.huella_de_carpeta(ae / "plugins") == hi.huella_de_carpeta(DISTRIBUCION_AE / "plugins")
    plan = json.loads((con / "plan.json").read_text(encoding="utf-8"))
    p = plan["profiles"]["automation-expert"]
    assert (p["le_habla"], p["puerto"], p["herramientas"], p["administran"]) == ("slack", None, ["automation_expert_read"], [ADMIN])
    assert plan["profiles"]["agente-comercial"]["le_habla"] == "bridge"
