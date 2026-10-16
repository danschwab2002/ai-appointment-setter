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
- La v2 (`consented_intent_in_cohort`) estuvo activa del 06/10 a las 18:42Z al 08/10 a las 22:42Z (los eventos de control de `config`). Tuvo 66 disparos (los carritos y pagos fallidos de la siembra del fixture recibidos en ese tramo) y no vuelve a correr, así que queda fuera.

**La medición**, en la rama `feat/claude-flow-engine-t1-v1`. Se midió en `41e6d04` y se volvió a correr con las correcciones de la revisión adversarial: los números de `41e6d04` no cambiaron, y se sumaron el reparto de P6 sin depender del orden y los que cedieron ante un disparo sin caso.

```sh
uv run python scripts/flow_engine_parity.py \
  --capture tests/fixtures/flow_engine/att1_recovery_capture_20261016.json \
  --named tests/fixtures/flow_engine/att1_parity_named_differences.json
```

Sale con 0. Sobre la captura completa da los mismos números (cambia solo cómo se escribe la hora de la cabecera, `Z` o `+00:00`). La salida, sobre el fixture:

```text
Paridad del motor sobre captura-paridad-recuperacion-v2 (captura 2026-10-16T21:03:26.141035Z)
desde: 2026-10-08T22:42:19.893671Z (pilot_scope_version_activated:att1-recuperacion:v>=3)

Disparos desde «desde»: 252
  carrito/create_recovery_case_failed: 4
  carrito/pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out: 1
  carrito/pilot_scope_rejected:pilot_audience_intent_unresolved: 26
  carrito/processed: 208
  pago_fallido/payment_failure_correlation_unresolved: 5
  pago_fallido/pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out: 2
  pago_fallido/processed: 5
  pago_fallido/processed:evidencia: 1
Llegaron al flujo: 248
Envios: 208
R - E: minimo 4.17 s, mediana 12.93 s, p95 106.94 s, maximo 144.61 s
Nunca antes (R >= E - 1 s): 0 violaciones
Fuera de la tolerancia (300 s): 0
P6: 192 primeros contactos cedieron (79 por persona, 113 por intencion); sin ganador: 0; ganadores sin cancelacion: 0
  sin depender del orden: 78 por las dos rutas, 1 solo por persona, 113 solo por intencion
  ante un disparo sin caso (no mando nada): 5
    create_recovery_case_failed: 4
    pilot_scope_rejected:pilot_runtime_not_armed: 1
Diferencias con nombre: 5
  lo_en_curso_sigue: 5
Sin explicar: 0
Resultado: verde
```

## El censo y el denominador

- **`webhook_events`, sin ventana:**
  - Hotmart, 402: 305 `PURCHASE_OUT_OF_SHOPPING_CART`, 72 `PURCHASE_APPROVED`, 13 `PURCHASE_CANCELED`, 10 `PURCHASE_BILLET_PRINTED` y 2 `PURCHASE_EXPIRED`. El primero es del 2026-10-05 a las 03:53Z.
  - `system`, 258 `PRECHECKOUT_FORM_SUBMITTED`.
  - Una captura anterior del mismo día, la v1 (`att1-captura-motor-20261016.json`, en los anexos del vault, fuera del repo), tenía 400 de Hotmart, con 303 carritos. Los 2 carritos de diferencia entraron entre las dos corridas. Los 449 «eventos de Hotmart desde el 05/10» que citaba el borrador de varios productos del 15/10 no salen de este censo.
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
  - Un `create_recovery_case_failed` es del adaptador, salvo cuando la persona del disparo tenía un caso del mismo producto en curso. Ese es el caso trabado por un `permanent_failed` de G14 (la 5b): el plan intentó abrir otra secuencia en el caso y la base contestó 23505. Ese sí llegó, frenado, y el reporte lo muestra como `create_recovery_case_failed:caso_en_curso`.
  - Los 4 `create_recovery_case_failed` de la ventana no tienen clave de persona, así que no hay un caso de la persona en curso: siguen como «no llegó», y el 248 no cambia con esa regla.
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
- **Con una tolerancia de 2 minutos, esos 7 quedan afuera.** La tolerancia de 5 tiene dientes, y es el lease del despachador (`src/bridge/worker.py:706`). Lo fija `test_p3_the_tolerance_has_teeth`.
- **Sobre este dato, P3 ejercita solo la franja.** El control y la bolsa no corren el E de ningún envío: en los 208, E es el ancla o la próxima apertura de la franja (`test_p3_on_the_real_data_only_the_window_moves_e`). Que el control y la bolsa estén bien lo fijan las líneas de tiempo escritas a mano (`tests/test_flow_parity.py`) y el E de los 5 de `lo_en_curso_sigue`.
- **Un hecho observado explica solo su propia demora.** Un fallo antes del pedido, un incierto o un rechazo de Meta explican hasta el reintento que pidieron; un arranque rechazado (un intento `reserved` sin resultado), hasta que vence su lease de 5 minutos. R tiene que quedar a lo sumo 5 minutos después de eso. En este dato ningún envío tuvo un intento anterior, así que no cambia nada; cambia con la primera pausa de un flujo o con un tope que se libera dentro de la franja.

## P6: quién le gana a quién

- **192 primeros contactos cedieron ante un carrito o un pago fallido.** Todos tienen su ganador, y ningún ganador quedó sin su cancelación.
- **Las dos rutas, sin depender del orden en que se buscan:** 78 cedieron por las dos, 1 solo por persona y 113 solo por intención.
  - Buscando primero la persona (`MATCH_ORDER`), el reparto da 79 por persona y 113 por intención; buscando primero la intención daría 191 y 1. Por eso el reparto que vale es el de las tres columnas.
  - Por persona, el legado cede solo si el contacto tiene un caso de Hotmart del producto **en curso** (`supabase/migrations/20261001000200_portable_precheckout_first_contact.sql:297-313`), y el contrato de `le_gana_a` dice lo mismo.
  - De los 113 solo por intención, 108 cedieron después del envío del carrito o del pago fallido que les ganó, por la clasificación de la intención.
  - Los otros 5 cedieron ante un disparo sin caso, que no mandó nada: 4 carritos sin teléfono (`create_recovery_case_failed`) y 1 de la siembra rechazado con el control sin armar (`pilot_scope_rejected:pilot_runtime_not_armed`). Hoy, un disparo que no llega a mandar igual le gana al primer contacto. Hay que tenerlo en cuenta cuando se migre ese flujo.

## Las diferencias con nombre

Son lo único que se acepta fuera de P3 y P6. Cada una lleva el caso generado que la reproduce, o la cita del código de hoy ([`att1_parity_named_differences.json`](../../tests/fixtures/flow_engine/att1_parity_named_differences.json)).

| Nombre | Cuántas | Qué es | Caso |
|---|---|---|---|
| `lo_en_curso_sigue` | 5 | Los carritos del 12/10 de 15:20 a 18:04 de CDMX (21:20Z a 00:04Z), con la franja abierta y la v4 del scope. Los frenó el tope de 40 del día de la v4, gastado antes del primero. A las 00:20Z del 13/10 se activó la v5, y desde ahí el legado rechazó su arranque por la versión (`pilot_scope_version_mismatch`) hasta que vencieron. El motor los habría mandado el 13/10 a las 15:00Z (las 09:00 de CDMX, con el cupo del día nuevo), con su versión fijada | G21 |

**Sin explicar: 0.**

## Lo que mostraron los casos de hoy

`tests/sql/followup_engine/validate_flow_engine_legacy.mjs` genera, con la cadena real de migraciones en PGlite, lo que hace hoy el legado en 25 casos (G1 a G25, en [`legacy_recovery_cases.json`](../../tests/fixtures/flow_engine/legacy_recovery_cases.json)). Con `--check`, la CI falla si regenerarlo da otra cosa. G25 lo pidió la revisión: la reevaluación en el instante exacto del vencimiento vence la acción (`expired`, `reevaluation_expired`) y el caso.

Tres comportamientos que el motor tiene que tener en cuenta:
- **G14.** Después de un `permanent_failed` (un nombre rechazado), el carrito siguiente del mismo contacto falla con `create_recovery_case_failed`, por la unicidad de `followup_sequences_one_active_per_case_idx` (23505). No da `pilot_case_binding_conflict`. El caso queda abierto y frena los carritos siguientes.
- **G17.** Un conflicto semántico de compra sin resolver frena el arranque con un error `P0001`, no con un `pilot_request_start_rejected`. El despachador lo trata como un error genérico.
- **G13 y G16.** Las barreras de la compra conocida antes del plan actúan solo si la compra no cierra la intención. Si la cierra, el plan se rechaza antes, con `pilot_audience_consented_intent_not_live`.

## Lo que no prueba

- **Que la admisión ni la autorización del motor funcionen.** Eso es la tajada 2, con su paridad en PGlite contra estos 25 casos.
- **Nada sobre el cupón, la reactivación ni el primer contacto como flujos del motor.**
- **P4 sobre los topes**, porque los rechazos del arranque no se guardan. Se mide solo en los casos generados (G4 a G6).
- **El límite de mensajes de la reevaluación** (`automatic_message_limit_reached`). No tiene caso generado porque la cadena de hoy no llega a esa rama con el carrito ni con el pago fallido: la secuencia nace con `max_attempts` igual al límite de la política, y al aceptar el único envío se completa. No queda una acción pendiente con el límite alcanzado. Llega con un flujo de dos envíos, en T3.
- **La vida de una intención**, porque la captura tiene solo su foto.

## Sobre el rojo 5

El rojo 5 de §5.4 («adelantar E») corre E un segundo más tarde: es la única lectura con la que el «nunca antes» puede fallar. Muestra los dientes del «nunca antes» solo en la línea de tiempo escrita a mano. Sobre el dato real, la menor holgura es de 4,17 s, así que correr E un segundo no lo dispara: salta recién con 5,17 s o más. Con un segundo de más o de menos, lo que falla sobre el fixture es la demora fijada y que E sea el `due_at`.

## La CI

- **Antes:** el job `verify` tardó entre 626 y 769 s en las últimas cinco corridas de `main`, hasta `7c0a61b`.
- **Con la tajada**, en la corrida del PR (`38974693146`): `verify` tardó 773 s, la más larga de las medidas, a 127 s del límite de 15 minutos.
  - El script de PGlite, `validate_flow_engine_legacy.mjs --check`, tardó 5,2 s en la CI (la cadena en 3,2 s, la gemela en 0,36 s y los casos en 1,6 s), unas 3 veces lo de local.
  - En local, con los 25 casos y los 20 de la gemela, tarda 1,8 s.
  - El paso de pytest pasó de 68 a 74 s en `main` a 77 s.
- **El aporte es de unos 10 s, pero el margen es chico.** El ruido entre corridas ya es de ±60 s, y cada tajada que suma un script de PGlite agrega de 5 a 10 s. Antes de la T2, que trae su paridad en PGlite, conviene subir `timeout-minutes` o partir el job en dos (pytest y SQL). No es de este PR.
