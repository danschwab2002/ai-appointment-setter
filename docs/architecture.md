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
             -> detector determinista de baja explícita
                -> RPC SQL autoritativa + corte del turno
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

## Baja inbound durable

La baja explícita se resuelve antes de Hermes sobre el turno canónico completo.
La detección es determinística y la autoridad resultante vive en Supabase, no en
el modelo ni en una etiqueta de Chatwoot:

```text
validación canónica de inbox + JID en Chatwoot
  -> turno canónico completo
  -> consulta del stop durable previo
  -> detector cerrado de baja explícita
  -> apply_chatwoot_inbound_opt_out
     -> stop fact + denied + contacto do_not_contact
     -> cierre del workflow no iniciado
     -> delivery_unknown si request_started ya existía
  -> sin Hermes ni respuesta automática

contact_opt_out_events(projection pending)
  -> OptOutProjectionWorker con lease
  -> macro dedicado de Chatwoot
  -> confirmación de automation_opted_out + automation_paused
```

Un stop `unmatched` o `ambiguous` conserva la identidad canónica de Chatwoot. Si
la identidad interna aparece después, la misma evidencia puede reconciliarse. La
frontera `request_started` toma un advisory lock por cuenta y usuario externo y
consulta también esos stops anteriores: una planificación creada en el orden
inverso no puede iniciar el request. Los intentos ya iniciados se conservan como
efecto incierto; evidencia tardía `not_applied` los cierra sin retry y una
aceptación tardía no reabre el caso ni crea sucesores.

La tabla de intentos no admite `INSERT`, `UPDATE` ni `DELETE` directo desde
`service_role`; las transiciones necesarias pasan por entrypoints
`SECURITY DEFINER` de firma cerrada. La proyección Chatwoot consulta primero las
labels canónicas: si ambas ya existen no repite el macro. Su finalización exige
owner, generación y lease vigente, por lo que un replay tardío no muta el evento.
La proyección es reintentable y no puede revertir la autoridad SQL. Esta
implementación está presente en el árbol y
tiene pruebas locales, PGlite y PostgreSQL real; todavía no constituye evidencia
de migración aplicada ni de worker activo en producción.

## Ingreso autoritativo de abandono de carrito

`PURCHASE_OUT_OF_SHOPPING_CART` se autentica por Hottok y se valida contra el contrato Hotmart `2.0.0` antes de reservar identidad durable. La RPC `admit_hotmart_cart_abandonment` es la frontera transaccional que inserta el evento, reconoce replays exactos y registra diferencias bajo el mismo `external_event_id` como conflictos semánticos.

La resolución consulta email y teléfono y falla cerrado si apuntan a contactos distintos o si un identificador tiene múltiples dueños. La planificación sigue siendo asíncrona, pero un trigger de base valida en la misma transacción que evento, contacto, producto, oferta y timestamp coincidan exactamente antes de asociar el evento con un caso. Conflictos semánticos no resueltos bloquean globalmente el inicio de requests outbound. El contrato detallado está en `docs/contracts/hotmart-cart-abandonment-v1.md`.

## Perímetro durable del piloto Lancemos

El árbol incluye una capa SQL default-off que acota el piloto Lancemos y wiring
runtime para aplicarla en planificación y request-start. El código y las
migraciones tienen evidencia local; no se aplicaron todavía en producción ni se
armó una cohorte real.

Las fuentes de verdad son:

- `pilot_scope_versions`, para el scope publicado e inmutable de tenant,
  account/inbox, cuenta opaca de canal, evento Hotmart, producto, oferta, policy
  y límites;
- `pilot_runtime_controls`, para versión seleccionada, estado
  `inactive|armed|paused|closed` y generación CAS;
- `pilot_cohort_memberships`, para la cohorte explícita por versión y contacto;
- `pilot_outbound_request_authorizations`, como ledger append-only de
  autorizaciones de request-start y consumo conservador de presupuesto;
- `pilot_control_events`, para la auditoría de activación, pausa/cierre, cambio
  de versión y membresía.
- `pilot_recovery_case_bindings`, para ligar de forma inmutable cada caso al
  scope/version y al evento autoritativo que admitió su planificación.

`plan_lancemos_pilot_cart_recovery` compone evaluación, planificación y binding
durable en una sola transacción. Recibe sólo scope/version; tenant y routing se
derivan del scope publicado. Rechaza antes de persistir trabajo si scope,
versión, policy, identidad o cohorte no coinciden. Los RPC históricos de
planificación ya no son entrypoints para roles API.

`mark_lancemos_pilot_request_started` no acepta dimensiones de scope del caller:
las deriva del binding inmutable del caso. Compone la autorización actual del piloto
con la frontera de request-start y con los guards existentes de autorización
del contacto, compra, takeover y opt-out. El entrypoint histórico conserva su
firma sólo para composición interna, exige la autorización durable del mismo
action/attempt y no es ejecutable por roles API; la función interna y la
función de autorización standalone tampoco lo son. Así, un caller con
`service_role` no puede separar autorización y efecto ni omitir el perímetro.

Publicar o activar una versión nunca arma outbound. Un cambio de versión sólo es
válido desde `inactive|paused`, siempre vuelve a `inactive`, no copia la cohorte
y no reinicia presupuesto. `inactive`, `paused` y `closed` bloquean
autorizaciones nuevas. Los replays exactos recuperan el ledger original, pero
`replayed=true` nunca habilita otro efecto externo. El cap diario usa el reloj de
PostgreSQL y la timezone es constante para todas las versiones de un mismo
`scope_key`. Las tablas niegan DML directo a roles API y `service_role`; sólo los
entrypoints explícitos tienen `EXECUTE`.

El bridge valida al arrancar que cualquier worker de planificación u outbound
esté asociado a una configuración completa del perímetro. `/health` conserva
liveness simple y `/ready` consulta una RPC de estado sanitizada. Un runtime
`inactive` es operacionalmente ready pero no autoriza automatización; un scope
o una versión incoherentes producen HTTP 503. Docker y Compose usan `/ready`,
por lo que el diagnóstico normal no depende de consola interactiva.

El sender local usa siempre la API de Chatwoot. Para un scope `waba`, el factory
exige templates aprobados separados para primer contacto y seguimiento y envía
`template_params` con un único placeholder de body. No existe fallback a texto
libre ni a Evolution cuando el scope durable declara WABA. El dispatcher registra
esos intentos como `approved_template`; la frontera SQL request-start rechaza
fail-closed una reserva WABA marcada como `freeform`.

Siguen pendientes el despliegue de migraciones, la configuración remota
automatizada, IDs reales, cohorte, caps y owner del kill switch, WABA oficial y
HTTP E2E contra el entorno desplegado. El contrato de fase 1 está en
[Perímetro Lancemos V1](contracts/lancemos-pilot-boundary-v1.md), el wiring en
[Wiring runtime V1](contracts/lancemos-pilot-boundary-runtime-v1.md) y la
evidencia local en `docs/operations/`.

## Handoff humano ejecutable

El árbol implementa un handoff stop-first para casos Lancemos que ya tienen una
conversación Chatwoot canónica:

```text
Hermes suggest_handoff
  -> bridge valida motivo allowlisted + policy/scope piloto
  -> request_human_handoff
     -> pausa durable caso/secuencia/conversación
     -> cierra reservas previas a request
     -> preserva request_started como delivery_unknown
     -> crea assignment + private_note pendientes
  -> HumanHandoffProjectionWorker
     -> reconcilia assignment sin sobrescribir persona/otro equipo
     -> reconcilia nota privada por marcador estable
     -> finaliza cada efecto con lease fenced
```

Supabase es autoridad del stop y Chatwoot es una proyección operativa. Cada
request fija policy, scope, account, inbox, conversación externa, equipo y nota.
La admisión y la proyección son flags separados y default-off; apagar admisión no
detiene el drain de efectos existentes. No se crean conversaciones, labels,
macros ni mensajes externos al contacto.

`/ready` publica conteos sanitizados del backlog cuando la proyección está
habilitada. Esta implementación tiene evidencia local y PGlite, pero no acredita
migración aplicada, IDs/equipo reales ni worker o Chatwoot productivos. La
decisión está en [ADR-0010](decisions/0010-executable-human-handoff.md) y la
interfaz exacta en [Handoff humano V1](contracts/executable-human-handoff-v1.md).

## Cierre determinístico por compra aprobada

La implementación del repositorio admite `PURCHASE_APPROVED` de Hotmart como un
evento durable distinto del abandono:

```text
Hotmart PURCHASE_APPROVED
  -> autenticación + anti-replay + admisión semántica transaccional
  -> webhook_events(received)
  -> ResolutionWorker
  -> correlación transaccional por identidad + producto + oferta
  -> recovery_case(won)
  -> followup_sequence(completed)
  -> scheduled_action(cancelled si todavía no inició entrega)
```

La transacción Hotmart no se trata como duplicate por sí sola. La RPC de
admisión compara una tupla de negocio normalizada. Un replay idéntico se
deduplica; una tupla distinta para la misma transacción crea un incidente
durable y activa un bloqueo global fail-closed en la frontera
`request_started`. Admisión y request-start comparten un advisory lock: la
operación que gana se vuelve visible antes de que la otra continúe. Así, un
request ya iniciado conserva honestamente su posible efecto y ningún request
nuevo puede comenzar hasta una resolución operativa explícita. Los casos y
acciones pueden permanecer visibles como pendientes, pero no pueden producir
un efecto externo.

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
