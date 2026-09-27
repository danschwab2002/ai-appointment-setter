# Contrato: atencion de derivaciones (`attended_at`)

Migracion `20260927000300_handoff_attendance_v1.sql`. Decision:
[ADR-0019](../decisions/0019-handoff-attendance-gates-automation.md).

## Por que existe

`human_handoff_requests.status` va `requested -> projected` y se queda ahi para
siempre. `projected` dice que la nota privada llego a Chatwoot, no que alguien
la haya leido. Sin esa distincion, la automatizacion no podia saber si una
derivacion seguia esperando, y el 2026-09-27 se midio la consecuencia: nueve de
los diez envios de reactivacion salieron sobre conversaciones derivadas y sin
atender.

## La senal: quien escribio, no quien esta asignado

Una derivacion esta atendida cuando **una persona del equipo escribio un
mensaje publico en esa conversacion, despues de la derivacion**.

El asignado no sirve como senal: las derivaciones de este inbox van a un team
sin asignado individual, asi que la conversacion figura sin humano incluso
mientras alguien la esta contestando. Es la misma razon por la que el scanner
de reactivacion mide silencio del equipo en vez de mirar `assignee`.

Un mensaje del equipo es, con el criterio que ya aplica
`seconds_since_last_team_message`: `private = false`, `message_type` 1 o 3, y
`sender.type == "user"`. Los del agente salen como `agent_bot` y no cuentan;
las actividades de sistema (`message_type` 2) tampoco, porque asignar una
conversacion no es atender a nadie.

## Quien marca, y cuando

El bridge, en el handler del webhook de Chatwoot donde
`classify_chatwoot_event` devuelve `pause_automation`. Ese es exactamente el
evento "una persona del equipo escribio": el bridge ya lo usa para poner la
etiqueta `automation_paused`.

Se eligio ese punto y no el scanner de reactivacion porque el scanner puede
estar apagado --- lo esta desde el 2026-09-27 --- y entonces `attended_at`
naceria siempre vacio.

El momento se toma del reloj del bridge (`datetime.now(UTC)`), no del payload.
El unico fixture capturado de `message_created` es de un entrante
(`chatwoot_paused_lead_reply_inbox_9_conv_177_20260925.json`) y todavia no se
capturo uno saliente de una persona; suponer la forma de ese campo seria
escribir contra un payload inventado. La diferencia es de segundos y la RPC
recorta cualquier fecha futura con `least`.

La llamada **falla blanda**: la pausa ya quedo puesta, que es lo que protege al
lead. Si la marca no sale, la derivacion sigue contando como pendiente y lo
unico que pasa es que nadie reactiva esa conversacion.

## La RPC

`mark_human_handoff_attended(p_external_conversation_id bigint,
p_attended_at timestamptz, p_now timestamptz default clock_timestamp())`

Devuelve `outcome`, `attended_count` y `attended_commercial_case_id`.

| `outcome` | Cuando |
|---|---|
| `attended` | marco al menos una derivacion |
| `noop` | el caso existe y no quedaba ninguna esperando |
| `not_found` | Chatwoot conoce la conversacion y Supabase no |

`noop` no es un fallo: es lo normal a partir del segundo mensaje del equipo.

Marca las derivaciones del caso que cumplen las tres condiciones: `attended_at`
nulo, `status` en `requested`/`projected`/`projection_failed`, y **`created_at`
anterior o igual al momento de atencion**. Esa tercera condicion es la que
impide que el primer mensaje del equipo marque como atendidas derivaciones que
todavia no existen.

Solo `service_role` la ejecuta. Es la unica funcion de la allowlist de
`validate_handoff_postgres.py` que **escribe** en `human_handoff_requests`, y
solo toca esa columna.

## Que cambia para quien ya leia la tabla

Nada. `attended_at` es nulable y el CHECK de `status` queda igual: el proyector
de notas y el conector de Slack siguen viendo los mismos cuatro valores. El
indice de unicidad `human_handoff_requests_one_live_per_commercial_case_idx`
tampoco se toca, asi que `request_inbound_human_handoff` sigue rechazando
exactamente las mismas derivaciones que antes.

## Quien lo consume

1. **`claim_conversation_reactivation`**: con una derivacion sin atender
   devuelve `blocked_pending_handoff` y no reserva el envio. El outcome es
   nuevo; el cliente del bridge lo acepta y el barrido lo cuenta por su nombre
   en el resumen (`blocked_pending_handoff=N`) en vez de `not_reactivated`.
2. **`resume_paused_conversation`**: `projected` sin atender ya cuenta como
   pendiente, asi que la respuesta del lead no levanta la pausa. Su outcome
   `blocked_pending_handoff` ya existia; lo que cambia es cuando aplica.

## Como sale una conversacion de este estado

Por decision de una persona, por cualquiera de los tres caminos que ya
existian:

1. contestarle al lead (el bridge marca `attended_at` y despues la reanudacion
   procede);
2. sacar la etiqueta `automation_paused` a mano en Chatwoot (camino del
   PR #186);
3. correr el macro de reanudacion (`CHATWOOT_RESUME_MACRO_ID`).

No hay salida automatica, y es a proposito: el lead pidio un humano.

## La metrica que habilita

`human_handoff_requests_unattended_idx` (parcial, sobre `created_at`) responde
cuantas derivaciones estan esperando y desde cuando. El 2026-09-27 habia 8
conversaciones esperando respuesta, la mas vieja hacia 72 horas.

## Lo que este contrato NO cubre

- **No marca las derivaciones historicas.** Quedan con `attended_at` nulo: el
  dato de si alguien las contesto no esta en esta base.
- **No avisa.** Que una derivacion lleve 72 horas sin atender no dispara nada;
  la cola se consulta.
- **No distingue por que se derivo.** `explicit_human_request`,
  `commercial_exception` y `policy_requires_human` se tratan igual: en los tres
  casos el caso esta esperando a una persona.

## Verificacion

Comportamiento contra el esquema real:
`tests/sql/followup_engine/validate_handoff_attendance.mjs`. Propiedades
estructurales: `tests/test_handoff_attendance_migration.py`. El barrido:
`tests/test_reactivation.py`.

La evidencia en produccion queda para `docs/operations/` despues del
despliegue.
