# Contrato del perímetro de Lancemos V1

- **Estado:** Implementado localmente en fase 1; wiring runtime de fase 2 pendiente
- **Versión:** `lancemos-pilot-boundary-v1`
- **Fecha:** 2026-08-10
- **Autoridad:** PostgreSQL/Supabase
- **Canal y propósito:** `whatsapp/cart_recovery`
- **No implica:** DDL desplegado, scope real publicado ni outbound activado

## 1. Identidad del scope

Una versión se identifica por:

```text
(scope_key, version)
```

V1 exige valores exactos y no vacíos para:

```text
tenant_key = lancemos
chatwoot_account_id > 0
chatwoot_inbox_id > 0
channel = whatsapp
channel_provider
channel_account_ref
source = hotmart
source_event_type = PURCHASE_OUT_OF_SHOPPING_CART
external_product_id
offer_code
purpose = cart_recovery
policy_key
policy_version > 0
timezone
max_cohort_contacts > 0
max_outbound_request_starts_total > 0
max_outbound_request_starts_per_day > 0
```

`channel_account_ref` es una referencia opaca al número/cuenta configurada. No es un teléfono ni se registra en logs.

## 2. Estados

### Scope version

- `draft`: modificable por dueño de base/migración; no evaluable.
- `published`: inmutable y elegible para activación.

### Runtime

- `inactive`: default; bloquea.
- `armed`: única condición positiva de runtime.
- `paused`: kill switch reversible; bloquea.
- `closed`: terminal; bloquea y no puede rearmarse.

Transiciones permitidas:

```text
inactive → armed|paused|closed
armed    → paused|closed
paused   → armed|closed
closed   → closed (replay solamente)
```

Toda transición exige `expected_generation`, actor y motivo no vacíos. Una transición efectiva incrementa la generación. Un replay exacto no crea otro evento.

### Activación de versión

```text
activate_lancemos_pilot_scope_version(
  scope_key,
  target_scope_version,
  expected_generation,
  actor,
  reason
) → (scope_version, runtime_state, generation, changed, reason_code)
```

La versión objetivo debe estar publicada. Sólo puede reemplazarse la versión activa desde `inactive|paused`; `armed` exige pausar primero y `closed` es terminal. Toda activación efectiva fuerza `runtime_state=inactive`, incrementa generación y crea `pilot_scope_version_activated`. La membresía es versionada y no se copia. El presupuesto se cuenta por `scope_key` a través de todas las versiones, por lo que un cambio o rollback no reinicia caps.

## 3. Cohorte

La membresía se identifica por:

```text
(scope_key, scope_version, contact_id)
```

Estados:

- `active`;
- `removed`.

La inscripción activa es idempotente. Reactivar una membresía removida es una transición auditada. El conteo de miembros activos nunca puede superar `max_cohort_contacts` bajo transacciones concurrentes.

## 4. Evaluación temprana

RPC conceptual:

```text
evaluate_lancemos_pilot_scope(
  scope_key,
  scope_version,
  tenant_key,
  chatwoot_account_id,
  chatwoot_inbox_id,
  channel_provider,
  channel_account_ref,
  source,
  source_event_type,
  external_product_id,
  offer_code,
  contact_id
) → (allowed, reason_code, runtime_generation)
```

No consume presupuesto. Un resultado `allowed=true` sólo significa que los datos están dentro del scope en ese instante. No autoriza un efecto futuro.

Precedencia de razones:

1. entrada inválida;
2. scope inexistente/no publicado;
3. versión runtime distinta;
4. runtime no armado;
5. tenant;
6. account/inbox;
7. canal/cuenta;
8. source/evento;
9. producto/oferta;
10. cohorte.

## 5. Autorización de request-start

RPC conceptual:

```text
authorize_lancemos_pilot_request_start(
  ...misma identidad del scope...,
  action_id,
  attempt_id,
  contact_id,
  now
) → (
  authorized,
  reason_code,
  runtime_generation,
  request_authorization_id,
  replayed
)
```

Además de repetir la evaluación, debe demostrar desde estado canónico que:

- `attempt_id` corresponde a `action_id`;
- el attempt está en `reserved` y todavía no tiene request iniciado;
- action → case corresponde a `contact_id`;
- case tiene producto/oferta del scope;
- la identidad seleccionada pertenece a account/inbox del scope;
- la cohorte está activa;
- caps total y diario conservan capacidad.

`now` sólo puede diferir hasta cinco minutos del reloj autoritativo de PostgreSQL. Fuera de esa ventana retorna `pilot_request_time_invalid`. La fecha presupuestaria y `authorized_at` siempre se calculan con `clock_timestamp()` del servidor; el caller no puede elegir otro día para eludir el cap diario.

La autorización inserta una fila append-only antes de que el request externo pueda comenzar. El posterior wiring debe hacer que esta autorización y `mark_followup_request_started` formen una única frontera transaccional; fase 1 no declara esa integración terminada.

Un replay exacto devuelve la autorización durable original aunque el runtime se haya pausado, la cohorte haya cambiado o se haya activado otra versión después. `replayed=true` significa **efecto ya cruzado o respuesta previa recuperada**: el caller nunca debe iniciar otra llamada externa a partir de ese resultado. Los parámetros de identidad del scope sí deben seguir coincidiendo; un replay con tenant/account/inbox/canal/producto/oferta diferentes falla cerrado.

Semántica de presupuesto:

- cuenta autorizaciones durables de request-start, no entregas confirmadas;
- una entrega incierta no devuelve cupo;
- replay del mismo attempt no consume otro cupo;
- el día se calcula con la timezone publicada;
- los consumos de versiones anteriores del mismo `scope_key` siguen contando;
- la `timezone` permanece constante entre versiones del mismo `scope_key`; un cambio de zona requiere cerrar el piloto y una decisión operativa explícita, no un rollover de versión.

## 6. Kill switch

RPC conceptual:

```text
set_lancemos_pilot_runtime_state(
  scope_key,
  scope_version,
  expected_generation,
  target_state,
  actor,
  reason
) → runtime control
```

El cambio y la autorización de request-start bloquean la misma fila. Una pausa confirmada bloquea toda autorización posterior. Si una autorización confirmó primero, ese request se considera ya iniciado desde la perspectiva de seguridad.

## 7. ACL

- RLS habilitado en todas las tablas nuevas.
- `anon` y `authenticated`: sin DML ni EXECUTE.
- `service_role`: sin DML directo.
- `service_role`: EXECUTE sólo en los entrypoints explícitos.
- funciones internas y triggers: sin EXECUTE efectivo para roles API.
- funciones elevadas: `SECURITY DEFINER` y `SET search_path = public, pg_temp`.

## 8. Auditoría y privacidad

Eventos mínimos:

- `pilot_runtime_state_changed`;
- `pilot_scope_version_activated`;
- `pilot_cohort_member_enrolled`;
- `pilot_cohort_member_removed`;
- `pilot_outbound_request_authorized`.

La evidencia contiene IDs internos, versión, generación y reason codes. No contiene teléfono, JID, email, nombre, contenido de mensajes, tokens ni payloads externos.

## 9. Compatibilidad y rollout

- La migración sólo agrega objetos; no activa nada.
- Sin scope real publicado y runtime `armed`, todos los entrypoints bloquean.
- La allowlist de pruebas actual no se elimina en fase 1.
- La fase 2 debe reemplazarla por la conjunción completa, no simplemente deshabilitarla.
- WABA y templates siguen siendo gates externos separados.
