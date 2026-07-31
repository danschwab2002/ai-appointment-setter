# ADR-0003: Frontera determinista–razonamiento en la recuperación de carrito

- **Estado:** Aceptada
- **Fecha:** 2026-07-31

## Contexto

El webhook de abandono de carrito de Hotmart (`PURCHASE_OUT_OF_SHOPPING_CART`
v2.0.0) llega al bridge con 9 campos: nombre, email, teléfono (opcional),
producto, oferta y país. A partir de ahí, el sistema debe determinar quién es
la persona, qué datos preexistentes hay sobre ella en Supabase, y qué hacer:
iniciar una conversación de recupero, extender un caso existente, frenar
por intervención humana, abstenerse por `do_not_contact`, o derivar.

El abanico de situaciones posibles es amplio y crece con casos atípicos:

- el payload trae email y teléfono;
- el payload trae email pero no teléfono (el infoproductor no lo pidió en
  el checkout);
- la persona no existe en la base;
- la persona existe, identificada por email;
- la persona existe y tiene una conversación activa con `human_takeover`;
- la persona existe y tiene un `recovery_case` abierto por un abandono
  anterior;
- la persona está marcada `do_not_contact` u `opted_out`;
- la persona existe por un contacto previo en Instagram, no por Hotmart.

Codificar todos estos caminos con lógica determinista (if/else) crece como
un árbol de decisiones frágil que se rompe en el primer caso no anticipado.
Pero delegar la **totalidad** del proceso al agente comercial —incluyendo los
SELECTs contra Supabase— choca con la frontera definida en ADR-0001: el
profile no autoriza a modificar Supabase ni conservar el historial canónico
del prospecto, y un LLM que alucina un contacto duplica o pierde datos con
errores caros de detectar.

## Decisión

La recuperación de carrito se procesa en **dos capas** que se comunican
mediante un **informe de situación** estructurado:

### Capa 1 — Bridge (determinista)

El bridge ejecuta todas las operaciones que tienen una respuesta objetiva
y verificable:

1. **Extraer** email y teléfono del payload de Hotmart (`data.buyer.email`,
   `data.buyer.phone`).
2. **Normalizar** email (lower + trim) y teléfono (DDI sin `+`).
3. **Consultar Supabase** sin invocar al agente:
   - ¿existe un `contacts` con ese email en `contact_points`?
   - ¿existe un `contacts` con ese teléfono en `contact_points`?
   - ¿tiene `channel_identities` de WhatsApp?
   - ¿tiene `conversations` activas? ¿cuál es su `status`,
     `automation_status`, `human_takeover`, `paused_until`?
   - ¿tiene `recovery_cases` abiertos? ¿en qué `status` y `lead_stage`?
   - ¿`contact_permission` o `identity_status` bloquean el contacto?
4. **Crear o actualizar** `contacts`, `contact_points`, `channel_identities`
   y `recovery_cases` según corresponda —todo por SQL, sin pasar por el LLM.
5. **Registrar** el intento en `identity_resolution_attempts` con la
   strategy usada y el resultado.
6. **Armar un informe de situación** en JSON, con toda la información
   relevante para que el agente razone.
7. **Marcar** `webhook_events.processing_status = 'processed'`.

### Capa 2 — Agente comercial (razonamiento)

El agente comercial recibe el informe de situación y **decide** qué hacer:

- No consulta Supabase directamente. No puede alucinar contactos ni
  duplicar filas.
- Razona sobre el informe: ¿hay conversación activa con humano? ¿ya hay
  un recovery_case? ¿el contacto está bloqueado? ¿el teléfono falta?
- Decide el camino: primer mensaje de recupero, extender caso existente,
  frenar, derivar a humano, o abstenerse.
- Su salida es una **propuesta estructurada** (no una ejecución directa),
   igual que en el modo sombra ya implementado.
- El bridge valida y ejecuta la propuesta con los mismos controles
  determinísticos de ADR-0002 (pausa, JID autorizado, idempotencia).

### El informe de situación

El bridge construye un JSON con —como mínimo—:

- Identidad del evento: `event_id`, `event_type`, `source`.
- Datos del payload: `buyer_name`, `buyer_email`, `buyer_phone`,
  `product_name`, `offer_code`, `checkout_country`.
- Estado de la identidad resuelta: `contact_id` (o `null` si es nuevo),
  `identity_resolution_status`, strategy usada.
- Estado de conversaciones existentes: `conversation_id`, `status`,
  `automation_status`, `human_takeover`, `last_inbound_at`,
  `last_outbound_at`.
- Estado de recovery_cases existentes: `recovery_case_id`, `status`,
  `lead_stage`, `current_goal`.
- Permisos: `contact_permission`, `identity_status`.
- Flags derivados: `has_active_conversation`, `has_open_recovery_case`,
  `phone_available`, `contact_blocked`.

El agente no necesita hacer SELECTs porque el bridge ya los hizo. Si el
informe no contiene información que el agente necesita, el problema es
del bridge (que no la incluyó), no del agente (que no la buscó).

### Procesamiento diferido

El endpoint `POST /webhooks/hotmart` responde 202 inmediatamente después
de persistir el evento en `webhook_events` (ya implementado). La resolución
de identidad y el razonamiento del agente ocurren en un **proceso separado**
que consume eventos en `processing_status = 'received'`. Esto mantiene la
respuesta HTTP instantánea, evitando que Hotmart desactive la configuración
del webhook por timeout.

## Por qué no todo determinista

Un árbol de if/else que enumera todos los caminos posibles se rompe en el
primer caso atípico no anticipado. La realidad operativa incluye:

- un lead que abandonó dos veces el mismo producto en días distintos;
- un lead que escribió por Instagram la semana pasada y ahora abandonó el
  carrito;
- un lead cuyo teléfono en Hotmart difiere del que usó en WhatsApp;
- un lead marcado `do_not_contact` por un humano hace un mes;
- un lead cuyo `recovery_case` anterior quedó en estado `lost` pero ahora
  abandona otro producto distinto.

El agente razona sobre estos casos sin necesidad de que el bridge los
prevea. Su valor es el juicio, no el lookup.

## Por qué no todo razonamiento

Las operaciones de identidad (existe/no existe, match por email, crear
contacto) tienen una respuesta objetiva. Un LLM que alucina:

- puede inventar un `contact_id` que no existe;
- puede decidir que un contacto no existe cuando sí existe (duplicado);
- puede no ver un `human_takeover = true` y mandar un mensaje sobre una
  conversación que un humano está manejando;
- puede no respetar `do_not_contact` si no se le da el contexto explícito.

Estos errores corrompen la base de datos o violan la frontera operativa
definida en ADR-0001, y son caros de detectar porque el agente los produce
con confianza.

## Consecuencias

- El bridge asume una nueva responsabilidad: construir el informe de
  situación completo antes de invocar al agente.
- El agente no necesita credenciales de Supabase. No puede leer ni escribir
  la base directamente.
- El contrato de entrada del agente se enriquece: además del contexto de
  conversación que ya recibe (mensajes, known_fields), ahora recibe el
  informe de situación con el estado de identidad, conversaciones y
  recovery_cases.
- El contrato de salida del agente se mantiene: una propuesta estructurada
  que el bridge valida y ejecuta.
- La skill del agente comercial debe documentar cómo leer el informe de
  situación y qué decisiones tomar según cada escenario.
- Los casos atípicos se manejan en la capa de razonamiento sin necesidad de
  extender el código del bridge cada vez que aparece un nuevo patrón.
- El bridge debe ser el único que escribe en Supabase. Cualquier estado que
  el agente necesite persistir (lead_stage, current_goal, etc.) se envía
  como propuesta y el bridge lo ejecuta.
- El procesamiento diferido requiere un mecanismo de consumo (poller o
  worker) que aún no está implementado.

## Alternativas descartadas

### Procesamiento inline en el endpoint

Se descarta porque la resolución de identidad + invocación del agente
agrega latencia a la respuesta HTTP. Hotmart desactiva la configuración del
webhook si la URL responde lento o con error (408). El endpoint debe
responder 202 inmediatamente.

### El agente consulta Supabase directamente

Se descarta porque viola la frontera de ADR-0001 (el profile no puede
modificar Supabase ni conservar el historial canónico) y porque un LLM que
alucina un contacto corrompe la base con errores caros de detectar.

### Árbol de decisiones determinista completo

Se descarta porque la variedad de situaciones operativas —especialmente
casos atípicos con contactos preexistentes de múltiples canales— crece como
un árbol frágil que se rompe en el primer caso no anticipado. El costo de
mantener y extender el código por cada nuevo patrón supera el beneficio de
la predictibilidad.
