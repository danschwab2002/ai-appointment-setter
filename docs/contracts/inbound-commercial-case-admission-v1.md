# Contrato de admisión inbound comercial V1

- **Estado:** Implementado en feature branch; no mergeado, desplegado ni activado
- **Versión:** 1
- **Migración:** `20260816000200_inbound_commercial_case_draft_only.sql`
- **Efectos externos:** ninguno

## 1. Propósito

Crear o reutilizar una raíz `commercial_cases.case_kind=inbound_sales` desde la
tupla Chatwoot exacta. Si no existe identidad canónica, crea el mínimo
`contact → channel_identity → conversation` sin nombre, email, teléfono,
consentimiento ni correlación pre-checkout. No fabrica abandono Hotmart, handoff,
scheduling, invocación Hermes ni outbound.

## 2. Scope server-side

`inbound_commercial_scope_versions` contiene la conjunción versionada:

- tenant;
- Chatwoot account e inbox;
- producto y oferta.

El RPC sólo acepta un `scope_key/version`; deriva las dimensiones anteriores de
una fila `published`. La migración no publica ninguna fila productiva. Un scope
publicado es inmutable.

## 3. RPC

```text
admit_inbound_commercial_case(
  p_scope_key text,
  p_scope_version integer,
  p_external_conversation_id bigint,
  p_external_user_id text
)
```

Única firma ejecutable por `service_role`. `anon` y `authenticated` no tienen
`EXECUTE`; los roles API no reciben DML directo sobre tablas de Corte B.

## 4. Canonicalización

La admisión serializa por command key, conversación externa e identidad. Resuelve
la identidad estable por `whatsapp + account + external_user_id`; account e inbox
provienen del scope. Si no existe, crea un contacto mínimo con permiso `unknown` y
una identidad activa. Nunca usa nombre, email o teléfono para fusionar contactos.

`channel_identities.external_conversation_id` es sólo el puntero denormalizado
last-write-wins de ADR-0008. La conversación autoritativa se resuelve o crea por:

```text
channel_identity_id + commercial_context =
{"chatwoot_conversation_id": "<id exacto>"}
```

El objeto de anchor es mínimo y exacto durante la admisión; claves adicionales no
confiables no se incorporan al contexto comercial y el ID debe ser un entero
decimal positivo, sin cero inicial. El ownership se comprueba
contra todas las conversaciones ancladas del account, no contra el puntero
last-write-wins. Un anchor histórico no puede ser reclamado por otra identidad.

Una conversación nueva queda `active + draft_only`. Una conversación existente
debe pertenecer al mismo contacto/identidad y ya estar `draft_only`; conflictos de
ownership, inbox o estado fallan cerrado sin estado parcial.

## 5. Resultado

Devuelve una fila con:

- `outcome`: `created | already_exists | evidence_conflict`;
- `commercial_case_id`;
- `contact_id`;
- `channel_identity_id`;
- `conversation_id`;
- `automation_status`, siempre `draft_only`.

La raíz creada queda:

- `case_kind=inbound_sales`;
- `status=active`;
- `automation_status=draft_only`;
- `identity_resolution_status=resolved`, sólo para la identidad Chatwoot exacta;
- `authority_mode=shadow`;
- `version=1`.
- `inbound_scope_key`, `inbound_scope_version` y `tenant_ref` derivados del scope.

`resolved` no prueba correlación pre-checkout, consentimiento ni autorización de
contacto proactivo.

## 6. Replay y conflicto

La command key durable es:

```text
(scope_key, scope_version, external_conversation_id)
```

- replay con la misma identidad y bindings devuelve `already_exists`, aunque la
  conversación haya sido pausada después de la admisión original;
- una identidad que dejó de estar activa, un `external_user_id` distinto u otro
  drift canónico devuelve
  `evidence_conflict`, conserva el caso original y agrega evidencia append-only;
- el conflicto no crea una segunda raíz ni habilita ningún efecto.

## 7. Correlación de intención

`commercial_case_intent_correlations` mantiene un vínculo separado con estado:

```text
resolved | candidate | ambiguous | conflict | unmatched
```

La tabla es append-only y no tiene writer RPC en Corte B. Ningún estado, incluso
`resolved`, cambia consentimiento, destinatario, identidad canónica o autorización.

## 8. Errores fail-closed

- `invalid_inbound_commercial_case_parameters`;
- `inbound_commercial_scope_unavailable`;
- `inbound_canonical_identity_conflict`;
- `inbound_external_conversation_owned_by_another_identity`;
- `inbound_canonical_conversation_conflict`.

Los errores no producen estado parcial porque la admisión es una transacción SQL.

## 9. Fuera de alcance

- enriquecer o fusionar contactos desde nombre, email, teléfono u otros datos fuzzy;
- wiring del webhook Chatwoot;
- publicación del scope real;
- correlación automática con pre-checkout;
- agent calls, drafts generados por IA, handoff o mensajes;
- scheduler, dispatcher y follow-ups.
