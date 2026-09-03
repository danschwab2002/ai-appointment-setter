# Evidencia local: portabilidad inicial por aliada comercial

- Fecha: 2026-09-01
- Rama local: `feat/att1-commercial-ally-portability`
- Estado: implementación local parcial; no publicada ni desplegada
- Alcance: configuración, binding/readiness, admisión lead, stop durable de compra y política de descuento versionada sin efectos

## Cambios verificados

- manifiesto JSON no secreto de claves exactas;
- rechazo de account/inbox no legado sin manifiesto;
- rechazo de drift entre entorno, manifiesto y binding durable;
- validación parametrizada aislada de lead precheckout y pago fallido Hotmart;
- startup permite sólo `LEAD_PRECHECKOUT_ENABLED` y/o el stop portable de compra para manifiestos explícitos; Hottok exige el stop activo y todo otro flag booleano sigue rechazado, cerrando workers, agente, controles y efectos heredados;
- tabla/RPC de binding sin semillas específicas de cliente;
- readiness `default_off` que exige binding activo para ATT1.
- RPC portable atómica para `lead.precheckout`, cercada por la identidad durable
  server-owned del manifiesto y compatible con replay/conflicto;
- flag default-off `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED`; sólo bajo ese flag
  un manifiesto explícito puede usar `HOTMART_HOTTOK`;
- `PURCHASE_CANCELED` y `PURCHASE_OUT_OF_SHOPPING_CART` se ignoran antes de
  admisión durable con un reason code sin PII;
- RPC portable de compra que bloquea el binding activo, valida producto/oferta,
  exige una política temporal durable explícita sin seed y correlaciona sólo sus
  intents;
- match exacto marca `purchased` y cancela una reevaluación existente en la misma
  transacción; unmatched, ambiguous, duplicate y semantic conflict quedan
  fail-closed;
- cero filas en las nueve tablas presentes de timer/reevaluation, scheduled
  action, command, message o delivery durante el probe PGlite.
- tabla de políticas de descuento sin seed, con lifecycle forward-only,
  inmutabilidad desde aprobación y unicidad de publicación por binding/trigger;
- resolver fail-closed de política publicada/vigente; `service_role` no puede
  leer ni mutar la tabla y registrar/publicar no crea efectos.

La primera revisión independiente devolvió `CHANGES_REQUESTED`: detectó que las RPCs de lead y pago fallido seguían específicas de Johanna, que el fence inbound se había abierto antes de portar la cadena y que los campos enteros aceptaban floats. La segunda revisión confirmó esas correcciones, pero encontró que el worker de resolución, el formulario provisional y el receptor Hotmart genérico aún podían habilitarse. La tercera encontró un bypass por construcción directa de `Settings` con valores truthy no booleanos. Se añadieron regresiones exhaustivas sobre todos los campos booleanos con `1`, además de `"true"`, `None`, listas y objetos; ahora todos exigen tipo exacto `bool`. Para un manifiesto explícito el fence exceptúa únicamente `lead_precheckout_enabled` y `portable_hotmart_purchase_stop_enabled`; Hottok sólo es válido junto con ese stop. La cuarta detectó que un manifiesto podía copiar exactamente los valores Johanna y ser confundido con compatibilidad legada. El runtime conserva ahora la procedencia del archivo y aplica el fence y el readback durable a todo manifiesto suministrado, independientemente de la igualdad de valores. La quinta detectó que privilegios por defecto de Supabase podían dejar DML directo a `service_role` y que el validador SQL conservaba 54 entrypoints. La migración revoca ahora todos los privilegios de tabla antes de conceder sólo `select`; inventario y PGlite verifican ausencia de `insert`, `update`, `delete`, `truncate`, `references` y `trigger`, y el allowlist canónico contiene 55 funciones.

Una revisión posterior detectó que el inventario nuevo fallaba si la migración ATT1
aún no existía. Las consultas de privilegios usan ahora OIDs obtenidos con
`to_regprocedure` y `to_regclass`; el mismo validador PGlite exige
`fingerprint_absent` antes de aplicar la migración y `fingerprint_present` después.

## Pruebas automatizadas

Comando enfocado:

```text
uv run pytest tests/test_commercial_ally_config.py tests/test_commercial_ally_portability.py tests/test_commercial_ally_portability_migration.py tests/test_lead_precheckout.py tests/test_hotmart_webhook.py tests/test_deployment_config.py -q
```

Resultado posterior a la corrección de revisión: pasó, incluyendo regresiones de enteros fraccionarios, todos los flags booleanos de `Settings` y Hottok.

Comando canónico:

```text
uv run pytest -q
```

Resultado más reciente:

```text
exit 0; suite completa aprobada
```

Después de preservar el trabajo concurrente del dashboard en un commit local y
cerrar su claim, los paths compartidos se transfirieron al claim ATT1. El migration
tail, el inventario de esquema, el allowlist ACL y sus pruebas incluyen ahora
`20260901000100_commercial_ally_portability.sql`. La suite enfocada de 17 pruebas,
`compileall`, `git diff --check` y el preflight de coordinación también pasaron.

La validación PGlite específica de la admisión portable terminó con código 0:
exigió binding activo exacto; rechazó missing, inactive, drift y cada dimensión
canónica incorrecta; verificó `inserted`, replay, conflicto y replay del conflicto;
y midió cero efectos. El allowlist actualizado contiene 58 entrypoints
`service_role`, incluidas las RPCs portables y el resolver de descuentos.

El segundo contrato PGlite verificó missing, inactive y drifted binding; producto
y oferta incorrectos; unmatched, ambiguous y exact; duplicate y semantic
conflict; compra antes y después de una reevaluación existente; cancelación
atómica; y cero filas en nueve superficies de command/message/delivery/outbound.
El comando canónico `npm test` pasó e incluye los validadores de precheckout,
stop de compra y política de descuento, además del perfil ACL de 58 entrypoints.

El validador de descuentos probó tabla vacía/default-off, lifecycle
`draft → approved → published → retired`, rechazo de retiro sin aprobación,
inmutabilidad, una sola publicación por binding/trigger, expiración y ACL runtime
sin lectura ni DML directo.

La suite Python completa `uv run pytest -q`, `compileall`, los dos `node --check`
y `git diff --check` terminaron con código 0. La única advertencia fue la
deprecación conocida de `starlette.testclient` respecto a `httpx2`.

## Prueba HTTP real local

Se inició un servidor Uvicorn sobre TCP loopback con configuración ATT1 ficticia y una autoridad durable controlada. No hubo conexión a proveedores ni efectos salientes.

Resultado sanitizado:

```text
health_status=200
health_body={status: ok}
ready_status=200
ready_binding=active
ready_automation=default_off
server_stopped=true
```

Esto prueba transporte HTTP, fábrica ASGI, lifespan y readiness local. No prueba Supabase Cloud, Chatwoot, Hotmart, WABA, persistencia tras reinicio ni entrega física.

## Verificación SQL física

Docker continúa sin daemon disponible:

```text
Cannot connect to the Docker daemon at unix:///var/run/docker.sock
```

Además de PGlite, el stack completo de 53 migraciones se aplicó en PostgreSQL
real rootless 17.11. El inventario pasó de `fingerprint_absent` a
`fingerprint_present`; el binding conservó ACL `t|f|f|f|f|f|f` y la política de
descuento `f|f|f|f|t` para lectura/insert/update/delete/execute-resolver. Esto es
evidencia local desechable, no Supabase Cloud. El probe owner-level también
rechazó importe fijo sin moneda, comprobó timestamps de aprobación/publicación
asignados por PostgreSQL, impidió reescribir `created_at` y rechazó falsificar
`published_at` al retirar una versión aprobada que nunca fue publicada.

La revisión independiente final del snapshot V4 devolvió `APPROVE`, sin blockers
de seguridad, lógica ni documentación. El artefacto físico verificado tuvo SHA-256
`8d96250360f1122bfaef74819cc82cacd45709fb9decd404a2580050dd187f0d` y el
fingerprint de sus 14 paths fue
`3c0007729d430e81cfc8bbe98c04531fe568cc611e82e309eee8411755cc71f3`.

Los insumos comerciales recibidos el mismo día quedaron en un intake
machine-readable separado del manifiesto operativo. La prueba focalizada verificó
oferta `Alimenta Tu Tiroides`, precio base `USD 47`, audiencia, outcome de compra,
Mariana como receptora candidata y todos los gates de release/activación en
`false`. El artefacto no contiene IDs productivos, secretos, PII de leads ni
contenido crudo de las capturas.

## Fronteras no cruzadas

- sin commit del feature;
- sin push, PR o merge;
- sin migración Supabase Cloud;
- sin deploy o cambio de EasyPanel;
- sin creación de profile runtime ATT1;
- sin activación comercial;
- sin mensajes reales;
- sin secretos ni PII en los artefactos.
- sin cambios a conteos de mensajes, cadencia, triggers, delays o mecánica del
  workflow aprobado. La estructura durable/versionada existe, pero no contiene
  ninguna política publicada; la decisión de contenido de la primera plantilla sigue pendiente; este
  corte no importa tiempos ni cadencia desde documentación comercial.

## Pendientes antes de ATT1 funcional

- ratificar los facts recibidos y fijar owner comercial/operativo;
- recibir, custodiar y sanitizar los materiales declarados disponibles;
- confirmar país(es), idioma, referencias Hotmart/landing y destino Chatwoot de
  Mariana, incluido horario y SLA;
- parametrizar por separado la cadena outbound heredada (plantillas, delayed first-touch, carrito y pago fallido); ninguna forma parte de este corte;
- instalar el stack ATT1 limpio y cargar un binding real como `draft`;
- validar, aprobar y activar ese binding sin habilitar efectos;
- realizar tracer, inbound, precheckout y stops con autorizaciones separadas.
