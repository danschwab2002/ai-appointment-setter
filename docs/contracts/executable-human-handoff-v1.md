# Contrato V1 — handoff humano ejecutable

- **Estado:** Implementado en el árbol; no desplegado
- **Versión:** 1
- **Fecha:** 2026-08-10
- **Alcance:** casos Lancemos con conversación Chatwoot canónica existente

## 1. Configuración

Todos los efectos permanecen apagados por defecto.

- `HUMAN_HANDOFF_PROJECTION_ENABLED=false`
- `HUMAN_HANDOFF_ADMISSION_ENABLED=false`
- `HUMAN_HANDOFF_PROJECTION_WORKER_ID`
- `HANDOFF_PROJECTION_POLICY_KEY`
- `HANDOFF_PROJECTION_POLICY_VERSION`

Proyección habilitada exige Supabase, control plane Chatwoot, account/inbox canónicos y worker ID. Admisión habilitada exige además proyección, dispatcher outbound y perímetro Lancemos. Configuración incompleta impide arrancar.

## 2. Propuesta Hermes

El drafting durable acepta exactamente una de estas formas:

```json
{"strategy":"texto no vacío, máximo 120","message":"texto no vacío, máximo 500"}
```

```json
{"proposal":"suggest_handoff","reason_code":"commercial_exception"}
```

Los únicos `reason_code` válidos para handoff son:

- `explicit_human_request`;
- `commercial_exception`;
- `policy_requires_human`.

La sugerencia no inicia requests ni llama Chatwoot. El bridge invoca la RPC durable con action, attempt, worker y generación de lease.

## 3. Admisión durable

`request_human_handoff` recibe:

- `recovery_case_id`;
- `command_key` idempotente;
- motivo y requester;
- policy key/version;
- opcionalmente action/attempt/worker/generación, todos juntos.

`requested_by=agent` exige siempre esos cuatro campos y el fence de la reserva;
no existe un camino agent source-less. `system` y `operator` pueden originar un
stop operativo sin intento outbound.

La policy activa deriva scope piloto, equipo y nota. La RPC exige:

- caso no terminal con `conversation_id`;
- `pilot_recovery_case_bindings` coincidente;
- scope publicado Lancemos/WhatsApp/Hotmart/cart recovery;
- identidad seleccionada activa con account/inbox del scope;
- conversación del mismo contacto e identidad con ID externo positivo;
- source attempt todavía `reserved` y lease vigente cuando la sugerencia viene del dispatcher.

En una sola transacción:

1. crea o reutiliza un request;
2. fija snapshots inmutables de policy, scope y routing;
3. crea efectos `assignment` y `private_note`;
4. cierra reservas como `failed_before_request`;
5. conserva intentos `request_started` como `delivery_unknown` reconciliable sin
   vencimiento artificial; una aceptación tardía puede cerrar el intento pero no
   crear sucesores después del stop;
6. cancela acciones futuras;
7. pausa secuencia, caso y conversación.

Un replay con la misma command key y semántica responde `already_requested`, incluso si llega concurrentemente. La RPC serializa por command key antes de leer el replay. Cambiar motivo, requester, source o policy bajo la misma key produce `human_handoff_command_conflict`. Un request ya existente tampoco acepta evidencia bajo otra versión de policy.

## 4. Proyección Chatwoot

Los efectos usan leases con owner, generación y vencimiento calculados contra el reloj de PostgreSQL, nunca contra el timestamp enviado por el caller. Claim deriva routing sólo del request snapshot y vuelve a comprobar caso, binding, scope, identidad y conversación canónica.

### Assignment

1. assignee con `assignee_type=User`: `applied`, sin POST;
   `AgentBot` no cuenta como humano y un tipo desconocido falla cerrado;
2. equipo esperado presente: `applied`, sin POST;
3. otro equipo sin persona: `conflict`, sin POST;
4. sin assignee ni equipo: POST del equipo esperado y GET de confirmación.

### Private note

La nota usa un marcador estable:

```text
[supportmagician-handoff:<request_id>:<template_key>:v<version>]
```

El worker escanea hasta un límite explícito que falla cerrado si no alcanza el borde del historial. Cero marcadores permite un POST; uno confirma idempotencia; más de uno es conflicto. Tras un POST incierto, el estado `delivery_unknown` sólo permite escanear: nunca vuelve a crear la nota automáticamente y termina en `dead_letter` al alcanzar el límite de intentos.

La proyección durable no muta labels, macros ni mensajes públicos. En el flujo
inbound con respuesta automática, el work que originó el handoff aplica una
postcondición adicional y ordenada:

1. envía o reconcilia exactamente una respuesta pública segura;
2. ejecuta el macro de pausa para asegurar `automation_paused`;
3. confirma la etiqueta antes de completar el work.

La etiqueta ya presente es éxito idempotente. Un error HTTP/protocolo o una
postcondición no confirmada conserva el work en retry; nunca habilita una segunda
respuesta. Esta postcondición no agrega un tercer efecto durable ni cambia que
`assignment` y `private_note` gobiernan el estado `projected` del request.

## 5. Estados

Request:

- `requested`;
- `projection_failed`;
- `projected`;
- `dead_letter`.

Efecto:

- `pending`;
- `retryable_failed`;
- `delivery_unknown`;
- `applied`;
- `conflict`;
- `dead_letter`.

Finalización exige lease vigente y generación exacta contra el reloj de PostgreSQL. Dos efectos `applied` llevan el request a `projected`. Un efecto en dead letter no reanuda automatización ni impide drenar su efecto hermano pendiente.

## 6. Readiness y rollback

Con proyección habilitada, `/ready` consulta `get_human_handoff_projection_status` y publica sólo conteos de pending, retryable, delivery unknown, conflicts y dead letters. Un fallo de la dependencia responde 503 `human_handoff_readiness_unavailable`.

Rollback seguro:

1. apagar admisión;
2. mantener proyección para drain;
3. resolver backlog/conflictos/dead letters;
4. apagar proyección.

Desactivar una policy impide requests nuevos y no altera snapshots ni efectos existentes.

## 7. Compatibilidad y evidencia

La migración y el runtime son aditivos y default-off. Las tablas niegan DML directo a roles API; sólo los RPC explícitos tienen `EXECUTE` para `service_role`. Policies, identidad de request, evidencia y tipo de efecto están protegidos contra mutación.

Las pruebas locales y PGlite no acreditan migración remota, Chatwoot real, equipo real, worker productivo ni mensajes enviados.
