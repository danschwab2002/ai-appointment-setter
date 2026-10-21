# La paridad del carrito de tres toques contra ATT1: medición del 2026-10-21

**Qué se midió.** Si la puerta del motor de flujos explica lo que hizo el legado del carrito de ATT1 desde que corre con tres toques: A1, A2 a las 24 h y Z a las 72 h de la primera aceptación. Es la T2.2 del motor (especificación «T2.2: la captura v3 y la paridad del carrito de tres toques», del 2026-10-21, en el vault). No ejecuta nada ni cambia nada de lo que corre. Se miden cuatro cosas:
- **P3, por paso:** ¿cada A1, A2 y Z salió cuando la puerta lo permitía, nunca antes y a lo sumo 5 minutos después, salvo un hecho observado?
- **Los finales:** ¿cada paso que no salió tiene su motivo, y la cuenta cierra contra el censo?
- **P6:** ¿cada primer contacto que cedió tiene quién le ganó?
- **El denominador:** ¿qué pasó con los disparos del carrito de la ventana?

**El resultado.** Cero sin explicar y ninguna diferencia con nombre. Ningún envío salió antes de E. Dos cesiones del primer contacto se explican por una ruta de P6 que es una cota (`intencion_clasificada`), y dos casos de la ficha quedan aparte como «no comparables».

Este reporte es el de después de la revisión adversarial de la T2.2. Ver «Lo que cambió con la revisión», al final.

## El dato

La captura `captura-paridad-recuperacion-v3` de `att1-db`. Se leyó el 2026-10-21 a las 17:16:07Z, con `default_transaction_read_only` y la sesión en UTC.
- **La consulta:** [`tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql`](../../tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql), con sha256 `4e018e57…2655`. Se probó antes en PGlite sobre la cadena de migraciones (`tests/sql/followup_engine/validate_flow_engine_capture_v3.mjs`).
- **Lo que suma a la v2:** las secuencias, los cupones (sin el código), el link de cada intento (sin URL ni sck), las renovaciones y la primera respuesta de cada envío, solo como hora. También los pasos de la política con `delay`, `approved_template` y `coupon_ordinal`. Las reglas de privacidad son las mismas de la v2.
- **Solo el carrito.** Los disparos son los carritos. El pago rechazado y la ficha entran como hechos de la persona: sus casos sí, sus acciones no.
- **La captura completa:** 1.959.974 bytes, sha256 `4a730355…e355`. Queda fuera del repo, en el vault de operación (`productos/soporte-infoproductores/documentacion/2026-10-21-motor-anexos/att1-captura-motor-v3-20261021.json`). La forma cerrada dio verde (`--check-privacy`).
- **El fixture:** [`att1_recovery_capture_v3_20261021.json`](../../tests/fixtures/flow_engine/att1_recovery_capture_v3_20261021.json), de 897.748 bytes. Ver «El recorte» más abajo.

**La ventana.** Arranca con la activación de la v7 de `att1-recuperacion`, el 2026-10-19 a las 18:13:51Z, y lleva 7 días de siembra.

**Los conteos de la captura.** 364 eventos, 72 disparos del carrito desde «desde», 252 casos (249 del carrito), 249 secuencias, 330 acciones, 1344 intentos en 318 tramos, 267 envíos, 2740 reevaluaciones en 417 tramos, 63 cupones, 84 links, 3 renovaciones, 435 autorizaciones, 354 planes del primer contacto, 332 intenciones y 447 contactos.

## La medición

```sh
uv run python scripts/flow_engine_parity.py --forma v3 \
  --capture tests/fixtures/flow_engine/att1_recovery_capture_v3_20261021.json \
  --named tests/fixtures/flow_engine/att1_parity_v3_named_differences.json
```

Sale con 0. Sobre la captura completa da exactamente lo mismo: el `--json` es idéntico byte a byte, y en el texto cambia solo cómo se escribe la hora de la cabecera. La salida, sin la lista de los 46 explicados:

```text
Disparos del carrito desde «desde»: 72
  carrito/create_recovery_case_failed: 1
  carrito/pilot_case_binding_conflict: 1
  carrito/pilot_scope_rejected:pilot_audience_consented_intent_not_authorized: 5
  carrito/pilot_scope_rejected:pilot_audience_intent_unresolved: 7
  carrito/processed: 58
Llegaron al flujo: 71
Carritos de la ventana: 58
Envios por paso: cart_last_touch 3, cart_second_touch 22, first_contact 58
  R - E de cart_last_touch: minimo 485.91 s, mediana 491.68 s, p95 496.66 s, maximo 496.66 s
  R - E de cart_second_touch: minimo 4.28 s, mediana 41.99 s, p95 48.07 s, maximo 48.68 s
  R - E de first_contact: minimo 8.50 s, mediana 615.31 s, p95 626.65 s, maximo 43536.57 s
Nunca antes (R >= E - 1 s): 0 violaciones
Fuera de la tolerancia (300 s): 43
Explicadas por una excepcion: 46
  a1_espera_al_cupon: 40
  renovacion_adelanta_el_ultimo_toque: 3
  toque_con_cupon_cierra_sin_cupon: 3
Finales por paso:
  cart_last_touch: baja 1, compro 1, contesto 1, enviado 3, identidad:conversacion 16, pendiente 36
  cart_second_touch: baja 1, compro 1, contesto 1, enviado 22, identidad:conversacion 16, pendiente 17
  first_contact: enviado 58
Censo: 58 carritos, 174 de 174 pasos clasificados, 83 enviados (83 en P3)
P6: 62 primeros contactos cedieron; sin ganador: 0; ganadores sin cancelacion: 0
  por la primera ruta que encuentra un ganador: caso_en_curso 1, intencion 18, intencion_clasificada 2, persona 41
Personas no comparables (su disparo no se captura: la ficha): 2
Diferencias con nombre: 0
Sin explicar: 0
Resultado: verde
```

## Las clases

| Qué se ve | Cuántos | Clase | Por qué y evidencia |
|---|---|---|---|
| A1 más de 5 min tarde, esperando el cupón 1 | 40 | E `a1_espera_al_cupon` | Intentos `failed_before_request` con `recovery_coupon_not_ready`, con reintento a los 5 min (`src/bridge/cart_first_touch.py:130-139`). Hecho observado. |
| De esos 40, A1 que pasó a la mañana | 2 | E `a1_espera_al_cupon` y la franja | Ver «Los dos A1 de 12 horas». |
| Z adelantado por «Quiero mi 10 %» | 3 | E `renovacion_adelanta_el_ultimo_toque` | E se cuenta desde el pedido (`recovery_renewal_requests`, efecto `adelanto`; `20261019000600:321-336`). |
| Z esperando el cupón 2 tras la renovación | 3 | E `toque_con_cupon_cierra_sin_cupon` | Un intento `recovery_coupon_not_ready` con reintento a los 8 min (`src/bridge/cart_followups.py:215-230`). R − E entre 486 y 497 s. |
| A2 escalado sin poder verificar la conversación, y su Z que no nació | 16 + 16 | motivo `identidad:conversacion` | Ver «Los 16 A2 del bug de la v1.15.5». |
| A2 y Z que esperan a la hora de la captura | 17 + 36 | `pendiente` | El paso existe y su E todavía no llegó; o no se creó porque el anterior tampoco salió; o el anterior salió y el E del paso (la primera aceptación más su `delay`, con la puerta) más 5 min todavía no pasó. Un paso no creado cuyo E ya pasó sería `paso_no_creado`, una diferencia: no hay ninguno. |
| Contestó, compró o se dio de baja | 1 + 1 + 1 en cada paso | motivos `contesto`, `compro` y `baja` | Por `flow_engine.LEGACY_REASON_TO_MOTIVO`. Z hereda el final de A2. |
| Primer contacto que cedió ante un caso de la ficha | 1 | D (§1.7, fila 13), ruta `caso_en_curso` | Ver «P6». |
| Dos reenvíos del formulario sobre una intención clasificada antes de la siembra | 2 | D (§1.7, fila 13), ruta `intencion_clasificada`, **una cota** | Ver «P6». |
| Casos de la ficha cuyo evento no viene en la captura | 2 | no comparables | Ver «P6». |

### Los dos A1 de 12 horas

Son dos acciones: `c62de8b6-6269-49c8-935f-352add926cb4` (E 02:55:12Z, R 15:00:49Z) y `63fe4473-bd8e-404d-b628-bd92a6300926` (E 03:00:07Z, R 15:00:22Z), las dos del 20/10.

**Lo que muestra la captura.** No es el incidente del link de recuperación. Cada uno esperó al cupón: la reevaluación dio `execute` a las 02:55:24Z y a las 03:00:19Z, y el intento falló con `recovery_coupon_not_ready`. El reintento cayó a las 03:05:26Z y a las 03:05:20Z, que en CDMX son las 21:05, con la franja ya cerrada. Ahí la reevaluación dio `defer` con `business_window_closed` (03:05:27Z y 03:05:21Z), y la acción quedó con `due_at` a las 15:00Z, las 09:00 de CDMX. A las 15:00 salieron con el cupón `valid` (verificado a las 14:51:58Z y a las 14:52:30Z) y con su link: el intento 3 y el 2, `accepted_by_chatwoot`.

**Cómo se mide.** Un hecho observado explica la demora hasta que la puerta vuelve a decir «mandar» desde el hecho, no solo hasta el hecho. En la T1 la cota era max(E, el hecho), y no veía la franja que se cierra en el medio. El motor tiene la misma excepción, así que haría lo mismo.

El hecho no explica cualquier reintento: su hora se acota al arranque del intento más lo que pide el despachador, más 1 minuto de holgura. Son 5 minutos en A1 (`cart_first_touch.py:56-58`), 8 en Z (`cart_followups.py:96-98`) y 1 para un fallo genérico (`worker.py:1013`, `_finalize_pre_request_failure`). La espera del cupón vale solo en los dos primeros intentos (`defer_attempts`); un tercero es un fallo genérico. Además, R no puede quedar antes de que la puerta vuelva a abrir: un envío con la puerta cerrada no se excusa. En ATT1 el reintento pedido fue a los 5 min 01 s del arranque, dentro de la cota.

**El incidente del link (#282)** se ve en la captura de otra forma: no hay ningún link emitido antes del 20/10 a las 15:00Z. Los 16 A1 de la ventana anteriores a esa hora salieron sin link. Para P3 no es una demora, y no hace falta una diferencia con nombre. Además, 39 links quedaron `reserved`, creados entre las 15:00:35Z y las 18:12:27Z del 20/10: es el cabo de las emisiones en `reserved` de ese día.

### Los 16 A2 del bug de la v1.15.5

Las 16 acciones `cart_second_touch` de la ventana (17 con la siembra) terminaron `cancelled` con `terminal_reason = chatwoot_authority_unavailable`, del 20/10 a las 17:40Z al 21/10 a las 02:39Z.
- Cada una tiene una sola reevaluación: `escalate`, `chatwoot_authority_unavailable`, `chatwoot_checked = false`.
- La secuencia quedó `paused` y el caso `escalated`, así que su Z no nació.

**La causa** es el bug que arregló la v1.15.5 (#286, subida el 21/10 a las 14:35Z). La verificación de Chatwoot de un paso siguiente comparaba contra el `allowed_jid` del runtime, y no encontraba la conversación (`src/bridge/worker.py:1131-1146`). La base, sin la evidencia, escala (`supabase/migrations/20260805000300_per_case_conversation_anchor.sql:377-379`).

**El motivo.** Es `identidad:conversacion`: el contrato de lectura lo llama `identidad:<motivo>` («si la conversación es del inbox de la instancia», `docs/contracts/automation-expert-read-v1.md:190`). Está en la tabla de §2.9, `flow_engine.LEGACY_REASON_TO_MOTIVO`, como `identidad:<motivo>`, con los parámetros del contrato como el resto de la tabla. El parámetro `conversacion` lo pone `flow_engine.LEGACY_REASON_PARAMETERS` (vía `legacy_motivo`). Z hereda el final de su A2.

**No son diferencias con nombre.** Con el mismo hecho observado, que Chatwoot no se pudo verificar, la puerta del motor tampoco manda: escala por identidad. El final del motor y el del legado coinciden, así que la paridad no observa ninguna diferencia. Una diferencia con nombre sin una diferencia observada la paridad la rechaza (`nombrada_sin_diferencia`). La pérdida, 16 A2 y sus Z, es de la operación y no de la paridad, y quedó medida en el registro del frente 1.


### P6

- **De 259 a 62.** La primera corrida contó los planes de la siembra (259 cesiones, 10 sin ganador). P6 mira ahora los planes desde «desde», como hace la T1 sobre su fixture (§6.4, paso 2). Así la captura completa y el fixture dan lo mismo. Siete de esos diez eran de la siembra: su ganador pudo llegar antes de la siembra, y la captura no lo trae.
- **La ficha (1).** El plan del 21/10 a las 02:15:45Z, de la intención `6ca55dea…`, se canceló a las 03:15:46Z. La persona tenía un caso `att1-ficha` en curso desde las 02:18:33Z, y el legado cede ante cualquier caso de Hotmart abierto del producto (`20261001000200_portable_precheckout_first_contact.sql:300-311`, §1.7 fila 13). El evento de la ficha (`PURCHASE_BILLET_PRINTED`) no viene en la captura, pero el caso sí. La paridad v3 suma esa ruta, `caso_en_curso`: era un hueco del cálculo, no del legado.
- **Los dos casos no comparables.** Son dos casos de la ficha sin su evento en la captura (`1b5b9026…` y `b50829ee…`, rol `payment_slip`). No hay clave del evento que comparar con la del contacto, así que no se comparan y se cuentan aparte. Cualquier otro caso sin su evento (del carrito o del pago fallido) sigue siendo una diferencia `persona_no_coincide`.
- **La intención clasificada (2 planes, una cota).** Son dos planes `not_planned` del 20/10 (00:30:16Z y 00:37:49Z) de la intención `f6431dad-fe82-4764-adfe-21ba9890b8c6`.
  - Los dos son reenvíos del formulario. Se pegan a una intención del 11/10 a las 22:52Z, anterior a la siembra (12/10 18:13Z), que en la captura está `confirmed_abandonment`. El legado cede ante la intención clasificada (`20261001000200:297-299`).
  - El carrito que la clasificó es anterior a la siembra: ningún evento de la captura tiene una correlación con esa intención. Y la persona no tenía un caso en curso a esa hora: su pago rechazado abrió caso a las 01:51:59Z.
  - La ruta `intencion_clasificada` lo explica, y se usa solo si ninguna otra encuentra un ganador. **Es una cota:** la captura trae la foto de la intención, no desde cuándo está clasificada (`updated_at` lo pisa cualquier cambio, F#9). Ya no lleva diferencia con nombre: [`att1_parity_v3_named_differences.json`](../../tests/fixtures/flow_engine/att1_parity_v3_named_differences.json) queda vacío, con la razón en su `_doc`.
  - Es la otra cara del hallazgo de producto «un reenvío de formulario se pega a la intención abierta».

## El recorte

La captura completa daba un fixture de 1.282.358 bytes. El tope es de 1.000.000 y no se sube. El fixture v3 recorta, además de lo de la v2 (el primer contacto y las intenciones de la siembra), dos cosas:
- **Las acciones y las secuencias de los casos que no son de la ventana** (330 → 138 acciones, 249 → 58 secuencias; los intentos, de 1344 → 163). P3 y los finales miran solo los carritos de la ventana. P6 y «llegó al flujo» miran los casos, que quedan los 252. Las autorizaciones quedan las 435, porque la bolsa cuenta las observadas.
- **Las reevaluaciones de esos mismos casos** (2740 → 286, en 417 → 125 tramos). La paridad v3 no las lee, porque la respuesta viene en cada acción. Las de la ventana quedan como evidencia.

Los cupones, los links y las renovaciones quedan enteros, porque son la evidencia de este reporte. Cada recorte queda anotado en `_captured.fixture.recortes`, y el cargador recuenta contra él. El resultado pesa 897.748 bytes.

**Verificado:** la paridad sobre la captura completa y sobre el fixture da el mismo `--json` byte a byte.

## Lo que no mide

- P1 (`no_arranca_mientras`, el pago rechazado abierto) ni P4. Los casos del pago rechazado y de la ficha están en la captura como hechos de la persona.
- La primera respuesta de cada envío sale de lo que la base guarda: la adopción, la reevaluación que vio un entrante y el pedido de renovación. La hora de la reevaluación es una cota: es cuándo la base lo vio, no cuándo llegó el mensaje. La base de ATT1 no guarda los mensajes entrantes.

## Lo que cambió con la revisión

La revisión adversarial de la T2.2 encontró seis cosas. Ninguna cambió los totales de arriba, salvo P6 y el peso del fixture.
1. **El reintento acotado.** Un hecho observado explicaba cualquier `finalized_next_attempt_at`. Ahora se acota a lo que pide el despachador, y R no puede quedar antes de que la puerta vuelva a abrir. Hay rojos: el reintento a 7 h, el fallo genérico que pide 30 min y la tercera espera del cupón.
2. **`paso_no_creado`.** Un paso que el legado no creó, cuyo anterior salió y cuyo E más la tolerancia ya pasó a la hora de la captura, es una diferencia. Antes quedaba `pendiente`. En ATT1 no hay ninguno.
3. **Las reevaluaciones del fixture.** Ahora se recortan a los casos de la ventana, en vez de vaciarse. El `--json` de la captura completa y el del fixture siguen idénticos.
4. **La intención clasificada.** Es una ruta de P6, rotulada como cota, en lugar de la diferencia con nombre.
5. **`chatwoot_authority_unavailable`.** Pasó a la tabla de `flow_engine`.
6. **Los casos de la ficha.** Quedan aparte como no comparables, y solo ellos.

