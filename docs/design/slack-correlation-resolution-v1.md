# Resolución de correlaciones mediante Slack V1

- **Estado:** Base aprobada; notificación outbound implementada, resolución interactiva pendiente
- **Fecha:** 2026-09-06
- **Alcance:** notificación, revisión humana y proyección de estado para correlaciones Hotmart no inequívocas
- **Complementa:** ADR-0016 y los contratos `operator-correlation-review-v1` y `operator-correlation-resolution-v1`
- **Catálogo de copy relacionado:** [Mensajes operativos para Slack V1](slack-operations-message-catalog-v1.md)
- **Implementado localmente:** conector central, admisión autenticada, persistencia, deduplicación, worker de publicación, binding de hilo, cliente productor y superficies UI aisladas
- **Pendiente:** wiring de todos los productores, endpoints interactivos, autorización de operador, replay durable de interacciones, despliegue y activación

## 1. Decisión de producto propuesta

Slack será la superficie operativa universal para los casos `unmatched`, `ambiguous` y `conflict`. Chatwoot será una proyección opcional solamente cuando ya exista una conversación canónica segura.

Cada aliado tendrá un canal operativo exclusivo. `C0C0YEACVT2` queda asignado
únicamente a Johanna, independientemente de su nombre visible actual. ATT1 tendrá
otro Channel ID para toda su gama de productos. El conector selecciona el canal
desde un mapa server-owned derivado del bearer del productor; el caller no envía
ni puede sobreescribir el canal. No existe fallback entre aliados: si ATT1 todavía
no tiene canal configurado, sus admisiones fallan cerrado y nunca llegan a Johanna.

Mariana no abrirá un HTML externo en el MVP. Verá un mensaje publicado por una app de Slack y, al pulsar **Revisar caso**, un modal nativo de Slack.

Supabase Cloud conserva la autoridad sobre el caso y la resolución. El contenido visible, los botones y el estado mostrado por Slack son proyecciones recuperables: nunca constituyen por sí mismos una resolución ni habilitan contacto, automatización u outbound.

## 2. Experiencia de Mariana

### 2.1 Mensaje inicial

Ejemplo conceptual, con PII enmascarada:

```text
⚠️ Correlación pendiente
Caso C-7F3A · Conflicto de teléfono · 2 candidatos

Plataforma   teléfono ••41 · email m•••@dominio
Hotmart      teléfono ••73 · email m•••@dominio

Estado       Pendiente
Revisar antes de 24 h

[Revisar caso]
```

El mensaje puede incluir un enlace a Chatwoot sólo si el caso ya tiene una conversación canónica. La ausencia de ese enlace no impide revisar o resolver el caso.

No se muestran números, emails, nombres ni payloads completos. Los candidatos proceden exclusivamente de la proyección enmascarada y scoped que ya usa el contrato de revisión del operador.

### 2.2 Modal de revisión

El modal muestra:

1. identificador corto del caso y estado actual;
2. motivo determinístico de la falta de correlación;
3. observaciones de cada fuente, separadas y enmascaradas;
4. candidatos elegibles del snapshot actual;
5. elección única:
   - vincular un candidato;
   - cerrar sin coincidencia válida;
   - cancelar y mantener pendiente;
6. fundamento cerrado de verificación compatible con la elección.

No admite IDs escritos a mano ni comentarios libres como autoridad. El modal transporta identificadores opacos suministrados por el servidor.

### 2.3 Confirmación

El primer envío del modal llama a `prepare` y reemplaza el contenido del mismo modal por una confirmación explícita:

```text
Vas a vincular el caso C-7F3A con el candidato 2.
Evidencia: confirmación del cliente.
La automatización continuará bloqueada.

[Volver] [Confirmar resolución]
```

El segundo envío llama a `confirm`. Esta secuencia conserva la separación durable prepare/confirm de ADR-0016. Una interacción firmada de Slack no sustituye las revalidaciones de PostgreSQL.

### 2.4 Estado posterior

El mensaje raíz se actualiza, no se publica otro mensaje raíz:

- **Resuelto — candidato vinculado**;
- **Cerrado — sin coincidencia válida**;
- **Pendiente — revisión vencida**;
- **Pendiente — evidencia cambió; revisar nuevamente**;
- **Pendiente — escalado**.

Los estados terminales muestran actor y fecha, pero no PII ni evidencia libre. Resolver una correlación no autoriza mensajes ni seguimiento.

## 3. Identidad durable entre caso y Slack

El texto del mensaje, su posición en el canal y el `case_id` contenido en un botón no son autoridad suficiente.

### 3.1 Hilo canónico por caso

Persistir un registro `slack_correlation_threads` con, como mínimo:

- `case_id` UUID, único para una proyección activa;
- `slack_team_id`;
- `slack_channel_id`;
- `slack_root_message_ts`;
- `projection_status`;
- `projected_case_version` o fingerprint autoritativo;
- `first_notified_at`;
- `review_due_at`;
- `next_reminder_at`;
- `last_reconciled_at`;
- `superseded_at` cuando un mensaje eliminado deba reemplazarse.

Debe existir unicidad sobre `(slack_team_id, slack_channel_id, slack_root_message_ts)` y como máximo un hilo no supersedido por caso.

### 3.2 Todo mensaje relacionado queda registrado

Persistir `slack_correlation_messages` para el mensaje raíz y cualquier recordatorio:

- referencia obligatoria al hilo y al `case_id`;
- `(team_id, channel_id, message_ts)` único cuando Slack confirmó la publicación;
- tipo cerrado: `root`, `reminder`, `escalation` o `repair`;
- estado de entrega: `reserved`, `request_started`, `accepted`, `delivery_unknown`, `failed`;
- key semántica única derivada de `case_id + message_kind + reminder_sequence`;
- hash/fingerprint del contenido sanitizado, nunca el contenido con PII.

Esto permite demostrar a qué caso pertenece cada mensaje o respuesta generada por la app. Los mensajes humanos libres dentro del thread son contexto operativo, no comandos ni evidencia autoritativa.

### 3.3 Validación de cada interacción

Antes de abrir o enviar el modal, el bridge debe:

1. verificar la firma de Slack sobre los bytes crudos y rechazar timestamps fuera de ventana;
2. comprobar `team_id`, el `channel_id` exclusivo del tenant, `message_ts` y el usuario Slack contra configuración server-owned;
3. resolver el hilo por esos identificadores, no sólo por el valor del botón;
4. exigir que el `case_id` opaco del action coincida con el hilo encontrado;
5. releer el caso y los candidatos desde Supabase Cloud;
6. rechazar casos terminales, evidencia obsoleta o candidatos fuera del snapshot;
7. ejecutar `prepare` y `confirm` con idempotency keys durables creadas por el servidor.

Un callback tardío sobre un mensaje ya resuelto no crea otro comando: devuelve el estado actual y repara la proyección si fuera necesario.

## 4. Entrega idempotente y reconciliación

Slack es un sistema externo y puede aceptar una publicación aunque el bridge pierda la respuesta. Por eso no alcanza con marcar “enviado” después del HTTP.

El flujo de publicación o actualización debe ser:

```text
reservar efecto durable
→ persistir request_started
→ llamar a Slack
→ validar team/channel/message_ts de la respuesta
→ finalizar accepted
```

Una respuesta incierta produce `delivery_unknown`; nunca un retry ciego. Un reconciliador busca la proyección mediante metadatos opacos de la app o un marker no sensible y sólo reintenta cuando existe evidencia positiva de que Slack no aplicó el efecto.

Si el mensaje raíz fue eliminado, el reconciliador lo marca supersedido y crea un reemplazo mediante una nueva key semántica. La restricción parcial impide dos raíces activas para el mismo caso.

La resolución durable nunca se revierte porque `chat.update` falle. Ese fallo deja una proyección pendiente de reparación.

## 5. Prevención de casos indefinidos

Cada caso recibe `review_due_at` según una política versionada y configurable. La propuesta inicial es 24 horas, pendiente de aprobación operativa.

Mientras el caso siga pendiente:

- el worker reconcilia periódicamente estado autoritativo y mensaje raíz;
- al vencer `review_due_at`, actualiza el estado visible a **Revisión vencida**;
- crea como máximo un recordatorio por secuencia configurada;
- después del umbral de escalamiento, menciona un grupo operativo configurado y registra `escalated_at`;
- no cierra ni resuelve automáticamente el caso.

Los recordatorios son filas correlacionadas al mismo hilo. Una resolución concurrente cancela durablemente recordatorios todavía no iniciados. Un recordatorio con request ya iniciado conserva su outcome real, pero no crea continuidad adicional.

Debe existir una vista o métrica sanitizada con conteos por estado y antigüedad: pendientes, vencidos, escalados, resueltos y proyecciones con error. El conteo sale de Supabase Cloud, no del historial visible de Slack.

## 6. Estados y precedencia

Estado autoritativo del caso:

```text
pending
→ linked_candidate
→ closed_without_match
```

`linked_candidate` y `closed_without_match` son terminales para esta resolución. Los estados `overdue`, `escalated`, `stale_projection` y `delivery_unknown` describen operación/proyección; no reescriben el resultado determinístico ni la resolución manual.

Precedencia para renderizar Slack:

1. resolución terminal durable;
2. evidencia obsoleta que exige nueva revisión;
3. escalamiento;
4. vencimiento;
5. pendiente normal.

Así, un recordatorio tardío nunca vuelve a mostrar como pendiente un caso ya resuelto.

## 7. Fronteras de seguridad

- App instalada sólo en el workspace y canales exclusivos configurados.
- Un Channel ID no puede estar asignado a más de un tenant y no existe fallback a
  un canal global.
- Usuarios resolutores mediante allowlist de IDs Slack por tenant; el nombre visible no concede autoridad.
- Firma y anti-replay obligatorios para interactividad.
- Token bot y signing secret sólo en el secret store del runtime.
- Sin payloads, teléfonos, emails o nombres completos en logs.
- Sin datos completos en Slack; sólo masking ya producido por la proyección SQL.
- Sin creación de contactos o conversaciones Chatwoot cuando la identidad sea incierta.
- Sin mutar correlación determinística, `purchase_intents`, consentimiento, activación, timers comerciales u outbound.

## 8. Primer corte implementable

1. contrato de bloques/modal y estados;
2. tablas de hilo, mensajes y efectos con invariantes físicas;
3. publicador default-off de un mensaje raíz por caso;
4. endpoint firmado para abrir el modal;
5. submit `prepare → confirm` usando el contrato manual existente;
6. actualización del mismo mensaje raíz;
7. reconciliador y una política de vencimiento sin cierre automático;
8. pruebas con servidor HTTP local y Slack simulado; activación real separada.

## 9. Decisiones todavía abiertas

- SLA inicial y cadencia de recordatorios; recomendación: vencimiento a 24 h, recordatorio al vencer y escalamiento a 48 h.
- el canal exclusivo de Johanna ya existe; siguen pendientes el canal de ATT1, la
  conexión interactiva de la app, sus IDs privados, permisos y grupo de escalamiento;
- lista inicial de usuarios autorizados;
- si un mensaje raíz eliminado debe recrearse automáticamente o escalar primero; recomendación: recrear una vez y escalar ante una segunda pérdida.

Estas decisiones son configuración operativa. No cambian la autoridad durable ni el modelo caso↔mensaje.
