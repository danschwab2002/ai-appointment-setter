# El aviso de una persona derivada que sigue esperando (handoff-wait-alerts-v1)

- **Estado:** contrato de la construcción (E-309, P-20 del plan del 2026-11-02; diagnóstico E-308). Apagado por defecto (`HANDOFF_WAIT_ALERTS_ENABLED`).
- **Decisión de Dan:** 2026-11-03, «Dale, apuntado a Mari»: arranca en Maribel, portable para ATT1 y prendido solo en Maribel. Los defaults de criterio (D1 a D7) están en el reporte de E-309 para que Dan los revise.
- **Migración:** `20261103001000_handoff_wait_alerts_v1.sql`. **Código:** `src/bridge/handoff_wait_alerts.py`; la tarjeta, en `src/slack_correlation/catalog.py`.

## El problema

La tarjeta `HND-001` sale cuando el bridge deriva y después nadie vuelve a avisar. Medido el 2026-11-02 en solo lectura (`esperas.sh` de E-308): en Maribel 3 de las 4 derivaciones seguían sin respuesta, con 66 a 126 h de espera y la ventana de 24 h cerrada; en ATT1 la mediana es de 6,1 h, la del fin de semana de 59 h, y una sola persona contesta 31 de cada 33. `attended_at` (`20260927000300`) coincide con Chatwoot en 37 de 37 derivaciones de ATT1.

## Los tres avisos

Al canal de Slack de la aliada, por su conector (el mismo productor que `HND-001`, `MSG-002` y `MOT-002`):

| Aviso | Código | Cuándo |
|---|---|---|
| Derivación sin respuesta del equipo | `HND-006` | la derivación más nueva sin atender de una conversación abierta lleva `HANDOFF_WAIT_ALERT_AFTER_HOURS` horas corridas (2 por defecto) sin un mensaje del equipo, a cualquier hora |
| Se cierra la ventana de 24 h | `HND-005` | en esa misma conversación faltan 2 h o menos para que se cierren las 24 h desde el último mensaje de la persona, a cualquier hora |
| La persona volvió a escribir en una conversación pausada | `HND-012` (nuevo) | una conversación abierta con `automation_paused`, sin derivación sin atender, donde el último saliente fue del equipo y la persona escribió después, con el mismo umbral de horas corridas desde su primer mensaje sin contestar |

**Lo que cuenta como respuesta del equipo:** un mensaje público saliente (`message_type` 1 o 3) de un `user` de Chatwoot, el mismo criterio con el que el bridge marca `attended_at`. El agente (`agent_bot`) y las notas privadas, también la del bridge, no cuentan. Si el historial ya muestra la respuesta, la derivación se da por atendida aunque `attended_at` todavía no haya llegado.

**El recordatorio de la ventana (E-486):** HND-005 sale `HANDOFF_WAIT_ALERT_WINDOW_WARNING_HOURS` antes de que cierre la ventana de 24 h (1-20, por defecto 2, lo de siempre). Con 4, sale a las 20 h del último mensaje de la persona.

**Lo que nunca avisa:** una conversación que no está abierta, una con `automation_opted_out` (las bajas «No más mensajes»: 11 de las 14 que esperaban en ATT1 el 02/11), una pausada cuyo último saliente fue del agente (la pausa al comprar, la confirmación de una baja), y lo que empezó a esperar hace más de `HANDOFF_WAIT_ALERT_MAX_AGE_HOURS` (168 por defecto). La lectura de la base ya deja afuera las derivaciones más viejas que ese techo.

## Sin corte por horario

Las horas se cuentan de reloj, sin calendario (Dan, 2026-11-03, E-309b: «no tiene mucho sentido, no es necesario un corte por horario»). Una derivación del sábado a la noche avisa a las 2 h aunque el equipo no atienda a esa hora. La primera versión (E-309) contaba horas hábiles con `HANDOFF_WAIT_ALERT_BUSINESS_DAYS` y `HANDOFF_WAIT_ALERT_BUSINESS_HOURS`; se sacaron antes de prenderse en ninguna aliada, y `HANDOFF_WAIT_ALERT_AFTER_BUSINESS_HOURS` pasó a llamarse `HANDOFF_WAIT_ALERT_AFTER_HOURS`.

## Qué lleva la tarjeta

El número de conversación, el motivo de la derivación (una frase cerrada por motivo conocido; un motivo nuevo sale como código), las horas de espera de reloj al primer aviso y, en `HND-005`, la hora en que se cierra la ventana (Slack la muestra en la hora de quien lee). Con la base de Chatwoot del tenant (`SLACK_TENANT_CONVERSATION_BASE_URLS_JSON` del conector), el botón «Ir a la conversación», el mismo que `HND-001`. **No lleva** teléfono, mail, nombre ni texto de ningún mensaje: el comando no los tiene. La tarjeta va al canal, no al hilo de la `HND-001` (`subject_ref` vacío).

## Un aviso por conversación y ciclo

El ciclo es la derivación (`HND-006`), el último mensaje de la persona (`HND-005`) o el primer mensaje que nadie contestó (`HND-012`). El `event_id` es un UUID v5 de la cuenta, la bandeja y el ciclo, así que volver a admitirlo da `duplicate` en el conector, o 409 si cambió algún número (las horas de espera de otra corrida), y no se vuelve a publicar. Lo admitido se recuerda en memoria para no pedirlo en cada barrido; después de un reinicio se pide una vez más y el conector lo frena. Un rechazo terminal del conector no se reintenta; un resultado incierto, sí, en el barrido siguiente.

## La lectura de la base

`list_unattended_human_handoffs_v1(p_chatwoot_account_id, p_chatwoot_inbox_id, p_since, p_limit)`: solo lectura, `security definer`, solo `service_role`. Por conversación de esa cuenta y bandeja, la derivación más nueva con `attended_at` nulo, en `requested`, `projected` o `projection_failed`, creada desde `p_since`: `handoff_request_id`, `external_conversation_id`, `primary_reason_code`, `detail_reason_code` y `created_at`, de la más vieja a la más nueva, hasta `p_limit` (1 a 500). No devuelve la nota privada ni nada de la persona. Entrada inválida: `22023`. Correr la migración dos veces aborta con `55000` sin cambiar nada.

## El barrido

`HandoffWaitAlertSweeper`, cada `HANDOFF_WAIT_ALERT_INTERVAL_SECONDS` (600, entre 60 y 3600):

1. Pide a la base las derivaciones sin atender de la bandeja.
2. Lista las conversaciones abiertas con actividad dentro del techo, sin pedir sus mensajes.
3. Lee el detalle y el historial solo de las que tienen una derivación sin atender o la etiqueta `automation_paused`, sin baja, hasta 60 por barrido (primero las derivaciones).
4. Decide con `evaluate_wait_alerts` y admite lo que tocó.

Un barrido caído no tumba el bridge. `/ready` publica, solo con el flag prendido, `handoff_wait_alerts` (`never`, `healthy`, `error`, `stopped`), `handoff_wait_alerts_last_scan` (los conteos del último barrido, sin números de persona) y `handoff_wait_alerts_after` (`2h`). Logs: `handoff_wait_scan <conteos>` y `handoff_wait_alert_admitted kind=… conversation=… reason=… waited_hours=…`.

## Lo que exige

El manifiesto, el cliente de control de Chatwoot con la cuenta y la bandeja, Supabase con la migración `20261103001000` y el conector de Slack de la aliada (`SLACK_CONNECTOR_PROJECTION_ENABLED` con su URL y su token). Sin alguno, el bridge no arranca con el flag prendido. El conector tiene que ser de la misma versión o posterior: el `HND-012` y la tarjeta nueva viven en su catálogo.
