# Arquitectura inicial

## Flujo del primer hito

```text
WhatsApp -> Evolution API -> Chatwoot -> POST /webhooks/chatwoot -> captura privada
```

El primer receptor no llama a un modelo ni envía mensajes. Su objetivo es obtener un evento real y confirmar el contrato de Chatwoot 4.13.0 con la integración de Evolution API 2.3.7.

## Restricción de prueba

Solo se acepta para procesamiento el contacto cuyo identificador de WhatsApp
coincide exactamente con `ALLOWED_WHATSAPP_JID`. El valor es configuración
sensible del despliegue y no se documenta en el repositorio.

La restricción se aplica por código antes de invocar Hermes; no depende de una
instrucción de prompt.

## Ingreso durable desde Chatwoot

```text
Chatwoot -> POST /webhooks/chatwoot
         -> autenticación + anti-replay + filtro de JID
         -> captura privada + admisión atómica en CAPTURE_DIR/.work
         -> HTTP 202

worker local -> debounce durable + lock hasheado por conversación (30 s)
             -> líder por mayor message_id canónico
             -> historial de Chatwoot validado contra todos los IDs del batch
             -> API Server de agente-comercial
             -> validación JSON
             -> archivo privado en SHADOW_DIR
             -> divisor opcional de formato + validación determinista
             -> 1–4 autorizaciones finales + AgentBot de Chatwoot
```

El receptor sólo devuelve HTTP 202 después de persistir una admisión recuperable.
No espera la consulta de historial, la ejecución de Hermes ni el envío de la
respuesta. El worker procesa una admisión por vez y retoma archivos con estado
`admitted` al reiniciarse. Sólo marca `completed` después de un resultado
terminal; las guardas y marcadores existentes mantienen idempotentes las
evaluaciones y los efectos externos ante replay.

El historial se trunca en el ID canónico del mensaje que originó el webhook.
Para mensajes públicos entrantes, cada nueva admisión de la conversación reinicia
una ventana durable de 30 segundos. Cuando vence, el mayor `message_id` canónico
del grupo se convierte en el trigger aunque los webhooks hayan llegado fuera de
orden. El cliente pagina el historial con `before`, y los mensajes anteriores del
mismo turno forman parte de una única evaluación. Los mensajes posteriores al
trigger no forman parte de esa evaluación. Si algún ID del batch no aparece en la
lectura acotada, el bridge falla cerrado y no invoca Hermes. Las intervenciones
humanas no esperan esta ventana. El worker repite el scan y la decisión del turno
bajo el lock conversacional para que una admisión ocurrida entre el scan inicial y
el lock reinicie efectivamente el deadline.

La lectura canónica conserva una ventana reciente mínima y pagina hasta encontrar
los IDs requeridos del batch, alcanzar el inicio real o agotar 100 páginas. Ese
último caso entra al circuito terminal acotado en vez de bloquear la conversación
con retries infinitos.

La cola usa el mismo volumen privado persistente de las capturas. Los nombres de
archivo derivan del hash del delivery ID, y las escrituras de admisión y
finalización son atómicas y sincronizadas a disco. Esta implementación presupone
un único servicio del bridge compartiendo ese volumen; el lock por archivo evita
procesamiento concurrente dentro de ese despliegue. El dead-letter de un grupo
persistente usa además un journal privado de intención: si el proceso cae entre
miembros, el próximo escaneo termina la transición antes de elegir otro turno.

## Flujo de envío implementado

```text
WhatsApp -> Evolution API -> Chatwoot -> bridge
         -> agente-comercial (Hermes) -> controles determinísticos
         -> AgentBot de Chatwoot -> Evolution API -> WhatsApp
```

El flujo fue validado E2E con el WhatsApp autorizado. Hermes genera una propuesta
estructurada; el bridge vuelve a consultar Chatwoot y conserva la decisión final.
Antes de publicar valida pausa, intervención humana, JID canónico, trigger,
avance de conversación, idempotencia e identidad exclusiva del AgentBot.

La división outbound opcional, implementada localmente pero apagada por defecto,
consulta el mismo API server Hermes con `provider`
y modelo pequeño explícitos. El prompt por request sólo propone cortes; el bridge
exige 1–4 partes y reconstrucción del texto original, y aplica fallback a una
sola parte persistida si el modelo no cumple el contrato. Un fallo de almacenamiento
falla cerrado antes de cualquier POST. La división válida se persiste
en el volumen privado antes del primer envío como un manifiesto inmutable
identificado por conversación + trigger canónico, nunca por delivery. Una claim
hash-only independiente se sincroniza antes del JSON; si el manifiesto desaparece
pero la claim permanece, el lote falla cerrado sin recalcular geometría. Cada parte
multipart tiene marker
propio con hash de lote, índice y total. Entre partes nuevas se esperan dos
segundos configurables y se repiten todas las guardas; un inbound nuevo o una
intervención humana bloquean las restantes. Antes de cada POST se sincroniza un
journal hash-only `posting`; si el efecto queda incierto, los replays sólo
reconcilian y nunca repiten el POST. El historial se pagina con trigger requerido
y falla cerrado al agotar el límite de 100 páginas.

La autorización se repite inmediatamente antes de cada `POST`. La respuesta
creada se acepta sólo si coincide en conversación, dirección, visibilidad,
contenido, AgentBot y marcador idempotente. En replay, Chatwoot se consulta por
el marker exacto de cada parte antes de repetir un efecto externo.

La división multipart todavía no tiene evidencia de despliegue ni E2E real por
WhatsApp; `CHATWOOT_REPLY_SPLITTER_ENABLED=false` continúa siendo el default.
Ese flag impide crear lotes nuevos, pero no desactiva la lectura y reconciliación
de manifiestos ya existentes.
Los journals de respuesta única previos a la activación también bloquean una
geometría multipart nueva mientras su entrega permanezca incierta.

## Cierre determinístico por compra aprobada

La implementación del repositorio admite `PURCHASE_APPROVED` de Hotmart como un
evento durable distinto del abandono:

```text
Hotmart PURCHASE_APPROVED
  -> autenticación + anti-replay + deduplicación
  -> webhook_events(received)
  -> ResolutionWorker
  -> correlación transaccional por identidad + producto + oferta
  -> recovery_case(won)
  -> followup_sequence(completed)
  -> scheduled_action(cancelled si todavía no inició entrega)
```

La correlación no se delega a Hermes. Una coincidencia exacta cierra el caso y
la secuencia en la misma transacción. Una coincidencia ambigua pausa los casos
candidatos y requiere revisión humana; no elige el primer resultado. Los envíos
con resultado externo incierto conservan su estado `delivery_unknown` para no
confundir ausencia de confirmación con ausencia de efecto.

El contrato detallado se encuentra en
[Compra aprobada de Hotmart V1](contracts/hotmart-purchase-approved-v1.md). La
implementación y el DDL están presentes en Supabase, con permisos efectivos y
ambos órdenes de eventos verificados mediante un probe transaccional con
rollback. Esto no prueba que el bridge desplegado use esta versión ni que una
compra real haya sido verificada end-to-end. La evidencia se registra en
[Postflight Supabase del 2026-08-08](operations/2026-08-08-hotmart-purchase-cancellation-supabase.md).

## Decisiones arquitectónicas

- [ADR-0001: Profile comercial como motor de razonamiento aislado](decisions/0001-commercial-profile-boundary.md)
- [ADR-0002: Detección y señalización de intervención humana](decisions/0002-human-takeover-detection.md)
- [ADR-0003: Frontera determinista–razonamiento en la recuperación de carrito](decisions/0003-deterministic-reasoning-boundary.md)
- [ADR-0004: Capa de mensajería abstraída para soportar migración Evolution → WABA](decisions/0004-messaging-layer-abstraction.md)
- [ADR-0005: Empaquetado reproducible y aislamiento por cliente](decisions/0005-reproducible-client-deployments.md)
- [ADR-0006: Superficie de producto de tres agentes](decisions/0006-three-agent-product-surface.md)
- [ADR-0007: Motor durable de próxima acción](decisions/0007-durable-next-action-engine.md)

## Estado operativo

El registro de validación E2E, despliegue y supervisión durable del gateway se
encuentra en [Registro operativo del 2026-07-31](operations/2026-07-31-production-readiness.md).
