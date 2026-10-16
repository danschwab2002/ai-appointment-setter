# La paridad del motor de flujos contra ATT1: medición del 2026-10-16

**Qué se midió.** Si la puerta del motor de flujos explica lo que hizo hoy el legado del carrito y del pago fallido en ATT1. Las preguntas eran dos:
- **P3:** ¿cada envío salió cuando la puerta lo permitía, nunca antes y a lo sumo 5 minutos después?
- **P6:** ¿cada primer contacto que cedió tiene el disparo que le ganó?

Es la tajada 1 del motor (pieza 2 de la Etapa 1b). No ejecuta nada ni cambia nada de lo que corre. Es la vara contra la que se mide el motor cuando mande.

**El dato.** La captura `captura-paridad-recuperacion-v2` de `att1-db`, leída con `default_transaction_read_only` el 2026-10-16 a las 21:03:26Z.
- **Su consulta:** [`tests/fixtures/flow_engine/att1_recovery_capture_query_v2.sql`](../../tests/fixtures/flow_engine/att1_recovery_capture_query_v2.sql), con sha256 `0a63678a…db51`.
- **Las personas:** salen como sha256 truncado, con una sal aleatoria por corrida que no sale de la consulta.
- **`config`:** sale con una lista cerrada de claves, sin nombres de operador.
- **El fixture:** la captura completa pesa 1.400.924 bytes (sha256 `df6addec…e015`) y queda fuera del repo. El fixture recortado es [`att1_recovery_capture_20261016.json`](../../tests/fixtures/flow_engine/att1_recovery_capture_20261016.json), de 984.039 bytes, y lo arma `scripts/flow_engine_capture_fixture.py`. Ese script valida la forma y la privacidad de cada clave, recorta la siembra que la medición no lee y saca las columnas constantes, que el cargador vuelve a poner.

**La ventana.** Arranca con la activación de la v3 del scope del carrito, `att1-recuperacion`, el 2026-10-08 a las 22:42:19Z, y tiene 7 días de siembra para el estado.
- La v2 (`consented_intent_in_cohort`) estuvo activa del 06/10 al 08/10. Tuvo 66 disparos y no vuelve a correr, así que queda fuera.

**La medición**, en el commit `41e6d04` de la rama `feat/claude-flow-engine-t1-v1`:

```sh
uv run python scripts/flow_engine_parity.py \
  --capture tests/fixtures/flow_engine/att1_recovery_capture_20261016.json \
  --named tests/fixtures/flow_engine/att1_parity_named_differences.json
```

Sale con 0. Sobre la captura completa da los mismos números.

## El censo y el denominador

- **`webhook_events`, sin ventana:**
  - Hotmart, 402: 305 `PURCHASE_OUT_OF_SHOPPING_CART`, 72 `PURCHASE_APPROVED`, 13 `PURCHASE_CANCELED`, 10 `PURCHASE_BILLET_PRINTED` y 2 `PURCHASE_EXPIRED`. El primero es del 2026-10-05 a las 03:53Z.
  - `system`, 258 `PRECHECKOUT_FORM_SUBMITTED`.
  - Una captura anterior del mismo día, la v1, tenía 400 de Hotmart. Los 2 de diferencia entraron entre las dos corridas. Los 449 «eventos de Hotmart desde el 05/10» que citaba el borrador de varios productos del 15/10 no salen de este censo.
- **Disparos desde la v3:** 252.
  - **Los 239 carritos:**
    - 208 planificados;
    - 26 `pilot_scope_rejected:pilot_audience_intent_unresolved`;
    - 1 `pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out`;
    - 4 `create_recovery_case_failed`.
  - **Los 13 pagos fallidos:**
    - 5 con caso propio;
    - 1 sumado como evidencia a un caso abierto;
    - 5 `payment_failure_correlation_unresolved`;
    - 2 `pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out`.
- **Llegaron al flujo:** 248.
  - Cuenta el evento con caso, o con un rechazo de la planificación.
  - Los 4 `create_recovery_case_failed` de la ventana no tienen clave de persona y se cuentan como «no llegó».
  - Ojo: el caso G14 muestra que ese mismo error también lo da un caso trabado por un `permanent_failed` (ver «Lo que mostraron los casos de hoy»). En la ventana no hay ninguno.
- **Envíos:** 208, que son 203 carritos y 5 pagos fallidos. La captura trae 210; los otros 2 son de casos de la v2 que siguen como estado.

## P3: la hora de salida

E es el primer instante en que la puerta dice «mandar» con los hechos observados: la gracia, la franja, el control armado y la bolsa del día con las autorizaciones observadas antes. R es el `request_started_at` observado.

| | |
|---|---|
| Envíos medidos | 208 |
| R − E mínimo | 4,17 s |
| R − E mediana | 12,93 s |
| R − E p95 | 106,94 s |
| R − E máximo | 144,61 s |
| Antes de E (R < E − 1 s) | **0** |
| Más de 5 minutos después de E | **0** |

- **E coincide exacto con el `due_at` que dejó la base** en los 208. 89 envíos llegaron con la franja cerrada, y su E es las 09:00 de `America/Mexico_City`.
- **Los 7 envíos más lentos**, de 120 a 145 s, son todos de la tanda de las 09:00 del 12/10: los carritos de la noche salieron juntos.
- **Con una tolerancia de 2 minutos, esos 7 quedan afuera.** La tolerancia de 5 tiene dientes, y es el lease del despachador (`src/bridge/worker.py:705`).
- **El tope del día**, con las autorizaciones observadas, no corre el E de ningún envío. Con la bolsa predicha, el E de las 213 acciones da igual.

## P6: quién le gana a quién

- **192 primeros contactos cedieron ante un carrito o un pago fallido.** Todos tienen su ganador, y ningún ganador quedó sin su cancelación.
- **Las dos rutas:** 79 por persona y 113 por intención.
  - Por persona, el legado cede solo si el contacto tiene un caso de Hotmart del producto **en curso** (`supabase/migrations/20261001000200_portable_precheckout_first_contact.sql:297-313`), y el contrato de `le_gana_a` dice lo mismo.
  - Los 113 por intención cedieron después del envío del carrito, por la clasificación de la intención.
  - Entre ellos hay 4 carritos sin teléfono que no llegaron al flujo: hoy, un disparo que no llega al flujo igual le gana al primer contacto. Hay que tenerlo en cuenta cuando se migre ese flujo.

## Las diferencias con nombre

Son lo único que se acepta fuera de P3 y P6. Cada una lleva el caso generado que la reproduce, o la cita del código de hoy ([`att1_parity_named_differences.json`](../../tests/fixtures/flow_engine/att1_parity_named_differences.json)).

| Nombre | Cuántas | Qué es | Caso |
|---|---|---|---|
| `lo_en_curso_sigue` | 5 | Los carritos de la noche del 12/10 (CDMX), con la v4 del scope. El legado rechazó su arranque por la versión (`pilot_scope_version_mismatch`) hasta que vencieron. El motor los habría mandado el 13/10 a las 15:00Z, con su versión fijada | G21 |

**Sin explicar: 0.**

## Lo que mostraron los casos de hoy

`tests/sql/followup_engine/validate_flow_engine_legacy.mjs` genera, con la cadena real de migraciones en PGlite, lo que hace hoy el legado en 24 casos (G1 a G24, en [`legacy_recovery_cases.json`](../../tests/fixtures/flow_engine/legacy_recovery_cases.json)). Con `--check`, la CI falla si regenerarlo da otra cosa.

Tres comportamientos que el motor tiene que tener en cuenta:
- **G14.** Después de un `permanent_failed` (un nombre rechazado), el carrito siguiente del mismo contacto falla con `create_recovery_case_failed`, por la unicidad de `followup_sequences_one_active_per_case_idx` (23505). No da `pilot_case_binding_conflict`. El caso queda abierto y frena los carritos siguientes.
- **G17.** Un conflicto semántico de compra sin resolver frena el arranque con un error `P0001`, no con un `pilot_request_start_rejected`. El despachador lo trata como un error genérico.
- **G13 y G16.** Las barreras de la compra conocida antes del plan actúan solo si la compra no cierra la intención. Si la cierra, el plan se rechaza antes, con `pilot_audience_consented_intent_not_live`.

## Lo que no prueba

- **Que la admisión ni la autorización del motor funcionen.** Eso es la tajada 2, con su paridad en PGlite contra estos 24 casos.
- **Nada sobre el cupón, la reactivación ni el primer contacto como flujos del motor.**
- **P4 sobre los topes**, porque los rechazos del arranque no se guardan. Se mide solo en los casos generados (G4 a G6).
- **La vida de una intención**, porque la captura tiene solo su foto.

## La CI

- **Antes:** el job `verify` tardó entre 626 y 769 s en las últimas cinco corridas de `main`, hasta `7c0a61b`.
- **Lo que suma esta tajada:**
  - un script de PGlite, de 1,7 s en local (la cadena en 1,2 s y los casos en 0,5 s);
  - en pytest, las pruebas nuevas, de pocos segundos.
- El tiempo del job con la tajada va en el PR.
