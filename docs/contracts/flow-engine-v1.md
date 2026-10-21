# Contrato: lo que el motor de flujos le da a las herramientas (v1)

- **Estado:** propuesto el 2026-10-21, para construir contra él. No describe nada construido, desplegado ni activado. Las funciones de la base de este contrato todavía no existen: las crea la migración de la T2.3.
- **Fecha:** 2026-10-21
- **Contrato:** `flow-engine-v1`, versión `1.0.0`
- **Alcance:** la interfaz entre el motor de la 1b y las piezas que lo usan sin ser el motor. Hoy son las herramientas del Automation Expert y el comando del operador, la T4. Fija cuatro cosas: la lectura del motor (`read_flow_engine_v1`), la lectura de la historia para simular (`read_flow_engine_history_v1`), la ruta de un flujo (crearla y moverla entre `legado` y `motor`) y cómo se arma la simulación de una propuesta en Python con `flow_replay.simulate`. No fija la admisión, la autorización ni el despacho del motor, que son internos.
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

- **Dos lecturas y dos escrituras en la base.** Las lecturas son solo para `service_role`, como las tres de la 1a. Las escrituras son solo del dueño de la base, como las siete del almacén, y las corre el comando del operador.
- **La ruta de un flujo es `legado` o `motor`.** Nace en `legado`. Pasar a `motor` exige que el motor sepa ejecutar la versión activa. Volver a `legado` exige que el scope del legado siga armado.
- **En la v1, solo el carrito puede ir a `motor`.** El pago rechazado y la ficha tienen ruta, pero solo `legado`: la ruta existe para que la pausa del almacén también los frene (decisión del 2026-10-21).
- **La simulación corre en Python**, en el bridge, con `flow_replay.simulate`. La base da la historia con la forma cerrada de la captura de la paridad, así que la simulación de la vista previa y la de la paridad leen lo mismo.
- **La vista previa cuenta lo en curso por versión** con `read_flow_engine_v1`. Con `en_curso: seguir`, lo en curso termina con su versión.

## 1. `read_flow_engine_v1`: qué ejecuta el motor

```sql
read_flow_engine_v1(p_instance jsonb, p_after text default null) returns jsonb
```

- `stable`, `security definer`, `search_path` fijo. Solo la ejecuta `service_role`.
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

**Para la simulación**, el bridge traduce `flows` a `Versions` con una función nueva de `flow_replay`, `engine_flows_from_read(document)`, que hace con esta forma lo mismo que `engine_flows_from_definitions` hace con el documento de la paridad (`flow_replay.py:325-360`): una `EngineFlow` por versión, con su `legacy_kind`, su zona fija, su bolsa y las excepciones de su `legacy_kind`.

## 2. `read_flow_engine_history_v1`: la historia para simular

```sql
read_flow_engine_history_v1(p_instance jsonb, p_product_ref text, p_since timestamptz) returns jsonb
```

- `stable`, `security definer`, `search_path` fijo. Solo la ejecuta `service_role`.
- Devuelve **la captura** de la paridad, con su forma cerrada (`src/bridge/flow_capture.py`), restringida a la instancia y al producto, desde `p_since`, con la siembra que pide la forma. `p_since` no puede ser anterior a 8 días: la vista previa simula los últimos 7 (el ciclo), y el día de más es la siembra de lo que ya estaba en curso. Si es anterior, `22023 flow_engine_history_window_too_wide`.
- La forma es la de la captura v3 (`captura-paridad-recuperacion-v3`), que trae la T2.2: la v2 de hoy más los pasos siguientes (A2 y Z), los cupones por persona, el link por intento y los pedidos de renovación. Hasta que exista, el doble de la T4 usa el fixture de la v2 (`tests/fixtures/flow_engine/att1_recovery_capture_20261016.json`), que tiene la misma forma salvo esas secciones.
- **La forma es la prueba de privacidad.** La función no devuelve nada que `flow_capture.validate_shape` rechace: claves de persona de 16 hex, sin arroba y sin nueve dígitos seguidos. El bridge la valida igual antes de usarla, y si falla, la simulación no se construye (`simulation: failed`, sección 4).
- Con más de 1 MB, la función corta y devuelve `{"ok": false, "reason_code": "flow_engine_history_too_large"}`. La vista previa lo muestra como «la simulación no se pudo armar».

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
  - que tenga versión activa (`flow_route_needs_published_version`);
  - que el disparador de la versión activa corresponda al `legacy_kind` (`flow_route_kind_mismatch`);
  - que exista el scope del legado (`flow_route_legacy_scope_unknown`);
  - que el par no tenga otra ruta (`flow_route_already_exists`).
- Devuelve `{"ok": true, "route": {...}}`, con la forma de `route` de la sección 1, o `{"ok": false, "reason_code": ...}`. Deja un `flow_change_events` de tipo `flow_route_created` (se suma a la lista de `event_type` del almacén).

### 3.2 Mover la ruta

```sql
operator_route_flow_v1(p_instance jsonb, p_product_ref text, p_flow_key text, p_executor text,
    p_expected_generation bigint, p_actor_name text, p_reason text, p_revalidation jsonb) returns jsonb
```

- La ejecuta solo el dueño de la base, `security invoker`. La corre el comando del operador, que en la T4 es de C y que hoy corre Dan, como `piloto.sh`.
- `p_expected_generation` es la `generation` de la ruta que leyó quien la mueve. Si no coincide, `{"ok": false, "reason_code": "flow_route_generation_stale"}`.
- **A `motor`** exige, en este orden:
  1. que el `legacy_kind` esté en las capacidades de esta versión del motor. En la v1, solo `carrito`. Si no, `flow_route_kind_not_supported`;
  2. que el flujo esté `prendido` o `pausado`, no `apagado` (`flow_route_flow_off`);
  3. que la versión activa pase las reglas 14 y 16 con las capacidades del motor. `p_revalidation` es el resultado del validador sobre esa versión, con la forma de la revalidación de `operator_publish_flow_change_v1`. Si trae un bloqueo, o si es de otra versión o de otro `definition_sha256`, devuelve `flow_route_revalidation_invalid`;
  4. que la instancia no tenga renglón de tope por persona (`flow_route_person_cap_present`, el principio 12 del diseño).
- **A `legado`** exige que el scope del legado esté armado en la versión que tiene el bridge (`flow_route_legacy_not_armed`). Armarlo es un paso del operador y no lo hace esta función.
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
   - la vigente, `current_versions(history, versions)` (`flow_replay.py`), o la activa que dio la lectura;
   - la propuesta, `proposed_versions(versions, current, items)` con los `ProposalItem` de la propuesta (`flow_definition.py:844`).
4. **Simular**, `flow_replay.simulate(history, versions, fixed(vigente), fixed(propuesta), capabilities=ENGINE_CAPABILITIES_V1)` (`flow_replay.py:1326`). Devuelve una `Simulation` con su `summary`: los disparos, los cambios y el resumen de cada lado (`replay_summary`).
5. **Guardar.** La vista previa lleva el `summary` y los cambios recortados. Aprobar registra `simulation: built` con el sha256 del `summary`, o `simulation: failed` con su `reason_code` si algún paso falló. Con la ruta en `legado`, la simulación vale igual: simula lo que haría la definición, que es lo que el motor ejecutaría al pasar la ruta.

**Las capacidades** de la simulación son las del motor de la instancia. La v1 del motor ejecuta varios envíos por camino desde la T2.1. Hasta entonces, una propuesta con más de un envío sale con `engine_not_supported:mas_de_un_envio`. `PREVIEW_CAPABILITIES` es solo para el lienzo y nunca para aprobar.

**Lo que la simulación no sabe** lo dice el ciclo («Qué tan fiel es la simulación»): no sabe qué habría contestado alguien a un mensaje que no salió. La forma de decirlo es `sin_saber` en el resumen, con su nombre.

## 5. Los códigos

Los de este contrato, todos en forma `^[a-z][a-z0-9_]{1,79}$`:

| Código | Dónde | Qué quiere decir |
|---|---|---|
| `flow_engine_history_window_too_wide` | 2 | `p_since` es anterior a 8 días |
| `flow_engine_history_too_large` | 2 | la historia pasa de 1 MB |
| `flow_route_needs_published_version` | 3.1 | el flujo no tiene versión activa |
| `flow_route_kind_mismatch` | 3.1 | el disparador no es el del `legacy_kind` |
| `flow_route_legacy_scope_unknown` | 3.1 | el scope del legado no existe |
| `flow_route_already_exists` | 3.1 | el par ya tiene una ruta |
| `flow_route_generation_stale` | 3.2 | la ruta cambió desde que se leyó |
| `flow_route_kind_not_supported` | 3.2 | el motor de esta versión no ejecuta ese `legacy_kind` |
| `flow_route_flow_off` | 3.2 | el flujo está apagado |
| `flow_route_revalidation_invalid` | 3.2 | la revalidación falta, bloquea o es de otra versión |
| `flow_route_person_cap_present` | 3.2 | la instancia tiene tope por persona |
| `flow_route_legacy_not_armed` | 3.2 | el scope del legado no está armado |

Los errores de entrada siguen la convención del almacén: `22023` para una entrada inválida, `P0002` para lo que no existe o no es de la instancia (sin distinguir), y `55000` para un estado que no lo permite.

## 6. Las pruebas que fija el contrato

- **La forma de `read_flow_engine_v1`** contra un JSON Schema en `tests/fixtures/flow_engine/read_flow_engine_v1.schema.json`. Lo usan la prueba de la base (PGlite) y el doble de la T4, así que los dos verifican lo mismo.
- **La historia** pasa `flow_capture.validate_shape`, en PGlite, sobre una base sembrada con un carrito de tres toques.
- **La ruta:** cada `reason_code` de la sección 5 tiene su caso en PGlite, y el rojo a propósito: un `pago_fallido` a `motor` en la v1 da `flow_route_kind_not_supported`.
- **La simulación de punta a punta:** con la historia del fixture y una propuesta que cambia la espera de A2, `simulate` da cero cambios contra la vigente consigo misma, y cambios solo en el paso 2 del carrito.

## Lo que queda para la versión siguiente

- La ruta a `motor` del pago rechazado y de la ficha. Piden un esquema que exprese las condiciones (la variante de B1, el cupón de B1 y la fecha de vencimiento de la ficha).
- `en_curso: cancelar` y `migrar` (el ciclo).
- Un rol propio para el Automation Expert que lea `read_flow_engine_v1` sin pasar por `service_role`, si la T4 lo pide.
