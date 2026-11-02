# identity-recycle-v1: reciclar un teléfono de prueba

**Versión:** 1.0.0 (F-147, PR 1: la base).
**Migración:** `20261102000400_identity_recycle_v1.sql`.
**Pruebas:** `tests/sql/followup_engine/validate_identity_recycle.mjs` (PGlite, cadena entera).
**Lo que no trae:** la página (`GET /flow-approval/reciclar`, la vista previa, el segundo paso con `confirm_token`, el aviso OPS), Chatwoot (sacar etiquetas y resolver) y el tick de la purga son el PR 2. Sin el PR 2 nadie llama a estas funciones.

Un número de prueba deja rastro (compras, carritos, formularios, una baja, el «una vez por persona») y ese rastro activa los mismos frenos que protegen a un cliente real. Ningún freno lee «la persona»: todos cruzan **valores** de teléfono o de mail contra los puntos de contacto vivos. Por eso el reciclado (modelo A, D1) reescribe la identidad **en su origen**, a un marcador que no cruza con nadie, por una sola puerta auditada. La prueba siguiente crea un contacto nuevo y todos los lectores, los de hoy y los de mañana, ven lo mismo sin cambiar.

## 1. Qué se reescribe

| Celda | Marcador |
|---|---|
| `contact_points` (teléfono) `normalized_value` y `raw_value`, `contacts.phone`, `channel_identities.external_user_id` | `reciclado:<reciclado8>:<fila8>` |
| `purchase_intents.normalized_phone`, `hotmart_purchase_intent_event_identities.normalized_phone`, `webhook_events.payload` `{data,buyer,phone}` y `{data,buyer,checkout_phone}`, `contact_opt_out_events.external_user_id` (D4) | `999` + el número del reciclado con 9 cifras |
| Con `p_recycle_emails` (D3): los mails que son **solo** de los contactos del teléfono, en `contact_points` (mail), `contacts.email`, `purchase_intents.normalized_email`, `hotmart_purchase_intent_event_identities.normalized_email` y `webhook_events.payload` `{data,buyer,email}` | `reciclado-<número>-<n>@reciclado.invalid` |

Las formas del teléfono salen de `_whatsapp_phone_variants` (52/521, 54/549). Las cuentas de Chatwoot, de los bindings activos de la instancia. **No se toca:** `precheckout_submissions`, `inbound_commercial_case_admissions`, los casos, recorridos, cohortes y correlaciones (cuelgan del contacto viejo por id), y el `do_not_contact` del contacto viejo.

## 2. La excepción con nombre

`protect_hotmart_purchase_intent_correlation` (solo para `hotmart_purchase_intent_event_identities`) y `protect_hotmart_cart_source_event` llaman a `_identity_recycle_allows(tabla, old, new)` antes de frenar. Pasa un UPDATE solo si:

1. hay un reciclado `aplicando` o `deshaciendo` con `txid = txid_current()` (creado en **esta** transacción);
2. cada celda que cambia tiene su fila en `identity_recycle_rows` con el valor de partida (el viejo al aplicar, el marcador al deshacer);
3. la fila nueva es la vieja con esas celdas puestas en el valor de llegada, y nada más.

Un DELETE nunca pasa. **Sin `session_replication_role`.** Las tablas del reciclado no tienen permisos para ningún rol de la API (RLS, `revoke all`), así que solo las escriben estas funciones `security definer`. Un disparador más las cuida: un reciclado nace `aplicando` en la transacción que lo escribe, el diario y la actividad solo se agregan mientras tanto, y nada se borra.

## 3. Las funciones

**De la página, solo `flow_change_approver`** (la sesión viva, `_flow_approval_session_actor`: el actor sale de la sesión, nunca de un parámetro; sin sesión, 28000):

| Función | Qué hace |
|---|---|
| `approver_identity_recycle_preview_v1(p_instance, p_session_hash, p_phone, p_new_email, p_recycle_emails)` | La vista previa, sin identidad: `last2`, `can_recycle`, `blocked_by`, `guards`, personas (contactos, dados de baja), cohortes, historia (compras, carritos, otros eventos, formularios, casos, recorridos), las bajas con su fecha, filas y celdas por tabla, los mails enmascarados (`d···@g···.com`: reciclados, compartidos, no reciclados) y la `fingerprint` (sha256 del inventario). |
| `approver_identity_recycle_apply_v1(…, p_fingerprint)` | Aplica lo que vio el aprobador. Devuelve `{ok, recycle_id, recycle_number, last2, counts}` o `{ok: false, reason_code}`. |
| `approver_get_identity_recycle_v1(p_instance, p_session_hash, p_recycle_id)` | Relee un reciclado: estado, conteos, `can_undo`, `undo_blocked_by`, `undo_until`. |
| `approver_identity_recycle_undo_v1(p_instance, p_session_hash, p_recycle_id)` | Deshace: restaura cada celda desde el diario por la misma excepción. |

**De `service_role`:** `purge_identity_recycles_v1()`, para el tick del bridge. **De nadie de la API:** `test_activity_ids_v1()` (la leen los SQL del OS como dueño). **Nunca** el rol del Automation Expert, `anon` ni `authenticated`: un lead podría pedirle al agente «borrá mi número» para volver a recibir un cupón de una vez por persona.

`p_phone` son los dígitos internacionales (10 a 15). Entra y no vuelve: ninguna respuesta lo trae, ni los mails en claro.

## 4. Guardas y verificaciones

Abortan sin cambiar nada y vuelven como `{ok: false, reason_code}`, en este orden: `nada_que_reciclar`, `sin_huella_de_prueba` (D2: ningún contacto del teléfono está o estuvo en `flow_cohort_members` ni en `pilot_cohort_memberships`), `fila_de_otra_persona` (el teléfono con un mail ajeno, o un mail reciclado con otro teléfono, o una identidad de otra cuenta), `otro_formato_o_ruta` (los últimos 10 dígitos o un mail reciclado en una fila o una ruta que no se reescribe, como `payload.id`), `casos_abiertos`, `envios_pendientes`, `intenciones_vivas`, `mail_nuevo_ya_conocido` y, al aplicar, `vista_vencida` (la huella no es la de la vista).

Después de reescribir, antes del COMMIT, 17 verificaciones por presencia: las filas de las siete tablas en cero, el número en ningún formato, y los nueve frenos de E-253 para una persona nueva con el teléfono, con el mail nuevo y con los mails reciclados (dueños del formulario, contacto por teléfono, identidad de WhatsApp, compra admitida por identidad, intención comprada, intención viva, `_portable_checkout_link_purchase_exists` y `get_portable_person_activity_v1` llamados de verdad, baja del móvil). Si una falla: 55000 `identity_recycle_verification_failed` y no cambia nada.

## 5. Deshacer y purgar (D5)

Deshacer se puede durante 72 h, o hasta que el teléfono (o un mail reciclado) vuelve a entrar en alguna de las siete tablas, lo que pase primero (`identity_recycle_undo_window_closed`, `identity_recycle_phone_returned`). También frena si una celda ya no tiene su marcador (`identity_recycle_changed_since`). Al deshacer, el diario queda sin los valores viejos (ya volvieron a su tabla) y el reciclado deja de contar como prueba.

`purge_identity_recycles_v1()` pone en nulo los valores viejos de los reciclados `aplicado` de más de 72 h o cuyo teléfono volvió a entrar, y los pasa a `purgado`. Quedan el marcador, los ids y el actor. Después, deshacer da `identity_recycle_purged`.

## 6. Fuera de los números (D7)

Todo reciclado registra como prueba los ids que reescribió y los que cuelgan del contacto viejo: contactos, casos comerciales y de recuperación, intenciones, eventos de Hotmart (los reescritos y los correlacionados con las intenciones) y emisiones de link. `test_activity_ids_v1()` los devuelve como `(kind, id)`, de los reciclados `aplicado` o `purgado`. Los SQL de ventas del OS excluyen `where not exists (… test_activity_ids_v1() …)`; las listas viejas (`PRUEBAS_ATT1`, `PRUEBAS_EVENTOS_ATT1`, `PRUEBAS_MARIBEL`) se congelan.

## 7. Errores

22023 entrada inválida (`identity_recycle_phone_invalid`, `identity_recycle_email_invalid`, `identity_recycle_fingerprint_invalid`); 23514 una escritura que la excepción no deja pasar; 28000 sesión inválida; P0002 `identity_recycle_not_found`; 55000 un estado que no lo permite (incluida la segunda corrida de la migración, `identity_recycle_already_applied`).
