# Correlaciones V3: verificacion operativa desde Codex

- Fecha: 2026-09-18.
- Estado: transporte HTTP local legacy/V3 y correccion SQL verificados localmente;
  E2E externo pendiente de integracion, migracion y confirmacion humana.
- Claim: `codex-correlation-operations-v1`.
- Base de trabajo: `0a79688645624a3ed641cfba3245b48feea414d2`.
- Alcance: Johanna; no valida ATT1 ni entrega originada desde Hotmart.

## Procedencia y limites

El integrador habilito el worktree y aprobo el preflight inicial el
2026-09-18 a las 19:16:44 UTC, exit 0. La autorizacion operativa y el scope
constan en `/var/lib/codex-development/operations/workspaces.json` y su README.
Los efectos externos se serializan con coordinacion mediante `codex-ops`.

Los resultados administrativos citados abajo son inventarios sanitizados.
No se leyeron secretos ni identidades privadas desde esta tarea. Salud, presencia
de RPC y pruebas con simuladores no equivalen a un E2E real de Slack/Supabase.

## Release observado

Inventario `54fd47f43e8b4827846bfd220fa43aca`, completado a las 19:32:46 UTC:

| Componente | SHA declarado | Evidencia |
| --- | --- | --- |
| Appointment Bridge | `108d2ee889babb31ac121e5e8db82b8817135b81` | Una instancia sana; `correlation_preresolution.py` coincide con el SHA declarado y `origin/main` inspeccionado. |
| Slack Connector | `6287c14f8d937530460a28b47ded1f6405b06317` | Una instancia sana; `slack_correlation/app.py` coincide con el SHA declarado y `origin/main` inspeccionado. |

El bridge ya tenia habilitadas lectura/escritura operator, pre-resolucion y
proyeccion Slack; el prompt era `correlation-preresolution-v3`. El conector ya
tenia ingreso, notificaciones e interacciones habilitados; backfill apagado.
Esta inspeccion no cambio flags ni desplego servicios.

OpenAPI expuso las RPC de pre-resolucion, proyeccion V3 y el schema del motivo
de abstencion. Esto no prueba el ledger de migraciones, cuerpo SQL ni ACL exacta.
Los mapas de Slack contenian un tenant; su presencia tampoco acredita por si
sola canal y operador autorizados para el caso de prueba.

## Verificacion local ejecutada

El operador reporto 62 pruebas iniciales aprobadas antes de esta tarea. Sin
un listado individual, ese conteo no se suma al siguiente como cobertura unica.

Se ejecuto adicionalmente:

```sh
uv run --locked pytest tests/test_slack_connector_http_e2e.py tests/test_slack_projection_worker.py tests/test_slack_correlation_producer.py -q
```

Resultado: 16 pruebas aprobadas, exit 0. El recorrido TCP usa servidores
Uvicorn y Slack sintetico exclusivamente en loopback, con SQLite temporal y
Supabase sustituido por un doble. Comprueba publicacion, interaccion firmada,
modal privado, prepare/confirm separados y actualizacion del mismo mensaje.
Las pruebas de proyeccion/producer comprueban el contrato V3 y recomendacion
persistida.

El integrador amplio el claim para `tests/test_slack_connector_http_e2e.py`
y aprobo el preflight a las 19:46:20 UTC, exit 0, solicitud
`edb01f9e152b4f8aa14bd4a7166518ee`. Se parametrizo la interaccion TCP para
legacy y V3; se verifican replay sin duplicado, recomendacion publica sin
identidad privada, boton ligado al caso y ausencia de prepare/confirm antes
de la decision humana simulada. La prueba V3 inyecta una recomendacion fixture
con evidencia `prior_verified_identity`; no prueba que SQL o el modelo la
generen.

```sh
uv run --locked pytest tests/test_slack_connector_http_e2e.py -q
```

Resultado tras el cambio: 3 pruebas aprobadas, exit 0. Incluyen una notificacion
generica y las dos variantes de interaccion. No se suman a las 16 anteriores
como pruebas independientes.

La suite canonica `uv run --locked pytest` termino con 2125 aprobadas,
3 fallidas y una advertencia en 222.85 segundos. Las tres fallas corresponden
al instalador ATT1 en `tests/test_att1_product_profiles.py`: `PermissionError`
al abrir el ancestro `hermes` bajo el usuario restringido, antes de probar
sus escenarios de instalacion. No se modificaron esos archivos ni permisos.
Este resultado no se declara suite completa aprobada; requiere verificacion
del integrador en un entorno de desarrollo con lectura autorizada.

El operador habilito un alias estable del mismo worktree bajo
`/var/lib/codex-development/workspaces/ai-appointment-setter-codex-correlations`,
sin clonarlo ni ampliar acceso a secretos. Tras la reparacion se revalido
`tests/test_att1_product_profiles.py`: 10 pruebas aprobadas, exit 0. El preflight
documental `4eb737532ea14ef987e116ac21f8a664` termino con exit 0 a las
20:43:08 UTC; los dos paths compartidos de daily feedback siguen reservados.

Desde el alias estable se repitio la suite canonica con el entorno indicado
por el operador:

```sh
PYTHONPATH=src .venv/bin/python -m pytest
```

Resultado final: 2126 aprobadas, 2 fallidas y una advertencia en 202.70 segundos,
exit 1. Ya no hay fallos ATT1. Las dos fallas son la ultima migracion esperada
en `tests/test_supabase_release_readiness.py` y la cobertura del inventario
en `tests/test_supabase_schema_inventory.py`; ambas detectan correctamente
la migracion nueva aun no incorporada a los metadatos reservados.

## Correccion del reloj de prepare

La suite SQL de CI expuso `operator_correlation_resolution_command_invalid`
al preparar un cierre sin asociacion. La implementacion previa omitio
`prepared_at`, tomando su default `clock_timestamp()`, mientras calculaba
`expires_at` con otra lectura del reloj. El trigger exige correctamente
`expires_at = prepared_at + interval '10 minutes'`; una diferencia entre las
dos lecturas provoca SQLSTATE `23514`. Afecta ambas acciones nuevas de prepare,
no el replay de un comando existente.

El integrador reservo `20260918000100_operator_correlation_prepared_at.sql`
y el validador SQL mediante `967a728c7c0243b7bf81dd53ba6ba3f3`, con preflight
exit 0 a las 20:09:32 UTC. La migracion forward captura un unico instante
inmediatamente antes del INSERT y lo usa explicitamente para ambos campos.
Conserva la igualdad estricta del trigger, la firma, owner, ACL, `SECURITY
DEFINER`, `search_path`, scope, snapshots e idempotencia. El rewrite exige
exactamente una ocurrencia de cada fragmento esperado y verifica el resultado
y la preservacion del trigger antes de commit.

Regresion local determinista en PGlite: el validador cambia temporalmente el
default de `prepared_at` a 2000-01-01, con restauracion en `finally`. Un RPC
correcto aporta su propio instante actual y un TTL exacto de diez minutos.
Esto evita depender de que dos lecturas del reloj caigan en el mismo tick.

- Sin la migracion nueva: SQLSTATE `23514`,
  `operator_correlation_resolution_command_invalid`, exit 1.
- Con la migracion: `manual_resolution`, `replay`, `stale_guard`,
  `owner_forgery_guard` y `zero_effects` aprobados, exit 0. Incluye asociacion,
  cierre sin asociacion, timestamps actuales y expiracion inmutable en replay.
- Herramientas locales: Node 22.23.1 oficial verificado por el operador y
  PGlite 0.5.4 instalado con `npm ci --ignore-scripts` y lockfile existente.
- No se aplico esta migracion a Cloud desde el worktree.

La integracion debe agregar el fingerprint de esta migracion a
`scripts/supabase_schema_inventory.sql` y actualizar el ultimo archivo esperado
en `tests/test_supabase_release_readiness.py`. Ambos paths conservan cambios y
reservas ajenas: no fueron editados por esta tarea. Su reconciliacion serial por
el integrador es un requisito antes de publicar el parche completo.

Verificaciones focales adicionales: 41 pruebas de migracion, resolucion y
consultas operator aprobadas, exit 0. Inventario/readiness: 15 aprobadas y los
2 fallos de metadatos anteriores, exit 1; no se suman a la suite canonica.

La suite SQL completa se intento desde `tests/sql/followup_engine`:

```sh
TZ=UTC PATH=/home/codex/.local/bin:$PATH npm test
```

Sin `TZ=UTC`, el primer validador falla en la ventana de negocio por diferir la
zona horaria local de la politica UTC del fixture; ese validador no carga esta
migracion. Una interrupcion posterior por `MODULE_NOT_FOUND` ocurrio durante
el fallo de acceso al ancestro del worktree: tras la reparacion se comprobo que
el archivo existe, es legible y su validador termina correctamente.

El intento final con UTC y alias estable alcanzo
`validate_acl_hardening.mjs:267` y termino con exit 1: falta exclusivamente el
fingerprint de `20260918000100_operator_correlation_prepared_at.sql`, sin
fingerprints parciales. Los validadores anteriores, incluidos proyeccion,
contexto, pre-resolucion V3, abstencion y handoff, terminaron correctamente.
Los validadores posteriores no se ejecutaron en esa suite encadenada; la
regresion focal de resolucion manual citada arriba si fue ejecutada por separado.
No se presenta este intento como suite SQL completa aprobada.

## Caso necesario para el E2E externo

La inspeccion exacta `41be9cf0b7e54c4991428978a71f9752`, completada a las
19:51:06 UTC, devolvio HTTP 200, caso encontrado, `unmatched` y cero candidatos.
Confirmo scope configurado, canal coincidente, allowlist no vacia y exactamente
un binding raiz aceptado en el ledger local del conector. No abrio Slack ni
consulto identidad privada; tampoco genero mensajes o cambios.
No se lo debe reusar para acreditar una recomendacion V3 nueva: el claim SQL
excluye eventos con proyecciones historicas V1/V2 o entregas ya intentadas.

El caso positivo debe ser nuevo, sintetico y comprobado en el scope autorizado,
con al menos dos candidatos y una unica evidencia discriminante. La politica
SQL de proximidad temporal exige una candidata a 0..15 minutos y otra al menos
5 minutos mas lejos; una sola candidata no satisface ese discriminador.
El fixture local existente usa 4 y 20 minutos. No se copian sus INSERT directos
a Cloud sin validar privilegios, scope, coherencia e aislamiento.

La preparacion propuesta usa dos intents nuevos con email sintetico compartido
y telefonos sinteticos distintos, ambas autorizaciones en false, sin crear
contactos, submissions, timers o secuencias. El ingreso de compra lleva email
pero no telefono ni `sck` de checkout, con transaction y external event ID nuevos
y colision cero. El wrapper de compra no ejecuta delivery sincronico; la
correlacion ambigua marca exclusivamente los candidatos `tracking_incomplete`
y mantiene `activation_authorized=false`. La cancelacion de timers del wrapper
solo ocurre con outcome `resolved`.

Es obligatorio revalidar esa superficie SQL desplegada y comprobar que el worker
generico de compras sigue apagado y no existe otro consumidor antes del probe.
Reutilizar una transaccion podria crear un conflicto semantico que bloquee efectos
comerciales globalmente; por eso el probe aborta ante cualquier identidad previa
o outcome diferente del esperado. No se ejecuta un pago real.

La aceptacion exige:

1. Admision HTTP de evento Hotmart sintetico y correlacion ambigua/conflictiva
   con candidatos coherentes, sin autorizar contacto ni recovery.
2. Modelo real y validacion deterministica producen recomendacion durable V3.
3. Una unica tarjeta aparece en el canal autorizado y queda ligada al evento.
4. Operador allowlisted abre el modal privado, elige candidato y fundamento,
   revisa y confirma en un segundo paso.
5. Supabase conserva una unica resolucion y Slack actualiza el mensaje raiz
   original; cero duplicados y cero nuevos efectos comerciales.

Controles: unmatched no llama al modelo ni Slack; abstencion no notifica;
cancelacion mantiene pendiente; replay no duplica; evidencia obsoleta rechaza;
identidad publica permanece enmascarada y privada no se persiste en logs.

Los workers ya activos obligan a comprobar backlog y aislamiento antes de
crear el fixture. No se ampliaran ACL del runtime para preparar datos ni se
asumira backlog cero por una lectura denegada. La accion humana final es abrir
y confirmar el caso en Slack una vez que exista una tarjeta controlada real.

## Referencias

- [Contrato vigente Slack](../contracts/slack-correlation-resolution-v1.md).
- [Pre-resolucion y rollout V3](../design/slack-correlation-ai-preresolution-v1.md).
- [Release de identidad privada](2026-09-12-slack-correlation-private-identity-release.md).
- [Procedimiento de despliegue](slack-connector-deployment-runbook.md).

Los pasos historicos que describen unmatched accionable o modal privado
enmascarado no sustituyen el contrato V3 y la implementacion actuales.
