# Contrato: lo que el motor de flujos le da a las herramientas (v1)

- **Estado:** propuesto el 2026-10-21, para construir contra él; la sección 7 la suma la T2.3b el 2026-10-22, y la 7.4 y la 8, la T2.4 el mismo día. Las funciones de la base de este contrato las crean las migraciones de la T2.3 (`20261021000200`, la T2.3a, y `20261022000100`, la T2.3b) y de la T2.4 (`20261022000200`). Este documento fija la interfaz: no dice en qué base están aplicadas.
- **Fecha:** 2026-10-21
- **Contrato:** `flow-engine-v1`, versión `1.0.0`
- **Alcance:** la interfaz entre el motor de la 1b y las piezas que lo usan sin ser el motor. Hoy son las herramientas del Automation Expert y el comando del operador, la T4. Fija cuatro cosas: la lectura del motor (`read_flow_engine_v1`), la lectura de la historia para simular (`read_flow_engine_history_v1`), la ruta de un flujo (crearla y moverla entre `legado` y `motor`) y cómo se arma la simulación de una propuesta en Python con `flow_replay.simulate`. Desde la T2.3b fija además lo que el bridge de la T2.4 llama del motor (sección 7): la admisión de un carrito, el arranque de un envío y el contexto del paso. La autorización por dentro y el despacho en Python siguen siendo internos.
- **Citas:** `origin/main` en `b163669`. «El esquema» es [flow-definition-v1.md](flow-definition-v1.md); «el ciclo», [flow-change-cycle-v1.md](flow-change-cycle-v1.md); «el contrato de lectura», [automation-expert-read-v1.md](automation-expert-read-v1.md). El almacén es `supabase/migrations/20261016000100_flow_change_store_v1.sql`.
- **Relacionados:** el esquema, el ciclo, el contrato de lectura, [portable-cart-followups-v1.md](portable-cart-followups-v1.md) (el carrito de tres toques que el motor ejecuta primero).

## Para qué

La T4 construye las herramientas con las que el Automation Expert propone un cambio con su simulación y el operador lo aprueba, lo rechaza o lo vuelve atrás. Para eso necesita saber, del motor:
- qué flujos ejecuta el motor y cuáles el código de hoy (el legado);
- qué versiones tienen recorridos en curso, para la vista previa (el ciclo, «Lo que está en curso»);
- la historia real de la semana, para la simulación (el ciclo, «la simulación»);
- cómo se mueve un flujo del legado al motor y de vuelta.

Este contrato fija eso para que la T4 se construya en paralelo con el motor, contra un doble que respete esta forma. El doble no puede esconder los controles: tiene que devolver los mismos `reason_code` que devuelve la base real.

## En corto

- **Dos lecturas y dos escrituras en la base.** Las lecturas son para `service_role` y para el rol de la T4, `automation_expert_change`. Las escrituras son solo del dueño de la base, como las siete del almacén, y las corre el comando del operador.
- **La ruta de un flujo es `legado` o `motor`.** Nace en `legado`. Pasar a `motor` exige que el motor sepa ejecutar la versión activa. Volver a `legado` exige que el scope del legado siga armado.
- **En la v1, solo el carrito puede ir a `motor`.** El pago rechazado y la ficha tienen ruta, pero solo `legado`: la ruta existe para que la pausa del almacén también los frene (decisión del 2026-10-21).
- **La simulación corre en Python**, en el bridge, con `flow_replay.simulate`. La base da la historia con la forma cerrada de la captura de la paridad, así que la simulación de la vista previa y la de la paridad leen lo mismo.
- **La vista previa cuenta lo en curso por versión** con `read_flow_engine_v1`. Con `en_curso: seguir`, lo en curso termina con su versión.

## 1. `read_flow_engine_v1`: qué ejecuta el motor

```sql
read_flow_engine_v1(p_instance jsonb, p_after text default null) returns jsonb
```

- `stable`, `security definer`, `search_path` fijo. La ejecutan `service_role` y el rol `automation_expert_change` de la T4, con el grant dentro de un guard (`to_regrole('automation_expert_change') is not null`): el rol lo crea la migración de la T4, que va antes que la del motor.
- `p_instance` es la marca de la instancia de la 1a. Se resuelve como en el almacén: el par (`tenant_ref`, `ally_ref`) con un binding activo (`_flow_store_instance`). Con otra cosa, `22023 flow_store_instance_invalid` o `flow_store_instance_not_bound`.
- Pagina de a 50 flujos, ordenados por (`product_ref`, `flow_key`). `p_after` es el `next` de la página anterior.

**La respuesta:**

```json
{
  "contract": "flow-engine-v1",
  "contract_version": "1.0.0",
  "database_at": "<now() de la función, UTC>",
  "engine": {"capabilities": "v1", "rules_version": 2},
  "flows": [
    {
      "flow_id": "<uuid>",
      "product_ref": "att1",
      "flow_key": "carrito",
      "state": "prendido",
      "active_version": 1,
      "generation": 3,
      "route": {
        "executor": "legado",
        "legacy_kind": "carrito",
        "legacy_scope_key": "att1-recuperacion",
        "generation": 0,
        "changed_at": "<UTC>"
      },
      "versions": [
        {
          "version": 1,
          "definition": { "...": "la definición publicada, setter-flujo/v1" },
          "definition_sha256": "<64 hex>",
          "published_at": "<UTC>",
          "pool": "att1-recuperacion",
          "in_flight": 12
        }
      ]
    }
  ],
  "next": null
}
```

- **`route`** es `null` si el flujo todavía no tiene ruta. Un flujo sin ruta no lo ejecuta nadie desde el almacén: es un borrador del lienzo o un flujo de la 1c.
- **`versions`** trae la versión activa y toda versión con recorridos en curso, en orden ascendente. No trae las demás versiones publicadas: `volver` a una vieja las lee del almacén.
- **`in_flight`** cuenta los recorridos abiertos de esa versión. Abierto quiere decir en espera, por salir, incierto, pausado o abierto para siempre (las filas 2 y 5b del diseño). Con la ruta en `motor`, se cuenta desde `flow_engine_runs` contra los casos abiertos. Con la ruta en `legado`, se cuentan los casos abiertos del `legacy_scope_key` con la secuencia de su `legacy_kind`, y se atribuyen a la versión activa. El legado no sabe de versiones del almacén.
- **`pool`** es la bolsa de los topes de esa versión (`topes.bolsa`, o el `legacy_scope_key`). Dos flujos con la misma bolsa gastan el mismo cupo.
- **No lleva datos personales.** Ningún teléfono, mail, nombre ni id de contacto.

**Para la simulación**, el bridge traduce `flows` a `Versions` con una función nueva, `engine_flows_from_read(document)`, que escribe la T4 en `src/bridge/flow_change_simulation.py`, que hace con esta forma lo mismo que `engine_flows_from_definitions` hace con el documento de la paridad (`flow_replay.py:325-360`): una `EngineFlow` por versión, con su `legacy_kind`, su zona fija, su bolsa y las excepciones de su `legacy_kind`.

## 2. `read_flow_engine_history_v1`: la historia para simular

```sql
read_flow_engine_history_v1(p_instance jsonb, p_product_ref text, p_since timestamptz) returns jsonb
```

- `stable`, `security definer`, `search_path` fijo. La ejecutan `service_role` y el rol `automation_expert_change` de la T4, con el grant dentro de un guard (`to_regrole('automation_expert_change') is not null`): el rol lo crea la migración de la T4, que va antes que la del motor.
- Devuelve **la captura** de la paridad, con su forma cerrada (`src/bridge/flow_capture.py`), restringida a la instancia y al producto, desde `p_since`, con la siembra que pide la forma. `p_since` no puede ser anterior a 8 días: la vista previa simula los últimos 7 (el ciclo), y el día de más es la siembra de lo que ya estaba en curso. Si es anterior, `22023 flow_engine_history_window_too_wide`.
- La forma es la de la captura v3 (`captura-paridad-recuperacion-v3`), que trae la T2.2: la v2 de hoy más los pasos siguientes (A2 y Z), los cupones por persona, el link por intento y los pedidos de renovación. Hasta que exista, el doble de la T4 usa el fixture de la v2 (`tests/fixtures/flow_engine/att1_recovery_capture_20261016.json`), que tiene la misma forma salvo esas secciones.
- **La forma es la prueba de privacidad.** La función no devuelve nada que `flow_capture.validate_shape` rechace: claves de persona de 16 hex, sin arroba y sin nueve dígitos seguidos. El bridge la valida igual antes de usarla, y si falla, la simulación no se construye (`simulation: failed`, sección 4).
- Con más de 4.000.000 bytes (4 MB; debajo del corte de 4 MiB del bridge, `MAX_STORE_BYTES` en `flow_change_api.py`), la función corta y devuelve `{"ok": false, "reason_code": "flow_engine_history_too_large"}`. La vista previa lo muestra como «la simulación no se pudo armar».
- **Las autorizaciones son las de los dos caminos** (desde `20261022000300`). `autorizaciones`, su conteo y los contactos suman las del legado (`pilot_outbound_request_authorizations`, por `scope_key`) y las del motor (`flow_engine_request_authorizations`, con `pool` = `scope_key`, el mismo criterio del tope). Una del motor sale con la misma forma, con `scope_key` = `pool` y `scope_version` y `runtime_generation` en `null`: el motor no tiene versión de scope ni generación del control del legado. Así la bolsa de la simulación ve los envíos del motor.

## 3. La ruta de un flujo

**La tabla** es `flow_engine_routes`: una fila por flujo, con `flow_id`, `executor` (`legado` o `motor`), `legacy_kind`, `legacy_scope_key`, `generation`, `changed_at`, `changed_by_ref` y `change_reason`. `legacy_kind` y `legacy_scope_key` no cambian nunca después de crearla. Es única por (`legacy_scope_key`, `legacy_kind`).

**Los `legacy_kind`** son seis: `carrito`, `pago_fallido`, `ficha`, `precheckout`, `descuento` y `reactivacion`. Cada uno va con el disparador de la definición del flujo: `carrito` con `dispara.evento: carrito`, `pago_fallido` con `pago_fallido`, `ficha` con `ficha_impresa`, `precheckout` con `intencion`, y `descuento` y `reactivacion` con su silencio.

### 3.1 Crear la ruta

```sql
operator_create_flow_route_v1(p_instance jsonb, p_product_ref text, p_flow_key text,
    p_legacy_kind text, p_legacy_scope_key text, p_actor_name text, p_reason text) returns jsonb
```

- La ejecuta solo el dueño de la base, `security invoker`, como las siete del almacén. La corre la migración de cada flujo, la T5, al registrar el flujo y publicar su v1.
- Crea la ruta **siempre en `legado`**, con `generation = 0`.
- Exige:
  - que el flujo esté registrado (`P0002 flow_not_found`);
  - en la v1, que el `legacy_kind` sea uno que la guarda del almacén frena en el legado: `carrito`, `pago_fallido` o `ficha` (`flow_route_kind_not_guarded`). `precheckout`, `descuento` y `reactivacion` no tienen guarda, y una ruta suya haría creer al operador que pausarlos en el almacén los frena;
  - que tenga versión activa (`flow_route_needs_published_version`);
  - que el disparador de la versión activa corresponda al `legacy_kind` (`flow_route_kind_mismatch`);
  - que exista el scope del legado (`flow_route_legacy_scope_unknown`);
  - que el par no tenga otra ruta (`flow_route_already_exists`);
  - desde la T2.3b, que el flujo esté `prendido` (`flow_route_flow_off`). Un flujo recién registrado nace `apagado`, y una ruta suya en `legado` frenaría con la guarda el flujo de hoy de ese scope. Se mira después del scope y antes de crear la fila, así que con el flujo apagado y el par ya ruteado devuelve `flow_route_flow_off` y no `flow_route_already_exists`.
- Devuelve `{"ok": true, "route": {...}}`, con la forma de `route` de la sección 1, o `{"ok": false, "reason_code": ...}`. Deja un `flow_change_events` de tipo `flow_route_created` (se suma a la lista de `event_type` del almacén).

### 3.2 Mover la ruta

```sql
operator_route_flow_v1(p_instance jsonb, p_product_ref text, p_flow_key text, p_executor text,
    p_expected_generation bigint, p_actor_name text, p_reason text, p_revalidation jsonb) returns jsonb
```

- La ejecuta solo el dueño de la base, `security invoker`. La corre el comando del operador (`mover`, en `flow-change-cycle-v1`, «Mover la ruta de un flujo»), que en la T4 es de C y que hoy corre Dan, como `piloto.sh`.
- `p_expected_generation` es la `generation` de la ruta que leyó quien la mueve. Si no coincide, `{"ok": false, "reason_code": "flow_route_generation_stale"}`.
- **A `motor`** exige, en este orden:
  1. que el `legacy_kind` esté en las capacidades de esta versión del motor. En la v1, solo `carrito` (desde la T2.3b; en la T2.3a no había ninguno). Si no, `flow_route_kind_not_supported`;
  2. que el flujo esté `prendido` o `pausado`, no `apagado` (`flow_route_flow_off`);
  3. que la versión activa pase las reglas 14 y 16 con las capacidades del motor. `p_revalidation` es el resultado del validador sobre esa versión, con estas claves exactas y ninguna más: `version` y `definition_sha256` (las de la versión activa), `rules_version` (2 o más), `capabilities` (`"v1"`), `blocks` (lista, vacía para pasar) y `validator` (las seis claves del validador del ciclo: `contract`, `rules_version`, `phase`, `actor`, `product_version` y `git_sha`). No lleva `fingerprint`: mover una ruta no es una propuesta. Si falta o sobra una clave, si `blocks` no está vacía, o si `version` o `definition_sha256` no son las de la versión activa, devuelve `flow_route_revalidation_invalid`;
  4. que la instancia no tenga renglón de tope por persona (`flow_route_person_cap_present`, el principio 12 del diseño). Y al revés, desde la T2.3b: con una ruta del tenant en `motor`, la base no deja cargar ni cambiar un renglón de `pilot_proactive_contact_caps` (un disparador de la tabla, `55000 pilot_proactive_contact_cap_with_engine_route`).
- **A `legado`** exige que el control del scope del legado esté armado (`pilot_runtime_controls.runtime_state = 'armed'`; si no, `flow_route_legacy_not_armed`). La base no conoce la versión del scope que tiene el bridge, así que no la compara: que el bridge tenga esa versión lo verifica el operador. Armarlo es un paso del operador y no lo hace esta función.
- Con el mismo `executor` que ya tiene, devuelve `{"ok": true, "unchanged": true}` y no sube la `generation`.
- Toma la fila de la ruta `FOR UPDATE` y después la de `flow_controls` `FOR SHARE`, en ese orden.
- Devuelve `{"ok": true, "route": {...}}` o `{"ok": false, "reason_code": ...}`, y deja un `flow_change_events` de tipo `flow_route_moved` con la generación vieja y la nueva.
- **Lo en curso no se mueve.** Lo que arrancó el legado termina en el legado, y lo que arrancó el motor termina en el motor, con su versión, aunque la ruta cambie.

**El Automation Expert no mueve rutas.** Mover una ruta es una decisión de producto, no un cambio de flujo: no pasa por una propuesta. Las herramientas de la T4 la muestran (sección 1) y no la cambian.

## 4. La simulación de una propuesta

Corre en el bridge, en Python, y no escribe en la base salvo el resultado que guarda el ciclo. Los pasos:

1. **Leer el motor**, `read_flow_engine_v1`, y traducir a `Versions` (`engine_flows_from_read`).
2. **Leer la historia**, `read_flow_engine_history_v1` con `p_since = ahora − 8 días`, validar la forma y armar `History` (`flow_replay.history_from_capture`, `flow_replay.py:427`).
3. **Elegir las versiones:**
   - la vigente, la activa que dio la lectura (`current_versions`, `flow_replay.py:1350`, la lee de la captura y da lo mismo);
   - la propuesta, `proposed_versions(versions, current, items)` (`flow_replay.py:1363`) con los `ProposalItem` de la propuesta (`flow_definition.py:844`).
4. **Simular**, `flow_replay.simulate(history, versions, fixed(vigente), fixed(propuesta), capabilities=ENGINE_CAPABILITIES_V1)` (`flow_replay.py:1326`). Devuelve una `Simulation` con su `summary`: los disparos, los cambios y el resumen de cada lado (`replay_summary`).
5. **Guardar.** La simulación va adentro de la vista previa, en `validation.preview.simulacion`, y queda congelada al validar: `{"status": "built", "summary": {...}, "summary_sha256": "<64 hex>", "cambios": [...recortados]}` o `{"status": "failed", "reason_code": "<^[a-z][a-z0-9_:]{0,79}$>"}`. **Aprobar la lee de ahí**, sin cambiar la firma de `operator_approve_flow_change_v1`, y registra `simulation: built` con el `summary_sha256`, `failed` con su `reason_code`, o `not_built` con cualquier otra forma (también cuando el operador lo fuerza). Lo implementa la T4, en su migración. Con la ruta en `legado`, la simulación vale igual: simula lo que haría la definición, que es lo que el motor ejecutaría al pasar la ruta.

**Las capacidades** de la simulación son las del motor de la instancia. La v1 del motor ejecuta varios envíos por camino desde la T2.1. Hasta entonces, una propuesta con más de un envío sale con `engine_not_supported:mas_de_un_envio`. `PREVIEW_CAPABILITIES` es solo para el lienzo y nunca para aprobar.

**Lo que la simulación no sabe** lo dice el ciclo («Qué tan fiel es la simulación»): no sabe qué habría contestado alguien a un mensaje que no salió. La forma de decirlo es `sin_saber` en el resumen, con su nombre.

## 5. Los códigos

Los de este contrato, todos en forma `^[a-z][a-z0-9_]{1,79}$`:

| Código | Dónde | Qué quiere decir |
|---|---|---|
| `flow_engine_history_window_too_wide` | 2 | `p_since` es anterior a 8 días |
| `flow_engine_history_too_large` | 2 | la historia pasa de 4 MB |
| `flow_route_kind_not_guarded` | 3.1 | en la v1, el legado de ese `legacy_kind` no tiene guarda del almacén |
| `flow_route_needs_published_version` | 3.1 | el flujo no tiene versión activa |
| `flow_route_kind_mismatch` | 3.1 | el disparador no es el del `legacy_kind` |
| `flow_route_legacy_scope_unknown` | 3.1 | el scope del legado no existe |
| `flow_route_already_exists` | 3.1 | el par ya tiene una ruta |
| `flow_route_flow_off` | 3.1 | el flujo no está prendido (desde la T2.3b) |
| `flow_route_generation_stale` | 3.2 | la ruta cambió desde que se leyó |
| `flow_route_kind_not_supported` | 3.2 | el motor de esta versión no ejecuta ese `legacy_kind` |
| `flow_route_flow_off` | 3.2 | el flujo está apagado |
| `pilot_proactive_contact_cap_with_engine_route` | 3.2 | un renglón de tope por persona con una ruta del tenant en `motor` (55000, del disparador) |
| `flow_route_revalidation_invalid` | 3.2 | la revalidación falta, bloquea o es de otra versión |
| `flow_route_person_cap_present` | 3.2 | la instancia tiene tope por persona |
| `flow_route_legacy_not_armed` | 3.2 | el scope del legado no está armado |

Los errores de entrada siguen la convención del almacén: `22023` para una entrada inválida, `P0002` para lo que no existe o no es de la instancia (sin distinguir), y `55000` para un estado que no lo permite.

## 6. Las pruebas que fija el contrato

- **La forma de `read_flow_engine_v1`** contra un JSON Schema en `tests/fixtures/flow_engine/read_flow_engine_v1.schema.json`. Lo usan la prueba de la base (PGlite) y el doble de la T4, así que los dos verifican lo mismo.
- **La historia** pasa `flow_capture.validate_shape`, en PGlite, sobre una base sembrada con un carrito de tres toques.
- **La ruta:** cada `reason_code` de la sección 5 tiene su caso en PGlite, y el rojo a propósito: un `pago_fallido` a `motor` en la v1 da `flow_route_kind_not_supported`.
- **La simulación de punta a punta:** con la historia del fixture y una propuesta que cambia la espera de A2, `simulate` da cero cambios contra la vigente consigo misma, y cambios solo en el paso 2 del carrito.

## 7. El camino del motor: la admisión, el arranque y el contexto (T2.3b)

Las tres funciones que llama el bridge de la T2.4 para el carrito. Las tres son `security definer`, con `search_path` fijo, y las ejecuta solo `service_role`. Las formas de salida son las de esta sección (decisión 1 de la T2.3b); el bridge de la T2.4 las consume.

**La delegación en las funciones de hoy.** Con la ruta del carrito en `motor`, `plan_lancemos_pilot_cart_recovery` admite por el motor (llama a la admisión) y `mark_lancemos_pilot_request_started` arranca un caso del motor por la autorización del motor, así que un bridge que todavía no conoce estas funciones manda bien lo del motor. Por ese camino los motivos van sin `:` ni dígitos (`no_arranca_mientras_pago_fallido`, `no_entra_permiso`), porque el bridge de hoy descarta un motivo con esa forma, y un no-arranque es el `raise` de hoy (`pilot_scope_rejected` o `pilot_request_start_rejected`, con el motivo en `detail`): su fila de rechazo se deshace con el `raise`. La delegación va **por caso** (un renglón en `flow_engine_runs`): lo que arrancó el legado termina en el legado, con la guarda de pausado y apagado. `mark_followup_request_started` acepta también la autorización del motor. El pago rechazado y la ficha no delegan.

### 7.1 La admisión de un carrito

```sql
admit_flow_engine_hotmart_v1(p_webhook_event_id uuid, p_contact_id uuid, p_external_product_id text,
    p_product_name text, p_offer_code text, p_abandoned_at timestamptz, p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint, p_destination_phone text, p_legacy_scope_key text,
    p_legacy_scope_version integer, p_legacy_policy_key text, p_legacy_policy_version integer,
    p_force_legacy boolean default false)
returns table (outcome text, recovery_case_id uuid, created boolean, reason text,
    divergence_id uuid, divergence_first boolean, followup_sequence_id uuid, scheduled_action_id uuid)
```

- Los argumentos son los del envoltorio de hoy: `p_destination_phone` es lo que hoy va como `p_external_user_id`, y los `p_legacy_*` son los que usa la caída al legado. Una entrada inválida es `22023 invalid_flow_engine_admission_parameters`.
- **Nunca hace `raise` por el motor.** El camino del motor corre en una subtransacción. Lo que no puede (una divergencia de la ruta, el producto, la política compilada o el planificador, o un error transitorio como un lock que no llega en 3 s) queda en `flow_engine_divergences` (`count + 1`) y lo planifica el legado en la misma llamada. El `lock_timeout` de quien llama queda como estaba.
- `outcome`:

| `outcome` | Qué pasó | Las otras columnas |
|---|---|---|
| `motor_arranco` | el caso del motor, con su recorrido y su versión fijada | el caso, la secuencia y la acción; `created = false` en la repetición del mismo evento |
| `motor_no_arranca` | el motor no arranca este disparo | `reason` (abajo); con su fila en `flow_engine_start_rejections` (`phase admision`) salvo `flujo_apagado` y los códigos estáticos del scope |
| `legado` | la ruta no está en `motor`, o el evento ya lo planificó el legado: lo planificó el envoltorio de hoy | el caso del legado |
| `legado_por_divergencia` | el motor no pudo y lo planificó el legado | el caso del legado, `divergence_id` y `divergence_first` (la fila es nueva: el aviso) |
| `legado_rechazo` | el legado tampoco planificó | `reason` = `<message>:<detail>` del rechazo de hoy (por ejemplo `pilot_scope_rejected:pilot_runtime_not_armed`); la divergencia, si la hubo, queda escrita |

- Los `reason` de `motor_no_arranca` son los del contrato de lectura: `flujo_apagado`, `flujo_pausado`, `baja`, `ya_no_entra`, `no_entra:permiso`, `ya_en_curso`, `no_arranca_mientras:<flujo>` (`pago_fallido`, `ficha`, `precheckout`), o el código del legado cuando el evento no coincide con lo estático del scope (`pilot_offer_mismatch`, `pilot_product_mismatch`…; decisión 2, sin divergencia). El motor no mira el estado del runtime del scope del legado ni la versión que pasa el bridge: la versión del binding es la del control del scope (decisión 3).
- Cae al legado (divergencia) una versión que la v1 no ejecuta: `repeticion.otra_despues_de` distinto de `0m`, o un scope con cohorte (decisión 5).
- `p_force_legacy = true` no entra al camino del motor: deja una divergencia `transitorio` (`transitorio:forzado_al_legado`) y planifica el legado.

### 7.2 El arranque de un envío del motor

```sql
mark_flow_engine_request_started_v1(p_action_id uuid, p_attempt_id uuid, p_worker_id text,
    p_lease_generation bigint, p_now timestamptz)
returns table (outcome text, reason text, id uuid, action_id uuid, idempotency_key text,
    attempt_number integer, channel text, mode text, phase text, lease_generation bigint,
    request_started_at timestamptz, expected_case_version bigint, expected_sequence_revision bigint,
    flow_authorization_id uuid, flow_generation bigint, authorization_replayed boolean)
```

- Solo para una acción de un caso del motor: si no, `55000 flow_engine_run_not_found`. Una entrada inválida es `22023 invalid_flow_engine_request_start_parameters`.
- **Nunca hace `raise` por un rechazo.** `outcome`:
  - `request_started`: el intento arrancó, con su fila en `flow_engine_request_authorizations` (`flow_authorization_id`, `flow_generation` = la generación del control del flujo). Repetido, devuelve la misma con `authorization_replayed = true`.
  - `rechazado`: no arranca ahora. Deja su fila en `flow_engine_start_rejections` (`phase despacho`, `count + 1` por llamada), el intento queda reservado y la llamada confirma: el bridge reintenta en el lease siguiente. `reason`: `flujo_pausado`, `tope_del_flujo` (los topes de la definición fijada, contados sobre la bolsa por los dos caminos), `baja`, `ya_no_entra`, `no_entra:permiso`, `pilot_request_time_invalid`, `pilot_delivery_mode_mismatch`, `pilot_attempt_mismatch` o `flow_engine_pool_unknown`.
  - `terminado` con `flujo_apagado`: el flujo se apagó después de que el recorrido arrancó. Cierra el intento (`completed`, `failed_before_request`: el intento no llegó a pedir el envío), cancela la acción, completa la secuencia y cancela el caso, todo con `flujo_apagado`, y confirma.
- Por la función de hoy (`mark_lancemos_pilot_request_started`), la imagen vieja recibe los mismos motivos como `raise`, y además `flow_engine_needs_engine_image` cuando la versión fijada manda otros envíos que la primera versión del flujo (la que salió del manifiesto).
- **El apagado no depende de la imagen:** `reevaluate_followup_action` cancela con `flujo_apagado` un `execute` o un `defer` de un recorrido cuyo flujo se apagó después de arrancar, aunque se haya vuelto a prender. Cualquier otro motivo de la reevaluación le gana.

### 7.3 El contexto del paso

```sql
flow_engine_execution_context_v1(p_action_id uuid)
returns table (flow_id uuid, version integer, product_ref text, flow_key text, step_key text,
    send_ordinal integer, template_name text, template_language text, template_variables text[],
    template_button jsonb, legacy_dispatch_ok boolean)
```

- `stable`. La plantilla, el idioma, las variables y el botón del envío del paso en la **versión fijada** del recorrido (principio 10). `send_ordinal` es la posición del `step_key` en la forma de pasos del `legacy_kind`. Cero filas para una acción del legado: el bridge sigue con el manifiesto.
- `legacy_dispatch_ok` dice si la imagen de hoy puede mandar ese recorrido: cada envío igual al de la primera versión del flujo.

### 7.4 Lo que el bridge lee y marca (T2.4)

Las tablas del motor no tienen permisos para la API, así que el bridge las lee por tres funciones de `20261022000200`. Las tres son `security definer`, con `search_path` fijo, y las ejecuta solo `service_role`. La migración no cambia ninguna función existente.

```sql
read_flow_engine_routes_v1(p_instance jsonb) returns jsonb
read_flow_engine_divergences_v1(p_instance jsonb, p_only_unnotified boolean, p_limit integer) returns jsonb
mark_flow_engine_divergence_notified_v1(p_id uuid, p_notified_at timestamptz) returns boolean
```

- `read_flow_engine_routes_v1`: `{"routes": [{"product_ref", "flow_key", "executor", "legacy_kind", "legacy_scope_key", "generation", "flow_state"}]}`, las rutas de los flujos de la instancia, por producto y flujo. No trae definiciones ni lo en curso: es la lectura barata del lote.
- `read_flow_engine_divergences_v1`: `{"unnotified": <int>, "divergences": [{"id", "kind", "reason", "flow_key", "product_ref", "version", "count", "first_at", "last_at"}]}`, de la más vieja a la más nueva. `unnotified` cuenta todas las de la instancia sin avisar, no solo las de la página. No lleva `first_source_ref` ni `last_source_ref`. `p_limit` va de 1 a 50 y `p_only_unnotified` no puede ser nulo: si no, `22023 flow_engine_divergences_parameters_invalid`.
- `mark_flow_engine_divergence_notified_v1`: pone `notified_at` solo si estaba en `null`, y devuelve si la cambió. Una divergencia que no existe devuelve `false`. La fila no se borra.
- La instancia es la marca de la 1a (`_flow_store_instance`): `tenant_ref` y `ally_ref`, con un binding activo. Sin binding, `22023 flow_store_instance_not_bound`.

## 8. El bridge del motor (T2.4)

**El flag.** `FLOW_ENGINE_ENABLED`, apagado por defecto. Apagado, el bridge no lee nada del motor y hace exactamente lo de hoy: ninguna llamada nueva a la base, y el payload de `/ready` no cambia. Prendido, el arranque exige el manifiesto de la instancia, la frontera del piloto (`LANCEMOS_PILOT_BOUNDARY_ENABLED`), el worker de resolución, el despachador en plantilla directa, A1 con cupón y A2/Z (`RECOVERY_CART_FOLLOWUPS_ENABLED`); si falta uno, no arranca. Con una ruta en `motor` y el flag apagado, el bridge sigue igual: la base delega en el motor por caso (sección 7). Por eso, antes de mover una ruta a `motor`, el flag tiene que estar prendido en la instancia: lo exige el runbook de la ruta, no el bridge.

**Qué cambia con el flag prendido:**
- **Las rutas.** El lote del worker de resolución lee `read_flow_engine_routes_v1` a lo sumo cada 60 s y las guarda por (`legacy_scope_key`, `legacy_kind`). Si la lectura falla, el carrito va por el envoltorio de hoy hasta la lectura siguiente.
- **La admisión.** Solo el carrito, y solo con la ruta del scope de la frontera del piloto en `motor`: el planificador es `admit_flow_engine_hotmart_v1` (7.1), con los mismos argumentos que el envoltorio de hoy. El evento queda `processed` con `motor_arranco`, `legado` y `legado_por_divergencia`; `failed` con el `reason` del legado con `legado_rechazo`; y `failed` con `flow_engine_no_start:<reason>` con `motor_no_arranca`. Una admisión sin respuesta (el transporte, un 502, 503 o 504, o un SQLSTATE de timeout, lock, serialización o conexión) deja el evento en `received`, sin marcar; desde la cuarta seguida del mismo evento, la llamada va con `p_force_legacy = true`. Cualquier otro error de la función marca el evento `failed` como hoy. El pago rechazado y la ficha nunca pasan por la admisión. Con la ruta en `legado`, sin ruta o sin lectura buena, la llamada es la de hoy.
- **El despacho.** Una acción del motor se reconoce por su política, `flujo:<flow_id>`, sin preguntarle a la base; cualquier otra acción no hace ninguna llamada nueva. Antes de componer, el despachador pide el contexto del paso (7.3): la plantilla y el idioma de la versión fijada reemplazan a los de la configuración, el portón final de Meta usa ese idioma, y el arranque va por `mark_flow_engine_request_started_v1` (7.2). `rechazado` deja el intento reservado y el lote sigue; `terminado` no se finaliza otra vez. Una respuesta del arranque que no se entiende es comprometida (como en el legado) y el intento se cierra como `delivery_unknown`. A1 que cae al respaldo de hoy (sin cupón o sin su plantilla en el catálogo) sale con la plantilla del manifiesto, y el portón lleva el idioma de lo que sale. Sin contexto, o con la llamada caída, va por el camino de hoy: con la primera versión la plantilla del manifiesto es la correcta, y con otra la base rechaza con `flow_engine_needs_engine_image`. Una versión con otras variables o con otro botón que los que el bridge sabe armar cierra el paso sin mandar y sin reintento, con `flow_engine_step_unsupported`.
- **`/ready`.** Suma `flow_engine` (`enabled`), `flow_engine_routes_motor`, `flow_engine_routes_legado`, `flow_engine_read` (`ok`, `failing` o `pending`), `flow_engine_divergences_unnotified` (un número, o `unknown`) y `flow_engine_alerts` (`slack` o `sin_slack`), en texto. Sale de lo que ya está en memoria: `/ready` no hace ninguna llamada nueva a la base, y ninguna de estas claves da 503.
- **`MOT-001`.** Al arrancar y cada 60 s, el bridge lee las divergencias sin avisar y avisa una por una con el código `MOT-001` (`p2`, «El motor no pudo y lo planificó el legado») por el productor del conector de Slack: `event_id` es el id de la divergencia, `dedupe_key` el `sha256` de `flow-engine-divergence-v1\0<id>`, `reason_code` el `reason` (que ya trae la clase adelante; si no, `<kind>:<reason>`) recortado a 80, y `count`. Marca `notified_at` solo después de que el conector la admitió; si falla, no marca y la vuelta siguiente reintenta. Un conflicto semántico del conector (el mismo id con otro contenido, porque la fila sumó `count` después de una admisión cuya respuesta no llegó) cuenta como avisada y se marca. El aviso sale de esta lectura y no del `divergence_first` de la admisión: una sola vía. Sin conector, no avisa, lee igual el conteo y publica `sin_slack`. El comando no lleva ningún id de evento ni de contacto. El conector es otra imagen: hasta que se redespliegue con el catálogo que trae `MOT-001`, rechaza el aviso.

## Lo que queda para la versión siguiente

- La ruta a `motor` del pago rechazado y de la ficha. Piden un esquema que exprese las condiciones (la variante de B1, el cupón de B1 y la fecha de vencimiento de la ficha).
- `en_curso: cancelar` y `migrar` (el ciclo).
