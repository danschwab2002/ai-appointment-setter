# Evidencia local: portabilidad inicial por aliada comercial

- Fecha: 2026-09-01
- Rama local: `feat/att1-commercial-ally-portability`
- Estado: implementación local parcial; no publicada ni desplegada
- Alcance: configuración, validación aislada de parsers, binding durable y readiness

## Cambios verificados

- manifiesto JSON no secreto de claves exactas;
- rechazo de account/inbox no legado sin manifiesto;
- rechazo de drift entre entorno, manifiesto y binding durable;
- validación parametrizada aislada de lead precheckout y pago fallido Hotmart;
- rechazo de startup para cualquier flag booleano o Hottok configurado en ATT1, cerrando receptores, admisiones, workers, agente, controles y efectos heredados;
- tabla/RPC de binding sin semillas específicas de cliente;
- readiness `default_off` que exige binding activo para ATT1.

La primera revisión independiente devolvió `CHANGES_REQUESTED`: detectó que las RPCs de lead y pago fallido seguían específicas de Johanna, que el fence inbound se había abierto antes de portar la cadena y que los campos enteros aceptaban floats. La segunda revisión confirmó esas correcciones, pero encontró que el worker de resolución, el formulario provisional y el receptor Hotmart genérico aún podían habilitarse. La tercera encontró un bypass por construcción directa de `Settings` con valores truthy no booleanos. Se añadieron regresiones exhaustivas sobre todos los campos booleanos con `1`, además de `"true"`, `None`, listas y objetos; ahora todos exigen tipo exacto `bool` y ATT1 exige `False`. La cuarta detectó que un manifiesto podía copiar exactamente los valores Johanna y ser confundido con compatibilidad legada. El runtime conserva ahora la procedencia del archivo y aplica el fence y el readback durable a todo manifiesto suministrado, independientemente de la igualdad de valores. La quinta detectó que privilegios por defecto de Supabase podían dejar DML directo a `service_role` y que el validador SQL conservaba 54 entrypoints. La migración revoca ahora todos los privilegios de tabla antes de conceder sólo `select`; inventario y PGlite verifican ausencia de `insert`, `update`, `delete`, `truncate`, `references` y `trigger`, y el allowlist canónico contiene 55 funciones.

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

La suite SQL conductual completa se ejecutó con PGlite mediante `npm test` y
terminó con código 0. El probe ACL informó `public_functions=127` y
`service_entrypoints=55`, incluyendo la lectura del binding y la ausencia de DML
directo para `service_role`.

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

Se intentó iniciar PostgreSQL desechable mediante Docker. El host expone el cliente Docker, pero el daemon no está disponible:

```text
Cannot connect to the Docker daemon at unix:///var/run/docker.sock
```

La migración se aplicó y ejercitó localmente mediante PGlite, incluido el perfil ACL con privilegios por defecto estilo Supabase. No se pudo ejecutar contra PostgreSQL real y no se afirma esa evidencia.

## Fronteras no cruzadas

- sin commit del feature;
- sin push, PR o merge;
- sin migración Supabase Cloud;
- sin deploy o cambio de EasyPanel;
- sin creación de profile runtime ATT1;
- sin activación comercial;
- sin mensajes reales;
- sin secretos ni PII en los artefactos.

## Pendientes antes de ATT1 funcional

- obtener una nueva revisión independiente del snapshot corregido;
- ejecutar migration y probes en PostgreSQL desechable;
- parametrizar la cadena outbound heredada (plantillas, delayed first-touch, carrito y pago fallido);
- instalar el stack ATT1 limpio y cargar un binding real como `draft`;
- validar, aprobar y activar ese binding sin habilitar efectos;
- realizar tracer, inbound, precheckout y stops con autorizaciones separadas.
