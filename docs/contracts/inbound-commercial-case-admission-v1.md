# Contrato de admisión inbound comercial V1

- **Estado:** Implementado en feature branch; no mergeado, desplegado ni activado
- **Versión:** 1
- **Migración:** `20260816000200_inbound_commercial_case_draft_only.sql`; el
  entrypoint portable de §3.1, `20261001000400` y `20261001000500` (bridge
  1.3.1, sin publicar)
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

### 3.1 Entrypoint portable: adopción de la conversación de una plantilla (H7)

Migración `20261001000400_portable_inbound_adopts_template_conversation.sql`,
bridge 1.3.1, sin publicar:

```text
admit_portable_inbound_commercial_case_v1(
  p_scope_key text,
  p_scope_version integer,
  p_external_conversation_id bigint,
  p_external_user_id text
)
```

La llama solo el bridge con manifiesto, en las tres admisiones del entrante:
el mensaje, la reautorización antes de responder y la readmisión después de
reanudar. Sin manifiesto (Johanna) el bridge sigue llamando a
`admit_inbound_commercial_case_v2` con los mismos argumentos. Mismo resultado
(§5), mismos errores (§8) y la misma ACL: `security definer`,
`search_path = pg_catalog, public, pg_temp`, `EXECUTE` revocado a `public`,
`anon` y `authenticated` y concedido a `service_role`.

**Qué resuelve.** Cuando Chatwoot acepta una plantilla del dispatcher del
piloto (carrito, pago fallido o primer contacto), la aceptación crea la
conversación canónica del caso en `automation_status = 'enabled'`. La
admisión de §4 solo toma una conversación existente en `draft_only`, así que
la respuesta del lead daba `inbound_canonical_conversation_conflict`.

**Qué hace.** Con el scope publicado, toma los mismos tres advisory locks que
la admisión base, en el mismo orden. Si todavía no hay admisión para la
command key (§6), bloquea la identidad que contesta (activa, del inbox del
scope) y después su conversación (`commercial_context` exacto, `enabled`, sin
`human_takeover`, status vivo). La **adopta** solo si se cumplen las tres
condiciones:

1. Una plantilla del piloto salió en esa conversación. La cadena es: mensaje
   outbound de `ai_agent` con `strategy = durable_followup` → intento
   `accepted_by_chatwoot` → acción → caso de recuperación de esa identidad,
   contacto y conversación → su fila en `pilot_recovery_case_bindings`.
2. El contacto no está dado de baja: `contact_permission` fuera de
   `opted_out`, `blocked` y `restricted`, `lifecycle_status` distinto de
   `do_not_contact`, y ninguna baja de Chatwoot de ese móvil en
   `contact_opt_out_events` (las formas del que contesta y las de
   `contacts.phone`; `correlation_status` `applied`, `unmatched`, `ambiguous`
   o `evidence_conflict`, los estados que frena el arranque del piloto en
   `_portable_chatwoot_opt_out_stop`). Una baja que no quedó aplicada a este
   contacto, porque entró por la otra forma del móvil o hay dos contactos,
   también frena. Se lee sin el advisory lock de opt-out: la adopción ya
   tiene la identidad bloqueada y `apply_chatwoot_inbound_opt_out` toma ese
   lock antes que la identidad.
3. Ninguna acción de recuperación de esa persona (en esta conversación o sin
   conversación) está en `pending`, `deferred`, `retryable_failed` o
   `delivery_unknown`. Desde `20261009000200` no cuenta un primer contacto
   (`anchor_type = 'precheckout_intent'`) en `pending`, `deferred` o
   `retryable_failed` que ya no va a salir: su intención tiene la
   clasificación del carrito o del pago fallido (`confirmed_abandonment`,
   `payment_failure_supported`) o una compra (`intent_purchased`,
   `purchase_by_identity`, `intent_purchase_ambiguous` de
   `_portable_precheckout_stop_reason`). Con cualquiera de esos, la
   reevaluación lo cancela cuando el despachador lo toma, a su hora; hasta
   entonces la acción sigue en `pending`, y sin esta excepción la respuesta a
   la plantilla del carrito o del pago fallido no llegaba al agente (medido en
   ATT1 el 2026-10-09).

Adoptar es pasar la conversación a `draft_only` (`version + 1`) y escribir un
`conversation_events` `inbound_adopted_template_conversation` (actor
`integration`, `related_message_id` = la plantilla, `related_action_id` = su
acción). Después delega **siempre** en `admit_inbound_commercial_case_v2`,
sin tocarla, en la misma transacción: si la v2 levanta un error, la adopción
también se deshace. Si no adopta, el resultado es exactamente el de la v2.

**Lo que no adopta, a propósito.** Un contacto dado de baja (condición 2) que
responde a una plantilla vieja lo atiende una persona en Chatwoot: la v2 da el
conflicto y solo el respaldo del bridge para la baja actúa (decisión de Dan
del 2026-10-01). Lo mismo pasa con un paso de recuperación pendiente
(condición 3) y con dos identidades del mismo móvil (`23505`).

**El caso de la conversación adoptada.** La conversación adoptada tiene dos
raíces: el `cart_recovery` que copia el trigger de sombra y el `inbound_sales`
de la respuesta. La migración `20261001000500` hace que
`mark_human_handoff_attended`, `claim_conversation_reactivation`,
`resume_paused_conversation` y `claim_conversation_followup_v1` cuenten y
elijan solo `case_kind = 'inbound_sales'` **cuando la conversación tiene el
evento de adopción**. Sin el evento cuentan todas las raíces, como antes. El
filtro depende del evento porque una medición de solo lectura en la base de
Johanna (2026-10-01) encontró 2 conversaciones con un único `cart_recovery`, y
un filtro para todas les habría cambiado el resultado.

**Despliegue.** Las migraciones `000400` y `000500` van antes que el bridge.
Sin la `000400`, PostgREST responde `404` (`PGRST202`) y el bridge reintenta el
entrante sin tope hasta que se aplique.

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
ownership, inbox o estado fallan cerrado sin estado parcial. La única
conversación `enabled` que pasa a `draft_only` es la que adopta el entrypoint
portable de §3.1, antes de delegar en esta admisión.

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

Con el entrypoint portable (§3.1):

- la adopción solo ocurre si no hay fila de admisión para la command key. Una
  entrega repetida del mismo entrante se serializa en el primer advisory lock;
  la segunda ve la fila, no adopta otra vez, y la v2 da `already_exists`. Queda
  un solo evento `inbound_adopted_template_conversation`;
- la readmisión después de reanudar una conversación adoptada da
  `already_exists` con `draft_only`, como en una conversación que nació
  `draft_only`;
- una respuesta que la admisión confirma **antes** de que la reconciliación
  acepte su plantilla (envío en `delivery_unknown`) no encuentra conversación
  que adoptar: la v2 la crea en `draft_only` y la aceptación tardía le ata el
  caso de recuperación. Quedan `cart_recovery` e `inbound_sales` sin el
  evento de adopción, y en esa conversación las cuatro búsquedas del caso
  siguen dando `*_ambiguous_case`. La respuesta no se pierde, pero una
  derivación ahí no se puede marcar atendida. Es un límite conocido, fijado en
  `tests/sql/followup_engine/real_postgres_portable_inbound_adoption.py`.

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
