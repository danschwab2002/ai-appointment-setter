# identity-link-decider-v1: el correlador que decide solo

Contrato de E-536 (2026-11-13). Diseño: «El correlador que decide solo» (E-535, coordinación etapa-1), con las decisiones de Dan del 13/11. Migraciones `20261114000100_identity_link_decider_v1.sql` (bloque A) y `20261114000150_identity_link_question_v1.sql` (bloque B). Bridge: `src/bridge/identity_link.py` y `src/bridge/identity_link_store.py`.

## 1. Qué resuelve

Cuando una persona deja un mail o un teléfono en el formulario y compra con otro, el correlador ([hotmart-purchase-intent-correlation-v1](hotmart-purchase-intent-correlation-v1.md) §4) marca `conflict / email_phone_conflict`, la intención queda en `identity_conflict` con `activation_authorized = false`, y el carrito no le escribe a nadie. Esta pieza resuelve cada conflicto de carrito sola, en minutos, en una de tres ramas:

- **ata**: la base escribe la atadura y vuelve a encolar el carrito, que sigue su flujo de hoy (legado, motor o nativo) con sus toques, su franja, sus topes, su baja y su compra. La pieza no manda nada por su cuenta.
- **pregunta** (bloque B): una plantilla única al teléfono del permiso, con dos respuestas rápidas.
- **nada**: solo se registra, con su motivo.

Solo el carrito (`PURCHASE_OUT_OF_SHOPPING_CART`, D7). El pago rechazado tiene su propia pieza.

## 2. La invariante (D2)

**Solo se escribe al teléfono que dio el permiso.** El bridge decide si es la misma persona; a qué teléfono se escribe lo fija la base al atar (`destination_source`) y lo vuelve a exigir `_portable_consented_intent_reason` antes de cada envío:

- `intent_phone`: el teléfono de la intención con permiso (`purchase_intents.normalized_phone`). Siempre, salvo el caso siguiente.
- `intent_national_digits` (D3, `IDENTITY_LINK_COUNTRY_CODE_FIX_ENABLED`): el formulario guardó `1` + los mismos 10 dígitos que el checkout trae con `52` (el selector de país del formulario) y el mail es exactamente el mismo. Se escribe a `52` + esos 10 dígitos. El `+1` nunca es destino.

El envío sale a `contacts.phone` (`get_followup_execution_context`). Con el evento atado, `bind_identity_link_contact_v1` deja el contacto del evento con el teléfono de la atadura (solo si el contacto es el del checkout, si ningún otro contacto tiene ese teléfono y si `contacts.phone` era el del checkout, vacío o, con D3, el `+1` del formulario); si no, devuelve null y el planificador rechaza como hoy. El teléfono que el contacto tenía antes queda en `identity_link_contact_rebinds` (append-only, el único lugar con un teléfono, que ninguna lectura ni aviso devuelve), y la revocación de la atadura lo restaura si nadie lo cambió después (efecto `contacto_restaurado`; si lo cambiaron, `contacto_sin_restaurar` y no se pisa).

## 3. El orden (D1)

1. **Los frenos de la base** (`get_identity_link_evidence_v1`, sin llamar a nadie), con su `guard_reason`: `fuera_de_alcance` (otro evento, otro motivo o más de una candidata), `intencion_no_viva`, `sin_permiso` (sin envío 1.1.0 con `whatsapp_contact` y `marketing_optin` y la `copy_version` del binding), `baja_previa` (en cualquiera de las formas del teléfono del permiso o del checkout), `ya_compro` (una compra aprobada del producto con el mail o los 10 dígitos de cualquiera de los dos lados, desde 2 h antes del carrito), `carrito_viejo` (más de 24 h, D8) y `contacto_ambiguo` (el mail o un teléfono llevan a más de un contacto).
2. **La regla** (sin modelo; `IDENTITY_LINK_RULES_FIRST=true`, el default): **R1**, el teléfono del checkout es el del permiso en forma canónica; **R2**, el mail es exactamente el mismo y el parecido del nombre es de 90 o más. Con el `+1` sin D3, `destino_sin_permiso`.
3. **El modelo** (prompt `identity-link-v1`, la conexión del pre-resolutor: `CORRELATION_PRERESOLUTION_MODEL_NAME`), solo para lo que queda.

| El modelo dice | Rama |
|---|---|
| `same_person` + `high`, citando un rasgo fuerte que es verdad en la evidencia | ata |
| `same_person` + `medium`, o `unsure` | pregunta (con B); sin B, nada con `habria_preguntado` |
| `different_person` | nada, `modelo_otra_persona` |
| cualquier `low` | nada, `modelo_confianza_baja` |
| otra forma, un token fuera de la lista | nada, `modelo_respuesta_invalida` |
| «ata» sin rasgo fuerte verdadero | nada, `modelo_sin_rasgo_fuerte` |
| el proveedor falla 3 veces | nada, `modelo_no_disponible` |

Los rasgos fuertes: `phone_canonical_equal`, `email_exact_equal`, `phone_national_equal_country_differs` (este, solo con D3). **Nunca se falla hacia «ata»**, y la base lo re-verifica: `complete_identity_link_decision_v1` rechaza (22023) una R1 sin el teléfono igual, una R2 sin el mail igual o con el nombre por debajo de 90, un «ata» del modelo sin un rasgo fuerte verdadero y cualquier destino fuera de §2; y vuelve a mirar los frenos en vivo (un freno nuevo convierte la decisión en «nada» con su motivo).

## 4. Qué ve el modelo

Solo rasgos (la lectura de evidencia compara los datos personales adentro de la base): `coincide_por` (`telefono`/`mail`), `telefono_canonico_igual`, `telefono_10_igual`, `telefono_7_igual`, `pais_formulario`, `pais_checkout` (`MX`, `NANP`, `AR`, `otro`: solo el prefijo), `mail_igual`, `dominio_igual`, `usuario_igual`, `parecido_usuario`, `parecido_dominio`, `parecido_nombre` (0-100, pares de letras), `primer_nombre_igual`, `minutos_formulario_a_carrito`, `misma_oferta`, `otras_intenciones_7d`, `conversacion_previa_con_el_telefono_del_permiso` y `selector_pais_sospechoso`. La intención va como `Persona 1`. El texto del modelo no se guarda: solo `decision`, `confidence` y `reason_codes` de una lista cerrada. El bridge rechaza una evidencia con otra clave o con otro tipo de valor.

**El nombre (D4)** entra como un número de parecido, nunca como texto, y nunca alcanza solo (R2 exige además el mail igual; el validador exige un rasgo fuerte).

## 5. El registro (sin datos personales)

Todo en la base de la aliada, con RLS y sin DML directo para los roles de la API. Las tablas append-only rechazan `UPDATE` y `DELETE` (55000 `identity_link_append_only`).

- `identity_link_controls`: la segunda llave, por tenant y funnel, con `enabled_at` (el corte: los conflictos anteriores no entran, D8). Sin renglón prendido, el claim no devuelve nada. La prende quien opera con `enable_identity_link_v1(tenant, funnel, actor, motivo)` y la apaga con `disable_identity_link_v1`.
- `identity_link_cases`: la cola (mutable: el lease), con la evidencia congelada en el primer intento y su sha256.
- `identity_link_decisions` (append-only): una por evento y modo; la de la persona (bloque B) aparte. `decided_by` es `freno`, `regla:R1`, `regla:R2`, `modelo:<nombre>` o `persona`; `destination_sha256` es el hash del teléfono, para cruzar sin verlo.
- `identity_link_effects` (append-only): `requeued`, `ya_estaba_en_cola`, `pregunta_planificada`, `ninguno`, `sombra`, y al revocar `contacto_restaurado` o `contacto_sin_restaurar`.
- `identity_link_revocations` (append-only): `revoke_identity_link_v1(decision_id, actor, motivo)`. Desde ese momento la atadura deja de contar y el próximo toque del carrito corta con el motivo del permiso. No borra nada ni toca la intención; el contacto vuelve a su teléfono anterior (§2).
- `identity_link_contact_rebinds` (append-only): el teléfono anterior del contacto que la atadura cambió, por decisión. Ninguna lectura ni aviso lo devuelve.
- `identity_link_notices` (append-only): los avisos que el conector de Slack ya tomó.
- Bloque B: `identity_link_questions` (la cola de la pregunta, mutable) e `identity_link_answers` (append-only: `si`, `baja`, `sin_respuesta`).

## 6. Las cuatro puertas del carrito

`_cart_event_intent_v1(webhook_event_id)` es la intención del evento: la de la correlación resuelta o la de una atadura vigente (`vivo`, `ata`, sin revocar y sin una compra posterior). Reemplaza la lectura de «la correlación resuelta» en `plan_lancemos_pilot_cart_recovery` (bloque `pilot_audience`), `_portable_cart_audience_checks` (motor v1) y `_flow_engine_native_cart_audience` (nativo). En `_portable_consented_intent_reason`, `identity_conflict` y `activation_authorized = false` dejan de cortar **solo** con una atadura vigente de esa intención; el resto del chequeo (teléfono de destino, `contact_points`, `contacts.phone`, baja, envío 1.1.0 con la `copy_version`) queda igual y sigue cortando. Sin atadura, las cuatro se comportan exactamente como antes. `reserve_recovery_checkout_issuance_v1` no cambia: con R1 y R2 su búsqueda por teléfono encuentra la misma intención; con D3 fabrica una nueva para el teléfono del checkout (pierde el `sck` del formulario en el link; el permiso no se toca).

Al atar en `vivo`, si el evento terminó `failed` con `pilot_scope_rejected:pilot_audience_intent_unresolved`, `pilot_scope_rejected:no_entra_permiso` o `flow_engine_no_start:no_entra:permiso`, la base lo vuelve a `received` con `processing_error = 'identity_link_requeued'`. Para la carrera (el evento falla justo después de atar), el worker corre `requeue_identity_linked_events_v1` en cada vuelta: idempotente, una sola vez por decisión, nunca en bucle.

## 7. La pregunta (bloque B, D5)

- Plantilla por instancia en `[plantillas].identidad` del manifiesto (nombre y variables configurables; por defecto `nombre`, `producto`, `marca`, donde `marca` es `instancia.marca`, el nombre con que se presenta la aliada), aprobada en Meta, casi seguro como MARKETING (`IDENTITY_LINK_ASK_TEMPLATE_CATEGORY`). Texto definitivo de Dan del 13/11 (es_MX): «Hola {{1}}, te escribe el equipo de {{3}}. Vimos que empezaste tu compra de {{2}}. Si fuiste tú y quieres ayuda para terminarla, aquí estamos.» Dos respuestas rápidas: **«Sí, fui yo»** y **«No más mensajes»**. No lleva ni mail ni teléfono.
- Un solo mensaje, sin seguimientos, al teléfono de la decisión (§2), dentro de la franja `IDENTITY_LINK_ASK_WINDOW` (09:00-21:00 en la zona de la instancia) y hasta 24 h del carrito. La puerta antes de enviar es la base (`start_identity_link_question_v1`: los frenos en vivo); pasada la puerta la pregunta queda `request_started` y nunca se reintenta (un resultado incierto queda `unknown`). Lo manda el worker del correlador con el sender de Chatwoot de siempre (no pasa por el despachador de seguimientos).
- **«Sí, fui yo»**: la base ata con `decided_by = 'persona'` (si no hay un freno vivo) y la conversación sigue con el agente, que tiene la ventana de 24 h abierta. El carrito **no** se vuelve a encolar (su primer toque saldría encima de la conversación): decisión con default de E-536.
- **«No más mensajes»** es el botón de baja del producto (`template_request.OPT_OUT_BUTTON`): la entrada de Chatwoot lo registra como cualquier otra baja (`opt_out.detect_explicit_opt_out`); acá solo se anota `baja`.
- Sin respuesta a las 72 h: `sin_respuesta`.

## 8. Los avisos al canal de la aliada

Decisión con default de E-536 (pedido de Dan del 13/11): un mensaje por cada decisión en `vivo` que ata o pregunta (`IDL-001`) y uno por cada envío de un carrito atado o de la pregunta que sale o falla (`IDL-002`), al canal de alertas de la aliada con el conector de Slack de la instancia (el mismo productor que MOT-001 y MOT-002). Sin teléfonos ni mails: la rama y la regla en el motivo (`ata:regla:r1`), el número de conversación de Chatwoot en el componente (`chatwoot.conversation.<n>`, o `identity_link.sin_conversacion`). Sin conector, no avisa y `/ready` dice `identity_link_alerts: sin_slack`.

## 9. Los flags

| Variable | Default | Qué hace |
|---|---|---|
| `IDENTITY_LINK_MODE` | `off` | `off`: no lee ni decide. `sombra`: decide y registra (`mode = 'sombra'`), no ata, no encola, no pregunta (queda para pruebas; D6: la activación va directo a `vivo`). `vivo`: todo. Prendido exige la conexión del modelo del pre-resolutor (`HERMES_API_BASE_URL`, su clave y `CORRELATION_PRERESOLUTION_MODEL_NAME`), un runtime portable y Supabase; si falta algo, el bridge no arranca. `/ready` dice el modo. |
| `IDENTITY_LINK_RULES_FIRST` | `true` | D1. Con `false`, R1 y R2 también pasan por el modelo. |
| `IDENTITY_LINK_COUNTRY_CODE_FIX_ENABLED` | `false` | D3 (Dan: sí). |
| `IDENTITY_LINK_ASK_ENABLED` | `false` | Bloque B. Exige `vivo`, el despachador directo, el control de Chatwoot y `[plantillas].identidad`. |
| `IDENTITY_LINK_ASK_WINDOW` | `09:00-21:00` | La franja de la pregunta, en la zona de la instancia. |
| `IDENTITY_LINK_ASK_TEMPLATE_CATEGORY` | `MARKETING` | La categoría con que Meta aprobó la plantilla. |
| `IDENTITY_LINK_POLL_INTERVAL`, `IDENTITY_LINK_WORKER_ID` | 15 s, el host | Como el pre-resolutor. |

Además, la segunda llave de la base (`identity_link_controls`, §5).

## 10. Errores

22023 entrada inválida o una decisión que la base no acepta (`identity_link_rule_not_met`, `identity_link_model_without_strong_trait`, `identity_link_destination_without_permission`); P0002 no existe; 55000 el lease se perdió, ya aplicada, o una escritura sobre una tabla append-only.
