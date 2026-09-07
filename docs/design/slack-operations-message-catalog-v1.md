# Catálogo de mensajes operativos para Slack V1

- **Estado:** Propuesta para revisión
- **Fecha:** 2026-09-07
- **Alcance:** mensajes que el producto puede enviar al canal operativo compartido de Slack
- **Implementación:** No iniciada
- **Canal:** ya existe; conexión, app, credenciales, IDs y permisos todavía no configurados
- **Complementa:** [Resolución de correlaciones mediante Slack V1](slack-correlation-resolution-v1.md)

Este documento mapea las familias de avisos y propone su copy. No crea un
contrato técnico ni afirma que exista integración con Slack.

## 1. Objetivo del canal

El canal será la bandeja operativa común para:

1. derivaciones que requieren intervención humana;
2. correlaciones que el sistema no pudo resolver de forma inequívoca;
3. efectos externos cuyo resultado es incierto o falló definitivamente;
4. bloqueos que impiden continuar un caso;
5. incidentes, degradaciones y cambios de estado del sistema que el equipo debe
   conocer;
6. resúmenes que permitan controlar pendientes sin recibir un mensaje por cada
   evento rutinario.

No será un espejo de logs ni una copia de las conversaciones. Un evento interno
sólo genera un aviso cuando exige una acción humana, cambia el riesgo operativo o
forma parte de un resumen acordado.

## 2. Forma común de todos los mensajes

### 2.1 Encabezado y cuerpo

Cada mensaje raíz usa esta estructura:

```text
{emoji} {título corto}
{tenant_label} · {object_type} {short_ref} · {occurred_at_local}

Qué pasó      {resumen verdadero y accionable}
Impacto       {qué quedó pausado, bloqueado, incierto o degradado}
Acción        {qué debe hacer el equipo, o “No requiere acción”}
Plazo         {deadline/SLA o “Sin plazo”}

{botones y enlaces permitidos}
Código        {reason_code estable}
```

Reglas:

- `tenant_label` ayuda a leer, pero el scope real siempre se deriva de datos
  server-owned;
- `short_ref` es opaco y suficiente para soporte; no es teléfono, email, nombre,
  JID ni ID externo del proveedor;
- la hora visible usa la zona operativa acordada y conserva internamente UTC;
- `reason_code` permite buscar y medir el caso sin exponer detalles sensibles;
- el copy distingue **hecho confirmado**, **resultado incierto** y **ausencia de
  evidencia**; nunca convierte uno en otro;
- si existe una conversación Chatwoot canónica, puede mostrarse **Abrir en
  Chatwoot**; si la identidad o conversación es incierta, el enlace se omite.

### 2.2 Privacidad

No se publican en el canal:

- nombres completos, teléfonos, emails, JIDs o direcciones;
- texto de conversaciones, notas humanas o payloads de proveedores;
- tokens, firmas, API keys, URLs con PII ni errores crudos;
- identificadores de candidatos que puedan reutilizarse fuera del modal seguro;
- datos de un tenant dentro del hilo de otro.

Cuando distinguir fuentes sea indispensable para una correlación, Slack recibe
únicamente la proyección enmascarada producida por Supabase Cloud, por ejemplo
`teléfono ••41` o `m•••@dominio`. El masking se realiza antes de construir el
mensaje.

### 2.3 Severidad y routing

| Nivel | Uso | Comportamiento propuesto |
|---|---|---|
| `P1 · Crítico` | riesgo de efecto indebido, cruce de tenant, seguridad o pérdida de control | mensaje inmediato + mención del grupo de guardia |
| `P2 · Acción requerida` | caso bloqueado, entrega incierta, handoff o pendiente vencido | mensaje inmediato; mención sólo al vencer SLA |
| `P3 · Atención` | degradación con fallback seguro o tarea próxima | mensaje sin mención; se agrega al resumen |
| `P4 · Informativo` | cambio planificado, recuperación o resultado rutinario útil | hilo existente o resumen; no mensaje raíz por evento |

La severidad no la decide el modelo. Surge de una tabla versionada
`event_type/reason_code → severity/routing/template`.

### 2.4 Un hilo por objeto operativo

- Un handoff, una correlación o un incidente crea como máximo un mensaje raíz.
- Recordatorios, escalamiento, toma, resolución y recuperación actualizan el
  mensaje raíz o se publican en su hilo.
- Un retry técnico no crea una nueva alerta salvo que cambie el estado o venza un
  umbral.
- Una key semántica durable evita duplicados ante replay.
- Los comentarios humanos libres son conversación operativa, no comandos ni
  autoridad.

## 3. Derivaciones y atención humana

### `HND-001 · Nueva derivación`

**Dispara:** el estado durable de la automatización quedó pausado y el caso exige
intervención humana.

**Motivos cubiertos:** solicitud explícita de la persona; caso desconocido;
contexto insuficiente; riesgo o límite de política; excepción comercial;
compromiso manual del agente/equipo; resolución que no puede ejecutar el sistema.

```text
🟠 Nueva derivación
{tenant_label} · Caso {case_ref} · {occurred_at_local}

Motivo        {reason_label}
Resumen       {sanitized_case_summary}
Automatización Pausada antes de la derivación
Acción        Tomar el caso y continuar en Chatwoot
Plazo         {handoff_due_at_local}

[Tomar caso] [Abrir en Chatwoot]
Código        {reason_code}
```

`sanitized_case_summary` usa una frase cerrada, no texto generado libremente. Por
ejemplo: “Pidió hablar con una persona”, “Caso no cubierto por la política activa”
o “Falta contexto autoritativo para responder”.

### `HND-002 · Derivación sin destino disponible`

**Dispara:** la pausa durable existe, pero no se pudo determinar un equipo/persona
autorizado o el destino configurado ya no existe.

```text
🔴 Derivación sin destino
{tenant_label} · Caso {case_ref}

Qué pasó      El caso quedó pausado, pero no hay un destino humano disponible
Impacto       Nadie quedó asignado automáticamente
Acción        Asignar manualmente y corregir la configuración de handoff
Plazo         Inmediato

[Abrir en Chatwoot] [Ver estado]
Código        handoff_destination_unavailable
```

### `HND-003 · Proyección de derivación fallida`

**Dispara:** Supabase Cloud ya pausó el caso, pero falló la asignación, label,
macro o nota privada de Chatwoot.

```text
🔴 Derivación pausada, proyección incompleta
{tenant_label} · Caso {case_ref}

Qué pasó      La pausa segura fue aplicada, pero Chatwoot no refleja toda la derivación
Impacto       La automatización no continuará; la atención humana puede no verla
Acción        Revisar la conversación y completar la asignación manual
Plazo         Inmediato

[Abrir en Chatwoot] [Reintentar proyección]
Código        {projection_reason_code}
```

El botón de retry sólo aparece si existe un comando idempotente y autorizado. No
se ofrece cuando el efecto remoto puede haber ocurrido y el resultado es incierto.

### `HND-004 · Derivación tomada`

**Dispara:** una persona autorizada tomó el caso de forma canónica.

```text
✅ Derivación tomada
{actor_display} tomó el caso {case_ref} a las {taken_at_local}.
La automatización continúa pausada.
```

Se publica en el hilo; no crea otro mensaje raíz.

### `HND-005 · SLA de derivación próximo a vencer`

```text
⏳ Derivación pendiente
El caso {case_ref} todavía no fue tomado.
Vence en {remaining_time} · {handoff_due_at_local}

[Tomar caso] [Abrir en Chatwoot]
```

### `HND-006 · Derivación vencida y escalada`

```text
🔴 Derivación vencida
{tenant_label} · Caso {case_ref}

Qué pasó      Nadie tomó el caso dentro del SLA
Impacto       La automatización sigue pausada
Acción        {escalation_group} debe asignar responsable
Vencida       Hace {overdue_duration}

[Tomar caso] [Abrir en Chatwoot]
Código        handoff_sla_overdue
```

### `HND-007 · Derivación resuelta`

```text
✅ Derivación resuelta
Caso {case_ref} · resuelto por {actor_display} · {resolved_at_local}
Resultado     {closed_resolution_label}
Automatización {automation_state_label}
```

`automation_state_label` sólo puede ser un estado durable confirmado, por ejemplo
“permanece pausada”, “cerrada” o “reanudada mediante autorización explícita”. Una
resolución humana no reanuda automáticamente el contacto.

### `HND-008 · Derivación cambió mientras se atendía`

**Dispara:** nueva compra, opt-out, takeover, cierre o cambio de evidencia vuelve
obsoleta la tarea humana.

```text
⚪ Estado del caso actualizado
Caso {case_ref}

Qué cambió    {authoritative_change_label}
Impacto       La acción anterior ya no corresponde
Acción        Revisar el estado actual antes de responder
Código        handoff_context_changed
```

### `HND-009 · Resultado incierto al proyectar la derivación`

**Dispara:** la asignación o nota privada pudo haberse aplicado en Chatwoot, pero
el bridge no pudo confirmar la postcondición.

```text
🔴 Proyección de derivación incierta
{tenant_label} · Caso {case_ref} · Efecto {effect_label}

Qué pasó      Chatwoot pudo aplicar el efecto, pero no se pudo confirmarlo
Impacto       El caso sigue pausado; no habrá retry ciego
Acción        Reconciliar el estado remoto antes de repetir

[Abrir en Chatwoot] [Revisar proyección]
Código        handoff_projection_delivery_unknown
```

### `HND-010 · Conflicto al proyectar la derivación`

**Dispara:** existe otro Team, un assignee incompatible, múltiples marcadores o
otra evidencia remota que el sistema no puede sobrescribir de forma segura.

```text
🔴 Conflicto en la derivación
{tenant_label} · Caso {case_ref} · Efecto {effect_label}

Qué pasó      El estado actual de Chatwoot contradice la proyección esperada
Impacto       El caso sigue pausado y no se modificó la evidencia en conflicto
Acción        Resolver manualmente el estado de Chatwoot

[Abrir en Chatwoot]
Código        handoff_projection_conflict
```

### `HND-011 · Proyección enviada a dead letter`

```text
🔴 Derivación requiere reparación manual
{tenant_label} · Caso {case_ref} · Efecto {effect_label}

Qué pasó      La proyección agotó su política de reconciliación
Impacto       El caso sigue pausado, pero la asignación o nota puede estar incompleta
Acción        Verificar Chatwoot y cerrar el pendiente operativo
Código        handoff_projection_dead_letter
```

Los estados físicos conocidos del handoff (`pending`, `retryable_failed`,
`delivery_unknown`, `applied`, `conflict`, `dead_letter`) se proyectan así:

| Estado | Aviso |
|---|---|
| `pending` / `retryable_failed` dentro de SLA | sin mensaje nuevo; permanece en el hilo y métricas |
| `delivery_unknown` | `HND-009` |
| `applied` para ambos efectos | `HND-004` o actualización del raíz como proyectado |
| `conflict` | `HND-010` |
| `dead_letter` | `HND-011` |

## 4. Correlaciones no resueltas

Los mensajes interactivos y el modal se detallan en
[Resolución de correlaciones mediante Slack V1](slack-correlation-resolution-v1.md).
Resolver una correlación no autoriza contacto, timers ni outbound.

### `COR-001 · Sin coincidencia`

```text
🟠 Correlación pendiente
{tenant_label} · Caso {case_ref} · Sin coincidencia elegible

Fuentes       {masked_source_observations}
Impacto       El evento no se aplicó al caso comercial
Acción        Revisar candidatos o cerrar sin coincidencia
Plazo         {review_due_at_local}

[Revisar caso]
Código        unmatched
```

### `COR-002 · Coincidencia ambigua`

```text
🟠 Correlación ambigua
{tenant_label} · Caso {case_ref} · {candidate_count} candidatos

Qué pasó      Más de un candidato cumple las reglas actuales
Impacto       El evento no se aplicó automáticamente
Acción        Elegir un candidato o cerrar sin coincidencia
Plazo         {review_due_at_local}

[Revisar caso]
Código        ambiguous
```

### `COR-003 · Evidencia en conflicto`

```text
🟠 Correlación en conflicto
{tenant_label} · Caso {case_ref} · {conflict_label}

Plataforma    {masked_platform_observation}
Proveedor     {masked_provider_observation}
Impacto       El evento permanece sin aplicar
Acción        Revisar la evidencia disponible
Plazo         {review_due_at_local}

[Revisar caso]
Código        conflict
```

### `COR-004 · Evidencia cambió`

```text
🟡 Correlación actualizada
Caso {case_ref}

Qué pasó      La evidencia cambió desde la última revisión
Impacto       La selección anterior ya no puede confirmarse
Acción        Abrir nuevamente el caso

[Revisar caso]
Código        correlation_snapshot_stale
```

Actualiza el mensaje raíz y vuelve inválido cualquier modal anterior.

### `COR-005 · Revisión vencida`

```text
⏳ Correlación vencida
Caso {case_ref} · pendiente desde hace {pending_duration}

Impacto       El evento continúa bloqueado y no produjo efectos comerciales
Acción        Revisar o cerrar sin coincidencia

[Revisar caso]
Código        correlation_review_overdue
```

### `COR-006 · Correlación escalada`

```text
🔴 Correlación escalada
{tenant_label} · Caso {case_ref}

Qué pasó      Superó el umbral operativo de {escalation_threshold}
Impacto       Sigue sin resolución; no hubo contacto ni cierre automático
Acción        {escalation_group} debe resolver el caso

[Revisar caso]
Código        correlation_review_escalated
```

### `COR-007 · Candidato vinculado`

```text
✅ Correlación resuelta
Caso {case_ref} · candidato {candidate_label}
Resuelta por  {actor_display} · {resolved_at_local}
Automatización Continúa bloqueada hasta una autorización independiente
```

### `COR-008 · Cerrada sin coincidencia`

```text
⚪ Correlación cerrada sin coincidencia
Caso {case_ref}
Resuelta por  {actor_display} · {resolved_at_local}
Efectos       No se vinculó el evento ni se habilitó contacto
```

### `COR-009 · Proyección Slack incierta o dañada`

```text
🔴 Aviso de correlación no verificable en Slack
{tenant_label} · Caso {case_ref}

Qué pasó      {projection_failure_label}
Impacto       El caso durable conserva su estado, pero el canal puede no reflejarlo
Acción        Revisar la bandeja autoritativa y reparar la proyección

[Ver estado]
Código        {projection_reason_code}
```

Cuando el mensaje se repara, el sistema agrega al hilo:

```text
✅ Proyección reparada
El mensaje vuelve a coincidir con el estado durable del caso.
```

## 5. Mensajería y efectos externos

### `MSG-001 · Resultado de envío incierto`

**Dispara:** el request pudo haber comenzado, pero no existe evidencia suficiente
para afirmar enviado ni no enviado.

```text
🔴 Resultado de envío incierto
{tenant_label} · Caso {case_ref} · Acción {action_ref}

Qué pasó      El proveedor pudo aceptar el mensaje, pero no se pudo confirmarlo
Impacto       No habrá retry automático para evitar un mensaje duplicado
Acción        Reconciliar el resultado antes de continuar
Plazo         {reconciliation_due_at_local}

[Revisar entrega] [Abrir en Chatwoot]
Código        delivery_unknown
```

Nunca dice “el mensaje falló” mientras el outcome sea incierto.

### `MSG-002 · Envío falló definitivamente`

```text
🔴 Mensaje no enviado
{tenant_label} · Caso {case_ref} · Acción {action_ref}

Qué pasó      El envío terminó en fallo permanente
Impacto       La secuencia no avanzó
Acción        Revisar el canal o contactar manualmente sólo si sigue autorizado

[Ver estado] [Abrir en Chatwoot]
Código        {permanent_failure_reason}
```

### `MSG-003 · Retries agotados antes del request`

```text
🟠 Reintentos agotados
{tenant_label} · Caso {case_ref} · Acción {action_ref}

Qué pasó      El sistema no logró iniciar un request válido tras {attempt_count} intentos
Impacto       No se envió ningún mensaje; la acción quedó bloqueada
Acción        Corregir la causa y decidir si corresponde reprogramar
Código        retry_budget_exhausted
```

Los retries intermedios no publican mensajes individuales; quedan en métricas y
se resumen sólo si superan un umbral.

### `MSG-004 · Template requerido no disponible`

```text
🟠 Template de WhatsApp no disponible
{tenant_label} · Caso {case_ref}

Qué pasó      La ventana exige un template aprobado y compatible
Impacto       El mensaje no fue enviado
Acción        Revisar aprobación, idioma, variables y binding del template
Código        {template_reason_code}
```

Incluye ausencia, template rechazado/pausado, idioma incompatible, parámetros
inválidos o binding no publicado. No incluye el contenido completo del template.

### `MSG-005 · Inbound admitido pero no procesable`

```text
🔴 Mensaje entrante bloqueado
{tenant_label} · Conversación {conversation_ref}

Qué pasó      El evento fue admitido de forma durable pero no pudo procesarse
Impacto       No se invocó o no se completó la respuesta automática
Acción        Revisar el dead-letter y atender manualmente si corresponde
Código        {dead_letter_reason_code}
```

Sólo se publica cuando el estado es terminal o superó el umbral de retry; no por
cada error transitorio.

### `MSG-006 · Respuesta automática bloqueada por una guarda`

Opt-out, compra, takeover, conversación nueva, restricción de canal o falta de
autorización pueden bloquear correctamente un envío. Estos stops son
**informativos**, no incidentes. Se agregan al hilo del caso o al resumen diario:

```text
⚪ Envío cancelado de forma segura
Caso {case_ref} · {guard_label}
No se inició ningún request externo.
Código        {guard_reason_code}
```

Se eleva a `P2` únicamente cuando la guarda revela una inconsistencia que requiere
corrección humana, por ejemplo identidad canónica no verificable.

### `MSG-007 · Tarea manual prometida`

```text
🟠 Tarea manual pendiente
{tenant_label} · Caso {case_ref}

Compromiso    {closed_commitment_label}
Impacto       El sistema no puede completar esta acción automáticamente
Acción        Cumplir o corregir el compromiso en Chatwoot
Plazo         {commitment_due_at_local}

[Tomar tarea] [Abrir en Chatwoot]
Código        human_task_required
```

No se copia una promesa libre del chat. `closed_commitment_label` pertenece a un
catálogo aprobado, por ejemplo “Enviar propuesta comercial”.

## 6. Automatización, dependencias y salud

### `SYS-001 · Servicio o worker no saludable`

```text
🔴 Componente no saludable
{tenant_label} · {component_label}

Qué pasó      {health_failure_label}
Impacto       {affected_capability_label}
Protección    {fail_closed_state_label}
Acción        Revisar el servicio y la cola durable
Desde         {started_at_local}
Código        {health_reason_code}
```

No incluye stack traces ni respuestas crudas.

### `SYS-002 · Cola atrasada`

```text
🟠 Cola operativa atrasada
{tenant_label} · {queue_label}

Pendientes    {pending_count_bucket}
Más antiguo   {oldest_age_bucket}
Impacto       {queue_impact_label}
Acción        Revisar workers y dependencias
Código        queue_age_threshold_exceeded
```

Los conteos se muestran como valores sanitizados o rangos acordados; nunca se
derivan contando mensajes visibles de Slack.

### `SYS-003 · Reconciliación vencida`

```text
🔴 Reconciliación vencida
{tenant_label} · {object_type} {short_ref}

Qué pasó      El resultado externo sigue indeterminado después del plazo
Impacto       El flujo permanece pausado y sin retry automático
Acción        Resolver manualmente el outcome
Código        reconciliation_overdue
```

### `SYS-004 · Dependencia externa degradada`

```text
🟡 Dependencia degradada
{tenant_label} · {dependency_label}

Impacto       {affected_capability_label}
Protección    El sistema está fallando cerrado
Acción        No requiere acción inmediata; se avisará al recuperar
Código        {dependency_reason_code}
```

Aplica a Supabase Cloud, Chatwoot, Hotmart, Meta/WhatsApp, Slack o el runtime de
Hermes. Se emite tras un umbral, no por un timeout aislado.

### `SYS-005 · Persistencia no disponible`

```text
🔴 Persistencia no disponible
{tenant_label} · {storage_label}

Impacto       Nuevos efectos permanecen bloqueados para evitar pérdida o duplicación
Acción        Restaurar escritura/volumen/base y reconciliar antes de reanudar
Código        persistence_unavailable
```

### `SYS-006 · Propuestas del agente inválidas o no disponibles`

```text
🟠 Agente no disponible para {capability_label}
{tenant_label}

Qué pasó      Se superó el umbral de propuestas ausentes o inválidas
Impacto       La automatización afectada está bloqueada; no se publicó contenido
Acción        Revisar runtime, release y contrato de salida
Código        agent_proposal_failure_threshold
```

Un fallo aislado reintentable no crea un mensaje raíz.

### `SYS-007 · Automatización pausada automáticamente`

```text
🟠 Automatización pausada
{tenant_label} · {automation_label}

Motivo        {pause_reason_label}
Alcance       {paused_scope_label}
Acción        Corregir la causa y solicitar reanudación explícita
Código        {pause_reason_code}
```

### `SYS-008 · Recuperación confirmada`

```text
✅ Servicio recuperado
{component_label} volvió a estado saludable a las {recovered_at_local}.
Pendientes    {reconciliation_summary}
```

Se publica en el hilo del incidente. “Recuperado” exige health/readiness y, cuando
corresponda, reconciliación de backlog; un proceso reiniciado no basta.

### `SYS-009 · Drift de configuración o versión`

```text
🔴 Configuración incompatible
{tenant_label} · {component_label}

Qué pasó      El runtime no coincide con la versión o scope esperado
Impacto       {affected_capability_label} permanece bloqueada
Acción        Reconciliar configuración antes de activar
Código        configuration_drift
```

## 7. Seguridad y controles

### `SEC-001 · Inconsistencia de tenant, cuenta, inbox o identidad`

```text
🚨 Bloqueo de scope
{tenant_label} · {component_label}

Qué pasó      Un evento o efecto no coincidió con el scope configurado
Protección    Se rechazó antes de la mutación externa
Acción        Investigar configuración y posible cruce de datos
Código        scope_mismatch
```

Es `P1`. No publica los valores enfrentados.

### `SEC-002 · Anomalía de autenticación o replay`

```text
🚨 Anomalía de ingreso
{tenant_label} · {integration_label}

Qué pasó      Se superó el umbral de firmas inválidas, timestamps vencidos o replays
Protección    Los eventos fueron rechazados
Acción        Revisar origen, credencial y exposición potencial
Ventana       {anomaly_window_label}
Código        webhook_auth_anomaly
```

Un intento aislado queda en métricas sanitizadas para evitar ruido y abuso del
canal como amplificador.

### `SEC-003 · Acción prohibida bloqueada`

```text
🚨 Acción no autorizada bloqueada
{tenant_label} · {capability_label}

Protección    No se inició el efecto externo
Acción        Revisar origen del comando y política activa
Código        forbidden_effect_blocked
```

### `SEC-004 · Credencial rechazada o integración revocada`

```text
🔴 Integración sin autorización válida
{tenant_label} · {integration_label}

Impacto       {affected_capability_label} está detenida
Acción        Un administrador debe reconectar o rotar la credencial fuera de Slack
Código        integration_authorization_failed
```

Slack nunca solicita ni recibe la credencial.

### `SEC-005 · Posible exposición de datos sensibles`

```text
🚨 Posible exposición de datos
{tenant_label} · {component_label}

Qué pasó      Un control detectó contenido no permitido en una superficie operativa
Protección    {containment_state_label}
Acción        Activar el procedimiento de respuesta a incidentes
Código        sensitive_data_exposure_suspected
```

No repite el dato detectado. La contención y rotación siguen un runbook separado.

## 8. Cambios operativos y gobierno

### `OPS-001 · Activación solicitada`

```text
🟡 Activación pendiente de aprobación
{tenant_label} · {capability_label} · versión {version_ref}

Alcance       {activation_scope_label}
Evidencia     {readiness_summary_label}
Acción        Revisar y aprobar o rechazar

[Revisar activación]
Código        activation_approval_required
```

Slack puede iniciar una revisión, pero la aprobación sólo es válida si el backend
revalida actor, versión, scope y evidencia.

### `OPS-002 · Capacidad activada`

```text
✅ Capacidad activada
{tenant_label} · {capability_label} · versión {version_ref}
Aprobó        {actor_display} · {activated_at_local}
Alcance       {activation_scope_label}
```

### `OPS-003 · Capacidad pausada o desactivada`

```text
🟠 Capacidad {state_label}
{tenant_label} · {capability_label}
Motivo        {change_reason_label}
Efectivo desde {changed_at_local}
Pendientes    {durable_work_disposition_label}
```

Debe distinguir “no admitir trabajo nuevo” de “dejar de procesar trabajo durable
existente”.

### `OPS-004 · Deploy, migración o cambio de release fallido`

```text
🔴 Cambio operativo fallido
{tenant_label} · {change_type_label} · {version_ref}

Estado        No completado
Impacto       {current_runtime_state_label}
Acción        Revisar evidencia sanitizada y decidir rollback/reintento
Código        {change_failure_reason_code}
```

No se publica “falló” ante un timeout ambiguo del proveedor; en ese caso se usa
“resultado incierto” y se reconcilia antes de repetir.

### `OPS-005 · Cambio operativo verificado`

```text
✅ Cambio operativo verificado
{tenant_label} · {change_type_label} · {version_ref}
Estado        {verified_state_label}
Evidencia     {sanitized_evidence_ref}
```

Se usa para cambios que el equipo realmente deba conocer, no para cada reinicio.

### `OPS-006 · Revisión de política, copy o release requerida`

```text
🟡 Revisión pendiente
{tenant_label} · {artifact_type_label} · borrador {version_ref}

Motivo        {review_reason_label}
Acción        Revisar, aprobar, pedir cambios o rechazar
Plazo         {review_due_at_local}

[Revisar borrador]
Código        artifact_review_required
```

Feedback o una resolución humana sólo crean un borrador; nunca cambian producción
directamente.

## 9. Resúmenes para evitar ruido

### `DIG-001 · Resumen operativo diario`

```text
📋 Resumen operativo · {tenant_label} · {period_label}

Derivaciones  {handoff_open} abiertas · {handoff_overdue} vencidas
Correlaciones {correlation_open} pendientes · {correlation_overdue} vencidas
Entregas      {delivery_unknown} inciertas · {permanent_failed} fallidas
Sistema       {incident_open} incidentes abiertos
Stops seguros {safe_stop_count} efectos bloqueados correctamente

[Ver pendientes]
```

Puede emitirse un bloque por tenant dentro de un único resumen. Los totales
provienen de fuentes canónicas, no del conteo de hilos de Slack.

### `DIG-002 · Resumen de pendientes por turno`

```text
📌 Pendientes para el próximo turno

P1 abiertos   {p1_open}
P2 sin dueño  {p2_unowned}
Vencen pronto {due_soon}
Más antiguo   {oldest_pending_age}

[Ver pendientes]
```

### Eventos que no generan alerta individual por defecto

- webhook válido admitido;
- mensaje enviado y aceptado normalmente;
- respuesta inbound procesada normalmente;
- compra correlacionada inequívocamente;
- retry transitorio antes de alcanzar umbral;
- stop esperado por compra, respuesta, opt-out o takeover;
- deduplicación/replay manejado correctamente;
- health check exitoso o poll de worker sin trabajo;
- reinicio planificado sin impacto.

Estos eventos sí alimentan métricas, auditoría y resúmenes. Una regla versionada
puede elevarlos si el piloto necesita supervisión temporal, sin cambiar el copy a
mano.

## 10. Variables cerradas y botones permitidos

### Variables comunes

| Variable | Fuente |
|---|---|
| `tenant_label` | catálogo server-owned de tenants |
| `short_ref` / `case_ref` / `action_ref` | proyección opaca creada por backend |
| `reason_code` | evento durable o clasificación determinística |
| labels de motivo/impacto/acción | catálogo versionado, no texto del payload |
| timestamps y deadlines | fuente durable canónica |
| `actor_display` | directorio de operadores autorizados; no nombre tomado del payload |
| conteos | query agregada y scoped de Supabase Cloud |
| link de Chatwoot | conversación canónica validada para el mismo tenant/caso |

### Botones V1

- **Tomar caso**: registra claim humano idempotente.
- **Abrir en Chatwoot**: sólo link; no muta autoridad.
- **Revisar caso**: abre modal nativo con snapshot fresco.
- **Revisar entrega**: abre estado de reconciliación, no reenvía.
- **Reintentar proyección**: sólo para proyecciones cuyo retry sea seguro.
- **Ver estado / Ver pendientes**: lectura scoped.
- **Revisar activación / Revisar borrador**: abre revisión; no aprueba con un solo click.

No habrá botones genéricos **Reintentar envío**, **Reanudar**, **Resolver** ni
**Activar** sin prepare/confirm, relectura autoritativa y autorización específica.

## 11. Cobertura y precedencia

Cuando un mismo hecho parece pertenecer a varias familias, se publica un solo
mensaje con esta precedencia:

1. seguridad y posible efecto indebido (`SEC`);
2. resultado externo incierto (`MSG-001` / `SYS-003`);
3. intervención humana necesaria (`HND` / `COR`);
4. fallo permanente o degradación (`MSG` / `SYS`);
5. cambio operativo (`OPS`);
6. información agregada (`DIG`).

Ejemplos:

- una correlación ambigua que bloquea una compra usa `COR-002`, no además
  `SYS-007`;
- una asignación Chatwoot fallida después de pausar usa `HND-003`, no además
  `MSG-002`;
- un mismatch de inbox durante un handoff usa `SEC-001` porque el riesgo de scope
  prevalece;
- una respuesta desconocida de Slack al publicar un aviso de correlación se
  refleja en `COR-009`; no crea otro aviso en el mismo canal si no puede
  demostrarse su entrega.

## 12. Decisiones abiertas para revisión

1. SLA de handoff por horario y tipo de caso.
2. SLA de correlación y umbral de escalamiento. La propuesta existente es 24 h
   para vencimiento y 48 h para escalamiento.
3. Grupo de Slack que recibe menciones `P1` y escalaciones `P2`.
4. Operadores autorizados para tomar handoffs, resolver correlaciones y revisar
   activaciones.
5. Si el canal mostrará proyecciones enmascaradas mínimas o sólo referencias
   opacas para cada tenant.
6. Horario y zona del resumen diario y del cambio de turno.
7. Qué estados exitosos se incluirán durante la supervisión intensiva del piloto
   aunque luego pasen sólo al resumen.
8. Superficie autoritativa de **Ver pendientes** antes de que exista una UI
   propia.
9. Política de retención y borrado de mensajes de Slack.
10. Qué avisos son visibles para todo el canal y cuáles, si existieran datos más
    sensibles, deben limitarse a un grupo o superficie separada.

## 13. Siguiente artefacto después de aprobar el catálogo

La implementación deberá convertir esta propuesta en un contrato versionado con:

- enum cerrado de `event_type`, `reason_code`, severidad y template;
- schema exacto de variables por template;
- tabla de routing, deduplicación, agrupación y actualización de hilos;
- Block Kit exacto y fallback de texto;
- estados de entrega `reserved → request_started → accepted | delivery_unknown | failed`;
- firma y anti-replay para interactividad;
- autorización de workspace, canal, usuario, tenant y objeto;
- pruebas de privacidad, idempotencia, precedencia y cero efectos cruzados;
- default-off, reconciliación y verificación HTTP/Slack controlada antes de activar.
