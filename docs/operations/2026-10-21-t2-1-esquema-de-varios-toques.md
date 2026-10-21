# La T2.1 del motor de flujos: el esquema de varios toques (2026-10-21)

**Qué es.** La segunda tajada de la T2 re-tajada: solo Python puro y contrato. No tiene migración, no toca el bridge que corre y no despliega nada. Cuando se mergee, el escalón más alto que alcanza es «mergeado y probado».

**Qué cambió.**
- **El esquema, 1.1** ([flow-definition-v1.md](../contracts/flow-definition-v1.md), «Historial»): las variables `cuando`, `precio` y `vence`; `boton.cupon: personal` (el código por persona del worker del cupón), a lo sumo dos por camino (regla 15, `coupon_personal_limit`); pasar de un código fijo a `personal`, o al revés, sigue siendo `coupon_change_not_supported`. Se levanta «un solo envío por camino»: el motor ejecuta los envíos de la forma de su flujo del legado. El carrito y el pago fallido de ATT1 ya no comparten bolsa desde el 19/10.
- **Las capacidades** (`flow_engine.ENGINE_CAPABILITIES_V1`): hasta tres envíos por camino y la espera entre ellos (`waits_between_sends`). Una espera después del último envío sigue sin ejecutarse. El código de más envíos pasó de `mas_de_un_envio` a `demasiados_envios`.
- **La política compilada** (`flow_policy`): la forma de cada flujo del legado (`STEPS_BY_LEGACY_KIND`) y la compilación de N pasos: la gracia es la suma de las esperas antes del primer envío; cada paso siguiente lleva `delay` desde el primer envío, `approved_template: true`, y `coupon_ordinal: 2` si lleva el segundo cupón. Cada paso de la forma lleva además las variables con las que lo compone el bridge. Lo que no entra en la forma da `flow_policy_not_compilable:<detalle>`: `more_sends_than_steps`, y con el paso, `coupon_not_allowed`, `coupon_missing`, `fixed_coupon`, `variables_differ` y `espera_entre_envios` (entre dos envíos, al menos un minuto). La plantilla de cada paso todavía no se compila: el bridge manda la de su entorno hasta la T2.4 (principio 10). `differences_from_row` compara los pasos enteros y en orden; las filas compartidas del 16/10 se comparan con `shared_row=True`.
- **El traductor** (`tests/flow_store_translator.py`) traduce la captura del 21/10 con las plantillas de los toques como parámetro (`ATT1_TOUCHES_20261021`). La del 16/10 da exactamente lo mismo: los fixtures se regeneraron y solo cambió el sha256 del traductor en la cabecera de `att1_definitions_by_scope_version.json`.
- **El validador**, al publicar un flujo con `legacy_kind` (`WorldFlow.legacy_kind`), compila la definición y convierte lo que no entra en la forma en un bloqueo de la regla 14 (`engine_not_supported:<detalle>`, con la ruta del paso).
- **Las excepciones** (`flow_parity.EXCEPTIONS_V1`): seis de los varios toques, cada una con sus `step_keys`. Las tiene solo un flujo de su `legacy_kind` cuyo plan tiene ese paso: un carrito de un toque no hereda las de Z.

**El dato.** [`att1_flow_config_20261021.json`](../../tests/fixtures/flow_store/att1_flow_config_20261021.json): att1-db el 2026-10-21 a las 15:43:39Z, en solo lectura, con `att1_flow_config_query.sql`.

## Cómo se verifica

```sh
uv run pytest tests/test_flow_policy.py tests/test_flow_validator_att1.py tests/test_flow_engine_simulate_att1.py
uv run python scripts/flow_engine_simulate.py --consistencia
```

- `compile_policy(translate(v7))` es igual a la fila `att1-carrito-tres-toques` v1, paso por paso, y la del pago fallido a `att1-pago-rechazado-dos-toques` v1.
- Los rojos: un cuarto envío, el cupón en A2, Z sin cupón, un código fijo, y un tercer cupón personal.
- La propuesta «A2 a las 36 h», sobre la captura del 16/10 ([`att1_cart_three_touches_a2_36h.json`](../../tests/fixtures/flow_simulation/att1_cart_three_touches_a2_36h.json)): 206 disparos cambian, todos en el paso 2 del carrito. A2 pasa de 188 a 178 enviados porque 10 todavía lo esperan a la hora de la captura; Z da 150 enviados y 2 compras en las dos.
- La vigente contra sí misma sigue en 0 cambios, y la consistencia contra la T1 en verde.

## Lo que no prueba, y lo que queda abierto

- **Las variables de las plantillas nuevas** quedaron cerradas en el mismo PR: el esquema suma `cuando`, `precio` y `vence`, con los nombres y el significado del bridge, y la regla 5 suma dos protecciones (`variable_needs_personal_coupon` y `variable_needs_event_trigger`). Van en la 5 porque la base acepta reglas de 1 a 17. La v7 se publica sin bloqueos contra el catálogo capturado del 18 y el 19/10.
- **El seguimiento del pago fallido.** La definición traduce B2 (`att1_pago_rechazado_descuento_01`), el que sigue a un B1 sin cupón. Que B1 lleve cupón y el seguimiento sea Z lo decide el bridge con la variante, y las variantes de B1 no se migran en la Etapa 1.
- **La regla 14 en el validador** sigue fuera: la cubre `not_supported` del motor.
