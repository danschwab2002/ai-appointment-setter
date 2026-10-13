# Referencia de nombres

Cómo se llama cada cosa que se despliega para una instancia. La regla la fija el [ADR-0025](decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md) (2026-10-13): con leer un nombre se sabe de qué instancia es y qué servicio es.

## La regla

1. **Todo lo de una instancia arranca con su nombre corto** (`<instancia>`) y sigue siempre con el mismo nombre para el mismo servicio.
2. **El nombre corto** va en minúsculas, con letras, números y guiones, y empieza con una letra (`^[a-z][a-z0-9-]{1,19}$`). Es el mismo en el repo, en el servidor, en Slack y en el manifiesto.
3. **Lo del producto no lleva el nombre de una instancia:** imágenes y paquetes van como `setter-<componente>` (`ghcr.io/danschwab2002/setter-bridge`, `setter-daily-feedback`, `setter-hermes`).
4. **Adentro de un stack, los servicios van sin prefijo.** El stack ya lo pone: Docker Swarm, y EasyPanel con sus proyectos, muestran cada servicio como `<stack>_<servicio>`, con un guion bajo que no se configura. Repetir la instancia en el servicio da nombres como `setter-att1_att1-bridge`.
5. **Adentro del Hermes de una instancia, los profiles van sin prefijo,** para que la declaración del producto sea igual en todas.
6. **Nada que esté corriendo se renombra solo por el nombre.** Cada pieza toma el nombre nuevo cuando se mude o se reconstruya. Los volúmenes de Swarm llevan el nombre del stack, así que renombrar un stack sin más crea volúmenes vacíos: el stack de una instancia que ya tiene datos se renombra solo con su migración de volúmenes.

## La lista

| Qué | Nombre | En ATT1 al 2026-10-13 |
|---|---|---|
| Repo de la instancia | `setter-instancia-<instancia>` | `setter-instancia-att1` (sigue la regla) |
| Stack de Swarm o proyecto de EasyPanel | `<instancia>` | `setter-att1` |
| Base | `db` | `att1-db` |
| API de la base | `rest` | `att1-rest` |
| Proxy interno de la base | `gateway` | `att1-gateway` |
| Bridge | `appointment-bridge` | `att1-bridge` |
| Revisión diaria | `daily-feedback` | `att1-revision` |
| Hermes | `hermes` | todavía en el Hermes compartido |
| Volumen de un servicio | `<servicio>-data` (Swarm lo muestra `<instancia>_<servicio>-data`) | `setter-att1_att1-db-data`, `setter-att1_att1-bridge-data` |
| Profiles de Hermes | `agente-comercial`, `automation-expert`, `client-copilot` | `att1-agente-comercial` en el Hermes compartido |
| Dominio | `<instancia>.<dominio del servidor>` | `setter-att1.u5iqmf.easypanel.host` |
| Rutas de Traefik (archivo y router) | `<instancia>-<servicio>` | `setter-att1.yaml` y `setter-att1-revision.yaml` |
| Timers del servidor | `<instancia>-alertas`, `<instancia>-respaldo` | `att1-alertas`, `att1-respaldo` (siguen la regla) |
| Canales de Slack | `#<instancia>-<uso>` | `#feed-chatwoot-att1` |

**El servicio de Hermes se llama exactamente `hermes`.** El bridge acepta http solo hacia ese nombre o hacia localhost (`src/bridge/app.py`, `trusted_http_hosts`). Con otro nombre, la instancia necesitaría https adentro de su propia red.

**El profile del manifiesto cambia con la mudanza.** `[agente].modelo` y `HERMES_MODEL_NAME` tienen que ser iguales, y el bridge no arranca si difieren. Cuando ATT1 pase a su Hermes, los dos pasan de `att1-agente-comercial` a `agente-comercial` en el mismo cambio, junto con `CORRELATION_PRERESOLUTION_MODEL_NAME`, que en ATT1 también nombra al profile del comercial. `LEAD_FIRST_NAME_MODEL_NAME` sin cargar toma el de `HERMES_MODEL_NAME`, y `HERMES_REPLY_SPLITTER_MODEL_NAME` nombra un modelo, no un profile.

## Lo que queda afuera de la regla

- **Las plantillas de Meta.** Meta acepta solo minúsculas, números y guion bajo, y con varios productos por instancia el prefijo puede ser el del producto. Se decide con el multiproducto.
- **La carpeta del servidor** (`/opt/setter-att1/`) **y el bucket del respaldo** (`setter-att1-backups`). Se deciden con la plantilla de instancia.

## Abierto

**El nombre corto de la instancia de la Dra. Nina Garza.** La unidad de instancia es la aliada (ADR-0021 §7), y ATT2, su segundo producto, va a vivir en la misma instancia. Por eso el nombre corto debería nombrar a la aliada y no a un producto, y `att1` deja de describirla cuando entre ATT2.
- No hace falta para el Hermes propio de ATT1: ese Hermes entra al stack que ya existe, como el servicio `hermes`.
- Hace falta antes de renombrar ese stack o de sumarle ATT2. Decide Dan.
