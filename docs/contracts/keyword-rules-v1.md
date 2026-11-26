# Las reglas de palabra clave: el texto de un link que pone una etiqueta

- **Estado:** escrito al construir Z1 3.9, el 2026-11-26, antes de cualquier merge o despliegue. Describe la interfaz; no dice que nada esté desplegado ni activado. Todo va detrás de `KEYWORD_RULES_ENABLED`, apagado por defecto.
- **Contrato:** `keyword-rules-v1`, versión `1.0.0`.
- **Diseño:** mapa de la serie Z1 (`documentacion/2026-11-26-mapa-funciones-faltantes-z1.md`, fuera del repo), función 3.9 y las decisiones D4, D5 y D13; la lista definitiva del 2026-11-20 («links `wa.me` con texto por segmento: soporte, check-in, asistencia, alumnos; cada uno lleva a su flujo» y «la palabra clave no se deriva»).
- **Reusa:** `campaign-inbound-entry-v1` (el mismo «primer mensaje», la misma normalización y el mismo matching), `segment-labels-v1` (el macro que solo suma la etiqueta, `ProductLabeler`) y la página de aprobación de `flow-change-cycle-v1` (la misma sesión de «Entrar con Slack» y la misma lista de aprobadores).
- **Migración:** `20261126400000_keyword_rules_v1.sql`.
- **Código:** `src/bridge/keyword_rules.py` (la forma, los choques, lo que se sirve y el turno), `keyword_rule_api.py` (las rutas del AE), `keyword_rule_approval.py` (la página). Las herramientas del agente: plugin `automation-expert-change` 0.10.0 y la guía `link-con-palabra-clave`.

## Para qué

Un link de WhatsApp por segmento lleva un texto prellenado («Hola, necesito soporte», «check-in», «soy alumna»). Una **regla** dice: si el primer mensaje de una conversación trae ese texto, la conversación lleva esta etiqueta. La etiqueta deja ver de dónde vino la persona (en Chatwoot, en los reportes) y es la entrada que más adelante puede abrir un flujo. Además, el agente no tiene que tomar ese texto como un pedido de hablar con una persona.

## 1. La regla

```json
{"clave": "soporte", "nombre": "Link de soporte", "palabra_clave": "Hola, necesito soporte",
 "etiqueta": "soporte", "macro_etiqueta": 77}
```

- `clave`: minúsculas, dígitos y guiones, hasta 64. Una clave nombra una sola regla abierta o viva.
- `nombre`: hasta 80 caracteres, para la página.
- `palabra_clave`: el texto del link, tal cual, hasta 200. El bridge guarda además `palabra_normalizada` (sin acentos ni mayúsculas, la puntuación y los espacios de más como un espacio: `normalize_entry_text`), de 3 a 120 caracteres y no solo dígitos.
- `etiqueta` y `macro_etiqueta`: la etiqueta de Chatwoot y un macro que **solo** la suma.
- **No hay `flujo`** (D-39.1): una regla con esa clave se rechaza (`keyword_rule_flow_not_supported`). Ver «El flujo».

## 2. Lo que frena (al guardar, al publicar y al reanudar)

| Código | Qué pasa |
|---|---|
| `keyword_rule_clashes_with_campaign` | la palabra choca con la de una campaña cargada (iguales, o una contiene a la otra como palabras enteras) |
| `keyword_rule_clashes_with_rule` | choca con la de otra regla servida |
| `keyword_rule_label_reserved` | la etiqueta es `cliente`, empieza con `automation_`, o es la de un producto o de un segmento |
| `keyword_rule_macro_reserved` | el macro es el de la pausa, la reanudación, la baja, `cliente` o el de un producto |
| `keyword_rule_macro_does_more_than_add_the_label` | el macro hace algo más que sumar la etiqueta (se lee de Chatwoot) |
| `keyword_rule_macro_unreadable` | no se pudo leer el macro |

«Chocan» (D-39.2) cubre la contención porque el matching de las campañas es «contenida en cualquier parte»: un mensaje con «black pack forever young» trae también «forever young», y las dos se aplicarían al mismo mensaje. La base, además, no deja la misma palabra normalizada en dos reglas publicadas o pausadas (índice único) ni más de 20 publicadas.

## 3. El recorrido

```
borrador ─publicar (el sí de un aprobador)─▶ publicada ◀─pausar/reanudar─▶ pausada ─descartar─▶ descartada
   └─descartar─▶ descartada
```

1. El AE mira primero `list_keyword_rules` (con `keyword_rules_not_enabled`, frena sin crear nada). Etiqueta y macro con `create_chatwoot_label` y `create_chatwoot_label_macro` (prendido también con este flag).
2. `preview_keyword_rule` valida sin guardar y devuelve el link `wa.me` (el número de la bandeja con el texto). `propose_keyword_rule` guarda el borrador (con bloqueos, 422 `keyword_rule_draft_blocked`), la base le calcula la huella y la ruta devuelve `approval_url` (`/flow-approval/w/<id>`). Hasta 10 borradores por instancia. Rehacer sube la revisión y cambia la huella.
3. **El sí**: «Aprobar y publicar» en la página. Antes de llamar a la base el bridge vuelve a medir los choques y el macro; la base exige la huella de la fila. El agente no tiene ninguna herramienta para publicar, pausar ni descartar.
4. Publicada, el bridge la lee con caché corta (30 s, o ya mismo después de tocar un botón). Una viva no se descarta sin pausarla antes.

Quien perdió la sesión entra con Slack y vuelve al inicio de la página, no a la regla (D-39.9): el link de la regla se abre de nuevo.

## 4. En el turno

Con la historia canónica ya leída, **después** de la entrada a la campaña y antes de armar el pedido:

1. Si la historia está completa, el primer mensaje de la persona (desde el último `/nuevo`) es parte de este turno (`first_message_of_turn`, el mismo de `campaign-inbound-entry-v1`) y la conversación **la abrió la persona** (`opened_by_the_person`: una respuesta a una plantilla nuestra no es el link, D-39.5), se busca la regla servida cuya palabra aparece en el mensaje. Si hay más de una, gana la que aparece primero; a igual lugar, la más larga (D-39.4).
2. Si la palabra choca con una campaña cargada en ese momento, la regla no se aplica (`keyword_rule_skipped … reason=choca_con_una_campana`): **la campaña gana** (D-39.3).
3. Si no, la etiqueta se pone por el macro de la regla (`ProductLabeler.ensure`: verifica el macro, es idempotente, nunca lanza) y el turno suma, último, un `system` fijo (D13):

```
# El primer mensaje es el texto de un link
El primer mensaje de esta persona es el texto que trae escrito un link de WhatsApp (el botón de una página o de un mensaje nuestro): lo mandó tocando el link. No es un pedido de hablar con una persona del equipo ni una queja. No derives la conversación por ese mensaje: contesta como a cualquier primer mensaje y pregunta en qué puedes ayudar.
```

En la procedencia del turno va con `kind: "nota_palabra_clave"`, clave `palabra-clave`, versión 1. No se toca el SOUL común: no hay que reinstalar nada en ninguna instancia.

4. A una persona dada de baja (`prior_opt_out`) no se le pone la etiqueta ni la nota (D-39.6).

Logs: `keyword_rule_label conversation rule label outcome`, `keyword_rule_skipped`, `keyword_rule_served`, `keyword_rule_not_served rule_id reason`, `keyword_rules_refresh_failed`. Ninguno lleva el texto de la persona.

## 5. El flujo (pendiente)

La lista pide que cada link «lleve a su flujo». **Esta versión no lo hace** (D-39.1): el motor de flujos admite hoy disparadores por evento de Hotmart o del formulario (`carrito`, `pago_fallido`, `ficha_*`, `compra`, `intencion`), por silencio y por audiencia, y la admisión es SQL (`admit_flow_engine_event` y las `_flow_engine_admit_*`) atada a un producto y a un contacto del recuperador. Un disparador `palabra_clave` necesita: el disparador en `flow-definition-v1` y su validador, la capacidad en el motor nativo, una admisión nueva en la base que resuelva el contacto desde la conversación de Chatwoot y el producto de la regla, y que el despacho autorice al destinatario sin oferta (el mismo hueco que E-661 resolvió para la audiencia). Cuando exista, pasa por los frenos de siempre (baja, salud de Meta, candado del portfolio, tope por persona) y necesita además `FLOW_ENGINE_ENABLED` (D5); en Maribel, con el motor frenado, no saldría nada. La regla v1 deja la etiqueta, que es la entrada natural de ese disparador.

## 6. La base (`20261126400000_keyword_rules_v1.sql`)

| Rol | Funciones |
|---|---|
| `automation_expert_change` | `save_keyword_rule_draft_v1`, `get_keyword_rule_v1`, `list_keyword_rules_v1` |
| `flow_change_approver` (con la sesión viva) | `approver_get_keyword_rule_v1`, `approver_publish_keyword_rule_v1`, `approver_pause_keyword_rule_v1`, `approver_resume_keyword_rule_v1`, `approver_discard_keyword_rule_v1` |
| `service_role` | `get_live_keyword_rules_v1` |

Las tablas (`keyword_rules`, `keyword_rule_events`) tienen RLS y no tienen permisos para la API; las filas no se borran y los eventos solo se agregan. El agente nunca ve un ID de Slack; la página ve quién aprobó y quién pausó. Correrla dos veces aborta con 55000.

## 7. Las rutas del AE

Bajo `/internal/automation-expert/v1`, con `AUTOMATION_EXPERT_CHANGE_TOKEN`: `POST /keyword-rules/preview`, `POST /keyword-rules`, `GET /keyword-rules?estado=abiertas|todas&limit=`, `GET /keyword-rules/{id}`. Cada lectura dice `serving` (`served` o el motivo).

## 8. Sin el flag

No existen las rutas del AE ni la página de una regla, no se lee la base, `/ready` no suma la clave `keyword_rules` y el pedido a Hermes es **byte a byte el de hoy** (test `test_the_request_is_byte_for_byte_today_without_the_flag_or_a_match`). Con el flag y sin una regla que calce, también.

## 9. El arranque (falla cerrado)

`KEYWORD_RULES_ENABLED` pide el manifiesto, el cliente de control de Chatwoot, `CHATWOOT_ACCOUNT_ID` y `CHATWOOT_INBOX_ID`, el cliente de Supabase, `AUTOMATION_EXPERT_CHANGE_ENABLED` y `FLOW_APPROVAL_ENABLED`. No pide `CAMPAIGN_INBOUND_ENTRY_ENABLED`: sin la entrada a la campaña, las palabras de los segmentos del manifiesto cuentan igual para los choques. `/ready` suma `keyword_rules: served=N,not_served=N,refresh=ok|failing:N`. Es una capacidad portable: por instancia, solo prenderlo.

## 10. Decisiones (con su default)

- **D-39.1.** La regla v1 solo pone la etiqueta y la nota; no dispara flujos (ver §5). Una regla con `flujo` se rechaza para que nadie crea que arranca algo.
- **D-39.2.** Dos palabras chocan si son iguales o una contiene a la otra como palabras enteras (el matching de las campañas).
- **D-39.3.** La campaña gana: una regla que choca con una campaña no se guarda, no se publica, y si la campaña se publicó después, no se aplica y la página lo dice.
- **D-39.4.** Una sola regla por mensaje: la de la palabra que aparece primero; a igual lugar, la más larga. Entre dos reglas publicadas que chocan, se sirve la aprobada antes.
- **D-39.5.** Solo cuenta el primer mensaje (D14 de `campaign-inbound-entry-v1`) de una conversación que abrió la persona.
- **D-39.6.** A quien se dio de baja no se le pone la etiqueta ni la nota.
- **D-39.7.** Una palabra por regla (un link lleva un texto). Hasta 10 borradores y 20 publicadas por instancia.
- **D-39.8.** La nota es fija, sin el texto de la regla: no depende de lo que escribió el AE y su sha es el mismo en todos los turnos.
- **D-39.9.** La página de una regla no tiene vuelta directa después de entrar con Slack (vuelve al inicio): no se tocó el ingreso común de la página.
- **D-39.11.** La página va en `/flow-approval/w/<id>` (w de wa.me): `/p/` es de las propuestas de flujo, `/t/` de las plantillas, `/c/` de las campañas y `/k/` del conocimiento (5.5).
- **D-39.10.** Sin juez: la regla no cambia lo que sabe el agente (solo la nota fija), así que no pasa por el control de calidad de las campañas.

## 11. Las pruebas

- `tests/test_keyword_rules.py`: la forma, los choques, lo que se sirve, y el turno sobre el primer mensaje real de la conversación 455 de Maribel (`chatwoot_conversation_messages_inbox_13_conv_455_keyword_20261126.json`, «forever young», dos entrantes antes de la primera respuesta), la campaña que gana, la plantilla nuestra, la historia incompleta, el arranque.
- `tests/test_keyword_rules_bridge.py`: el turno por el webhook (la etiqueta antes del agente y la nota última, la respuesta a una plantilla, la baja, sin el flag) y el pedido a Hermes byte a byte.
- `tests/test_keyword_rule_routes.py`: las rutas del AE, la página (sin sesión, fuera de la lista, publicar, el choque al publicar, pausar, reanudar, descartar) y `/ready`.
- `tests/sql/followup_engine/validate_keyword_rules.mjs` (PGlite, la cadena entera).
- **Deuda:** la nota no se probó contra el modelo real (que el agente efectivamente no derive con el texto del link): va al E2E.
