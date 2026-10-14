# El Hermes de una instancia

Cada instancia tiene su propio Hermes, adentro de su stack, declarado por el producto
([ADR-0025](../docs/decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md)).
Acá vive lo que lo declara y lo prueba:

- `declaracion.toml`: la imagen fija y los profiles que trae esta versión del producto.
- `prueba/`: el modelo de mentira y la prueba de punta a punta que corre el CI (workflow `hermes`).

El instalador y el chequeo son un solo script, `scripts/hermes_instancia.py`. La distribución de
cada profile es nativa de Hermes (`distribution.yaml`), y vive en `profiles/`.

## La declaración del producto: `hermes/declaracion.toml`

```toml
schema = 1

[imagen]
nombre = "nousresearch/hermes-agent"
etiqueta = "v2026.8.31"
digest = "sha256:…"            # aplicar frena si el contenedor corre otra imagen
version_hermes = "0.21.0"

[profiles.agente-comercial]
distribucion = "profiles/agente-comercial-comun"   # SOUL.md, config.yaml y distribution.yaml
puerto = 8642                                      # el del API server, adentro de la red del stack
le_habla = "bridge"
```

Lista solo los profiles que esta versión trae. Lo que tiene que estar apagado no se declara: el
chequeo lo exige siempre (el panel, el profile `default` y el multiplex de profiles).

## La declaración de la instancia: `hermes.toml`

Va en la raíz del repo de la instancia. No va en `instancia.toml`: el lector del manifiesto
rechaza las secciones que no conoce, y un bridge anterior se caería al reiniciar.

```toml
schema = 1
stack = "setter-att1"        # el contenedor tiene que tener esta etiqueta com.docker.stack.namespace
# servicio = "hermes"        # opcional; se busca el contenedor del servicio <stack>_<servicio>

[profiles.agente-comercial]
modelo = "z-ai/glm-5.3-flash"
proveedor = "openrouter"
# base_url = "…"             # opcional, para un proveedor custom

[[profiles.agente-comercial.respaldo]]
modelo = "…"
proveedor = "openrouter"

[profiles.agente-comercial.variables]
# Cada variable del profile sale de un archivo de secretos del servidor. Las keys no se escriben acá.
API_SERVER_KEY = { archivo = "/opt/setter-att1/secretos/bridge.env", variable = "HERMES_API_KEY" }
OPENROUTER_API_KEY = { archivo = "/opt/setter-att1/secretos/hermes.env", variable = "OPENROUTER_API_KEY" }
```

Tiene que declarar todos los profiles del producto, y ninguno más. Una ruta de secretos relativa se
resuelve contra la carpeta del `hermes.toml`, cosa que solo usan las pruebas. `API_SERVER_KEY` es la
misma `HERMES_API_KEY` del bridge: sale del mismo archivo para que no haya dos copias que se separen.

## Cómo se corre

En el servidor de la instancia, desde un checkout del producto en la versión que usa la instancia,
con `python3` 3.11 o más nuevo y permiso para usar docker:

```sh
python3 scripts/hermes_instancia.py plan     --instancia /opt/setter-att1/instancia/hermes.toml
python3 scripts/hermes_instancia.py aplicar  --instancia /opt/setter-att1/instancia/hermes.toml
python3 scripts/hermes_instancia.py chequear --instancia /opt/setter-att1/instancia/hermes.toml
```

- **`plan`** dice qué haría `aplicar` y no escribe nada. Se corre siempre antes.
- **`aplicar` es un deploy.** Hermes lee el SOUL y el `config.yaml` de un profile en cada pedido:
  lo que escribe `aplicar` lo usa el agente desde el pedido siguiente, sin reiniciar.
  - crea lo que falta (`hermes profile create`, que registra el servicio en s6);
  - instala la distribución con `hermes profile install --force`, que pisa solo `SOUL.md` y
    `config.yaml`, nunca memorias, sesiones ni registros;
  - pone las variables declaradas en el archivo de variables del profile, sin tocar las otras líneas;
  - arranca el gateway con `hermes --profile <p> gateway start`, que deja escrito que tiene que
    correr: si el contenedor se reinicia, vuelve solo;
  - correrlo dos veces da lo mismo, y si no hay nada que cambiar no reinicia nada;
  - no borra nada: un profile de más queda como diferencia.
- **`chequear`** compara lo que corre con lo declarado y sale con 1 ante cualquier diferencia:
  - la imagen y la marca de la instancia (`/opt/data/.setter-instancia.json`);
  - los profiles que sobran o faltan;
  - de cada profile: que lo instaló este script, el SOUL contra Git por hash, el `config.yaml` clave
    por clave, cero herramientas en el API server (calculadas con la función de Hermes), las
    variables contra los secretos por hash, el gateway corriendo, sin otras plataformas, y el API
    server respondiendo 200 con la clave y 401 sin ella;
  - el profile `default` sin correr, el panel apagado y sin multiplex.

Nunca imprime una key: el script las lee de los archivos de secretos, las pasa al contenedor por
stdin y adentro las compara por hash.

### Frenos

Sale con 2 sin tocar nada si el contenedor no es del stack que declara la instancia, si `aplicar`
encuentra otra imagen, si el Hermes tiene un profile que no instaló este script y no tiene la marca
de la instancia, o si falta un secreto. Es lo que impide, por ejemplo, pisar el `agente-comercial`
de Johanna en el Hermes compartido: ese contenedor no tiene la etiqueta de ningún stack (lo creó
EasyPanel), y su profile no tiene `distribution.yaml` (los dos, medidos el 2026-10-14). Los
contenedores del stack `setter-att1`, desplegado con `docker stack deploy`, sí la tienen.

### Cuándo hace falta reiniciar

Medido el 2026-10-14 en un Hermes descartable con la imagen declarada, salvo donde se indica:

- **Sin reiniciar:** el SOUL y el `config.yaml` (medido cambiando el modelo: el pedido siguiente ya
  usó el nuevo, con el mismo proceso). La key de OpenRouter también, según el código
  (`agent/credential_pool.py` la relee del archivo en cada turno), que es lo que ya usa
  `cargar-key-openrouter.py` de ATT1; no se midió acá.
- **Reiniciando el gateway:** el puerto y la clave del API server (`API_SERVER_KEY`), que se leen
  solo al arrancar (medido: después de rotar la clave, el gateway seguía aceptando solo la vieja).
  Si cambian con el gateway andando, `aplicar` lo deja como diferencia; con `aplicar --reiniciar`
  reinicia solo ese gateway y espera a que el proceso nuevo responda con la clave actual. Rotar
  `API_SERVER_KEY` también exige reiniciar el bridge, que lee `HERMES_API_KEY` al arrancar.
- **El reinicio corta el API entre 15 y 25 s** (medido en el VPS, cuatro veces): el proceso viejo
  sale enseguida y el nuevo tarda en cargar. En ese rato el bridge no tiene a quién hablarle.
- **Un reinicio que llega mientras el gateway todavía termina de arrancar lo deja apagado.** Hermes
  sale con 0, y su script `finish` le pide a s6 que no lo vuelva a levantar (medido en el CI el
  2026-10-14: el SIGTERM llegó 18 s después del arranque). `aplicar --reiniciar` lo detecta y lo
  arranca una vez. Si vuelve a pasar, o si el gateway salió por un error de configuración (78), frena.
  Lo mismo vale para un `hermes gateway restart` a mano: después, mirar que haya quedado arriba.

## El contenedor

- **Comando: `sleep infinity`.** Con eso s6 levanta solo los gateways de los profiles que tienen que
  correr, y el del profile `default` no arranca. El comando del Hermes compartido
  (`--profile agente-comercial gateway run`) es el camino viejo, que levanta un gateway por fuera.
- Sin `HERMES_DASHBOARD` ni `GATEWAY_MULTIPLEX_PROFILES`.
- Los datos en `/opt/data`, en un volumen del stack. El puerto del API server no se publica: el
  bridge le habla por la red del stack, al servicio `hermes`.
- **Memoria:** un Hermes con el comercial solo ocupó unos 165 MB en reposo (descartable con el modelo
  de mentira, 2026-10-14). El tope del contenedor se fija al llevarlo a ATT1, midiendo con tráfico.

## Probarlo

```sh
sh hermes/prueba/probar_en_contenedor.sh
```

Levanta dos Hermes descartables sin red con la imagen declarada: el de una instancia de prueba y el
de otra instancia, con un `agente-comercial` ajeno. Recorre `plan`, `aplicar`, `chequear`, le habla
al comercial con el modelo de mentira (que confirma el SOUL entero y cero herramientas), vuelve a
aplicar sin cambios y sin reiniciar, rota la clave, y pone en rojo a propósito un byte del SOUL, un
profile de más, el `default` prendido y el Hermes ajeno. Al final busca que las keys de prueba no
aparezcan en ninguna salida. El CI la corre cuando cambia algo de Hermes.

## Subir Hermes de versión

Un PR que cambia `[imagen]` (etiqueta, digest y versión). El CI corre la prueba contra la imagen
nueva. Antes, mirar dos cosas en el código de la versión nueva:

- `_config_version` y sus migraciones, por si alguna toca las claves del molde;
- los toolsets de plugins nuevos: uno que Hermes no tiene en su lista de apagados arranca prendido
  en todas las plataformas. El chequeo lo detecta (exige cero herramientas), pero es mejor saberlo
  antes.
