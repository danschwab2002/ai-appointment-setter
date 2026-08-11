# Diseño base — handoff humano ejecutable para el piloto

- **Estado:** Implementada en el árbol; no desplegada ni validada contra Chatwoot real
- **Fecha:** 2026-08-10
- **Alcance V1:** handoff por caso cuando ya existe una conversación canónica de
  Chatwoot
- **Fuera de V1:** crear una conversación sólo para escalar antes del primer
  contacto; esos casos siguen fail-closed como escalación operativa sin handoff
  conversacional
- **No implica:** migración desplegada, responsable configurado ni E2E real

## 1. Brecha actual

El sistema detecta takeover humano y reevalúa `human_takeover`, `paused_human` y
`automation_paused`. También puede producir reason codes de escalación. Pero el
runtime durable no convierte una escalación autorizada en una pausa durable más
una tarea visible para una persona.

La V1 debe lograr, para un caso que ya posee `recovery_cases.conversation_id`:

```text
reevaluación o sugerencia acotada
  → decisión determinística del bridge
  → pausa durable y cierre pre-request
  → request de proyección
  → asignación + nota privada reconciliadas por separado
```

Supabase detiene primero. Chatwoot es la superficie operativa recuperable.

## 2. Autoridad y alcance

### Supabase

Es autoridad de:

- existencia e identidad idempotente del handoff;
- pausa del caso, secuencia y conversación interna;
- cancelación de acciones futuras;
- cierre de reservas que no cruzaron `request_started`;
- preservación de efectos ya iniciados como aceptados o inciertos, sin sucesor;
- estado y lease de cada efecto de proyección Chatwoot.

### Chatwoot

Es autoridad de la conversación y superficie del operador. La conversación
externa se deriva **exclusivamente** de `recovery_cases.conversation_id`, validando
que pertenece al contacto y a `selected_channel_identity_id`. Nunca se usa
`channel_identities.external_conversation_id` como fallback.

### Hermes

Hermes no decide la acción ejecutable. En el camino durable actual, la reserva y
su `attempt_id` siguen existiendo antes del drafting. Hermes puede devolver:

- `draft_message`; o
- `suggest_handoff` con un motivo comercial allowlisted.

El bridge decide si la sugerencia satisface una política determinística. Si no,
la rechaza. Si sí, una RPC cierra la reserva antes de `request_started` y crea el
handoff en la misma transacción. No existe una acción de modelo llamada `send`.

Las guardas determinísticas que ya producen pausa/escalación pueden crear el
handoff antes de reservar, dentro de su propia reevaluación transaccional, sólo
para reason codes explícitamente allowlisted.

## 3. Garantía correcta ante carreras

Después del commit de handoff **no puede comenzar un nuevo request externo**.
Un request que ya ganó `request_started` puede ser aceptado después del commit;
su realidad se preserva y reconcilia, y nunca genera sucesor. La proyección debe
mostrar que puede existir un efecto en vuelo o incierto.

Orden de locks para competir con request-start:

```text
contacto → caso → secuencia → acción → intento
```

La conversación interna se actualiza después de asegurar el aggregate. No se
inserta `conversation` entre case y sequence ni se cambia el orden global de las
RPC de compra, opt-out y request-start.

## 4. Modelo durable mínimo

### `human_handoff_requests`

- `id uuid`;
- `recovery_case_id uuid`;
- `conversation_id uuid`;
- `source_action_id uuid null`;
- `source_attempt_id uuid null`;
- `command_key text not null unique`;
- `primary_reason_code text`;
- `requested_by text`;
- `projection_policy_key text`;
- `projection_policy_version integer`;
- `status text`: `requested | projected | projection_failed | dead_letter`;
- lease, intentos, próximo retry y error estable;
- timestamps.

Restricciones físicas:

- `command_key` da replay exacto aun con origen nullable;
- índice único parcial por `recovery_case_id` para estados vivos;
- un segundo motivo sobre un request vivo agrega evidencia append-only y no crea
  otra proyección;
- `conversation_id` es obligatorio en la V1.

### Política de proyección versionada

Cada request fija una versión inmutable que contiene:

- `expected_team_id`;
- `note_template_key` y versión;
- account/inbox del scope piloto.

IDs y contenido provienen de configuración aprobada, nunca de Hermes ni del
caller de la RPC.

### Efectos reconciliables

No se usa un macro de Chatwoot como unidad atómica: en 4.13.0 sólo encola un job,
continúa ante errores parciales y la macro es mutable.

La proyección mantiene dos efectos tipados e idempotentes:

1. `assignment`;
2. `private_note`.

Cada efecto tiene estado, lease, intentos y evidencia remota. El request es
`projected` sólo cuando ambos están confirmados.

No se muta la lista de etiquetas en este flujo: Chatwoot 4.13.0 sólo expone un
reemplazo completo, no un add atómico, y un `GET → merge → POST` podría perder
etiquetas concurrentes. `automation_paused` conserva su función en el takeover
humano de ADR-0002, pero el handoff solicitado se hace visible mediante
asignación y nota; el stop durable no depende de ninguna etiqueta.

La nota privada contiene un marcador estable derivado de `handoff_request_id` y
un texto fijo versionado sin PII adicional. Antes del POST se busca exactamente
ese marcador en la conversación canónica. La asignación queda confirmada si la
conversación tiene el `expected_team_id` o un assignee con `assignee_type=User`.
Un `AgentBot` no cuenta como humano; un tipo desconocido falla cerrado. Un team
distinto sin assignee humano es conflicto operativo y no se sobrescribe.

## 5. Entry points

### `request_human_handoff(...)`

`SECURITY DEFINER`, sólo `service_role`. Recibe IDs internos, worker/lease y
reason code. Deriva caso, política, conversación y scope. No acepta account,
inbox, JID, team, assignee, nota ni provider desde el caller.

En una sola transacción:

1. usa el orden global de locks;
2. valida caso, conversación canónica, versiones y lease cuando corresponda;
3. rechaza casos sin conversación (`handoff_conversation_unavailable`);
4. gana frente a reservas pre-request;
5. preserva efectos post-request;
6. pausa caso, secuencia y conversación interna;
7. cancela acciones futuras;
8. crea/reusa request, efectos y auditoría.

### Claim/finalización

Claims separados o un claim tipado para los dos efectos, con lease, reloj
autoritativo de PostgreSQL y batch acotado. La finalización idempotente no
modifica la vigencia del stop. Un error o dead letter nunca reanuda automatización.

Helpers internos y tablas no tienen DML/EXECUTE para roles API.

## 6. Reason codes V1

Crean handoff visible sólo:

- `explicit_human_request`;
- `commercial_exception`;
- `policy_requires_human`.

`insufficient_context` antes de existir conversación queda como escalación
operativa fail-closed, no como handoff conversacional V1.

No crean handoff nuevo:

- opt-out o denied/restricted;
- compra aprobada/caso cerrado;
- `human_takeover_active` ya observado;
- reconciliación técnica inconclusa.

## 7. Configuración y rollback

Flags separados:

```text
HUMAN_HANDOFF_ADMISSION_ENABLED=false
HUMAN_HANDOFF_PROJECTION_ENABLED=false
HUMAN_HANDOFF_PROJECTION_WORKER_ID=
CHATWOOT_HANDOFF_TEAM_ID=
HANDOFF_PROJECTION_POLICY_KEY=
HANDOFF_PROJECTION_POLICY_VERSION=
```

Se configura el equipo Chatwoot aprobado para el piloto. Admisión requiere
perímetro piloto y política publicada. Proyección requiere control plane Chatwoot y worker ID.
Configuración inválida impide arrancar el componente correspondiente.

El enforcement de handoffs persistidos no depende de flags. Rollback:

1. apagar admisión;
2. mantener proyección en drain;
3. llegar a backlog cero o reconocer dead letters;
4. recién entonces apagar proyección.

## 8. Adaptador Chatwoot

Operaciones explícitas:

- asignar el team esperado y releer hasta confirmación;
- buscar nota por marcador estable, crearla si falta y releer.

Para minimizar interferencia con operadores, la V1 usa un team. Antes de mutar,
el worker aplica esta regla única:

1. assignee humano presente: éxito, sin POST;
2. `team_id = expected_team_id`: éxito, sin POST;
3. otro `team_id` no nulo: conflicto operativo, sin POST ni corrección automática;
4. sin assignee ni team: asignar `expected_team_id` y releer con la misma regla.

Chatwoot 4.13.0 actualiza `team_id` sin borrar `assignee_id`. Si durante el POST
aparece un assignee humano, éste prevalece y satisface el efecto. Si aparece otro
team sin assignee, el worker registra conflicto y no intenta corregirlo de vuelta.

Cada operación valida account, inbox y conversación canónica antes de mutar. Un
`2xx` ambiguo no confirma el efecto. Timeout queda retryable. La búsqueda limita
y detecta cero, uno o múltiples marcadores; múltiples son conflicto auditable,
no éxito silencioso.

## 9. Lifecycle del drafting durable

### Guard determinístico antes de reserva

La reevaluación devuelve pausa/escalación y crea el request en la misma
transacción para reason codes V1. No se reserva intento.

### Sugerencia posterior a reserva

1. reevaluación autoriza drafting y reserva intento;
2. Hermes devuelve `draft_message` o `suggest_handoff`;
3. bridge valida schema y política;
4. si acepta handoff, RPC cierra `reserved` como `cancelled_before_request` y crea
   request;
5. si no acepta, falla cerrado o continúa con el draft según contrato;
6. jamás se crea handoff después de `request_started` desde esa sugerencia.

## 10. Observabilidad

Logs sin texto, nombres, teléfonos ni IDs externos:

- `handoff_requested reason=<enum>`;
- `handoff_projection_effect_claimed effect=<enum>`;
- `handoff_projection_effect_applied effect=<enum>`;
- `handoff_projection_retryable_failed effect=<enum> error=<enum>`;
- `handoff_projection_dead_letter effect=<enum> error=<enum>`.

`/ready` informa sólo configuración y conteos sanitizados. Estado comercial y
estado de proyección permanecen separados.

## 11. TDD y verificación

1. parser del drafting `draft_message | suggest_handoff`;
2. SQL: unicidad física, evidencia adicional, locks, precedencia y ACL;
3. PostgreSQL real: handoff contra reserva/request-start y aceptación tardía;
4. autoridad conversacional por caso y rechazo cross-case;
5. worker: dos efectos, retry, dead-letter y drain con admisión apagada;
6. Chatwoot: reconciliación exacta y cero POST en replay;
7. factory/readiness default-off y fail-fast;
8. HTTP real con dobles stateful;
9. E2E controlado real: team/assignee, nota privada y cero nuevo outbound;
10. reinicio y lease vencido sin duplicados;
11. revisión independiente y evidencia sanitizada.

## 12. Rollout

1. integrar DDL/código con ambos flags apagados;
2. aplicar migración y verificar ACL;
3. publicar política con team y nota versionada;
4. habilitar sólo proyección y probar un fixture durable sintético;
5. habilitar admisión sólo para el scope piloto;
6. ejecutar un handoff controlado en conversación allowlisted;
7. confirmar asignación, nota y estado durable;
8. mantener outbound general apagado hasta go/no-go.

## 13. Decisiones aceptadas y estado

| Decisión | Recomendación | Estado |
|---|---|---|
| Efectos Chatwoot | Asignación + nota reconciliables; no mutar etiquetas | aceptada 2026-08-10 |
| Responsable | Equipo Chatwoot provisionado y versionado para el piloto | aceptada 2026-08-10 |
| Mensaje al contacto | Silencio externo; nota privada fija al equipo | aceptada 2026-08-10 |
| Alcance V1 | Sólo casos con conversación canónica existente | aceptada 2026-08-10 |
| Autoridad | Supabase pausa primero; Chatwoot es proyección recuperable | aceptada 2026-08-10 |
| Sugerencia Hermes | `suggest_handoff` sin autoridad; bridge/política decide | aceptada por continuidad de ADR-0003/0007 |

Las decisiones de esta tabla forman la base implementada. ADR-0010 registra la
decisión arquitectónica; el contrato ejecutable vive en
`docs/contracts/executable-human-handoff-v1.md`.
