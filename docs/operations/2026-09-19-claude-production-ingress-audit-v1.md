# Auditoria del ingreso en produccion del Appointment Bridge

- **Tipo:** evidencia operativa de una auditoria de solo lectura.
- **Fecha de la medicion:** 2026-09-19, entre las 20:30 y las 00:30 UTC del 2026-09-20.
- **Alcance:** trafico HTTP que entra al bridge, correspondencia entre el codigo desplegado y Git, estado de automatizacion declarado por el propio servicio, configuracion de entrega de Chatwoot y capacidad de despliegue del Swarm.
- **Metodo:** log de acceso del proxy (`easypanel-traefik`), log del contenedor del bridge, `docker service inspect`, `docker inspect`, consulta de solo lectura a `/health` y `/ready` desde dentro del contenedor, comparacion de hashes de objeto Git entre el contenedor y el repositorio, y dos consultas de solo lectura a las tablas de configuracion de Chatwoot (`agent_bots`, `webhooks`, `inboxes`).
- **No se hizo:** ningun despliegue, reinicio, cambio de flag, migracion, escritura en Supabase ni envio de mensajes. No se leyeron secretos, cuerpos de webhook, conversaciones ni datos de personas.

Este documento no contiene secretos, identificadores de personas, numeros de telefono ni nombres de host.

## 1. Resumen

Tres hallazgos, dos de ellos activos en el momento de la medicion.

1. **Hotmart entrega y el bridge rechaza nueve de cada diez eventos.** En 3,05 dias de log de proxy: 103 entregas, **10 aceptadas**. La cadena observada es `503 webhook_persist_unavailable` en el primer intento y `401` en todos los reintentos posteriores, porque el reintento llega mas viejo que la ventana de frescura de 300 segundos. **Activo.**
2. **El AgentBot de Chatwoot entrega a una ruta que el bridge no expone**: 145 POST con `404` en el mismo periodo, de forma continua. El ingreso canonico (`/webhooks/chatwoot`, webhook de cuenta suscrito a `message_created`) funciona, asi que no hay perdida demostrada del camino principal, pero la configuracion del bot es incorrecta. **Activo.**
3. **`att1-agent-profile` lleva 13 dias sin replica por un error de unidades en las reservas del servicio**: pide 256 TiB de memoria en un nodo de 23 GB. **Activo, causa raiz identificada.**

Ademas quedan confirmadas con medicion directa dos cosas que hasta hoy estaban como reportadas: el commit que corre en produccion y el estado de automatizacion del bridge.

## 2. Codigo desplegado, verificado contra el artefacto

El contenedor del bridge arranco el **2026-09-18 a las 15:15:09 UTC** con la imagen `e6598d47375d`, construida a las 14:39 y republicada a las 15:12 UTC (log de accion de EasyPanel). El servicio declara `GIT_SHA=108d2ee889babb31ac121e5e8db82b8817135b81` y `DEPLOY_TIMESTAMP` equivalente a 2026-09-18T15:12:44Z.

Esa declaracion se verifico de forma independiente, sin confiar en la variable: se calculo el hash de objeto Git (`blob`) de los 44 archivos de `/app/src` dentro del contenedor y se comparo contra el arbol `src/` de los ultimos 40 commits de `main`.

| Commit | Archivos coincidentes |
|---|---|
| `108d2ee8` (merge del PR #154) | **44 de 44** |
| `1077282d` (merge del PR #156) | 43 de 44 |
| `origin/main` = `b04ad14` | 43 de 44 |

La declaracion y el artefacto coinciden. **La unica diferencia entre produccion y `origin/main` es `src/bridge/chatwoot.py`**, por el commit `2c80d82` del PR #156, que hace que los mensajes de actividad (`message_type` 2) no cuenten como actividad del usuario en el escaneo de conversaciones estancadas y rechaza los tipos desconocidos. Son 18 lineas agregadas y 2 borradas en un solo archivo: esa, y ninguna otra, es la superficie de riesgo de un release desde `main`.

El mismo metodo se aplico a los otros dos servicios construidos desde este repositorio, y en los dos la declaracion coincide con el artefacto:

| Servicio | `GIT_SHA` declarado | Arbol `src/` que corresponde | Archivos distintos de `origin/main` |
|---|---|---|---|
| `infra_supportmagician-slack-connector` | `6287c14f` (merge del PR #155) | 44 de 44 | 2 (`chatwoot.py`, `filtering.py`) |
| `infra_daily-feedback` | `943e8cf0` (merge del PR #141, 2026-09-13) | 41 de 41 | **13** |

`infra_daily-feedback` es el mas atrasado: seis dias y trece archivos de diferencia con `main`.

**Limite del metodo:** compara arboles, no commits. Cuando varios commits consecutivos no tocan `src/`, todos empatan y el metodo acota el commit a ese rango en vez de senalar uno. Paso con `infra_daily-feedback`, que empata con `943e8cf0`, `40f645ff` y `19cf850b`. Para desempatar hace falta otra senal, como la variable `GIT_SHA` o la hora del despliegue. El metodo esta descrito en el runbook de release, seccion 4.

## 3. Estado de automatizacion, medido en vivo

`GET /health` devolvio `200 {"status":"ok"}` y `GET /ready` devolvio `200` con:

```text
status=ready
pilot_boundary=disabled
automation_state=default_off
reason_code=pilot_boundary_disabled
precheckout_delayed_first_touch=enabled
precheckout_delayed_database=precheckout_first_touch_ready
precheckout_delayed_due=0
precheckout_delayed_reserved=0
precheckout_delayed_request_started=0
precheckout_delayed_delivery_unknown=1
human_handoff_projection=configured
human_handoff_pending=0
human_handoff_retryable=0
human_handoff_delivery_unknown=0
human_handoff_conflicts=0
human_handoff_dead_letters=0
chatwoot_stalled_monitor=disabled
```

Los flags de efectos siguen apagados (`CHATWOOT_STALLED_MONITOR_ENABLED`, `CHATWOOT_CUT_B_ADMISSION_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED`, `PAYMENT_LINK_ENABLED`, `LANCEMOS_PILOT_BOUNDARY_ENABLED`, `HOTMART_PURCHASE_WORKER_ENABLED`, todos en `false`). En cambio **la via de abandono de carrito de Hotmart si esta encendida**: `JOHANNA_ABANDONMENT_HOTMART_AUTO_ENABLED=true` y `HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=true`. Es la unica entrada de negocio activa, y es justamente la que esta fallando (seccion 4).

`precheckout_delayed_delivery_unknown=1` es el unico contador distinto de cero. Es un estado persistido en Supabase, anterior a esta medicion; no se pudo fechar ni atribuir sin acceso a la base, y el log del contenedor actual no lo menciona.

## 4. Hotmart: nueve de cada diez entregas rechazadas

### Lo medido

Log del proxy, del 2026-09-16 23:02 al 2026-09-20 00:21 UTC, endpoint `POST /webhooks/hotmart`:

| Codigo | Cantidad | Cuerpo de la respuesta (por tamano) |
|---|---|---|
| 202 Accepted | 10 | respuesta de aceptacion |
| 401 Unauthorized | 51 | 26 bytes: `invalid_token` o `stale_webhook` (ambos miden igual) |
| 503 Service Unavailable | 22 | 40 bytes: **`webhook_persist_unavailable`** |
| 502 Bad Gateway | 17 | pagina de error del proxy, el bridge no respondio |
| 422 Unprocessable | 3 | validacion del framework |

Por dia: el 17/09, 2 aceptadas y 33 fallidas; el 18/09, 6 y 19; el 19/09, **2 y 41**. La proporcion de fallas crece.

Todas las direcciones de origen pertenecen a rangos de AWS, consistentes con el emisor de Hotmart, y las aceptadas salen de ese mismo conjunto de rangos.

### La cadena observada

El patron temporal del 19/09 es regular, con una entrega cada quince minutos aproximadamente, y alterna asi:

```text
19:31 503   <- primer intento de un evento: falla la persistencia
19:44 401   <- reintento del mismo evento, ya con mas de 300 s de antiguedad
19:46 401
20:01 401
20:25 503   <- primer intento de otro evento
20:40 401
20:55 401
```

El handler valida en este orden: token, tamano, JSON, clasificacion, **frescura** (`is_stale_event` contra `HOTMART_MAX_AGE_SECONDS`, hoy en el valor por defecto de **300 segundos**) y recien despues persiste. Un evento que falla al persistir devuelve 503; cuando el emisor lo reintenta, ya pasaron mas de cinco minutos desde su `creation_date`, de modo que la guarda de frescura lo rechaza con **401** y **ningun reintento puede volver a entrar jamas**. El evento queda perdido de forma definitiva y silenciosa.

Que los 401 sean `stale_webhook` y no `invalid_token` no se puede demostrar desde el log, porque los dos cuerpos miden 26 bytes, pero es lo unico compatible con que haya 202 en el mismo periodo y desde el mismo pool de direcciones: un token equivocado rechazaria todas las entregas, no el noventa por ciento.

### Por que importa mas de lo que parece

- La documentacion de Hotmart y el relevamiento previo del producto indican que **Hotmart desactiva la configuracion del webhook cuando la URL falla de forma sostenida**, sin aviso y sin endpoint REST para recuperar el historial de abandono. Una racha de fallas como la medida pone en riesgo el canal entero, no solo los eventos de la racha.
- La verificacion de abandono de carrito de Hotmart es por lotes, con una demora tipica de decenas de minutos entre el abandono y la entrega. Una ventana de frescura de 300 segundos medida contra `creation_date` es incompatible con ese emisor incluso en el primer intento, salvo que Hotmart fije `creation_date` en el momento del envio.
- La via de abandono es la unica entrada de negocio encendida hoy.

### El 503 no es Supabase caido, y el nombre del error enganna

Dos evidencias acotan bastante la causa.

**Supabase responde bien en el mismo periodo.** `POST /webhooks/johanna-funnel-events` tambien persiste en Supabase (`admit_johanna_funnel_event`) y tiene su propio 503 (`johanna_funnel_persist_unavailable`). En los mismos 3,05 dias acumulo **1753 respuestas 202 y ningun 503**. Si la base estuviera intermitente, ese endpoint seria el primero en mostrarlo por volumen. El fallo es especifico del camino de abandono de Hotmart, no de la base.

**Los tiempos de respuesta muestran donde corta cada codigo.** Los 503 tardaron entre 239 y 2034 ms, el mismo orden que los 202 (388 a 2694 ms): llegaron hasta las llamadas a Supabase. Los 401, en cambio, se concentran entre 7 y 50 ms, sin ninguna entrada/salida externa de por medio.

**Y el nombre del error no describe lo que pasa.** El bloque `except SupabaseError` que devuelve `webhook_persist_unavailable` envuelve dos llamadas, no una: primero `admit_and_correlate_hotmart_cart_abandonment`, que persiste el evento y devuelve su `webhook_event_id`, y despues, porque `JOHANNA_ABANDONMENT_HOTMART_AUTO_ENABLED` esta en `true`, `correlate_hotmart_purchase_intent`. Si falla la segunda, el evento **ya quedo persistido** y el emisor igual recibe 503. De ahi salen dos consecuencias que hay que verificar contra la base: puede haber eventos de abandono guardados y sin correlacionar, y el reintento de Hotmart no aporta nada porque la guarda de frescura lo frena antes.

### Lo que falta para cerrar el diagnostico

1. Cual de las dos llamadas falla y por que. El analisis de arriba deja como hipotesis principal la correlacion de intencion de compra, no la persistencia. Distinguirlas requiere mirar Supabase o registrar el motivo, que hoy el bridge no hace.
2. Confirmar en el panel de Hotmart si la configuracion del webhook sigue activa, cuantos reintentos consumio y con que `creation_date` se envian los eventos de abandono. Lo hace una persona con acceso a esa cuenta.
3. La propuesta de cambio de comportamiento esta en `docs/design/hotmart-delivery-durability-v1.md`. Toca `src/bridge/app.py`, hoy reservado por el claim `codex-appointment-operations-v1` (PR #160), asi que no se implemento en esta tarea.

## 5. Chatwoot: el AgentBot entrega a una ruta inexistente

`POST /webhooks/chatwoot/agent-bot` recibio **145 peticiones con 404** en el periodo, todas desde la red interna de Docker, de forma continua desde antes del arranque del contenedor actual y hasta el momento de la medicion.

La configuracion de Chatwoot explica el origen:

| Objeto | Valor |
|---|---|
| `agent_bots` id 1, "Appointment Setter" | `outgoing_url` termina en `/webhooks/chatwoot/agent-bot` |
| Asignacion | inbox 9 (`Channel::Whatsapp`), que es el inbox que el bridge tiene configurado |
| `webhooks` id 1 (cuenta 1) | apunta a `/webhooks/chatwoot`, suscripcion `["message_created"]` |

El bridge expone `/webhooks/chatwoot` y **no** expone `/webhooks/chatwoot/agent-bot`; la arquitectura declara el webhook de cuenta como unico ingreso durable. El webhook de cuenta funciona: 27 entregas 2xx y ninguna falla en el periodo.

Por lo tanto el 404 no demuestra perdida del ingreso principal, pero deja tres cosas: entregas fallidas cada vez que hay actividad en el inbox, una configuracion que afirma algo falso sobre por donde recibe el bot, y el riesgo de que Chatwoot trate al bot como caido.

**Recomendacion:** vaciar la `outgoing_url` del AgentBot, no redirigirla a `/webhooks/chatwoot`. Ese endpoint exige los encabezados de firma del webhook de cuenta (`x-chatwoot-signature`, `x-chatwoot-timestamp`, `x-chatwoot-delivery`), que la entrega de AgentBot no envia: apuntarla ahi cambiaria 404 por 401 sin ganar nada. Es un cambio en produccion y lo autoriza Dan.

## 6. La caida del 16 y 17 de septiembre

Los 502 del periodo no estan repartidos: empiezan el 16/09 a las 23h UTC y terminan el 17/09 a las 14h UTC. Ninguno despues.

| Endpoint | 502 en esa ventana |
|---|---|
| `POST /webhooks/johanna-funnel-events` | 312 |
| `POST /webhooks/hotmart` | 17 |
| `POST /webhooks/lead` | 10 |

Un 502 significa que el proxy no obtuvo respuesta del bridge. Son unas quince horas con el servicio no disponible para todos sus emisores. Hotmart reintenta; el emisor del funnel y la landing, por lo que se sabe, no. **Diez entregas a `/webhooks/lead` quedaron sin respuesta en esa ventana**, y cada una es un lead potencial.

El incidente esta cerrado y no se investigo su causa: es anterior al contenedor actual y el log del contenedor viejo ya no esta. Se documenta porque nadie lo habia registrado y porque fija el costo de no tener alerta sobre el ingreso.

## 7. `att1-agent-profile`: causa raiz del 0/1

El servicio no tiene contenedor desde hace 13 dias, con el error de Swarm `no suitable node (insufficient resources on 1 node)`. La definicion del servicio dice:

| Campo | Valor declarado | Equivalente |
|---|---|---|
| `Reservations.NanoCPUs` | 250000000 | 0,25 CPU |
| `Reservations.MemoryBytes` | 281474976710656 | **256 TiB** |
| `Limits.NanoCPUs` | 1000000000 | 1 CPU |
| `Limits.MemoryBytes` | 844424930131968 | **768 TiB** |

El nodo tiene 23 GB de memoria y 8 CPU. Los numeros son exactamente 256 y 768 multiplicados por 1024 a la cuarta: **es un error de unidades**, se cargaron mebibytes en un campo que se interpreto en tebibytes. La intencion evidente era reservar 256 MiB y limitar en 768 MiB, que es holgado: los contenedores comparables del mismo stack usan entre 2 y 86 MiB.

Es el **unico** servicio del Swarm con reservas declaradas; los otros 21 tienen el campo vacio. El servicio se actualizo por ultima vez el 2026-09-07 a las 12:40 UTC y las tareas anteriores a esa fecha si habian corrido, asi que el error entro con esa edicion.

**Correccion:** en EasyPanel, en el servicio `att1-production/att1-agent-profile`, dejar la reserva de memoria en 256 MB y el limite en 768 MB, o vaciar los dos campos. Tiene que hacerse en el panel y no con `docker service update`: el panel es dueno de la definicion y la reescribe en el siguiente despliegue.

**No se aplico**, y no solo por la regla de autorizacion: corregir la reserva **levanta** el profile del agente comercial de ATT1, que no tiene Conversation Release aprobada. Antes de levantarlo hay que decidir si debe seguir definido.

## 8. Capacidad de volver atras, y lo que se hizo al respecto

Los tres servicios construidos desde este repositorio corren imagenes con tag movil (`easypanel/infra/<servicio>:latest`). En el momento de la medicion, el demonio **solo tenia la imagen actual**: cero imagenes anteriores, cero imagenes sin etiqueta. Es decir, un despliegue que resultara malo no tenia a donde volver, porque el build nuevo sobreescribe el tag y `docker service rollback` devolveria el spec anterior apuntando al mismo tag movil, o sea al codigo nuevo.

Durante esta auditoria se creo un alias local para las tres imagenes en produccion, sin tocar ningun servicio:

```text
easypanel/infra/appointment-bridge:preserved-20260919            -> e6598d47375d
easypanel/infra/supportmagician-slack-connector:preserved-20260919 -> 4392a90dc51f
easypanel/infra/daily-feedback:preserved-20260919                -> b8e60a55e28a
```

Es la unica escritura de toda la auditoria. No altera servicios en ejecucion, no consume espacio (es un alias del mismo identificador de imagen) y se deshace borrando el tag. A partir de ahora existe un destino de rollback concreto, descrito en el runbook de release.

## 9. Lo que esta auditoria no puede afirmar

- No mira Supabase, asi que no explica el `webhook_persist_unavailable` ni fecha el `delivery_unknown=1`.
- No distingue `invalid_token` de `stale_webhook` con evidencia directa; la atribucion de la seccion 4 es una inferencia, marcada como tal.
- No cubre el trafico anterior al 2026-09-16 23:02 UTC, que es donde empieza el log del proxy retenido.
- No inspecciono el estado de los servicios `att1-production_*` mas alla de la definicion del que esta caido, ni el SHA desplegado de `infra_daily-feedback`.
- No consulto el panel de Hotmart ni el de EasyPanel: todo lo de EasyPanel se leyo desde el host.
