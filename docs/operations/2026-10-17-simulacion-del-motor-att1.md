# La simulación del motor de flujos sobre la captura de ATT1: medición del 2026-10-17

**Qué se midió.** Dos cosas, sobre la misma semana real de ATT1:
- **La consistencia:** si el replay del motor, corriendo cada disparo con la versión que tenía, da lo mismo que midió la T1 (`docs/operations/2026-10-16-paridad-del-motor-att1.md`), y si la versión vigente contra sí misma no cambia nada.
- **Las propuestas:** qué habría pasado en esa semana con un cambio de flujo (una espera, un tope, un horario, tres toques), comparado contra la v5 aplicada a toda la ventana.

Es la tajada 3a del motor (pieza 2 de la Etapa 1b): código puro, que lee un archivo. No ejecuta nada ni cambia nada de lo que corre, y no se corrió en ATT1. No hay deploy: cuando se mergee, el escalón más alto que alcanza es «mergeado y medido».

**El código.** La rama `feat/claude-flow-engine-t3a-v1`, medida en el commit 6 (`fix(flow-engine): lo que encontraron las revisiones`, sobre `bd21749`), con base en `c7fa8ef` (el merge de #253). Los módulos son `src/bridge/flow_engine.py`, `src/bridge/flow_replay.py` y `scripts/flow_engine_simulate.py`.

**El dato.** La misma captura de la T1: `captura-paridad-recuperacion-v2` de `att1-db`, leída con `default_transaction_read_only` el 2026-10-16 a las 21:03:26Z.
- **El fixture:** [`att1_recovery_capture_20261016.json`](../../tests/fixtures/flow_engine/att1_recovery_capture_20261016.json), sha256 `05af597c…57b9`. Cómo se armó y qué deja afuera está en el documento de la T1.
- **Las definiciones:** la v3, la v4 y la v5 del carrito y del pago fallido, traducidas a `setter-flujo/v1` en [`att1_definitions_by_scope_version.json`](../../tests/fixtures/flow_simulation/att1_definitions_by_scope_version.json), sha256 `a989051f…de02`.
- **Las propuestas:** [`simulation_proposals.json`](../../tests/fixtures/flow_simulation/simulation_proposals.json), sha256 `649bbf96…e4bd`, con el resultado esperado literal de cada una y los números que dependen de un orden que la captura no trae (`sensible_al_orden`). Las lee `tests/test_flow_engine_simulate_att1.py`.
- **Sin datos de personas:** la captura trae a las personas como sha256 truncado con sal. Este documento no cita ningún uuid de disparo.

**La ventana.** Desde la activación de la v3 del scope del carrito, `att1-recuperacion`, el 2026-10-08 a las 22:42:19Z, hasta la hora de la captura. Es la misma de la T1.

## La consistencia

```sh
uv run python scripts/flow_engine_simulate.py --consistencia
```

Por defecto lee el fixture, las definiciones y las diferencias con nombre de la T1. Sale con 0. La salida, con los 5 uuid de las diferencias con nombre sacados:

```text
Consistencia del motor sobre captura-paridad-recuperacion-v2 (captura 2026-10-16T21:03:26.141035Z)
ventana: desde 2026-10-08T22:42:19.893671Z hasta 2026-10-16T21:03:26.141035Z

Vigente contra si misma: 0 cambios
Conteos (replay / T1):
  disparos: 252 / 252
  llegaron: 248 / 248
  arranques: 213 / 213
  envios: 213 / -
  envios_del_legado: 208 / 208
E distinta de la T1: 0
Envios de mas en el replay: 5 (explicados por una diferencia con nombre: 5)
  (los 5 uuid de att1_parity_named_differences.json)
Con nombre y sin envio en el replay: 0
P6 (replay / T1): cedieron 192/192, las_dos 78/78, solo_persona 1/1, solo_intencion 113/113, sin_ganador 0/0, ganadores_sin_cancelacion 0/0
Resultado: verde
```

El resto de §7 del diseño no sale en esa salida: lo fijan pruebas que corren sobre el mismo fixture.

| Conteo | T1 | Replay «como corrió» | Dónde se fija |
|---|---|---|---|
| Disparos desde «desde» | 252 | 252 | la salida |
| Llegaron al flujo | 248 | 248 | la salida |
| Arranques | 213 | 213 | la salida |
| No arrancaron | 35 | 26 `no_entra:permiso`, 3 `baja`, 5 sin motivo y 1 `ya_en_curso` | `--propuesta`, la vigente (abajo) |
| Envíos | 208 | 213 | la salida |
| E igual a la de la T1 | 213 | 213, a 0 s | la salida y `test_first_eligible_is_eligible_at_on_the_213_actions_of_the_window` |
| E igual al `due_at` del legado | 208 | 208 | `test_e_is_the_due_at_that_the_legacy_wrote_in_the_208_sends` |
| Enviados en el replay y no en el legado | 5 `lo_en_curso_sigue` | 5, a las 15:00Z del 13/10 | la salida y `test_the_five_carts_of_the_12th_are_the_named_differences` |
| P6: cedieron | 192 | 192 | la salida |
| P6: las dos rutas, solo persona, solo intención | 78, 1, 113 | 78, 1, 113 | la salida |
| P6: sin ganador, ganadores sin cancelación | 0, 0 | 0, 0 | la salida |
| Vigente contra sí misma | | 0 cambios | la salida |
| Envíos por recorrido, con la v1 | | a lo sumo 1 | `test_with_the_v1_no_run_sends_twice_and_none_is_looked_at_after_its_send` |
| En curso a las 2026-10-13T06:00Z | | 6 carritos y 1 pago fallido | `test_in_flight_at_six_of_the_13th` |

- **Los 5 de más son los de la T1.** Los carritos del 12/10 que frenó el tope de 40 de la v4: el replay los mueve a las 09:00 de CDMX del 13/10 (`moved_by` es `bolsa_dia` y `franja`) y anota que el legado los rechazó por la versión. Si el replay ve un envío de más sin nombre, o un nombrado sin envío, la consistencia da rojo (`test_named_with_an_invented_uuid_is_not_ok` y `test_named_without_one_uuid_is_not_ok`).
- **La puerta es una sola.** Un espía sobre `evaluate_gate` ve un «mandar» a la hora exacta de cada uno de los 213 envíos, y con un centinela que nunca manda no sale ninguno (`tests/test_flow_replay.py`).
- **No depende del orden de las filas.** La captura con sus secciones invertidas da el mismo replay (`test_the_capture_with_its_sections_reversed_gives_the_same_replay`).
- **Las igualdades que sostienen el número:** en las 213 acciones, el ancla es `creation_date_ms` y el vencimiento es el ancla más 1 día (`test_the_equalities_that_make_the_prototype_count`).

## Las propuestas medidas

Cada propuesta de `simulation_proposals.json`, con sus ítems en un archivo aparte:

```sh
uv run python scripts/flow_engine_simulate.py --propuesta <items>.json
```

Las cinco de un solo envío salen con 0. **La vigente** es la misma en todas: la v5 de los dos flujos aplicada a toda la ventana da 252 disparos, 213 enviados, 35 que no arrancan (26 `no_entra:permiso`, 3 `baja`, 5 sin motivo y 1 `ya_en_curso`) y 4 que no llegan. Los 213 envíos son `exacta`. La v5 no es lo que pasó: con 80 por día, los 5 carritos del 12/10 salen al llegar.

| Propuesta | Recibieron | Cambios | Fin, además de los 35 y los 4 | Cota del envío |
|---|---|---|---|---|
| El carrito a 1 h del disparo | 212 | 141 | 1 en curso | carrito como máximo |
| El pago fallido a 10 min | 211 | 4 | 1 compró; 36 no arrancan | pago fallido como máximo |
| El tope del día en 1, los dos flujos | 8 | 213 | 4 compraron, 1 en curso, 199 vencieron; 36 no arrancan | carrito como máximo |
| El horario de 10 a 20, los dos flujos | 212 | 119 | 1 compró | los dos como máximo |
| El horario de 10 a 20, solo el carrito | 213 | 117 | | carrito como máximo |

- **El carrito a 1 h.** Nadie compra en esa hora. Queda 1 en curso: su hora cae después de la captura. En 140 de los 212 enviados la baja queda sin saber (135 salen después del envío observado y 5 no tenían envío observado), El resumen dice 141 porque suma el que queda en curso.
- **El pago fallido a 10 minutos.** De los 5 pagos fallidos, 3 compraron solos a los pocos minutos, pero la espera le evita el mensaje a uno solo, que compra al minuto y medio. Los otros dos son el mismo contacto, que falló el pago dos veces en un minuto el 11/10: la repetición se suma al primero y le adelanta el envío a su llegada, antes de la compra. Es lo que hace el legado (`least(due_at, now())`, `20260904000100:142-154`), y por eso hay un `ya_en_curso` más.
- **El tope del día en 1.** Sale uno por día local, a las 15:00Z del 09/10 al 16/10. Quién sale cada día lo decide el desempate del despachador (abajo): con el del legado cambian 213 disparos contra la v5; con el viejo, por `source_ref`, eran 210. Los 8, los 4 y los 199 no cambian con el desempate. El cupo del 08/10 ya lo ocupa una autorización sembrada, y el del 17/10 cae después de la captura (el que queda en curso). Los 9 días de la ventana llegan al tope. De los 4 que compran mientras esperan el cupo, 2 son carritos y 2 pagos fallidos: en el legado la compra gana el caso abierto y cancela la acción diferida (`20260808000100:190-206`, `:405-412` y `:428-444`).
- **El horario de 10 a 20.** En el pago fallido, uno que llegaba a las 09:07 de CDMX pasa a las 10:00 y compra antes. Con el cambio solo en el carrito no compra nadie, y reciben los 213.

## Los tres toques

La propuesta es el carrito con el primer mensaje a 1 h del disparo, el segundo a 24 h del primer envío y el tercero a 48 h, con tope de 80 por día y plazo de 4 días. El pago fallido sigue como la v5. Las esperas de la definición son 24 h y 24 h, y se cuentan desde el primer envío, como hace el legado (`20260813000100:337-350`). El archivo de la propuesta es el de `tres_toques_vence_4d` del fixture.

```sh
uv run python scripts/flow_engine_simulate.py --propuesta <tres-toques>.json --varios-toques
```

Sale con 0. Sin `--varios-toques` salía con 2: `engine_not_supported:espera_despues_del_envio` y `engine_not_supported:mas_de_un_envio`, porque las capacidades v1 eran de un envío por camino. Desde la T2.1 (2026-10-21) la v1 ejecuta hasta tres envíos con la espera entre ellos, así que sin `--varios-toques` da lo mismo, y lo que sale con 2 es un cuarto envío (`engine_not_supported:demasiados_envios`; ver [la T2.1](2026-10-21-t2-1-esquema-de-varios-toques.md)).

| | Toque 1 | Toque 2 | Toque 3 |
|---|---|---|---|
| Enviados | 207 | 183 | 157 |
| Terminan en ese toque | 1 en curso | 2 compraron, 22 en curso | 1 compró, 25 en curso |
| Movidos por el tope del día | 49 | 24 | 41 |
| Cota | como máximo | como máximo | como máximo |

- **El fin:** de 252 disparos, 212 reciben al menos el primer mensaje; 162 terminan con todos sus envíos hechos (157 carritos y 5 pagos fallidos), 3 compran, 48 siguen en curso a la hora de la captura, y los 35 y los 4 de siempre.
- **El tope de 80 no alcanza.** 5 de los 9 días locales llegan a 80, del 11/10 al 15/10, con el máximo justo en 80. Los toques mueven 114 envíos al día siguiente.
- **Dos números dependen de un orden que la captura no trae:** los movidos por el tope de los toques 2 y 3. Los toques 2 y 3 no tienen acción del legado, así que a la misma hora se desempatan por la del primer toque, que es una suposición. Con ese desempate invertido dan 23 y 42 en vez de 24 y 41 (la suma, 65, no cambia), y nada más cambia. Lo declara `sensible_al_orden` en el fixture, y lo verifica `test_the_numbers_that_depend_on_an_unknown_order_are_declared`.
- **Los 49 primeros mensajes que mueve el tope salen tarde:** de 12 h 6 min a 23 h 53 min después de la hora (mediana, 19 h 43 min). Como máximo 49: si los toques 2 y 3 están sobreestimados (alguien contestó o se dio de baja antes), ocupan de más el tope, y entonces menos primeros mensajes se moverían.
- **En 1 de los 157, el toque 3 sale a la misma hora que el 2:** el 2 se demoró hasta pasar la hora del 3, que sigue programado desde el primer envío.
- **Con el plazo de hoy, 1 día, no sale ningún toque 2 ni 3:** los 207 carritos terminan `fuera_de_plazo`, porque todos los pasos comparten el vencimiento del primero (`20260813000100:380`, comparado en `:355`).
- **El archivo para el lienzo.** Con `--lienzo <archivo> --etiqueta <nombre>` escribe, por flujo y por versión, los conteos de cada paso con su cota y su índice dentro de `pasos`. Lleva solo conteos, sin uuid ni horas de nadie.

## El desempate y la compra

- **A la misma hora, el replay toma los recorridos como el despachador del legado:** por `coalesce(next_attempt_at, due_at)` y después por el id de la acción (`20260803000100:2621`). El `due_at` del recorrido es el que tendría su acción: su comienzo; el próximo inicio de la franja cuando la reevaluación lo difiere (`:1200`); el que reescribe F#11; y el de cada toque. Los rechazos del arranque (el control, los topes) no lo tocan, como en el legado. El id es el de la acción de la captura. En las siete propuestas, cada recorrido que arranca la tiene, así que el primer toque se desempata como el legado. Antes era `(hora, source_ref)`, una suposición.
- **La compra que corta un recorrido es una sola regla:** la primera de la misma persona, el mismo producto y la misma oferta (`offer_code is not distinct from`, `20260808000100:202`), aprobada desde 5 minutos antes del disparo (`20260808000200:70-77`). La que se aprobó antes de que llegara el disparo la vincula el legado al crear el caso, y el recorrido nace ganado. La compra entre dos toques cierra el recorrido a su hora. En la captura, ninguna de las dos reglas mueve un número; las fijan líneas de tiempo en `tests/test_flow_replay.py`.
- **F#11 con el control sin armar:** la repetición del pago fallido reescribe el `due_at` igual, porque la secuencia sigue `active` (`20260904000100:145` y `:149`); solo la pausan `pause` y `escalate` (`20260803000100:1213-1216`). El envío sale al rearmar. Tampoco mueve un número de la captura.

## La cota

Un número de un paso es `exacta` si ninguno de los envíos que cuenta depende de algo que la captura no trae, y `como_maximo` si alguno depende. Lo que la captura no trae es:
- **la baja o la respuesta** entre la hora que pasó y la simulada. La forma v2 no guarda bajas, y la historia sabe solo que no hubo ninguna hasta el envío observado. Un envío posterior a eso no sabe si la persona se dio de baja o contestó en el medio;
- **la intención:** la captura tiene solo su foto, así que para una hora nueva no se sabe si seguía viva;
- **la repetición sin contacto:** un disparo sin clave de persona no puede ver si es una repetición de otro;
- **el permiso de un arranque que el adaptador rechazó antes de mirar la audiencia:** con `pilot_scope_version_mismatch` y `pilot_runtime_not_armed`, el legado sale antes de verificarla (`20260930000300:270-300`). El replay lo deja arrancar con el permiso del formulario y lo marca `permiso`. En la captura no hay ninguno.

La compra no cuenta para la cota: la captura la trae, y la compra después de la captura la lleva un paso en curso, que no se cuenta como enviado.

Por eso, en la propuesta del carrito a 1 h, también el primer envío sale «como máximo»: sale una hora después que el envío observado, y en esa hora la baja no se sabe. Con la hora de hoy, el primer envío es `exacta`. En los tres toques, los toques 2 y 3 son siempre «como máximo», y el 1 también, por la hora.

## La CI

- **Las pruebas nuevas de la T3a** son 337, en `tests/test_flow_engine_definitions_fixture.py`, `_differential`, `_gate`, `_plan`, `_shared_cases`, `_simulate_att1`, `_simulate_script` y `tests/test_flow_replay.py`. En local tardan 11,7 s. Lo que sumó el commit 6 es sobre todo la prueba del desempate invertido, que corre las siete propuestas otra vez.
- **La suite entera,** en local en el commit 6: 6428 pasan en 73,94 s, con código de salida 0 y ninguna salteada.
- Todo cae en el paso de pytest del job `python-and-postgres`, que es el que marca el reloj de `verify`. La T3a no suma nada a PGlite. El diseño estimaba de 5 a 15 s; lo medido en local está en ese rango. El tiempo en la CI se mide en el PR.

## Cuando cambia el legado o la T1

La T3a se midió contra el legado de `c7fa8ef`. La sesión de los flujos comerciales lo va a cambiar, y nada de eso tiene que poner roja una prueba de la T3a:
- **El comando avisa.** Compara el sha256 de `tests/fixtures/flow_engine/legacy_recovery_cases.json` y el del archivo entero `tests/flow_store_translator.py` con los de `c7fa8ef`. Si alguno cambió, la cabecera dice «las definiciones de hoy son del 16/10; el legado cambió después», y el lienzo lleva `"legado_cambio": true` y ese `aviso`. El lienzo siempre lleva `"base": "c7fa8ef"`. Sale igual.
- **Las pruebas que leen un archivo de otro se saltean, con el motivo:** `pytest.skip`, que el resumen cuenta. Pasa con el traductor (el archivo entero: sumar OXXO alcanza; y ni se importa), con los casos generados (la columna `legado` de `gate_cases.json`), con las diferencias con nombre de la T1 (`att1_parity_named_differences.json`) y con la prueba de privacidad de la captura (`tests/test_flow_engine_capture_privacy.py`), cada uno con su sha256 de `c7fa8ef`.
- **Lo que no se pudo guardar así, y exige actualizar la T3a:** los constructores de las líneas de tiempo de la T1 (`tests/test_flow_parity.py`), que usan la diferencial, el replay y la simulación. Saltearlas con cualquier cambio dejaría sin probar casi todas las líneas de tiempo. Un cambio en esos constructores, o en la privacidad que valida `Capture.from_json`, pide correr las pruebas de la T3a y arreglarlas en el mismo PR. La diferencial, además, tiene que ponerse roja si cambia la semántica de `flow_parity.eligible_at` o de las cesiones: es lo que avisa que las dos puertas divergieron.

## Lo que no prueba

- **Nada sobre bajas ni respuestas,** que la forma v2 no trae: por eso los toques 2 y 3 son «como máximo».
- **La línea de tiempo contra el legado en PGlite.** Correr las mismas líneas de tiempo de la T3a contra la cadena real de migraciones toca `tests/sql/followup_engine/package.json`, que comparte la sesión comercial. Va en la T3b.
- **Nada en ATT1.** No se corrió ahí. La función de lectura de la historia (`read_flow_engine_history_v1`), la corrida en el VPS, la paridad continua y el conteo de lo en curso leído de la base son la T3b.
- **La regla de los toques es la del legado de hoy:** cada toque desde el primer envío y el vencimiento del primero para todos. Si la sesión de los flujos comerciales cambia esa regla, la simulación la sigue con un cambio chico.
- **Los toques 2 y 3 contra un legado que los haga:** en la ventana no hay ninguno, así que no hay contra qué compararlos.
- **Que la puerta del motor en SQL coincida con la gemela.** Eso es la T2, con `gate_cases.json`.
- **El primer contacto, el cupón ni la reactivación como flujos del motor.**
- **La deducción de la 1a,** que sigue siendo otra copia de la puerta hasta la migración.
- **Si el mensaje causó la compra.** El replay cuenta quién compró antes del mensaje y quién después; no atribuye.
- **La hora real del despachador.** El replay pone el envío en E; la T1 midió que el real sale de 4 a 145 s después, y eso no cambia ningún conteo de la ventana.
