# Contrato: el motor nativo de flujos (v2)

- **Estado:** propuesto el 2026-10-24, para construir contra él (el PR 1 del hito 4). No hay código que lo implemente: lo construyen los PR 2 (la base), 3 (el bridge) y 4 (el Automation Expert). Este documento fija la interfaz; no dice que nada esté desplegado, activado ni probado.
- **Fecha:** 2026-10-24
- **Contrato:** `flow-engine-v2`, versión `2.11.0` (la `2.1.0`, del 2026-10-24, suma la sección 13: lo que fija el PR 3, el bridge; la `2.2.0`, del mismo día, la sección 14: lo que fija el PR 4, el Automation Expert que crea flujos; la `2.3.0`, del 2026-10-25, la sección 15: lo que fija el PR 5, lo que el Automation Expert ve del motor nativo; la `2.4.0`, del 2026-10-26, la sección 16: las variables y los botones de la plantilla de un paso nativo; la `2.5.0`, del mismo día, la sección 17: la oferta del evento llega al chequeo del destinatario; la `2.6.0`, del 2026-10-27, la sección 18: las respuestas rápidas y la categoría según el permiso, y la sección 9 alineada con el código; la `2.7.0`, del 2026-10-28, la sección 19: el disparador `carrito`; la `2.8.0`, del mismo día, la sección 20: el motor se prende sin el carrito de varios toques, y la pausa al comprar se saltea solo para quien tomó el flujo nativo; la `2.9.0`, del 2026-10-30, la sección 21: con un flag, la pausa al comprar va después de la bienvenida nativa; la `2.10.0`, del mismo día, la sección 22: un carrito no abre un caso nativo y uno del legado para la misma persona y producto; la `2.11.0`, del mismo día, la sección 23: el disparador `pago_fallido`)
- **Decisión:** las siete del hito 4 (D1 a D7), aprobadas por Dan el 2026-10-24, en el roadmap del producto que se lleva fuera de este repo. Las que gobiernan este contrato: D2 (los flujos nuevos corren solo en el motor, sin legado), D4 («marcar como clienta» es la etiqueta `cliente` y una nota), D6 (un flujo nuevo se publica primero para la cohorte de prueba) y D1 (el legado no se mueve en este hito).
- **Alcance:** lo que la v1 no puede: un flujo que nace en el motor, sin `legacy_kind` ni scope del legado. Fija la ruta nativa, la bolsa propia (`flow_engine_pools`), la admisión genérica (`admit_flow_engine_event_v1`), el disparador `compra`, el paso `etiquetar`, el estado `cohorte` del control, el ítem `nuevo` del ciclo y el aviso `MOT-002`. La v1 ([flow-engine-v1.md](flow-engine-v1.md)) sigue entera para el carrito de hoy: este contrato la extiende y no cambia nada de lo que fija.
- **Citas:** `origin/main` en `89f0997` (1.17.1). Abreviaturas de las migraciones: `M-almacén` = `supabase/migrations/20261016000100_flow_change_store_v1.sql`, `M-rutas` = `20261021000200_flow_engine_routes_v1.sql`, `M-carrito` = `20261022000100_flow_engine_motor_carrito_v1.sql`, `M-lecturas` = `20261022000200_flow_engine_bridge_reads_v1.sql`. «El esquema» es [flow-definition-v1.md](flow-definition-v1.md), que sube a la 1.2 con este contrato; «el ciclo», [flow-change-cycle-v1.md](flow-change-cycle-v1.md), que sube a la 1.7.0.
- **Relacionados:** el esquema, el ciclo, [flow-engine-v1.md](flow-engine-v1.md), [automation-expert-read-v1.md](automation-expert-read-v1.md), [hotmart-purchase-approved-v1.md](hotmart-purchase-approved-v1.md).

## Para qué

Hoy el motor ejecuta un solo flujo, el carrito, y solo montado sobre el código viejo:
- la capacidad del motor es `{carrito}` (`_flow_engine_route_kinds`, `M-carrito:88-96`);
- toda ruta tiene un `legacy_kind` obligatorio (`M-rutas:105-106`) y nace en `legado` (`validate_flow_engine_route_insert`, `M-rutas:659-660`);
- la bolsa del motor tiene que ser el scope del legado: si la definición nombra otra, la admisión cae al legado con `ruta:bolsa_distinta_del_scope` (`M-carrito:524-528`), y el lock de la bolsa es la fila de `pilot_runtime_controls` de ese scope (`M-carrito:1107-1114`);
- el Automation Expert no puede crear un flujo: la base lo rechaza con `flow_creation_not_enabled` (`M-almacén:924-929`) y el validador también (`flow_validator.py:365-369` y `:973-974`).

Las dos pruebas del hito 4 no tienen por dónde correr así: la bienvenida después de la compra de ATT1 no tiene legado, y el carrito de Maribel es de una instancia nueva, sin historia. Este contrato fija cómo corre un flujo que nace en el motor.

## En corto

- **Un flujo nativo** tiene control, bolsa y ruta propios, y ningún scope del legado. Su ruta nace en `motor` y no se mueve: no hay legado al que volver. Su vuelta atrás es pausarlo o apagarlo.
- **La bolsa nativa** vive en `flow_engine_pools`, con su lock y su zona. **No puede llamarse como un scope del legado**, y un scope nuevo no puede llamarse como una bolsa nativa.
- **La admisión nativa** es una función nueva, `admit_flow_engine_event_v1`, al lado de la del carrito, que no se toca. Recibe un evento ya normalizado y arranca los flujos nativos que dispara.
- **El disparador `compra`** arranca un flujo después de que la compra cortó los casos abiertos. Con `permiso: compra` manda una sola plantilla de utilidad (regla 17).
- **El paso `etiquetar: cliente`** pone la etiqueta `cliente` y una nota privada en la conversación de Chatwoot (D4). No manda mensajes.
- **El estado `cohorte`** del control: el flujo corre solo para los contactos de su cohorte de prueba. Un flujo nuevo se publica en `cohorte`; pasarlo a todos es otra propuesta (D6).
- **El ítem `nuevo`** del ciclo registra el flujo, publica su v1, crea su bolsa y su ruta y lo deja en `cohorte`, todo en la misma transacción.
- **`MOT-002`** avisa en Slack cuando el motor no pudo arrancar un flujo por una falla suya. Hoy esos rechazos quedan en `flow_engine_start_rejections` y nadie los lee.
- **Lo de hoy no cambia:** el carrito de ATT1 sigue por su camino, y las lecturas de la v1 devuelven lo mismo que hoy, sin los flujos nativos.

## 1. Lo que no cambia de la v1

- **El camino del carrito.** `admit_flow_engine_hotmart_v1`, la caída al legado, `mark_flow_engine_request_started_v1` con la bolsa del scope y `flow_engine_execution_context_v1` siguen como los fija la v1 (secciones 7 y 8). El PR 2 suma funciones al lado y solo cambia una existente cuando un flujo nativo lo pide, con una rama que con un recorrido del legado o del carrito da falso.
- **Las lecturas de la v1 no muestran los flujos nativos.** `read_flow_engine_v1`, `read_flow_engine_history_v1` y `read_flow_engine_routes_v1` excluyen toda ruta nativa y todo flujo en `cohorte`. Por qué:
  - el JSON Schema de `read_flow_engine_v1` exige un `legacy_kind` del enum y un `state` sin `cohorte` (`tests/fixtures/flow_engine/read_flow_engine_v1.schema.json`), y la simulación rechaza una lectura que no sea `flow-engine-v1` con capacidades `v1` (`flow_change_simulation.py:60-61`, `:88` y `:184`);
  - el bridge lee las rutas con `legacy_kind` obligatorio (`supabase.py:6157`). Una ruta con `legacy_kind` nulo haría fallar la lectura entera, y con la lectura caída el lote deja de usar las rutas del carrito (`FlowEngineRoutes.refresh_if_due`, `flow_engine_runtime.py:95-102`).
- Los flujos nativos se leen por las funciones nuevas de este contrato (secciones 4.4 y 8). El PR 4 decide si el Automation Expert pasa a una lectura v2 o suma la nativa a la de hoy.

## 2. La ruta nativa

- **La fila** es la de `flow_engine_routes` con `executor = 'motor'`, `legacy_kind` nulo y `legacy_scope_key` nulo. El PR 2 relaja el `not null` de `legacy_kind` (`M-rutas:105`) con un `check`: `legacy_kind is null` solo con `executor = 'motor'` y `generation = 0`. El `check` de hoy (`flow_engine_routes_scope_by_kind`, `M-rutas:112-113`) sigue valiendo para toda ruta con `legacy_kind`.
- **Nace en `motor` y con `generation = 0`.** La crea solo la publicación de un ítem `nuevo` (sección 7). El disparador de hoy, que exige nacer en `legado` (`M-rutas:659-660`), deja pasar ese caso y ningún otro.
- **No se mueve.** `operator_route_flow_v1` sobre una ruta nativa devuelve `{"ok": false, "reason_code": "flow_route_native_cannot_move"}`. No hay legado al que volver: la vuelta atrás es pausarlo o apagarlo.
- **No necesita lugar en el manifiesto.** La regla 16 del esquema (el manifiesto como techo) no aplica a un flujo nativo y sigue valiendo para los del legado. El techo de un flujo nativo es la lista de eventos de la instancia (`EVENTS`, `instance_manifest.py:77`, que ya trae `compra`), como pide el esquema («El techo de la instancia»).
- **El tope por persona.** El disparador `pilot_proactive_contact_caps_no_engine_route` (`M-carrito:204-226`) frena cargar un renglón de tope con cualquier ruta del tenant en `motor`, y una ruta nativa es una ruta en `motor`. Es lo que se quiere en este hito: el tope en los dos caminos es el PR 6 (D5). Por lo mismo, publicar un ítem `nuevo` en una instancia con renglón de tope devuelve `flow_route_person_cap_present`, el mismo código que mover a `motor` (v1, 3.2).

## 3. La bolsa nativa: `flow_engine_pools`

**La tabla.** Una fila por bolsa nativa: `pool` (la clave, con la forma de `topes.bolsa` del esquema: `^[a-z][a-z0-9_-]{1,39}$`), `tenant_ref`, `ally_ref`, `timezone` (la zona en la que se cuenta el día), `created_at` y `created_by_proposal_id`. Sin permisos para la API, como las otras del motor (`M-rutas:194`).

- **Para qué la fila.** Hoy el lock de la bolsa y su zona salen de `pilot_runtime_controls` (`M-carrito:1107-1114`) y de la política del caso (`M-carrito:1186-1189`). Una bolsa nativa no tiene scope: la fila de `flow_engine_pools` es su lock (`FOR UPDATE`, igual que hoy) y su zona.
- **Cuándo nace.** La crea la publicación del primer flujo nativo que la nombra. Los flujos que la nombran después la comparten, y tienen que tener la misma zona (regla 8 del esquema).
- **La zona.** La de `horario.zona` del flujo que la crea. Con `horario: siempre` no hay zona en la definición: va la de la instancia, que el bridge pasa al publicar como pasa hoy el catálogo de plantillas (ciclo, «La publicación»). La zona de una bolsa no cambia nunca, como la de un scope (`pilot_scope_timezone_must_remain_constant`, `20260810000100:87-100`).
- **El conteo.** Sin cambios: los topes de la definición fijada se cuentan sobre la bolsa por los dos caminos (`M-carrito:1182-1203`). Con la regla que sigue, la parte del legado de una bolsa nativa siempre suma cero.
- **⭐ Una bolsa nativa no se llama como un scope del legado.** Los topes del legado se cuentan por `scope_key` (`pilot_runtime_controls`, clave primaria en `20260810000100:128`). Si una bolsa nativa se llamara como un scope, sumaría los envíos del legado sin que nadie lo vea. La base lo frena de los dos lados:
  - crear una bolsa con el nombre de un `scope_key` de `pilot_runtime_controls` o de `pilot_scope_versions`: `flow_engine_pool_is_legacy_scope`;
  - crear un scope con el nombre de una bolsa nativa: `pilot_scope_is_native_pool` (un disparador de las dos tablas del scope).
  - El validador lo mira también en la regla 8, cuando el mundo que le da el ciclo trae los scopes de la instancia (PR 4). La guarda que cuenta es la de la base.
- **El arranque de un envío nativo.** `mark_flow_engine_request_started_v1` (v1, 7.2) mantiene su firma y su forma de salida. Para un recorrido nativo toma el lock de la fila de `flow_engine_pools` en lugar de la de `pilot_runtime_controls`, y cuenta el día en la zona de la bolsa. Sin fila, `rechazado` con `flow_engine_pool_unknown`, el código de hoy. **A medir en el PR 2:** qué otra parte de esa función y de `_flow_engine_authorize_start` (`M-carrito:1030-1231`) asume un `legacy_scope_key` no nulo.

## 4. La admisión nativa: `admit_flow_engine_event_v1`

```sql
admit_flow_engine_event_v1(p_instance jsonb, p_flow_event jsonb)
returns table (flow_id uuid, outcome text, reason text, recovery_case_id uuid,
    followup_sequence_id uuid, scheduled_action_id uuid, created boolean)
```

- `security definer`, `search_path` fijo; la ejecuta solo `service_role`. Al lado de `admit_flow_engine_hotmart_v1`, que no se toca.
- `p_instance` es la marca de la instancia (`_flow_store_instance`, `M-almacén:513`), como en la v1.

### 4.1 El evento normalizado

`p_flow_event` es un objeto con estas claves exactas, ninguna más. Cualquier otra forma: `22023 invalid_flow_engine_event`.

| Clave | Qué es |
|---|---|
| `fuente` | `hotmart` en la v2 |
| `tipo` | el disparador del esquema: `compra` y, desde la `2.7.0`, `carrito` (`_flow_engine_native_trigger`, `20261028000200`, sección 19) |
| `evento_id` | el id del webhook persistido (uuid): la clave de la repetición |
| `producto_ref` | el producto de la instancia al que pertenece el evento. Lo resuelve el bridge por el ID de producto y la oferta, nunca por el nombre (choque 7 del diseño del hito 4) |
| `oferta` | el código de oferta, o `null` |
| `contacto_id` | el contacto ya resuelto (uuid) |
| `momento` | el momento del disparo, UTC: en Hotmart, `creation_date` |
| `chatwoot` | `{account_id, inbox_id}` de la instancia |
| `destino` | el teléfono de destino, como hoy `p_destination_phone` en la v1 (7.1). Nunca sale en una respuesta ni en un log |

### 4.2 Qué hace

1. Busca los flujos nativos de la instancia, de ese `producto_ref`, cuya versión activa tiene `dispara.evento` igual a `tipo`. Sin ninguno, devuelve una fila con `flow_id` nulo y `outcome = 'sin_flujo'`.
2. Para cada uno, en una subtransacción propia (la falla de uno no frena a los otros), mira en este orden: el estado del control (sección 6), la cohorte, el permiso, la repetición, la prioridad entre flujos del motor (`relations_between_engine_flows_only`, `flow_engine.py:141`) y si ya tiene un recorrido en curso.
3. Si arranca, crea el caso, la secuencia y la acción sobre el sustrato de hoy, con `policy_key = flujo:<flow_id>`, como el carrito del motor (`M-carrito:589` y `:708-716`), y la fila de `flow_engine_runs` con la versión fijada, su `definition_sha256`, la bolsa nativa y `legacy_kind` y `legacy_scope_key` nulos. El despacho, la reevaluación y el cierre del intento son los de hoy.
4. **No hay caída al legado.** Lo que el motor no puede termina en `motor_no_arranca` con su fila en `flow_engine_start_rejections` (`phase admision`). Una excepción de la subtransacción queda como `error_interno:<sqlstate>`. Es lo que avisa `MOT-002` (sección 8).

### 4.3 Las salidas

| `outcome` | Qué pasó |
|---|---|
| `motor_arranco` | el recorrido arrancó; `created = false` en la repetición del mismo `evento_id` |
| `motor_no_arranca` | no arrancó; `reason` dice por qué |
| `sin_flujo` | ningún flujo nativo dispara con ese evento (`flow_id` nulo) |

Los `reason` de `motor_no_arranca`: los de la v1 (7.1) que tienen sentido sin legado (`flujo_pausado`, `baja`, `ya_no_entra`, `no_entra:permiso`, `ya_en_curso`, `no_arranca_mientras:<flujo>`) y tres nuevos: `fuera_de_cohorte` (el flujo está en `cohorte` y el contacto no es de ella), `ya_disparado` (la repetición `otra_despues_de: nunca` ya se usó con esa persona) y `error_interno:<sqlstate>`. Con el flujo apagado no hay fila: el disparo ni se registra (esquema, «La versión, el control y el techo»). `fuera_de_cohorte` tampoco deja fila: es lo esperado mientras el flujo está en prueba.

### 4.4 Lo que el PR 2 tiene que medir antes de escribir

Son las partes del sustrato de hoy que asumen un carrito o un scope. No se suponen: se leen en el código y se dicen en el PR.
- **El planificador.** El carrito del motor usa `plan_cart_recovery_with_identity` (`M-carrito:708-716`), que es del carrito. La compra pide un planificador sin abandono ni oferta obligatoria.
- **El binding del piloto.** La admisión del carrito escribe `pilot_recovery_case_bindings` con el scope del caso (`M-carrito:720-728`), y lo leen la autorización, la guarda y la adopción de la respuesta (`20261001000400`). Un caso nativo no tiene scope. El PR 2 lista cada lector del binding y qué hace con un caso sin él.
- **La forma de los pasos.** La política compilada exige un `legacy_kind` (`compile_policy_legacy_kind_without_step`, `M-rutas:449-451`). Para un flujo nativo, la forma de los pasos sale del disparador. Se construyó la de `compra`, `nativo:compra`, de un envío (`20261024000200`), y desde la `2.7.0` la de `carrito`, `nativo:carrito`, también de un envío (sección 19). La de `carrito` con A1, A2 y Z (la de `_flow_engine_legacy_steps('carrito')`, `M-rutas:324-334`) no existe en el nativo: los varios toques son el PR 6 del hito 4 de la versión 1.
- **El cupón personal** de un carrito nativo: el worker del cupón lo ata al caso (`recovery_coupons`). Medir que no dependa del scope.
- **La compra como permiso.** Con `permiso: compra`, la evidencia es la compra aprobada de ese producto. Medir cómo se llega del comprador a su contacto y a su conversación: el evento de compra trae el teléfono y el mail (`HotmartPurchaseData`, `hotmart.py:65-77`), y no trae el nombre ni el producto como texto.

## 5. El disparador `compra`

- **El orden.** Hoy la compra aprobada entra al worker y llama a `apply_hotmart_purchase_approved`, que cierra los casos abiertos y corta los toques (`worker.py:3389-3423`). La admisión nativa va **después** de esa llamada y solo si terminó bien: primero la compra frena la recuperación, después arranca la activación. La prueba es un caso con A2 pendiente y una compra: A2 cancelado, bienvenida programada (choque 2 del diseño del hito 4).
- **Con una venta sin caso del recuperador** también dispara: el esquema lo pide («Qué lo dispara», `compra`).
- **Las variables:** `nombre` y `producto`. `HotmartPurchaseData` no trae el nombre del comprador (`hotmart.py:65-77`); extraerlo del evento (`data.buyer.name`) es del PR 3. Hasta entonces, la regla 14 frena `nombre` con la compra, como hoy (esquema, regla 14).
- **El permiso.** `formulario` es el valor por defecto (esquema, «Quién entra»). `compra` exige el OK del cliente en la propuesta que lo publica por primera vez (`ok_del_cliente`, `M-almacén:644-664`; ciclo, «Elecciones técnicas»). En ATT1 ese OK ya está dado (decisión de Dan del 2026-10-15, en el ciclo) y la activación va con `compra` (D3).
- **Los productos.** Una compra del bump o del upsell no es una compra del principal: el bridge resuelve `producto_ref` por el ID de producto, y un producto que la instancia no declara no dispara nada.
- **Sin el bridge nuevo no corre.** Un caso nativo se reconoce por su política `flujo:<flow_id>` y lo despacha solo el bridge con `FLOW_ENGINE_ENABLED` (v1, sección 8). La imagen de hoy lo rechaza con `flow_engine_needs_engine_image` (v1, 7.2): no manda nada.

## 6. El estado `cohorte`

- **Qué es.** Un cuarto estado del control (`flow_controls.state`, hoy `apagado`, `prendido` o `pausado`, `M-almacén:147`): el flujo arranca recorridos solo para los contactos de su cohorte de prueba. A todos los demás, `fuera_de_cohorte`, sin fila.
- **Solo para flujos nativos.** La cohorte de un flujo del legado sigue viviendo en su scope (`pilot_cohort_memberships`, `20260810000100:211`), y el carrito de ATT1 no cambia (choque 5 del diseño del hito 4). Un flujo con ruta del legado no puede pasar a `cohorte`: `flow_control_cohort_needs_native_route`.
- **Los miembros.** Una tabla nueva, `flow_cohort_members`: `flow_id`, `contact_id`, quién lo agregó y cuándo. Sin permisos para la API. El teléfono no se guarda: se guarda el contacto ya resuelto. Los carga el operador con su comando (sección 7.3), con el teléfono que elige Dan.
- **Las transiciones** (las de hoy en `validate_flow_control_change`, `M-almacén:292-298`, más estas):
  - `apagado → cohorte`: la publicación de un ítem `nuevo`;
  - `cohorte → prendido`: un ítem `prender`, con su propuesta y su sí (D6);
  - `cohorte → pausado` (pausar siempre se puede, ciclo, «Prender, pausar y apagar») y `cohorte → apagado`;
  - `pausado → cohorte`: un ítem `prender` con `a: cohorte`.
- **Lo en curso.** Pasar de `cohorte` a `prendido` no toca los recorridos de la cohorte: siguen con su versión. Pasar a `apagado` los termina, como hoy.

## 7. El ítem `nuevo` del ciclo

### 7.1 La forma

```json
{
  "tipo": "nuevo",
  "producto": "att1",
  "flujo": "activacion",
  "definicion": { "esquema": "setter-flujo/v1", "estado": "borrador", "...": "sin version" },
  "prender": "cohorte",
  "ok_del_cliente": {"quien": "…", "medio": "…", "fecha": "AAAA-MM-DD"}
}
```

- **No lleva `flow_id`**: el flujo todavía no existe. Las otras cuatro clases de ítem lo exigen (`_flow_change_items_are_valid`, `M-almacén:612-620`); `nuevo` es la excepción, y su identidad es (`producto`, `flujo`).
- **`prender` vale solo `cohorte`** (D6). Con otro valor: `new_flow_must_start_in_cohort`.
- **`ok_del_cliente`** con la misma forma que en una `version` (`M-almacén:644-664`), obligatorio si la definición lleva `permiso: compra`.
- **No lleva `en_curso`**: no hay nada en curso.
- Una propuesta puede llevar un `nuevo` y ningún otro ítem sobre ese flujo.

### 7.2 Qué hace cada paso del ciclo

- **Guardar el borrador.** Corren las reglas 1 a 10. La base de un `nuevo` es «no hay un flujo con esa clave en ese producto»: si lo hay, `flow_already_registered`, el código de `operator_register_flow_v1` (`M-almacén:1514-1555`).
- **Quién propone.** El Automation Expert y el operador. Para un `nuevo`, la base deja de devolver `flow_creation_not_enabled` (`M-almacén:924-929`), y el validador también (`flow_validator.py:973-974`). Una `version` sobre un flujo registrado sin versión sigue bloqueada para el agente: ese es el camino de los flujos del legado, que registra el operador.
- **Validar.** Las reglas 1 a 17, con las capacidades del motor nativo para la 14 (sección 9) y sin la 16. La vista previa dice que el flujo nace en `cohorte`, cuántos contactos tiene la cohorte (cero, si nadie cargó el teléfono todavía) y la simulación del flujo nuevo: cuántos eventos de su disparador llegaron en los últimos 7 días y cuántos mensajes habría mandado. La lectura que da ese conteo la fija el PR 4 en la sección 14 (2.2.0): la simulación de un `nuevo` sale `built` y el operador publica sin `--sin-simulacion`.
- **Aprobar.** Igual que hoy, con la huella. El botón firmado de Slack no existe todavía: aprueba el operador por terminal.
- **Publicar**, bajo el mismo lock y en una sola transacción, o nada:
  1. revalida que la clave siga libre en el producto;
  2. registra el control, como `operator_register_flow_v1`, pero atribuido a la propuesta;
  3. publica la v1 en `flow_definitions`;
  4. crea la bolsa si no existe (sección 3), con su guarda contra los scopes;
  5. crea la ruta nativa (sección 2);
  6. pasa el control a `cohorte` con la v1 activa;
  7. deja los eventos de la propuesta y uno nuevo, `flow_created`, en `flow_change_events`.

### 7.3 Lo que suma el comando del operador

`scripts/flow_change_operator.py` suma `cohorte <producto>/<flujo> agregar|sacar`, con el mismo código de confirmación que el resto. El teléfono se lee de la terminal (como el código), se resuelve a su contacto en la base y no se imprime ni queda en el log. `proponer` acepta un archivo con un ítem `nuevo`, como hoy acepta la v1 de un flujo (ciclo, 1.2.0).

## 8. `MOT-002`: el motor no pudo arrancar un flujo

- **El hueco.** Los rechazos del motor quedan en `flow_engine_start_rejections` (`M-rutas:156-166`, escritos por `_flow_engine_record_start_rejection`, `M-carrito:335-348`), y ninguna lectura del bridge ni del Automation Expert los lee. El único aviso del motor es `MOT-001`, que es de la caída al legado (`flow_engine_runtime.py:204`; `src/slack_correlation/catalog.py:158`). Un flujo nativo no tiene legado al que caer: si no arranca, hoy no se entera nadie.
- **La lectura.** Una función nueva, para `service_role`, en la forma de `read_flow_engine_divergences_v1` (v1, 7.4):

```sql
read_flow_engine_start_rejections_v1(p_instance jsonb, p_only_unnotified boolean, p_limit integer) returns jsonb
mark_flow_engine_start_rejection_notified_v1(p_flow_id uuid, p_phase text, p_reason text,
    p_notified_count integer, p_notified_at timestamptz) returns boolean
```

  - Suma por (`flow_id`, `phase`, `reason`): la tabla guarda una fila por evento (clave primaria `flow_id, source_ref, reason`, `M-rutas:165`), y el aviso es por flujo y motivo.
  - Devuelve `{"unnotified": <int>, "rejections": [{"flow_key", "product_ref", "version", "phase", "reason", "count", "first_at", "last_at", "notified_count"}]}`, sin `source_ref` ni nada de la persona.
  - Lo avisado se guarda en una tabla nueva, `flow_engine_rejection_notices` (`flow_id`, `phase`, `reason`, `notified_count`, `notified_at`). `flow_engine_start_rejections` no cambia.
- **Qué avisa.** Solo los motivos en los que falló el motor: `error_interno:<sqlstate>`, `flow_engine_pool_unknown` y los de la política compilada. No avisa los que dicen que la persona no entra (`baja`, `no_entra:permiso`, `ya_en_curso`, `ya_no_entra`, `ya_disparado`, `no_arranca_mientras:*`, `flujo_pausado`): son el flujo funcionando. Vale para todos los flujos del motor, nativos y el carrito, porque la tabla es la misma.
- **Cuándo.** A la primera vez de cada (flujo, fase, motivo), y de nuevo cuando la cuenta cruza 10 y 100. Lo lee el mismo bucle de 60 s de `MOT-001` (v1, sección 8).
- **El aviso.** Código `MOT-002`, `p2`, «El motor no pudo arrancar un flujo», con `flow_key`, `reason` (recortado a 80) y `count`. `dedupe_key` = `sha256` de `flow-engine-start-rejection-v1\0<flow_id>\0<phase>\0<reason>\0<escalón>`. Marca lo avisado solo cuando el conector lo admitió, como `MOT-001`.
- **El conector es otra imagen.** Hasta que el conector de la instancia traiga `MOT-002` en su catálogo, lo rechaza. En ATT1 se resuelve pasándola a su conector propio (D7), con el «Ok subida» de Dan.

## 9. Lo que levanta la regla 14 para un flujo nativo

**Corregido en la `2.6.0`:** este contrato decía que el motor nativo tenía las capacidades de la v1 más las de abajo. El código no lo construyó así: el nativo tiene **sus propias capacidades**, más chicas, en `ENGINE_CAPABILITIES_NATIVE` (`flow_engine.py`), y no hereda las de la v1 (`ENGINE_CAPABILITIES_V1`: carrito, pago fallido, intención, tres envíos, esperas entre envíos, `cuando`/`precio`/`vence`, el botón `checkout` y las salidas `compra` y `contesta`). Lo que la regla 14 deja pasar en un flujo nativo (`flow_engine.native_not_supported`, la misma lista que mira la base en `_flow_engine_native_unsupported`):
- el disparador `compra` y, desde la `2.7.0`, `carrito` (sección 19), y ningún otro;
- el permiso que le corresponde al disparador (`motor_nativo:permiso`): `compra` con la compra, con la regla 17 intacta (una sola plantilla de utilidad por camino), y `formulario` con el carrito;
- los pasos `esperar` (solo antes del envío), `plantilla` y `etiquetar: cliente` (esquema 1.2); un solo envío por camino y ninguna espera después de él;
- las variables que el paso declara, de entre `nombre` y `producto` (sección 16); `nombre` sale del evento desde el PR 3;
- ningún `boton` declarado por el paso. La plantilla puede traer botones de URL fija (sección 16) y de respuesta rápida (sección 18): no llevan parámetro;
- la salida `compra` (la que pide la regla 4 a todo evento que no es la compra), y ninguna otra;
- la prioridad solo entre flujos del motor (`no_arranca_mientras`);
- `repeticion` por `persona` o `persona_y_oferta`, con `otra_despues_de: nunca` o una duración: en el carrito del motor v1 cae al legado (`ruta:repeticion_no_soportada`, `M-carrito:520-522`), y en un flujo nativo se ejecuta.

`horario: siempre` y `sin_tope` no hace falta levantarlos: `siempre` ya compila (`M-rutas:461`) y un tope que no es un número no se cuenta (`M-carrito:1191-1203`).

`read_flow_engine_v1` sigue diciendo `capabilities: "v1"` para los flujos del legado. Las capacidades `v2` valen solo para un flujo con ruta nativa.

## 10. Los códigos nuevos

Todos en forma `^[a-z][a-z0-9_:]{1,79}$`. Los errores de entrada siguen la convención de la v1: `22023`, `P0002` y `55000`.

| Código | Dónde | Qué quiere decir |
|---|---|---|
| `invalid_flow_engine_event` | 4.1 | el evento normalizado no tiene la forma |
| `fuera_de_cohorte` | 4.3 | el flujo está en `cohorte` y el contacto no es de ella |
| `ya_disparado` | 4.3 | la repetición `nunca` ya se usó con esa persona |
| `error_interno:<sqlstate>` | 4.3 | el motor falló al admitir: lo avisa `MOT-002` |
| `flow_route_native_cannot_move` | 2 | se intentó mover una ruta nativa |
| `flow_engine_pool_is_legacy_scope` | 3 | la bolsa nativa se llama como un scope del legado |
| `pilot_scope_is_native_pool` | 3 | el scope nuevo se llama como una bolsa nativa |
| `flow_control_cohort_needs_native_route` | 6 | `cohorte` sobre un flujo con ruta del legado |
| `new_flow_must_start_in_cohort` | 7.1 | un `nuevo` con `prender` distinto de `cohorte` |
| `simulation_new_flow_not_built` | 7.2 | la simulación de un `nuevo` todavía no existe |

## 11. Las pruebas que fija el contrato

- **PR 2 (PGlite):** un flujo nativo disparado por una compra, de punta a punta: `nuevo` publicado en `cohorte`, la admisión con un contacto de la cohorte arranca y con otro devuelve `fuera_de_cohorte`, el arranque toma el lock de la bolsa nativa. Rojos a propósito: una bolsa que se llama como un scope (`flow_engine_pool_is_legacy_scope`), un scope que se llama como una bolsa (`pilot_scope_is_native_pool`) y `mover` sobre una ruta nativa. **La cadena de hoy sin cambios**, y las lecturas de la v1 iguales con un flujo nativo presente.
- **PR 3 (pytest):** el fixture de un `PURCHASE_APPROVED` real capturado, sin datos personales; el caso «A2 pendiente + compra»; `etiquetar` contra el fixture de Chatwoot; `MOT-002` con un `error_interno` sembrado; sin flujos nativos, cero llamadas nuevas.
- **PR 4:** la conversación de prueba «creá la bienvenida después de la compra» contra el doble, que termina en un `nuevo` publicado en `cohorte`.

## 12. Lo que no entra en la v2

- Las condiciones (`condicion`), `conversar` y `derivar`: siguen bloqueados por la regla 14.
- Los disparadores de la ficha y `medio_de_pago`.
- Pasar al motor los flujos que hoy corren en el legado (el pago rechazado, la ficha y el primer contacto): son los PR 7 a 9, después de prender a Maribel (D1).
- El tope por persona contado en los dos caminos y el arreglo de la adopción: el PR 6 (D5).
- `en_curso: cancelar` y `migrar` (el ciclo).
- Las ventas por flujo y la biblioteca del Automation Expert: el PR 5 (sección 15).
- Retirar un flujo para siempre.

## Para decidir antes del PR 2

Cada una con el valor por defecto con el que se construye si nadie dice otra cosa.

1. **Quién carga el teléfono de la cohorte y cuántos entran.** Por defecto: lo carga Dan con `cohorte agregar` del comando del operador, hasta 5 contactos por flujo.
2. **¿`MOT-002` avisa también cuando un flujo se queda sin cupo (`tope_del_flujo`)?** Por defecto: no. Es el flujo funcionando, y se ve preguntándole al Automation Expert.
3. **El carrito de Maribel como flujo nativo con la forma del de ATT1** (A1, A2 y Z, el cupón en los mismos pasos). Por defecto: sí. Cambiar la forma de los toques sigue siendo un cambio de código, como en ATT1.

## 13. Lo que fija el PR 3 (el bridge del motor nativo, `2.1.0`)

Escrito al construir el PR 3, antes de cualquier despliegue. Migración `20261024000500_flow_engine_native_bridge_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado.

- **Cuándo llama el bridge a la admisión nativa.** Con `FLOW_ENGINE_ENABLED`, el lote lee `read_flow_engine_native_flows_v1(p_instance)` a lo sumo cada 60 s (`{"flows": [{flow_id, product_ref, flow_key, state, active_version, trigger}]}`, los flujos con ruta nativa que no están apagados). Una compra va a la admisión solo si hay un flujo de ese producto, con disparador `compra`, en `prendido` o `cohorte`. Sin ninguno, cero llamadas nuevas por evento. `pausado` no cuenta: no llama y no deja la fila de `flujo_pausado`. Si la lectura falla, vale la última buena.
- **El orden de la compra y la cola.** El worker anota la compra con `enqueue_flow_engine_native_event_v1(p_instance, p_webhook_event_id)` **antes** de aplicarla. Después llama a `apply_hotmart_purchase_approved`, que corta lo abierto y marca el evento, y recién entonces a la admisión. Si la admisión no contesta, la compra sigue en `flow_engine_native_event_queue`. `read_flow_engine_native_event_queue_v1(p_instance, p_limit)` la devuelve (cada 60 s, solo si el evento ya no está `received`, en las últimas 72 h y con menos de 20 intentos), y la vuelta siguiente la admite sin aplicarla de nuevo. `finish_flow_engine_native_event_v1(p_webhook_event_id, p_done, p_now)` la cierra o le suma un intento. Si la anotación falla, el evento queda `received` y el lote lo reintenta entero.
- **El contacto y el evento normalizado (4.1).** El bridge resuelve el contacto por mail y por las formas del teléfono, como el carrito. Si no existe, lo crea con el nombre del comprador (`data.buyer.name`, que `HotmartPurchaseData` ahora extrae) y no toca el estado del evento. `producto_ref` sale del ID de producto, nunca del nombre. `momento` es la `creation_date` del webhook, y `destino`, la identidad de WhatsApp que el contacto ya tenga para ese móvil.
- **El envío del paso nativo.** Una acción con ancla `flow_engine_event` sale solo en el modo de plantilla directa, con la plantilla de su versión fijada (`flow_engine_execution_context_v1`). Las condiciones: `nombre` y `producto` a lo sumo, sin botón declarado, y categoría `UTILITY` en el catálogo de Chatwoot (regla 17; desde la `2.6.0`, la categoría sale del permiso, sección 18). Sin el flag cierra con `flow_engine_native_needs_engine`. En el modo Hermes cierra antes del agente, con `flow_engine_native_needs_direct_template`, así que nunca pide una derivación (`request_human_handoff` exige el binding del piloto). Una forma que el bridge no sabe armar cierra con `flow_engine_step_unsupported`.
- **El paso `etiquetar: cliente`.** Después de que la plantilla quedó aceptada, `flow_engine_native_step_tail_v1(p_action_id)` devuelve `{flow_id, version, etiquetar, producto, momento}` para la acción de un recorrido nativo, o null para cualquier otra. Con `cliente`, el bridge suma la etiqueta por un macro que solo hace `add_label` de `cliente` (`CHATWOOT_CLIENT_LABEL_MACRO_ID`, verificado antes de usarlo como `ProductLabeler`) y deja una nota privada con el producto y la fecha, con el marcador `[supportmagician-flujo:cliente:<action_id>]` (una sola nota por acción). Sin el macro, solo la nota. Falla blanda: el envío ya salió.
- **La respuesta a una plantilla nativa.** `admit_portable_inbound_commercial_case_v1` adopta también la conversación de una plantilla cuyo caso tiene su fila en `flow_engine_native_runs`. Así la respuesta a la bienvenida la atiende el agente. Para cualquier otro caso la adopción es la de antes.
- **`MOT-002` (sección 8).** La lectura suma `flow_id` (lo pide la marca) y `step` (1, 10 o 100). Avisa también `motor_nativo:*`: una versión publicada que el motor no ejecuta es una falla del motor. El comando lleva `component = flow_engine:<flow_key>`, `reason_code`, `count` y un `event_id` UUID derivado del `dedupe_key`, sin ningún id de evento ni de persona. La marca devuelve `true` solo si subió el escalón.
- **`/ready`** suma, con el flag, cuatro claves en memoria: `flow_engine_native_read`, `flow_engine_native_on`, `flow_engine_start_rejections_unnotified` y `flow_engine_native_queue_pending`.

## 14. Lo que fija el PR 4 (el Automation Expert crea flujos, `2.2.0`)

Escrito al construir el PR 4, antes de cualquier despliegue. Migración `20261024000600_flow_change_new_flow_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. El ítem `nuevo` en el ciclo está en el ciclo, «Lo que fija el PR 4 (1.7.0)».

- **La simulación de un flujo nuevo.** Una lectura nueva, para `service_role` y para el rol del agente (`automation_expert_change`), como las lecturas del motor:

```sql
read_flow_engine_native_trigger_counts_v1(p_instance jsonb, p_product_ref text, p_trigger text,
    p_since timestamptz) returns jsonb
```

  - Devuelve `{"contract": "flow-engine-v2", "trigger", "product_ref", "since", "until", "events", "people"}`: las compras aprobadas del producto (por el ID de Hotmart de sus bindings) que llegaron desde `p_since`, contadas por transacción, y de cuántas personas (el mail, o el teléfono si no hay mail). No trae nada de una persona.
  - `p_trigger` vale solo `compra` (`22023 flow_engine_native_trigger_invalid`). La ventana es la de la historia del motor: hasta 8 días (`flow_engine_history_window_too_wide`).
  - Quien valida (la ruta del agente o el comando del operador) la pide con los últimos 7 días, y `build_new_flow_simulation` arma `{"status": "built", "summary": {flujo_nuevo, disparador, desde, hasta, disparos, personas, mensajes_si_fuera_para_todos, nace_en: "cohorte"}, "summary_sha256", "cambios": []}`. Una propuesta con un `nuevo` y otro ítem sale `failed` con `simulation_new_flow_mixed`. Una respuesta que no tiene esa forma da `simulation_new_flow_counts_invalid`. Sin la lectura (una base sin la migración) da `flow_engine_unavailable`.
  - Desde E-241 (2026-10-31) la misma lectura simula también un `prender` o un `apagar` de un flujo nativo, con el evento de su versión activa (`build_native_flow_simulation`; una `version` o un `volver` siguen por la historia, D6 de E-245): la historia del motor exige la ruta del carrito y un flujo nativo no corre sobre ella. El detalle está en el ciclo (`flow-change-cycle-v1.md`), «Crear un flujo: el ítem `nuevo` y la cohorte (1.7.0)».
- **La regla 14 de un flujo nativo** en el validador es `flow_engine.native_not_supported`, con `ENGINE_CAPABILITIES_NATIVE`: el disparador `compra`, los pasos `esperar`, `plantilla` y `etiquetar`, un envío, `nombre` y `producto`, sin botón y sin salidas. Suma lo que mira `_flow_engine_native_unsupported` en la base. Los códigos están en el ciclo (1.7.0).
- **La vista previa de un `nuevo`:** `cambios` lleva `tipo: nuevo`, `nace_en: cohorte` y el diff contra nada; `a_quien` es `cohorte`; `volver_atras`, `{a_version: null, como: pausar_o_apagar}`.

## 15. Lo que fija el PR 5 (lo que el Automation Expert ve del motor nativo, `2.3.0`)

Escrito al construir el PR 5, antes de cualquier despliegue. Migración `20261025000100_automation_expert_native_reads_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. El detalle de las lecturas y de la regla de las ventas está en [automation-expert-read-v1.md](automation-expert-read-v1.md), «Lo que suma el motor nativo (1.2.0)», y el de `prender` con `a: cohorte`, en el ciclo (1.8.0).

- **Las lecturas de la v1 siguen sin mostrar los flujos nativos** (sección 1). El Automation Expert los ve por tres lecturas nuevas, de solo `service_role`: `read_automation_expert_native_flows_v1`, `read_automation_expert_native_history_v1` y `read_flow_sales_v1`. La sección 1 dejaba abierto si el agente pasaba a una lectura v2 o sumaba la nativa a la de hoy: suma la nativa a la de hoy, y el contrato de lectura sigue en el major 1.
- **Las ventas por flujo** cuentan sobre `flow_engine_runs`, así que valen para todo flujo del motor: los nativos y el carrito con su ruta en motor. Lo que corrió en el legado no entra.
- **Volver de `pausado` a `cohorte`** (sección 6, la última transición) es un ítem `prender` con `a: cohorte`. La base ya dejaba la transición; lo nuevo es que el ítem la pueda pedir.
- **El anclaje de una persona** a la instancia, en la historia y en la búsqueda del agente, suma un recorrido nativo de la bandeja: un caso nativo no tiene el binding del piloto, que era uno de los caminos.

## 16. Las variables y los botones de la plantilla de un paso nativo (E-115, `2.4.0`)

Escrito al construir el cambio, el 2026-10-26, antes de cualquier despliegue. Migración `20261026000100_flow_engine_native_template_shape_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. Lo motivó la bienvenida de ATT1 (`att1_bienvenida_01`, capturada en `tests/fixtures/chatwoot_message_templates_inbox_11_bienvenida_20261026.json`): un encabezado de texto fijo, un cuerpo con un solo `{{1}}` (el producto) y un botón de URL fija.

- **Las variables del paso** (cambia lo que fijaban las secciones 13 y 14). El paso `plantilla` de un flujo nativo manda las variables que declara en `variables`, en su orden, de entre `nombre` y `producto`: al menos una y ninguna repetida. El validador las compara contra los marcadores de la plantilla aprobada (regla 13, `template_variable_count_mismatch`, que en un flujo nativo dice en la razón cuántos marcadores tiene). Otra lista es `engine_not_supported:variables_differ` (`flow_engine.native_variables_fit`). El esquema sigue completando `["nombre", "producto"]` si el paso no las escribe.
- **En la base**, la forma `nativo:compra` (`_flow_engine_native_steps`) suma `variables_de: ["nombre", "producto"]`, y el compilador (`_flow_engine_compile_policy_core`), con un paso de una forma que trae `variables_de`, acepta esa lista en lugar de exigir la exacta. Las formas del legado no traen `variables_de` y compilan igual (`validate_flow_engine_native_template_shape.mjs` corre `compile_cases.json` antes y después).
- **Los botones.** El paso nativo sigue sin declarar `boton`. La plantilla puede traer botones de URL fija (sin `{{n}}` en la URL): el envío no lleva parámetros de botón, solo los del cuerpo (`parse_approved_template(parameterless_buttons_only=True)`, que hasta la `2.6.0` se llamaba `static_url_buttons_only`). Un botón de URL con variable cierra el envío con `approved_template_mismatch` / `url_button_with_variable`, y uno de otro tipo, con `approved_template_mismatch` / `unsupported_button`. Las respuestas rápidas pasan desde la `2.6.0` (sección 18). El validador frena lo mismo antes (`template_button_mismatch`, con el motivo en la razón). En el legado nada cambia: un botón de URL que no es el declarado sigue siendo `unsupported_button`.
- **El encabezado de texto fijo** se acepta como antes (un `HEADER` de texto sin variables no lleva parámetros).

## 17. La oferta del evento llega al chequeo del destinatario (E-122, `2.5.0`)

Escrito al construir el cambio, el 2026-10-26, antes de cualquier despliegue. Migración `20261026000200_flow_engine_native_offer_in_context_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. Lo motivó la compra de prueba de ATT1 del 2026-10-26 (capturada en `tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json`): el recorrido nativo abrió y el envío se cerró antes del pedido con `followup_recipient_not_allowlisted`.

- **El chequeo no cambia.** El despachador valida al destinatario con `_is_authorized_followup_recipient`: sin JID fijo, el producto del contexto tiene que ser el del binding y la oferta del contexto, una de sus ofertas aceptadas.
- **El caso nativo sigue con `offer_code` nulo** (sección 4, la admisión): los planificadores buscan el caso abierto de la persona, el producto y la oferta por esa columna, y un carrito o un pago no tienen que caer en un caso nativo.
- **La oferta del evento va al `context` del caso**, como `flow_engine_offer` (la misma que se guarda en `flow_engine_native_runs`), y `get_followup_execution_context` la devuelve como `offer_code` para un caso con política `flujo:<flow_id>` sin `offer_code`. Para todo otro caso devuelve `recovery_cases.offer_code`, como antes.
- **Lo que se sigue frenando:** una compra por una oferta que la instancia no acepta, o sin oferta, cierra el envío igual que antes. Un caso nativo admitido antes de la migración no tiene la oferta en el `context` y tampoco sale.
- **La prueba** es la compra capturada de punta a punta: la base (`validate_flow_engine_native_offer_in_context.mjs`, con el rojo de la definición anterior) y el bridge con el parser y el chequeo reales (`tests/test_flow_engine_native_purchase_e2e.py`).

## 18. Las respuestas rápidas y la categoría según el permiso (E-158, `2.6.0`)

Escrito al construir el cambio, el 2026-10-27, antes de cualquier despliegue. Sin migración. Describe la interfaz; no dice que nada esté desplegado ni activado. Es el PR 1 del hito 4 de la versión 1 (el mapa del 2026-10-28 en la operación del producto, fuera del repo). Lo motivó la plantilla de los tres flujos de Maribel, `maribel_retomar_proceso_01`: un cuerpo con `{{1}}` (el nombre) y `{{2}}` (el producto), dos respuestas rápidas («Retomar proceso» y «No mas mensajes») y categoría `MARKETING` (Meta la recategorizó; se subió como `UTILITY`). Está reconstruida en `tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json` desde lo que se subió a Meta; falta capturarla del catálogo del inbox 13.

- **Los botones de respuesta rápida.** Una plantilla de un paso nativo puede traer respuestas rápidas, solas o junto a botones de URL fija, en cualquier orden, dentro de un solo grupo de botones. No llevan parámetro: el envío manda solo los del cuerpo. El validador (regla 13, `_native_button_problem`) y el envío (`parse_approved_template(parameterless_buttons_only=True)`) aceptan lo mismo. Siguen frenados un botón de URL con variable (`template_button_mismatch` antes, `url_button_with_variable` en el envío), un botón de otro tipo y dos grupos de botones. En el legado nada cambia.
- **Lo que pasa con la respuesta a un botón.** Lo de antes: la respuesta a una plantilla nativa la adopta el agente (sección 13), y «No mas mensajes» es una baja por el texto (`opt_out.py`, que normaliza los acentos).
- **La categoría según el permiso.** Con `permiso: compra`, la plantilla tiene que ser `UTILITY` (regla 17, sin cambios). Con cualquier otro permiso se acepta también `MARKETING`. En el validador ya era así: solo la 17 mira la categoría. En el envío, `native_flows.native_template_categories(permiso)` da las categorías que acepta `parse_approved_template` (que ahora recibe una categoría o un conjunto), y `template_params.category` lleva la del catálogo.
- **Lo que no cambia en producción.** La base solo admite flujos nativos de la compra con permiso compra (sección 9), y el contexto del paso (`flow_engine_execution_context_v1`) no trae el permiso, así que el envío usa `NATIVE_PURCHASE_TEMPLATE_CATEGORIES` (las de `native_template_categories("compra")`) y sigue exigiendo `UTILITY`. Una plantilla `MARKETING` en un flujo nativo de hoy la frena la 17 al validar y el envío al mandar (`approved_template_mismatch` / `category_mismatch`). El permiso del paso tiene que llegar al envío con el primer disparador nativo que no sea la compra (el PR 2 del hito 4, `carrito`). Llega desde la `2.7.0` (`entry_permission`, sección 19).
- **Sin flujos nativos, ninguna llamada nueva:** el cambio vive dentro del envío de un paso nativo, que solo corre para una acción con ancla `flow_engine_event`.
- **El Automation Expert** lo sabe por su SOUL y por la página `chatwoot-whatsapp` de su biblioteca: los botones de respuesta rápida salen solos, y con la compra la plantilla tiene que ser de utilidad.
- **Las pruebas:** `tests/test_flow_engine_native_quick_replies.py` (la plantilla de Maribel en el validador, en el parser y en el envío contra el doble de Chatwoot; en rojo, la `MARKETING` con permiso compra y un botón de URL con variable) y `tests/test_flow_engine_native_template_shape.py` (la bienvenida de ATT1, igual que antes).

## 19. El disparador `carrito` (E-161, `2.7.0`)

Escrito al construir el cambio, el 2026-10-28, antes de cualquier despliegue. Migración `20261028000200_flow_engine_native_cart_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. Es el PR 2 del hito 4 de la versión 1 (el mapa del 2026-10-28 en la operación del producto, fuera del repo). Lo motivó el carrito de un toque de Maribel (`[flujos] carrito` de su `instancia.toml`: gracia 0, un envío, `maribel_retomar_proceso_01`, que es `MARKETING`). Reemplaza el punto 3 de «Para decidir antes del PR 2»: el carrito nativo nace de un toque; los varios toques con cupón son el PR 6.

- **El disparador.** `_flow_engine_native_trigger` devuelve `compra` o `carrito` (`dispara` exacto: `{"evento": "carrito"}`), y `_flow_engine_native_steps('nativo:carrito')` es la misma forma de un envío que la de la compra, con `variables_de: ["nombre", "producto"]`. `flow_engine_native_runs.trigger_event` admite `carrito`.
- **El permiso.** Con el carrito, `entra.permiso` tiene que ser `formulario`, y con la compra, `compra` (`_flow_engine_native_unsupported`; en el validador, `NATIVE_PERMISSION_BY_TRIGGER`). La evidencia de `formulario` es la intención con la que la admisión portable de hoy correlacionó **este** carrito (`hotmart_purchase_intent_correlations`, `outcome = resolved`), de la instancia, del producto del evento (su binding portable) y de su oferta, con el criterio de `_portable_consented_intent_reason`: no se busca por contacto. Si no alcanza, el recorrido no arranca con `baja`, `ya_no_entra` o `no_entra:permiso` (los motivos del carrito del motor, 7.1 de la v1), con su fila de rechazo. La intención y el envío del formulario quedan en dos columnas nuevas de `flow_engine_native_runs` (`audience_purchase_intent_id`, `audience_precheckout_submission_id`, las dos o ninguna, y solo con el carrito), y la autorización `cart_recovery` se concede con esa evidencia (`precheckout_whatsapp_consent`), como el pago rechazado portable.
- **La puerta antes del envío.** `_flow_engine_authorize_native_tail`, después de la cohorte y antes de los topes, vuelve a mirar la intención (`FOR SHARE`) con el mismo criterio, y rechaza con el mismo motivo.
- **La forma, también en la base.** Con el carrito, una definición del legado (A1, A2 y Z) ya pasa el disparador y el permiso. `_flow_engine_native_unsupported` suma `motor_nativo:forma`: un solo paso `plantilla`, solo `esperar`, `plantilla` y `etiquetar`, y `etiquetar` solo con la compra (la regla 5, también en la base: marcar como clienta a quien no compró). Vale para los dos disparadores; la bienvenida de ATT1 la cumple.
- **La salida `compra`.** El caso nace con `abandonment_event_id` = el carrito, que tiene su binding portable, así que la compra de la persona (de cualquier oferta del producto, por mail o teléfono) corta el paso antes del envío en la reevaluación de hoy (`_portable_case_purchase_stop_reason`, `purchase_detected_any_offer`). El caso sigue con `offer_code` nulo y la oferta en el `context` (sección 17).
- **El permiso llega al envío.** `flow_engine_execution_context_v1` se vuelve a crear con una columna más al final, `entry_permission` (el `entra.permiso` de la versión fijada del recorrido), solo para `service_role`. El envío del paso nativo acepta las categorías de ese permiso (`native_step_categories`: `UTILITY` con `compra`, también `MARKETING` con `formulario`). Sin la columna (un bridge nuevo contra una base sin la migración) usa las de la compra: nunca abre `MARKETING` por falta del dato. Un bridge de antes lee la fila por nombre y no mira la columna.
- **El orden en el bridge.** Con `FLOW_ENGINE_ENABLED`, si `read_flow_engine_native_flows_v1` trae un flujo del carrito de ese producto en `prendido` o `cohorte`, el resolver manda el carrito **primero** a `admit_flow_engine_event_v1` (el evento normalizado de 4.1, con `tipo: carrito`, `momento` = la `creation_date` del carrito y `destino` = la identidad de WhatsApp que el carrito ya usa). Un evento abre un solo caso (`recovery_cases.abandonment_event_id` es único), así que:
  - `motor_arranco`: el caso es del nativo; el legado y el motor del carrito no planifican, y el evento queda `processed`;
  - `motor_no_arranca` con un motivo de la persona (`no_entra:permiso`, `baja`, `ya_en_curso`…): el evento queda `failed` con `flow_engine_no_start:<motivo>`, sin el legado, como el carrito del motor;
  - `sin_flujo`, `fuera_de_cohorte`, `flujo_pausado`, `flujo_apagado`, o un rechazo de la forma (`22023`): el legado (o el motor del carrito, con su ruta en `motor`) planifica exactamente como hoy;
  - la admisión no contesta: el evento queda `received` y el lote lo reintenta; el cuarto intento seguido le pregunta igual (una respuesta perdida pudo dejar el caso creado, y la repetición lo devuelve) y, si tampoco contesta, planifica el legado (`native_admission_failures`, aparte del contador del motor del carrito).
  Sin un flujo nativo del carrito, ninguna llamada nueva. Por esto, durante la cohorte, al teléfono de prueba le llega **un** mensaje (el nativo), no dos: el hueco de la convivencia del mapa no aparece con el carrito.
- **La simulación.** `read_flow_engine_native_trigger_counts_v1` acepta `p_trigger = carrito` y cuenta los `PURCHASE_OUT_OF_SHOPPING_CART` del producto (las personas, por mail o por `buyer.phone`). Otro disparador sigue siendo `flow_engine_native_trigger_invalid`.
- **Lo que no cambia.** La admisión portable del carrito (la que correlaciona) y la del motor v1 no se tocan; tampoco las lecturas de la v1 (sección 1). Un flujo nativo del carrito necesita que la instancia admita el carrito por el camino portable (`PORTABLE_HOTMART_RECOVERY_ENABLED`): sin la correlación no hay permiso.
- **Las pruebas:** `validate_flow_engine_native_cart.mjs` (PGlite, la cadena entera: el formulario y el carrito por las admisiones portables, la admisión en la cohorte, fuera de ella y sin formulario, el despacho con la plantilla de Maribel y `entry_permission`, el rojo de la intención caída antes del arranque, la compra que lo corta, la bienvenida igual, el carrito sin flujo nativo y el planificador del legado igual; escribe `tests/fixtures/flow_engine/native_cart_execution_context_rows.json`) y `tests/test_flow_engine_native_cart.py` (la regla 14, el validador del ciclo, el envío `MARKETING` con el parser real, y el orden del resolver contra el doble de la base).

## 20. El motor sin el carrito de varios toques, y la pausa al comprar (E-165, `2.8.0`)

Escrito al construir el cambio, el 2026-10-28, antes de cualquier despliegue. Sin migración. Describe la interfaz; no dice que nada esté desplegado ni activado. Lo motivó la bienvenida nativa de Maribel (su PR #27): con el código de la 1.20.5, prender `FLOW_ENGINE_ENABLED` exigía A1 con cupón y A2/Z, que le cambian el carrito de un toque, y un flujo nativo de compra en `cohorte` salteaba la pausa al comprar para **todos** los compradores.

- **El arranque.** `FLOW_ENGINE_ENABLED` exige el manifiesto, la frontera del piloto, el worker de resolución, el despachador y la plantilla directa (`FLOW_ENGINE_REQUIRED_FLAGS`, `src/bridge/app.py`). A1 con cupón y A2/Z (`FLOW_ENGINE_CART_ROUTE_FLAGS`) dejan de frenarlo: son de la ruta del carrito del legado en `motor` (v1, sección 8), no de los flujos nativos, que mandan la plantilla de su versión fijada.
- **El carrito de una ruta en `motor` sin A1 ni A2/Z.** El runtime guarda `cart_touches_enabled` (los dos flags prendidos). Sin ellos, el resolver llama a `admit_flow_engine_hotmart_v1` con `p_force_legacy = true` (log `flow_engine_cart_forced_legacy reason=cart_touches_disabled`): el legado lo planifica con el carrito de la instancia, la base deja la divergencia `transitorio:forzado_al_legado` y `MOT-001` la avisa. No cuenta como admisión sin respuesta. El motor cerraría A2 como no soportado; así el carrito sale igual y el error de operación se ve. Con la ruta en `legado` o sin ruta, la llamada es la de hoy. El carrito nativo (sección 19) no cambia: no usa A1 ni A2/Z.
- **`/ready`** suma, con el flag, `flow_engine_cart_touches` (`enabled` o `disabled`), en memoria. El runbook de `mover` (v1, 3.2; ciclo, «Mover la ruta de un flujo») lo mira antes de pasar el carrito a `motor`: el comando del operador no lee los flags del bridge.
- **La pausa al comprar** (`purchase-agent-pause-v1`, E-100). La admisión nativa va **antes** de la pausa, y la pausa se saltea solo si un flujo nativo arrancó para ese comprador (`admit_native_purchase` devuelve `admitida`, alguna fila `motor_arranco`; log `reason=native_flow_started`). Con `sin_arranque` (`motor_no_arranca`, por ejemplo `fuera_de_cohorte` o `ya_disparado`, o `sin_flujo`) y con `descartada:<motivo>` (sin teléfono, identidad ambigua, un rechazo permanente), se pausa como sin flujo nativo. Si la admisión no contesta (`reintentar`, log `reason=native_admission_pending`), no se pausa todavía; la vuelta de la cola que obtiene respuesta pausa si ningún flujo arrancó. `reason=native_flow_on` deja de existir.
- **Lo que no cambia.** ATT1: con sus tres toques, `cart_touches_enabled` es `true` y la ruta del carrito sigue como hoy; su pausa al comprar está apagada (`PURCHASE_AGENT_PAUSE_ENABLED=false`, la `1.19.0`), así que el orden nuevo no le cambia nada. Con la pausa prendida y el flujo nativo para todos, todo comprador con teléfono que el flujo toma sigue sin pausa, como hoy. Sin flujo nativo, la pausa es la de E-100.
- **El hueco.** Una compra cuya admisión nativa nunca contesta (la cola la deja de traer a las 72 h o a los 20 intentos, sección 13) no se pausa. Queda en la cola con sus intentos.
- **Las pruebas:** `tests/test_flow_engine_bridge_settings.py` (el arranque sin A1 ni A2/Z, y con los tres toques), `tests/test_flow_engine_resolution.py` (`test_f_`: la ruta en `legado` igual que hoy y la de `motor` forzada al legado, con las filas de `bridge_rpc_rows.json` de PGlite), `tests/test_purchase_agent_pause.py` y `tests/test_purchase_agent_pause_native.py` (el comprador que tomó el flujo no se pausa; fuera de la cohorte, `ya_disparado`, `sin_flujo` y sin teléfono sí; la admisión sin respuesta espera y la cola pausa o no según la respuesta; sobre la base doble de `test_flow_engine_native_bridge`).

## 21. La pausa al comprar, después de la bienvenida nativa (E-190, `2.9.0`)

Escrito al construir el cambio, el 2026-10-30, antes de cualquier despliegue. Sin migración. Describe la interfaz; no dice que nada esté desplegado ni activado. Cierra lo que la sección 20 deja escrito en «Lo que no cambia»: con la pausa prendida y el flujo nativo de la compra para todos, ningún comprador que el flujo toma queda pausado. En Maribel (#27) la pausa es la réplica de «Ventas maribel» de GHL: desde el `prender` de la bienvenida, el agente le seguiría contestando a cada persona que compra.

- **El flag.** `PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED` (por defecto `false`, por runtime). Exige `PURCHASE_AGENT_PAUSE_ENABLED`, `FLOW_ENGINE_ENABLED` y `DURABLE_DISPATCHER_ENABLED`; sin ellos el bridge no arranca. Apagado, el despachador no recibe la pausa y no hace ninguna llamada nueva: todo como la `2.8.0`.
- **Dónde.** En el despachador, después del envío aceptado de una acción nativa (`accepted_by_chatwoot`) y de su paso `etiquetar` (sección 13), si la versión fijada entra con `permiso: compra` (`entry_permission` del contexto del paso, desde `20261028000200`). Pausa la conversación a la que fue la bienvenida, con la etiqueta, el team y la nota de `purchase-agent-pause-v1`; el marcador lleva el id de la acción. La admisión nativa y el worker no cambian: siguen salteando la pausa para quien tomó el flujo (`reason=native_flow_started`).
- **Por qué ahí.** La sección 9 fija un solo envío por camino y ninguna espera después: con la bienvenida aceptada no queda nada que mandar, y la pausa no frena el flujo. Antes del envío la frenaría (una conversación pausada bloquea todo envío proactivo).
- **Todo cierre definitivo (revisión E-196).** También pausa con el envío entregado sin aceptación confirmada (la conversación del envío) y con el paso rechazado, sin resultado claro o cerrado antes del pedido sin más reintentos (las conversaciones del comprador, buscadas como al comprar). Un fallo que se reintenta no pausa. La pausa reintenta dos veces lo transitorio y, si no puede, deja una nota privada para el equipo (`purchase-agent-pause-v1`).
- **Lo que no pausa.** Un paso del carrito nativo (`permiso: formulario`), un contexto sin `entry_permission` (una base sin la migración de E-161) y un paso cuyo contexto nunca se leyó.
- **Lo que no cambia.** ATT1 y Johanna, con el flag apagado: nada. La pausa del worker para quien el flujo no tomó (fuera de la cohorte, `ya_disparado`, `sin_flujo`) sigue igual.
- **Las pruebas:** `tests/test_purchase_agent_pause_after_native_welcome.py`, con la bienvenida real de Maribel (`chatwoot_message_templates_inbox_13_bienvenida_20261030.json`), su compra aprobada anonimizada (`hotmart_purchase_approved_maribel_20261029.json`) y el manifiesto de Maribel; el despacho es el doble de `test_flow_engine_native_dispatch`.

## 22. El doble caso del carrito: el nativo y el legado (E-204, `2.10.0`)

Escrito al construir el cambio, el 2026-10-30, antes de cualquier despliegue. Migración `20261030000100_flow_engine_native_legacy_double_case_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. Es M1a, el paso P12 del mapa de M16 (la migración de los flujos de Maribel al motor nativo, en la operación del producto, fuera del repo): tiene que estar en producción antes de cualquier cohorte del carrito nativo.

- **El hueco.** El caso de un carrito nativo nace con `offer_code` nulo (sección 17) y cada lado buscaba solo lo suyo: la admisión nativa miraba la repetición y la prioridad en `flow_engine_runs`, y el planificador del legado (`plan_cart_recovery`) busca el caso abierto por persona, producto y **oferta**. Con un caso abierto de un lado y un carrito nuevo de la misma persona, el otro lado abría un segundo caso y mandaba un segundo mensaje. Para el **mismo** evento no había doble caso (`evento_con_otro_flujo`, sección 19).
- **(a) La admisión nativa.** `_flow_engine_admit_native_flow`, con el disparador `carrito`, después de la cohorte y antes del permiso: si la persona tiene abierto (`grace_period`, `active` o `paused`, lo mismo que el planificador del legado llama abierto) un caso que no es nativo (sin fila en `flow_engine_native_runs`), de la fuente del evento y del producto del flujo (el `hotmart_product_id` de su binding), **de cualquier oferta**, no arranca: `motor_no_arranca` con `caso_del_legado_en_curso` y su fila de rechazo (la ve el Automation Expert; no es alertable, no es una falla del motor). Fuera de la cohorte sigue siendo `fuera_de_cohorte`. La compra (la bienvenida) no cambia.
- **Lo que hace el bridge con (a).** `caso_del_legado_en_curso` está en `NATIVE_CART_UNCLAIMED_REASONS` (`src/bridge/native_flows.py`): el carrito vuelve al legado, que hace **exactamente lo de hoy**. Con la misma oferta, el planificador del piloto aborta con `pilot_case_binding_conflict` y el evento queda `failed` con ese motivo, sin mensaje (no suma el carrito al caso: la transacción se deshace). Con otra oferta, el legado planifica su caso como hoy. Es el default de la D4 del mapa: la otra opción, dejar el evento `failed flow_engine_no_start:caso_del_legado_en_curso`, da los mismos mensajes (cero) pero un motivo y un conteo distintos de los de hoy para el mismo carrito.
- **(b) El planificador del legado.** `plan_cart_recovery`, después de la repetición del mismo evento y del lock del contacto, y antes de buscar el caso por oferta: si la persona tiene abierto un caso de un **carrito nativo** (`policy_key` `flujo:<id>` y `context.flow_engine_trigger = carrito`, que escribe solo la admisión nativa) de la misma fuente y producto, de cualquier oferta, aborta con `55000` `pilot_scope_rejected` / `flujo_nativo_en_curso`. Es la vuelta atrás del nativo: pausado o apagado, el carrito siguiente vuelve al legado mientras el recorrido nativo sigue abierto. El evento queda `failed` con `pilot_scope_rejected:flujo_nativo_en_curso` (`PilotPlanRejectedError`), sin mensaje; la caída al legado del motor v1 lo devuelve como motivo. No suma el carrito al caso nativo: le pondría el binding del piloto. Mira `recovery_cases` y no `flow_engine_native_runs` porque `plan_cart_recovery` es security invoker y `service_role` no tiene grants sobre esa tabla (Johanna lo llama sin el piloto).
- **El orden de los locks.** `admit_flow_engine_event_v1` ya bloquea el evento y el contacto (`FOR UPDATE`) antes de la admisión, en el mismo orden que el planificador del legado (evento, contacto, caso): dos carritos de la misma persona, uno por cada lado, se serializan y el segundo ve el caso del primero.
- **El primer contacto del legado.** Un caso nativo del carrito lo corta como hoy lo corta uno del legado: `_portable_precheckout_stop_reason` da `superseded_by_provider_event` con la intención en `confirmed_abandonment` o con cualquier caso de Hotmart abierto del producto, y el caso nativo cumple las dos (nace con `source` del evento y el producto del binding).
- **Los cortes de un recorrido nativo con la acción pendiente.** En la base: la compra (`purchase_detected_any_offer`, sección 19), la baja del contacto (la reevaluación cancela con `contact_blocked`) y la baja de Chatwoot (el arranque rechaza con `baja`). La toma humana, la derivación y la pausa por compra de un **primer** toque no se ven en la base (el caso nativo no tiene conversación hasta el envío): las corta el envío, como al carrito del legado, con la guarda G3 (`contact_active_conversation`) y el reuso de la conversación abierta (`contact_conversation_with_team`), en `ChatwootMessageSender.send_first_touch`, que el despachador usa también para una acción nativa.
- **El cupón después de la bienvenida (hueco 18 del mapa).** Con `PAYMENT_LINK_COUPON_RECOVERY_ONLY=true`, el 10 % va en el link del agente solo si la conversación tuvo un toque nuestro de recuperación (`payment_link_coupon.conversation_had_recovery_touch`, por las marcas de `content_attributes`). La bienvenida nativa sale como cualquier primer toque, con `recovery_first_touch_hash`, así que un comprador que salía de la pausa recibía el link con el cupón (medido con el despacho real: `coupon_applied: true`, `offDiscount=PROTOCOLO10%2B`). Ahora el despachador, para una acción nativa con `entry_permission = compra`, pide `purchase_welcome` al sender, y `ChatwootClient.send_first_message` suma `flow_engine_purchase_welcome: true` al lado de `recovery_first_touch_hash` (que se queda: lo usan la validación del envío, `daily_feedback_export.py` y `business_auto_reply.py`). Un mensaje con esa marca (exactamente `true`) no cuenta como toque de recuperación. El carrito nativo (`formulario`), los primeros toques del legado y un paso sin `entry_permission` no la llevan y siguen como hoy. La bienvenida que ya salió antes de este cambio no tiene la marca y sigue dando el cupón.
- **Lo que no cambia.** Sin un flujo nativo del carrito no hay casos nativos del carrito: (b) nunca da verdadero y (a) nunca corre. ATT1 (la bienvenida nativa de la compra y el carrito del legado) y Johanna no cambian: con la bienvenida abierta, el carrito del legado de esa persona planifica igual que antes. El motivo nuevo del legado (`flujo_nativo_en_curso`) no está en `LEGACY_REASON_TO_MOTIVO` (`flow_engine.py`): la réplica del legado lo lee sin motivo, como un código que el contrato de lectura no nombra.
- **Las pruebas:** `validate_flow_engine_native_legacy_double_case.mjs` (PGlite, el mundo de ATT1 con el carrito nativo de un toque: la forma de la migración, (a) con la misma oferta, con otra, con el caso pausado y cerrado, y el rojo con la admisión de antes; (b) con las dos ofertas y el rojo con el planificador de antes; ATT1 igual con las dos definiciones; el corte del primer contacto aislado al caso nativo; las dos bajas; escribe `tests/fixtures/flow_engine/native_legacy_double_case_rows.json`), `tests/test_flow_engine_native_legacy_double_case.py` (el resolver con esas filas: (a) va al legado exactamente como hoy, con el rojo sin el motivo; (b) termina el evento sin mensaje) `tests/test_flow_engine_native_send_cuts.py` (los cortes del envío de una acción nativa, con el despachador real y las capturas de Maribel: G3, la conversación con el equipo en sus tres formas, el caso sano y el reuso, cada uno con su variante con la guarda apagada) y `tests/test_purchase_welcome_coupon.py` (solo la bienvenida lleva la marca; con ella el link sale a precio completo, y el carrito nativo y el del legado siguen con el cupón).

## 23. El disparador `pago_fallido` (E-207, `2.11.0`)

Escrito al construir el cambio, el 2026-10-30, antes de cualquier despliegue. Migración `20261030000200_flow_engine_native_payment_failure_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. Es M1b, el paso P18 del mapa de M16 (fuera del repo). Lo motivó el pago rechazado de Maribel, que hoy corre por el legado con su scope propio (`PAYMENT_FAILURE_OWN_POLICY_ENABLED`, `maribel-pago-rechazado` v1 y la política `maribel-pago-rechazado-un-toque` v1, medidos en su base el 2026-10-30: gracia 15 min, 09:00-21:00 los siete días en CDMX, vence a 1 día, un mensaje, 20 por día y 1.000 en total, audiencia `consented_intent`, la plantilla de `WABA_PAYMENT_FAILURE_TEMPLATE_NAME`, `maribel_retomar_proceso_01`).

- **El disparador.** `_flow_engine_native_trigger` devuelve también `pago_fallido` (`dispara` exacto: `{"evento": "pago_fallido"}`), y `_flow_engine_native_steps('nativo:pago_fallido')` es la forma de un envío de la compra y del carrito. `admit_flow_engine_event_v1` acepta `tipo: pago_fallido` en el evento normalizado (4.1), y el evento persistido tiene que ser un `PURCHASE_CANCELED` de Hotmart del producto. `flow_engine_native_runs.trigger_event` admite `pago_fallido`, y las dos columnas de la intención son obligatorias también con el pago rechazado.
- **El permiso.** `formulario`, como el carrito (`_flow_engine_native_unsupported` y `NATIVE_PERMISSION_BY_TRIGGER`; la regla 5 deja `etiquetar` solo con la compra). La evidencia es la intención con la que la admisión portable de hoy correlacionó **este** rechazo (`commercial_ally_payment_failure_details` con `correlation_outcome = resolved`), con los controles de `plan_portable_payment_failure_recovery` en el mismo orden: la procedencia del binding portable (instancia y oferta del evento), la intención de ese funnel, producto y oferta, con el mismo móvil que el destino y en los `contact_points` del contacto, y el consentimiento de `_portable_consented_intent_reason` (`_flow_engine_native_payment_failure_audience`, de nadie de la API). Si no alcanza, `no_entra:permiso`, `baja` o `ya_no_entra`. Un `PURCHASE_CANCELED` que no es un rechazo (sin motivo y con un medio que no es tarjeta ni PayPal: un pendiente que se canceló, `payment_failure_refusal_kind = not_a_refusal`) da `no_es_un_rechazo`. La autorización `cart_recovery` se concede con la evidencia del formulario (`precheckout_whatsapp_consent`), y la puerta antes del envío vuelve a mirar la intención, como con el carrito.
- **Dos rechazos seguidos.** Quien reintenta la tarjeta deja varios rechazos en minutos (en Maribel, dos de la misma persona con 24 segundos de diferencia, el 2026-10-28; el legado mandó un solo mensaje). El legado manda uno solo, el del último rechazo, a la gracia contada desde ese rechazo (`payment_failure_variants`, `20261019000200`). El nativo hace lo mismo: un rechazo que llega con un recorrido del mismo flujo en curso de la persona da `ya_en_curso` (no abre otro), corre el primer envío pendiente a `momento + la gracia` del rechazo nuevo, nunca antes de lo que ya tenía, con el caso `FOR UPDATE` (el orden del legado: evento, contacto, caso), y deja el rechazo en `recovery_case_events` del caso (`payment_failure`). El bridge cierra ese evento `processed` (`NATIVE_PAYMENT_FAILURE_ABSORBED`), como el legado, y no `failed`. Lo mismo con un rechazo dentro de `otra_despues_de` de un recorrido que ya terminó (`ya_disparado`; en Maribel, las 72 h): queda en `recovery_case_events` del caso más nuevo, sin mensaje. Solo con el pago rechazado. No sube la versión del caso: una acción ya reservada sale con su hora.
- **La repetición del legado, en la definición.** El legado tiene un caso del pago rechazado por persona y producto (de cualquier oferta), y un rechazo dentro de las 72 h de un caso que ya mandó su mensaje, compró o tomó una persona se suma a ese caso sin otro mensaje. En el nativo eso se escribe `repeticion: {una_por: persona, otra_despues_de: 72h}`; la base no obliga esos valores (un flujo con `persona_y_oferta` o `0m` manda en casos donde el legado no manda), así que escribirlos es parte de migrar con los valores de hoy, y cambiarlos es un cambio de criterio que propone el AE. Diferencia que queda: el nativo cuenta las 72 h desde cualquier recorrido anterior del flujo; el legado, solo desde un caso `sequence_exhausted`, `won` o `escalated` (uno que terminó sin mandar, por ejemplo `unreachable`, el legado lo vuelve a intentar con el rechazo siguiente).
- **El doble caso, la regla de la sección 22 en las dos direcciones.**
  - **(a) La admisión nativa.** Con el pago rechazado, si la persona tiene un caso del pago rechazado del legado (`context.trigger_kind = payment_failure`) del producto, de cualquier oferta, que el planificador del legado tomaría (abierto, o cerrado con mensaje, compra o persona dentro de las 72 h), no arranca: `caso_del_legado_en_curso`, en `NATIVE_CART_UNCLAIMED_REASONS`. El rechazo vuelve al legado, que hace lo de hoy (lo suma a su caso). Un caso del carrito del legado no lo frena: el legado del pago rechazado tampoco lo mira.
  - **(b) El legado del pago rechazado.** `plan_payment_failure_recovery` (debajo de `plan_portable_payment_failure_recovery`), después de la repetición del mismo evento y del lock del contacto: con un caso de un pago rechazado **nativo** de la persona (`policy_key` `flujo:<id>`, `context.flow_engine_trigger = pago_fallido`) de la fuente y el producto, de cualquier oferta, que habría tomado, aborta con `55000` `pilot_scope_rejected` / `flujo_nativo_en_curso` y no escribe nada. Es la vuelta atrás del nativo pausado o apagado.
  - **El carrito del legado.** El bloque (b) de la sección 22 en `plan_cart_recovery` mira también un pago rechazado nativo abierto. Hoy un pago rechazado del legado abierto frena el carrito de la misma oferta (el carrito cae en su caso y aborta con `pilot_case_binding_conflict`); el caso nativo nace con `offer_code` nulo y el carrito no lo veía. Con el pago nativo y el carrito todavía en el legado, la persona habría recibido los dos mensajes.
- **D6: el carrito nativo detrás del pago.** Un carrito nativo puede declarar `prioridad: {no_arranca_mientras: [<la clave del pago nativo>]}` (en Maribel, `pago_rechazado`, una clave nativa propia: nunca `pago_fallido`, que es del legado y rompe la lectura del AE). Con un recorrido abierto de ese flujo de la persona y el producto, el carrito no arranca: `no_arranca_mientras:<clave>`, sin el legado. Sin esa prioridad, un carrito nativo arranca con un pago rechazado nativo abierto (dos mensajes): la prioridad es parte de la definición del carrito nativo de Maribel, no la impone la base. El bloque del carrito del legado frena con un pago nativo de **cualquier** oferta (hoy, con los dos en el legado, solo la misma oferta), la regla de la sección 22.
- **El orden en el bridge.** Con `FLOW_ENGINE_ENABLED` y un flujo nativo del pago rechazado del producto en `prendido` o `cohorte` (`read_flow_engine_native_flows_v1`), el worker le pasa al resolver el motor y el producto (`native_payment_failure_product_ref`), y el resolver manda el `PURCHASE_CANCELED` **primero** a `admit_flow_engine_event_v1` (`tipo: pago_fallido`, `momento` = la `creation_date` del rechazo, la misma que exige el planificador del legado, y `destino` = la identidad de WhatsApp que el rechazo ya usa). Lo que pasa después es lo de la sección 19 para el carrito: `motor_arranco` es del nativo; `ya_en_curso` suma el rechazo (arriba); otro motivo de la persona termina el evento `failed` con `flow_engine_no_start:<motivo>`; `sin_flujo`, `fuera_de_cohorte`, `flujo_pausado`, `flujo_apagado`, `caso_del_legado_en_curso`, `no_es_un_rechazo` o un `22023` van al planificador del legado exactamente como hoy (con el scope propio si `PAYMENT_FAILURE_OWN_POLICY_ENABLED`). Sin un flujo nativo del pago rechazado, ninguna llamada nueva: el pago rechazado de ATT1 (con sus variantes B1/B2) y el de Maribel van como hoy.
- **D1: las claves del legado no son de un flujo nativo.** Un ítem `nuevo` con la clave de un flujo del legado (`precheckout`, `carrito`, `pago_fallido`, `reactivacion`, `descuento`, las `FLOW_KEYS` de la lectura del AE) se frena con `flow_key_reserved_for_legacy`, aunque ese flujo no esté registrado en el producto: en el validador (al guardar) y en la base al publicar (`_flow_change_new_flow_blocker`). Con una de esas claves, la lectura del AE descarta todos los flujos nativos.
- **La salida `compra`.** El caso nace con `abandonment_event_id` = el rechazo, que tiene su binding portable (lo deja `admit_portable_hotmart_payment_failure`; sin él el legado tampoco planifica, `payment_failure_provenance_missing`), así que la compra de la persona corta el paso antes del envío (`purchase_detected_any_offer`). Le gana al primer contacto del legado por el sustrato: la intención queda `payment_failure_supported` y hay un caso de Hotmart abierto del producto (`_portable_precheckout_stop_reason`).
- **La simulación.** `read_flow_engine_native_trigger_counts_v1` acepta `p_trigger = pago_fallido` y cuenta los `PURCHASE_CANCELED` del producto que son un rechazo (las personas, por mail o por `buyer.checkout_phone`).
- **Lo que necesita la instancia.** La admisión portable del pago rechazado (`PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED`), que correlaciona el rechazo con la intención: sin ella no llega el evento ni el permiso. Cortar el legado del pago rechazado es pausar su scope (`piloto.sh pausar`), no apagar ese flag. Con `PAYMENT_FAILURE_OWN_POLICY_ENABLED` apagado, lo que el nativo no toma va al scope del carrito.
- **La correlación que resuelve una persona.** `confirm_operator_correlation_resolution` no toca `commercial_ally_payment_failure_details`: un rechazo sin correlación tampoco lo planifica el legado después de que un operador la resuelve. El nativo hace lo mismo.
- **Las pruebas:** `validate_flow_engine_native_payment_failure.mjs` (PGlite, el mundo de ATT1 con los rechazos capturados de Maribel, `tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json`: la forma de la migración; el flujo de Maribel compila a su política de hoy; la admisión en la cohorte, fuera de ella, sin formulario y con un pendiente cancelado, con el rojo de la forma del evento de antes; los dos rechazos seguidos con el rojo sin el bloque; el despacho con la plantilla de Maribel, `entry_permission` y el rojo de la intención caída; las 72 h; la compra que lo corta; (a) y (b) con sus rojos; D6 y el carrito del legado; ATT1 igual y la simulación; escribe `tests/fixtures/flow_engine/native_payment_failure_rows.json`) y `tests/test_flow_engine_native_payment_failure.py` (la regla 14 y el validador del ciclo con el pago de Maribel, el orden del resolver con esas filas, y el worker que pasa el producto solo con un flujo nativo del pago rechazado).

