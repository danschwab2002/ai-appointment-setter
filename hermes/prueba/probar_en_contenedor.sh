#!/bin/sh
# Prueba de punta a punta de scripts/hermes_instancia.py contra Hermes descartables (ADR-0025).
#
# Corre en el CI (.github/workflows/hermes.yml) y en cualquier servidor con docker y python3 >= 3.11:
#
#     sh hermes/prueba/probar_en_contenedor.sh
#
# Levanta dos contenedores con la imagen que declara el producto, sin red y con tope de memoria:
# el Hermes de una instancia de prueba (stack "prueba") y el de otra instancia (stack "otra"), con
# un agente-comercial que no es nuestro. El modelo es el de mentira, adentro del mismo contenedor.
# Prueba el camino verde (plan, aplicar, chequear, hablarle, aplicar otra vez sin cambios) y los
# rojos a proposito (un byte de mas en el SOUL, un profile de mas, el default prendido, un Hermes
# ajeno). Al final busca que la key de prueba no haya salido en ninguna salida. Borra todo al irse.
set -eu

RAIZ=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
INSTALADOR="$RAIZ/scripts/hermes_instancia.py"
IMAGEN=$(python3 -c 'import sys, tomllib; d = tomllib.load(open(sys.argv[1], "rb"))["imagen"]; print(d["nombre"] + "@" + d["digest"])' "$RAIZ/hermes/declaracion.toml")
ID=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')
C="prueba-hermes-$ID"
AJENO="prueba-ajeno-$ID"
TMP=$(mktemp -d)
SALIDAS="$TMP/salidas"
mkdir -p "$SALIDAS" "$TMP/secretos"

limpiar() {
  docker rm -f "$C" "$AJENO" >/dev/null 2>&1 || true
  docker volume rm -f "$C-datos" "$AJENO-datos" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap limpiar EXIT INT TERM

# Lo que deja ver por que fallo: el contenedor se borra al salir, y en el CI no queda otra cosa.
# Es el Hermes descartable, con keys de prueba; igual se tapa todo hexadecimal largo.
diagnostico() {
  docker inspect "$C" >/dev/null 2>&1 || return 0
  echo "--- diagnostico de $C"
  docker exec "$C" /command/s6-svstat /run/service/gateway-agente-comercial 2>&1 || true
  docker exec "$C" /opt/hermes/.venv/bin/python -c '
import json
d = json.load(open("/opt/data/profiles/agente-comercial/gateway_state.json"))
a = (d.get("platforms") or {}).get("api_server") or {}
print("gateway_state.json:", {k: d.get(k) for k in ("gateway_state", "desired_state", "pid", "exit_reason", "updated_at")},
      "api_server:", {k: a.get(k) for k in ("state", "writer_pid", "error_message")})
' 2>&1 || true
  docker exec "$C" sh -c 'tail -n 80 /opt/data/profiles/agente-comercial/logs/gateway.log; echo "--- s6-log"; tail -n 40 /opt/data/logs/gateways/agente-comercial/current' 2>&1 \
    | grep -v "rejected invalid API key" | sed 's/[0-9a-f]\{40,\}/***/g' || true
}

fallar() { echo "FALLA: $*"; diagnostico; exit 1; }
paso() { echo; echo "=== $*"; }

# instalador <nombre de la salida> <codigo esperado> <argumentos...>
instalador() {
  nombre=$1; esperado=$2; shift 2
  set +e
  python3 "$INSTALADOR" "$@" > "$SALIDAS/$nombre.txt" 2>&1
  codigo=$?
  set -e
  cat "$SALIDAS/$nombre.txt"
  [ "$codigo" = "$esperado" ] || fallar "$nombre salio con $codigo y se esperaba $esperado"
}

contiene() { grep -qF -- "$2" "$SALIDAS/$1.txt" || fallar "$1 no dice: $2"; }

# levantar <contenedor> <stack>
levantar() {
  docker run -d --name "$1" --network none --memory 1g \
    --label "com.docker.stack.namespace=$2" --label "com.docker.swarm.service.name=$2_hermes" \
    -v "$1-datos:/opt/data" "$IMAGEN" sleep infinity >/dev/null
  i=0
  until docker exec "$1" test -e /run/service/gateway-default 2>/dev/null; do
    i=$((i + 1)); [ "$i" -ge 90 ] && fallar "$1 no arranco"; sleep 1
  done
}

como_hermes() { c=$1; shift; docker exec -u hermes -e HOME=/opt/data "$c" "$@"; }

pid_del_gateway() { docker exec "$C" /command/s6-svstat "/run/service/gateway-$1" | sed -n 's/.*(pid \([0-9]*\).*/\1/p'; }

# esperar_s6 <servicio> <up|down>
esperar_s6() {
  i=0
  until docker exec "$C" /command/s6-svstat "/run/service/$1" | grep -q "^$2"; do
    i=$((i + 1)); [ "$i" -ge 60 ] && fallar "$1 no quedo $2"; sleep 2
  done
}

# La key de prueba no se imprime nunca; al final se busca que no aparezca en ninguna salida.
python3 -c 'import secrets; print("HERMES_API_KEY=" + secrets.token_hex(24))' > "$TMP/secretos/bridge.env"
cp "$RAIZ/tests/fixtures/hermes/instancia-prueba.toml" "$TMP/hermes.toml"
sed 's/^stack = "prueba"$/stack = "otra"/' "$TMP/hermes.toml" > "$TMP/hermes-otra.toml"

paso "1. Hermes de la instancia de prueba, con el modelo de mentira adentro ($IMAGEN)"
docker pull -q "$IMAGEN" >/dev/null
levantar "$C" prueba
docker exec "$C" mkdir -p /tmp/mentira
docker cp "$RAIZ/hermes/prueba/modelo_de_mentira.py" "$C:/tmp/mentira/modelo_de_mentira.py"
docker cp "$RAIZ/profiles/agente-comercial-comun/SOUL.md" "$C:/tmp/mentira/SOUL.md"
docker exec "$C" chown -R hermes:hermes /tmp/mentira
docker exec -d -u hermes "$C" /opt/hermes/.venv/bin/python /tmp/mentira/modelo_de_mentira.py \
  --puerto 9999 --soul /tmp/mentira/SOUL.md --registro /tmp/mentira/pedidos.jsonl

paso "2. plan sobre un Hermes nuevo: dice que haria y no escribe nada"
instalador plan-inicial 0 plan --instancia "$TMP/hermes.toml"
contiene plan-inicial "crear el profile agente-comercial"
contiene plan-inicial "VEREDICTO: aplicar haria 5 cambio(s)"
como_hermes "$C" test ! -e /opt/data/profiles/agente-comercial || fallar "plan creo el profile"
como_hermes "$C" test ! -e /opt/data/.setter-instancia.json || fallar "plan escribio la marca"

paso "3. aplicar"
instalador aplicar 0 aplicar --instancia "$TMP/hermes.toml"
contiene aplicar "VEREDICTO: igual a la declaracion"

paso "4. chequear"
instalador chequear 0 chequear --instancia "$TMP/hermes.toml"
contiene chequear "agente-comercial: cero herramientas en el API server"
contiene chequear "responde 200 con la clave y 401 sin ella"

paso "5. hablarle al comercial: el modelo recibe el SOUL entero y ninguna herramienta"
docker exec -i -u hermes "$C" /opt/hermes/.venv/bin/python - > "$SALIDAS/hablar.txt" 2>&1 <<'PY'
import json
import urllib.request

ruta = "/opt/data/profiles/agente-comercial/" + "." + "env"
clave = next(l.split("=", 1)[1].strip() for l in open(ruta) if l.startswith("API_SERVER_KEY="))
cuerpo = {"model": "agente-comercial", "messages": [{"role": "user", "content": "hola"}]}
pedido = urllib.request.Request("http://127.0.0.1:8642/v1/chat/completions", data=json.dumps(cuerpo).encode())
pedido.add_header("Content-Type", "application/json")
pedido.add_header("Authorization", f"Bearer {clave}")
with urllib.request.urlopen(pedido, timeout=180) as respuesta:
    print(json.loads(respuesta.read())["choices"][0]["message"]["content"])
PY
cat "$SALIDAS/hablar.txt"
contiene hablar "MENTIRA herramientas=0 soul=entero modelo=prueba/modelo"

paso "6. aplicar otra vez: sin cambios y sin reiniciar el gateway"
PID=$(pid_del_gateway agente-comercial)
instalador aplicar-otra-vez 0 aplicar --instancia "$TMP/hermes.toml"
contiene aplicar-otra-vez "Sin cambios: ya estaba igual a la declaracion."
[ "$(pid_del_gateway agente-comercial)" = "$PID" ] || fallar "aplicar sin cambios reinicio el gateway"
instalador plan-sin-cambios 0 plan --instancia "$TMP/hermes.toml"
contiene plan-sin-cambios "VEREDICTO: nada que aplicar"

paso "7. rojo a proposito: un byte de mas en el SOUL"
como_hermes "$C" sh -c 'printf x >> /opt/data/profiles/agente-comercial/SOUL.md'
instalador rojo-soul 1 chequear --instancia "$TMP/hermes.toml"
contiene rojo-soul "agente-comercial: SOUL.md distinto de Git"
instalador repara-soul 0 aplicar --instancia "$TMP/hermes.toml"
contiene repara-soul "instalar la distribucion en agente-comercial (SOUL.md)"
[ "$(pid_del_gateway agente-comercial)" = "$PID" ] || fallar "reparar el SOUL reinicio el gateway"
instalador verde-soul 0 chequear --instancia "$TMP/hermes.toml"

paso "8a. rotar la key del API server: el gateway la lee solo al arrancar"
cp "$TMP/secretos/bridge.env" "$TMP/clave-vieja.env"
python3 -c 'import secrets; print("HERMES_API_KEY=" + secrets.token_hex(24))' > "$TMP/secretos/bridge.env"
instalador rojo-key 1 chequear --instancia "$TMP/hermes.toml"
contiene rojo-key "agente-comercial: API_SERVER_KEY no coincide con los secretos de la instancia"
instalador plan-key 0 plan --instancia "$TMP/hermes.toml"
contiene plan-key "reiniciar el gateway de agente-comercial, porque cambia su puerto o su clave"
instalador rota-key 1 aplicar --instancia "$TMP/hermes.toml"
contiene rota-key "poner API_SERVER_KEY en las variables de agente-comercial"
contiene rota-key "sigue con el puerto o la clave de antes: reiniciarlo con aplicar --reiniciar"
instalador rota-key-reinicia 0 aplicar --instancia "$TMP/hermes.toml" --reiniciar
contiene rota-key-reinicia "reiniciar el gateway de agente-comercial: no respondia con el puerto y la clave actuales"
# Otra rotacion enseguida: el SIGTERM le llega al gateway nuevo mientras todavia termina de arrancar.
# Ahi Hermes sale con 0, y su script finish le pide a s6 que no lo levante (medido en el CI el
# 2026-10-14). aplicar --reiniciar lo tiene que dejar atendiendo igual.
cp "$TMP/secretos/bridge.env" "$TMP/clave-intermedia.env"
python3 -c 'import secrets; print("HERMES_API_KEY=" + secrets.token_hex(24))' > "$TMP/secretos/bridge.env"
instalador rota-key-enseguida 0 aplicar --instancia "$TMP/hermes.toml" --reiniciar
instalador verde-key 0 chequear --instancia "$TMP/hermes.toml"

paso "8. rojo a proposito: un profile de mas (aplicar no lo borra)"
como_hermes "$C" /opt/hermes/.venv/bin/hermes profile create intruso --no-alias --no-skills >/dev/null
instalador rojo-intruso 1 chequear --instancia "$TMP/hermes.toml"
contiene rojo-intruso "sobra el profile intruso"
instalador aplicar-con-intruso 1 aplicar --instancia "$TMP/hermes.toml"
como_hermes "$C" test -d /opt/data/profiles/intruso || fallar "aplicar borro el profile de mas"
como_hermes "$C" /opt/hermes/.venv/bin/hermes profile delete intruso -y >/dev/null
instalador verde-sin-intruso 0 chequear --instancia "$TMP/hermes.toml"

paso "9. rojo a proposito: el profile default prendido"
como_hermes "$C" /opt/hermes/.venv/bin/hermes gateway start >/dev/null 2>&1 || true
instalador rojo-default 1 chequear --instancia "$TMP/hermes.toml"
contiene rojo-default "el gateway del profile default esta corriendo"
como_hermes "$C" /opt/hermes/.venv/bin/hermes gateway stop >/dev/null 2>&1 || true
esperar_s6 gateway-default down
instalador verde-default 0 chequear --instancia "$TMP/hermes.toml"

paso "10. un Hermes de otra instancia: frena y no toca nada"
levantar "$AJENO" otra
como_hermes "$AJENO" /opt/hermes/.venv/bin/hermes profile create agente-comercial --no-alias --no-skills >/dev/null
SOUL_AJENO=$(docker exec "$AJENO" md5sum /opt/data/profiles/agente-comercial/SOUL.md)
instalador ajeno-stack 2 aplicar --instancia "$TMP/hermes.toml" --contenedor "$AJENO"
contiene ajeno-stack "es del stack 'otra', no de 'prueba'"
instalador ajeno-profile 2 aplicar --instancia "$TMP/hermes-otra.toml"
contiene ajeno-profile "el profile agente-comercial ya existe, no lo instalo este script"
[ "$(docker exec "$AJENO" md5sum /opt/data/profiles/agente-comercial/SOUL.md)" = "$SOUL_AJENO" ] || fallar "toco el SOUL ajeno"
docker exec "$AJENO" test ! -e /opt/data/.setter-instancia.json || fallar "marco el Hermes ajeno"

paso "11. la key de prueba no aparece en ninguna salida"
for archivo in "$TMP/clave-vieja.env" "$TMP/clave-intermedia.env" "$TMP/secretos/bridge.env"; do
  CLAVE=$(sed -n 's/^HERMES_API_KEY=//p' "$archivo")
  [ -n "$CLAVE" ] || fallar "no hay key de prueba en $archivo"
  if grep -rqF -- "$CLAVE" "$SALIDAS"; then fallar "una key de prueba aparecio en una salida"; fi
done
echo "ninguna de las $(ls "$SALIDAS" | wc -l | tr -d ' ') salidas tiene alguna de las tres keys de prueba"

echo
echo "TODO OK"
