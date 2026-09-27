# ADR-0019: una derivacion sin atender frena la automatizacion

- Estado: aceptada
- Fecha: 2026-09-27
- Contexto medido: produccion, inbox 9 (WABA `whatsapp_cloud`, canal 8)

## Contexto

El 2026-09-27 se midio el choque entre tres piezas que trataban
`automation_paused` como si significara siempre lo mismo.

La **derivacion** (`request_human_handoff`, `request_inbound_human_handoff`)
pausa porque el lead pidio una persona, o porque el sistema no puede seguir
solo. La pausa es el estado correcto y tiene que durar hasta que alguien del
equipo conteste.

La **reactivacion de conversaciones** (migracion `20260923000200`) asume que
toda pausa es un lead que se enfrio y hay que rescatarlo con una plantilla
aprobada.

La **reanudacion** (migracion `20260923000100`) apaga `human_takeover` cuando
el lead responde, sin preguntar si una persona llego a atenderlo.

Lo medido, cruzando `conversation_reactivation_events` contra
`human_handoff_requests`:

| conversacion | reactivada | motivo de la derivacion | estado | `human_takeover` |
|---|---|---|---|---|
| 186 | 27/09 17:20 | `explicit_human_request` | `projected` | true |
| 173 | 27/09 14:33 | `commercial_exception` | `projected` | true |
| 185 | 27/09 14:33 | `explicit_human_request` | `projected` | true |
| 177 | 25/09 20:36 | `policy_requires_human` | `projected` | true |
| 110, 124, 63, 136, 143 | 23-24/09 | sin detalle (previas a `20260924000100`) | `projected` | true |
| 126 | 23/09 23:06 | sin derivacion | -- | false |

**Nueve de los diez envios salieron sobre una conversacion derivada a una
persona y todavia sin atender.** En la conversacion 186 el lead habia pedido
hablar con alguien del equipo a las 14:07 del 26/09; el 27/09 a las 17:20
recibio "Hola, Gustavo. Soy el asistente virtual del equipo de la Psic.
Johanna". Ninguna persona le contesto nunca.

La misma conversacion se despauso dos veces (26/09 17:08 y 27/09 18:11) con
`inbound_after_quiet_period`: la derivacion se borraba sola cuando el lead
volvia a escribir.

El feature de reactivacion no tenia un caso borde. Su poblacion *era* la cola
de derivaciones sin atender: todos los caminos que ponen
`automation_status = 'paused'` en la capa durable son handoffs
(`20260810000400`, `20260823000100`, `20260924000100`), y pausa por
inactividad no existe en este sistema.

## El dato que faltaba

`human_handoff_requests.status` va `requested -> projected` y se queda ahi para
siempre. Ninguno de sus cuatro valores (`requested`, `projected`,
`projection_failed`, `dead_letter`) significa "ya la contestaron": `projected`
solo dice que la nota privada llego a Chatwoot. Sin esa distincion, ni la
reactivacion ni la reanudacion podian saber si la derivacion seguia viva.

El guard que `resume_paused_conversation` ya tenia (`blocked_pending_handoff`)
cubria unicamente `requested` y `projection_failed`, o sea los segundos en que
el proyector de notas esta trabajando. Una vez `projected`, no aplicaba.

## Decision

1. **`human_handoff_requests.attended_at`**, nulable: el momento en que una
   persona del equipo escribio en esa conversacion despues de la derivacion.
2. **`mark_human_handoff_attended`** la marca, y la llama el bridge en el
   mismo punto en el que ya detecta que una persona escribio y pausa la
   automatizacion (`decision.action == "pause_automation"`).
3. **`claim_conversation_reactivation`** devuelve `blocked_pending_handoff` y
   no reserva el envio mientras haya una derivacion sin atender.
4. **`resume_paused_conversation`** cuenta una derivacion `projected` sin
   atender como pendiente, asi que la respuesta del lead deja de levantar la
   pausa.

## Alternativas descartadas

**Un estado terminal nuevo en `status`.** Es lo primero que se penso y se
descarto: esa columna la leen el proyector de notas y el conector de Slack, y
un quinto valor les cambia el contrato a los dos. La migracion `20260923000100`
ya habia evitado tocarla por el mismo motivo. Una columna nulable no obliga a
nadie a cambiar: quien no la lea sigue funcionando igual.

**Ampliar `human_handoff_requests_one_live_per_commercial_case_idx` a
`projected`.** Habria dado el guard gratis, pero ese indice define cuando
`request_inbound_human_handoff` rechaza una derivacion nueva con
`inbound_handoff_live_request_conflict`: ampliarlo haria fallar derivaciones
legitimas de una conversacion que ya tuvo una.

**Marcar la atencion desde el scanner de reactivacion**, que ya lee el
historial y calcula `seconds_since_last_team_message`. Se descarto porque con
la reactivacion apagada (que es como quedo el sistema el 27/09) nadie marcaria
nada, y `attended_at` naceria siempre vacio.

**Saltear el candidato en el bridge antes de llamar a la RPC.** La
elegibilidad se evalua contra Chatwoot, que no sabe de derivaciones; saberlo
exigiria una consulta a Supabase por conversacion, duplicando llamadas para
adelantar un veredicto que la capa durable ya da. El bridge respeta el
rechazo, lo cuenta por su nombre en el resumen del barrido y no manda.

## Consecuencias

Una conversacion derivada y sin atender queda fuera del alcance de la
automatizacion hasta que un humano intervenga, por cualquiera de los tres
caminos que ya existen: contestarle (el bridge marca `attended_at`), sacar la
etiqueta `automation_paused` a mano, o correr el macro de reanudacion. Los tres
son decisiones de una persona, que es exactamente lo que el lead pidio.

Las derivaciones historicas quedan con `attended_at` nulo. Es lo unico honesto
--- el dato de si alguien las contesto no esta en esta base --- y en la
practica deja las conversaciones derivadas viejas en manos del equipo.

El indice parcial `human_handoff_requests_unattended_idx` habilita la metrica
que no existia: cuantas derivaciones estan esperando y desde cuando. El
2026-09-27, con la reactivacion recien apagada, habia 8 conversaciones
esperando respuesta, la mas vieja hacia 72 horas.

Este ADR describe lo que se construyo y probo; la evidencia de su efecto en
produccion queda para `docs/operations/` despues del despliegue.
