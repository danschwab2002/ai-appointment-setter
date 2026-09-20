# Verificacion operativa del Stalled Conversation Monitor

- Estado: preparacion habilitada; E2E real pendiente.
- Fecha: 2026-09-18.
- Base inspeccionada: `0a79688645624a3ed641cfba3245b48feea414d2`.
- Claim: `codex-stalled-verification-prep-v1`.
- Responsable de efectos: operador administrativo, serializado por coordinacion.

## Alcance y autorizacion

La autorizacion de Dan del 18-09-2026 y el canal administrativo descrito en
`/var/lib/codex-development/operations/README.md` habilitan implementacion,
configuracion, deploy y E2E necesarios para esta linea. Sustituyen la limitacion
documental inicial; no amplian los destinatarios autorizados ni entregan secretos
al agente. `workspaces.json` registra el claim ampliado y preflight aprobado a
`2026-09-18T19:16:41.060851+00:00`.

El [plan de verificacion](chatwoot-stalled-monitor-verification-plan-v1.md)
conserva la evidencia offline y matriz completa. Este registro agrega el estado
administrativo observado y el procedimiento del operador; no acredita un E2E
por la existencia de pruebas simuladas. El contrato implementado permanece en
[monitor V1](../contracts/chatwoot-stalled-conversation-monitor-v1.md).

## Evidencia administrativa

Solicitud `d9c1102dd8904561bfb9a2fb5a2ab276`, estado `completed`, respuesta del
operador a las `2026-09-18T19:21:10.138031+00:00`:

| Comprobacion | Resultado observado |
| --- | --- |
| Servicio `infra_appointment-bridge` | Una instancia running, health healthy |
| Inicio de instancia | `2026-09-18T15:15:09.77577183Z` |
| Imagen | `sha256:e6598d47375d9d90e26e39021914888d1908772b3fb6d129b1a520a6c98a663b` |
| `CHATWOOT_STALLED_MONITOR_ENABLED` | `false` |
| `CHATWOOT_CUT_B_ADMISSION_ENABLED` | `false` |
| `CHATWOOT_CUT_B_AGENT_ENABLED` | `false` |
| `CHATWOOT_AUTOMATED_REPLIES_ENABLED` | `false` |
| `PAYMENT_LINK_ENABLED` | `false` |
| Efectos de solicitud | Inventario de lectura, sin mutaciones de servicio/config |

Esta primera respuesta no fija el SHA del codigo desplegado, no demuestra el
contenido de `/ready` y no valida entrega de mensajes.

El inventario ampliado `54fd47f43e8b4827846bfd220fa43aca`, `completed` a las
`2026-09-18T19:32:46.266018+00:00`, confirma:

- SHA declarado del bridge: `108d2ee889babb31ac121e5e8db82b8817135b81`.
- `src/bridge/chatwoot.py` coincide con ese SHA y difiere de `origin/main`
  (`0a79688645624a3ed641cfba3245b48feea414d2`). La validacion offline del codigo
  de main no acredita que ese scanner este desplegado.
- `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=false`, con una restriccion de
  remitente configurada. Cuenta con un solo inbox y el inbox configurado existe.
- No se demostro aislamiento del inbox ni propiedad del destinatario de prueba.
  El operador no accedio a contactos ni mensajes en este inventario.

Antes del E2E se necesita release verificado del scanner actual con gates
apagados, coordinado con los cambios de links que comparten bridge. No hace
falta modificar scanner/sender para corregir una diferencia de despliegue.

Se ejecuto la suite completa `tests/test_chatwoot.py` sobre la base inspeccionada
para ampliar la cobertura previa al release: **93 passed in 0.94s**. Comando:

```sh
/home/codex/.local/bin/uv run --locked --offline --no-sync pytest \
  -p no:cacheprovider tests/test_chatwoot.py
```

No se suman esos 93 a los 30/13 previos como si fueran casos distintos; esta
ejecucion incluye aquellos grupos. Usa transporte simulado, sin proveedores reales.

La ampliacion `ef7c851ff6624f84964cf384dec130bd` reservo `tests/test_webhook.py`
y registro preflight con exit 0 a las `2026-09-18T19:46:18.524761+00:00`.
La prueba `test_stalled_monitor_recovers_through_real_worker_and_sender` agrega
dos escenarios con aplicacion, scanner, monitor, inbox durable, worker y sender
reales. Chatwoot usa `MockTransport`; Hermes y Supabase usan stubs existentes.

- Una actividad posterior permite recuperar el inbound y producir un POST.
- Dos scans adicionales del mismo snapshot no producen otra respuesta.
- Una pausa posterior al modelo bloquea el POST final.
- Un control ya pausado no genera admision ni envio.
- HTTP en proceso mediante `TestClient` comprueba `/ready=healthy`.

Resultado focal: **2 passed, 1 warning in 6.60s**. Suite del archivo modificado:
**100 passed, 1 warning in 21.04s**. El warning es la deprecacion existente de
`httpx` en `starlette.testclient`. Comando de la suite:

```sh
/home/codex/.local/bin/uv run --locked --offline --no-sync pytest \
  -p no:cacheprovider tests/test_webhook.py
```

Esto acredita integracion local con dependencias simuladas; no es HTTP de red,
prueba de credenciales, ejecucion de Hermes real ni entrega Chatwoot/WhatsApp.

La suite canonica se ejecuto con el mismo comando sin seleccionar archivos:
**2126 passed, 3 failed, 1 warning in 227.45s**. Los tres fallos pertenecen a
`tests/test_att1_product_profiles.py`: creacion del home privado, rechazo de
parent symlink y fallo previo a publicacion. En los tres, el instalador falla
al abrir un ancestro `hermes` con `PermissionError`, antes de alcanzar la
operacion bajo prueba. La restriccion de acceso requiere revalidacion por el
operador en su entorno; no se alteraron ACL ni codigo ATT1. Esta ejecucion no
se reporta como suite canonica aprobada. `git diff --check` no reporto errores.

## Procedimiento de ejecucion

### 1. Fijar revision, aislamiento y baseline

El operador verifica SHA/artefacto, revision desplegada y rollback; confirma
HTTP de `/ready` con monitor apagado y prerequisitos sanos. Publicacion o deploy
por si solos no acreditan los casos funcionales.

Antes de habilitar respuestas, fija referencias administrativas a cuenta/inbox,
JID exacto aprobado, conversacion de prueba y controles sinteticos. No copia
identidades, cuerpos ni credenciales al repositorio. Releva el gate de multiples
remitentes, conteos de candidatos elegibles y trabajo previamente admitido.

El scanner filtra por cuenta/inbox y remitente, pero no ofrece filtro exclusivo
por conversation_id. Activarlo requiere tambien admission, agent y replies, que
habilitan el pipeline normal. Por tanto la coordinacion debe confirmar el
aislamiento efectivo del conjunto de destinatarios antes de aplicar esos gates.
No se asume aislamiento porque una sola conversacion sea la deseada.

Preferir instancia e inbox de prueba con JID exacto y almacenamiento propios.
Si se utiliza infraestructura compartida, el operador documenta como mantiene
el scope aprobado y evita efectos sobre otros clientes; no cambia globalmente
webhooks, workers o gates para producir el caso.

### 2. Preparar un inbound recuperable

El operador verifica si existe un inbound sintetico autorizado, aun sin respuesta
y dentro de la ventana efectiva. Si no existe, prepara el contacto/canal antes
de solicitar el unico inbound humano necesario. No fabrica mensajes de clientes
ni expone su contenido en la evidencia versionada.

Debe demostrarse que el trigger no tiene una admision normal activa que pueda
responder durante la observacion. La omision controlada de entrega webhook al
bridge solo es valida para el entorno/contacto aislado y sin reentrega concurrente.
El mecanismo concreto queda pendiente hasta confirmar el inventario del canal;
no es una capacidad nueva implementada por este documento.

Antes de activar, registrar conteos de mensajes y partes por alias sintetico,
trabajo durable del trigger y controles. Fijar numero esperado de partes,
intervalo de scan, edad minima/maxima, cooldown, limite de admisiones, numero de
scans, presupuesto de envios y plazo de observacion. No inferir estos valores
de los defaults del contrato.

### 3. Ejecutar recuperacion y exclusiones

La coordinacion reserva la ventana del bridge; links de compra no ejecuta su
E2E al mismo tiempo. El operador registra solicitud aceptada, baseline y estado
`running` antes de cambiar gates. La activacion acotada queda pendiente hasta
la confirmacion del scope de la etapa 1.

Con el caso preparado, habilitar el monitor en el entorno autorizado y observar
scan completo, `/ready` healthy, admision `stalled-chatwoot:<conversation_id>:<message_id>`
y respuesta atribuible. Mantener las referencias reales solo en evidencia
administrativa privada. Para una parte: un POST aceptado, una respuesta visible
en el canal, cero respuestas adicionales al mismo trigger.

Repetir scans sobre ese trigger sin nuevas respuestas. Ejecutar por separado
los casos de actividad posterior, conversacion avanzada, pausa y asignacion
humana usando controles sinteticos autorizados. La matriz del plan define las
demas exclusiones; los fallos inducidos o carreras se ejecutan solo en entorno
controlado. Si no se ejecuta un caso, registrar `no ejecutado`.

### 4. Cerrar y conciliar

Detener admisiones al concluir la ventana; comprobar tambien colas y workers
que pueden procesar trabajo ya admitido. Apagar solo el monitor no acredita
contencion de envios. Aplicar el rollback acordado y verificar HTTP y gates
finales, sin borrar ledgers ni colas para obtener un resultado favorable.

Conciliar por trigger/parte los conteos, aceptacion y entrega; los controles
deben tener delta cero de envios atribuibles al monitor. Un timeout o entrega
incierta exige reconciliacion, no reintento ciego. Una solicitud administrativa
`running` o `unknown` tampoco se repite con otro ID.

## Variante canary privada pendiente de habilitacion

El codigo permite verificar el monitor en una instancia privada con JID exacto,
sin activar links ni el scope multirremitente. La propiedad del JID de prueba
debe estar verificada administrativamente antes de cualquier efecto. Esta
variante no esta desplegada ni aprobada por un resultado funcional en este registro.

La inspeccion historica `da16453cbe72483fa153f83660e9653e`, completada a las
`2026-09-18T19:51:06.962872+00:00`, encontro una submission, una intencion,
un telefono canonico, un owner y tres conversaciones bloqueantes para la
referencia historica revisada. Esa identidad no es un destinatario fresco:
no reutilizarla ni quitar sus handoffs para facilitar el E2E. Hace falta otra
referencia de prueba aprobada y propiedad verificada. Esta respuesta no demuestra
que el JID configurado actualmente sea esa misma identidad historica.

La canary usa imagen/SHA revisados, `CAPTURE_DIR` y `REPLY_DIR` propios y vacios,
sin reutilizar ledgers productivos. No recibe webhooks publicos y no se anuncia
en el routing compartido. La configuracion se construye explicitamente, sin
heredar ciegamente los workers y credenciales de otros subsistemas.

| Configuracion canary | Condicion |
| --- | --- |
| `ALLOWED_WHATSAPP_JID` | Referencia a identidad exacta verificada; valor privado |
| `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`, `PAYMENT_LINK_ENABLED` | `false` |
| `HERMES_SHADOW_ENABLED`, `CHATWOOT_CUT_B_ADMISSION_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED` | `true` solo en canary durante la ventana |
| `CHATWOOT_STALLED_MONITOR_ENABLED` | Apagado en baseline; activo solo durante la ventana |
| Cuenta, inbox, AgentBot, Cut B scope key/version | Valores canonicos server-side verificados |
| `RESOLUTION_WORKER_ENABLED`, `HOTMART_PURCHASE_WORKER_ENABLED`, `HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED` | `false` |
| `DURABLE_DISPATCHER_ENABLED`, `DURABLE_OUTBOUND_ENABLED` | `false` |
| `HUMAN_HANDOFF_PROJECTION_ENABLED`, `CORRELATION_PRERESOLUTION_ENABLED`, `SLACK_CONNECTOR_PROJECTION_ENABLED` | `false` |
| Gates de precheckout, first-touch, abandonment/payment-failure y endpoints de correlacion | Desactivados; no necesarios para este caso |
| `CHATWOOT_DURABLE_OPT_OUT_ENABLED` | `false` en esta canary sin ingress webhook |
| `CHATWOOT_OPT_OUT_MACRO_ID`, `CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID` | Omitidos; no heredar valores productivos |

La omision de esas dos ultimas configuraciones es necesaria: el codigo crea
`OptOutProjectionWorker` por presencia de IDs, macro y worker_id, aunque el
flag durable opt-out este apagado. La consulta del stop durable antes del modelo
y envio se conserva por `opt_out_enforcement_enabled` al tener Supabase, cliente
Chatwoot e IDs canonicos. El operador comprueba que solo existan los workers
Chatwoot y monitor; los otros siete consumidores del lifecycle deben ser nulos.

Con los gates de respuesta normales todavia apagados en el bridge compartido,
un inbound sintetico puede recibirse por el webhook existente sin suspenderlo.
La canary lo recupera por scan desde su almacenamiento propio. Antes hay que
comprobar que ninguna tarea pendiente de otros flujos activos pueda responder
al mismo contacto, y que el bridge compartido conserva sus gates apagados.
La trazabilidad debe separar su captura/shadow normal del POST emitido por la
canary. Esta alternativa no habilita cambios globales de webhook.

Readiness esperado: `chatwoot_stalled_monitor=disabled` en baseline; 503 mientras
no hay scan completo o despues de un error; `healthy` tras scan completo. El
campo general `automation_state=default_off` no prueba ausencia de envios del
monitor: este tiene sus gates propios. Si el runtime usa binding comercial
portable, su resolucion en Supabase tambien debe pasar para HTTP 200.

El scanner consulta conversaciones del inbox completo antes de filtrar el JID;
aislamiento de envios no significa aislamiento de lectura. La autorizacion y
evidencia administrativa deben contemplar ese alcance de consulta.

## Resultados pendientes

| Resultado | Estado |
| --- | --- |
| SHA declarado/archivo efectivo | Verificado por inventario; scanner difiere de main |
| HTTP `/ready` del baseline | Pendiente |
| Scope y mecanismo de inbound aislado | Pendiente de verificacion administrativa |
| Recuperacion con respuesta visible atribuible | No ejecutado |
| Repeticion sin duplicacion | No ejecutado |
| Actividad posterior y controles de exclusion | No ejecutado |
| Contencion final y conteos reconciliados | No ejecutado |
| Activacion recurrente | Sin decision; requiere revision posterior del resultado |

No se identifico un defecto de scanner/sender que justifique modificar codigo
con la evidencia disponible. Las pruebas offline previas se conservan en el
plan, sin reejecutarlas como sustituto de estas comprobaciones reales.
