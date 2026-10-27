# Contrato: el motor nativo de flujos (v2)

- **Estado:** propuesto el 2026-10-24, para construir contra él (el PR 1 del hito 4). No hay código que lo implemente: lo construyen los PR 2 (la base), 3 (el bridge) y 4 (el Automation Expert). Este documento fija la interfaz; no dice que nada esté desplegado, activado ni probado.
- **Fecha:** 2026-10-24
- **Contrato:** `flow-engine-v2`, versión `2.5.0` (la `2.1.0`, del 2026-10-24, suma la sección 13: lo que fija el PR 3, el bridge; la `2.2.0`, del mismo día, la sección 14: lo que fija el PR 4, el Automation Expert que crea flujos; la `2.3.0`, del 2026-10-25, la sección 15: lo que fija el PR 5, lo que el Automation Expert ve del motor nativo; la `2.4.0`, del 2026-10-26, la sección 16: las variables y los botones de la plantilla de un paso nativo; la `2.5.0`, del mismo día, la sección 17: la oferta del evento llega al chequeo del destinatario)
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
| `tipo` | el disparador del esquema: `compra` o `carrito` en la v2 |
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
- **La forma de los pasos.** La política compilada exige un `legacy_kind` (`compile_policy_legacy_kind_without_step`, `M-rutas:449-451`). Para un flujo nativo, la forma de los pasos sale del disparador: `carrito` usa la forma de hoy (`_flow_engine_legacy_steps('carrito')`, `M-rutas:324-334`: A1, A2 y Z), y `compra` una forma nueva de un solo envío. Así el carrito de Maribel se escribe igual que el de ATT1.
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

Las capacidades del motor nativo son las de la v1 (`ENGINE_CAPABILITIES_V1`, `flow_engine.py:132-142`) más:
- el disparador `compra`;
- `permiso: compra`, con la regla 17 intacta: una sola plantilla de utilidad por camino;
- `nombre` con la compra, cuando el PR 3 lo extraiga del evento;
- el paso `etiquetar: cliente` (esquema 1.2);
- `repeticion.otra_despues_de: nunca` (o cualquier duración distinta de `0m`): en el carrito cae al legado (`ruta:repeticion_no_soportada`, `M-carrito:520-522`), y en un flujo nativo se ejecuta.

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
- **El envío del paso nativo.** Una acción con ancla `flow_engine_event` sale solo en el modo de plantilla directa, con la plantilla de su versión fijada (`flow_engine_execution_context_v1`). Las condiciones: `nombre` y `producto` a lo sumo, sin botón, y categoría `UTILITY` en el catálogo de Chatwoot (regla 17). Sin el flag cierra con `flow_engine_native_needs_engine`. En el modo Hermes cierra antes del agente, con `flow_engine_native_needs_direct_template`, así que nunca pide una derivación (`request_human_handoff` exige el binding del piloto). Una forma que el bridge no sabe armar cierra con `flow_engine_step_unsupported`.
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
- **Los botones.** El paso nativo sigue sin declarar `boton`. La plantilla puede traer botones de URL fija (sin `{{n}}` en la URL): el envío no lleva parámetros de botón, solo los del cuerpo (`parse_approved_template(static_url_buttons_only=True)`). Un botón de URL con variable cierra el envío con `approved_template_mismatch` / `url_button_with_variable`, y uno de respuesta rápida o de otro tipo, con `approved_template_mismatch` / `unsupported_button`. El validador frena lo mismo antes (`template_button_mismatch`, con el motivo en la razón). En el legado nada cambia: un botón de URL que no es el declarado sigue siendo `unsupported_button`.
- **El encabezado de texto fijo** se acepta como antes (un `HEADER` de texto sin variables no lleva parámetros).

## 17. La oferta del evento llega al chequeo del destinatario (E-122, `2.5.0`)

Escrito al construir el cambio, el 2026-10-26, antes de cualquier despliegue. Migración `20261026000200_flow_engine_native_offer_in_context_v1.sql`. Describe la interfaz; no dice que nada esté desplegado ni activado. Lo motivó la compra de prueba de ATT1 del 2026-10-26 (capturada en `tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json`): el recorrido nativo abrió y el envío se cerró antes del pedido con `followup_recipient_not_allowlisted`.

- **El chequeo no cambia.** El despachador valida al destinatario con `_is_authorized_followup_recipient`: sin JID fijo, el producto del contexto tiene que ser el del binding y la oferta del contexto, una de sus ofertas aceptadas.
- **El caso nativo sigue con `offer_code` nulo** (sección 4, la admisión): los planificadores buscan el caso abierto de la persona, el producto y la oferta por esa columna, y un carrito o un pago no tienen que caer en un caso nativo.
- **La oferta del evento va al `context` del caso**, como `flow_engine_offer` (la misma que se guarda en `flow_engine_native_runs`), y `get_followup_execution_context` la devuelve como `offer_code` para un caso con política `flujo:<flow_id>` sin `offer_code`. Para todo otro caso devuelve `recovery_cases.offer_code`, como antes.
- **Lo que se sigue frenando:** una compra por una oferta que la instancia no acepta, o sin oferta, cierra el envío igual que antes. Un caso nativo admitido antes de la migración no tiene la oferta en el `context` y tampoco sale.
- **La prueba** es la compra capturada de punta a punta: la base (`validate_flow_engine_native_offer_in_context.mjs`, con el rojo de la definición anterior) y el bridge con el parser y el chequeo reales (`tests/test_flow_engine_native_purchase_e2e.py`).
