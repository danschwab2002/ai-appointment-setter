# Verificacion operativa coordinada de Appointment Setter

- Estado: en curso; no acredita E2E completo ni activacion recurrente.
- Fecha: 2026-09-18.
- Claim: `codex-appointment-operations-v1`.
- Base de las pruebas compartidas: `0a79688645624a3ed641cfba3245b48feea414d2`.
- Frentes: monitor de conversaciones, correlaciones V3 y links de compra V2.

## Autoridad y ejecucion

Dan autorizo implementar, configurar, desplegar y probar estos tres frentes.
Los worktrees y permisos estan registrados por el operador en
`/var/lib/codex-development/operations/workspaces.json`. El preflight inicial de
coordinacion termino con exit 0 a `2026-09-18T19:16:42.752096+00:00`.
La habilitacion administrativa esta en `READY.json`, con fecha
`2026-09-18T19:21:52.677890+00:00`.

El operador conserva Git compartido, preflight global, publicacion, integracion
y administracion de servicios. `codex-ops` es una bandeja revisada por el
operador, no una interfaz de ejecucion privilegiada arbitraria. Solo las
respuestas con efectos verificados acreditan una operacion completada.
Credenciales, payloads y datos personales permanecen fuera de este documento.

## Baseline administrativo

Fuente: respuesta `d9c1102dd8904561bfb9a2fb5a2ab276`, completada a
`2026-09-18T19:21:10.138031+00:00`; inventario de solo lectura.

| Servicio | Estado observado | Imagen |
| --- | --- | --- |
| `infra_appointment-bridge` | Una instancia running/healthy | `sha256:e6598d47375d9d90e26e39021914888d1908772b3fb6d129b1a520a6c98a663b` |
| `infra_supportmagician-slack-connector` | Una instancia running/healthy | `sha256:4392a90dc51f7b81ccbcf94b3e4da5b559afac3985f306b494bcb3ec41c28328` |

En ese baseline, `CHATWOOT_STALLED_MONITOR_ENABLED`,
`CHATWOOT_CUT_B_ADMISSION_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`,
`CHATWOOT_AUTOMATED_REPLIES_ENABLED` y `PAYMENT_LINK_ENABLED` estan en `false`.
Un estado healthy no demuestra la revision de codigo ni exito funcional.

## Pruebas compartidas

Ejecutadas como `codex`, en el worktree de coordinacion y su `.venv` propio:

```sh
/home/codex/.local/bin/uv run --locked --offline --no-sync pytest \
  -p no:cacheprovider tests/test_webhook.py tests/test_readiness.py \
  tests/test_deployment_config.py
```

Resultado: **147 passed, 1 warning in 15.69s**, exit 0.
El warning es `StarletteDeprecationWarning` por uso de `httpx` con
`starlette.testclient`. No se actualizaron dependencias. Estas pruebas no
acreditan llamadas reales a proveedores ni E2E productivo.

## Verificacion administrativa solicitada

Solicitud `54fd47f43e8b4827846bfd220fa43aca`, `coordination/inspect-runtime`:
correspondencia imagen/SHA, migraciones de checkout V2 y correlacion V3,
RPC/ACL, prompt V3, flags y readiness; referencias de prueba verificadas
server-side y aislamiento posible del monitor. Se solicito salida sanitizada
y ninguna mutacion de servicios o configuracion.

Respuesta completada a `2026-09-18T19:32:46.266018+00:00`:

- Bridge declara `108d2ee889babb31ac121e5e8db82b8817135b81`; `chatwoot.py`
  coincide con esa revision, pero difiere de main. Los archivos inspeccionados
  de checkout issuance y pre-resolucion si coinciden con main.
- Conector Slack declara `6287c14f8d937530460a28b47ded1f6405b06317`;
  su `app.py` coincide con main. Ambos servicios siguen healthy.
- Prompt V3 confirmado. OpenAPI expone las RPC V2/V3 y el campo de motivo de
  abstencion, pero no acredita ledger de migraciones, cuerpos ni ACL exactas.
- Hay un unico inbox Chatwoot y un remitente configurado; aislamiento y
  propiedad del contacto de prueba no estan acreditados. Tampoco se verifico
  autorizacion del canal/operador Slack, catalogo, importe o entregas ambiguas.

Complemento `d6b577d1066642bc882eb2edbe06f5b0`: la credencial runtime obtuvo
HTTP 403 al intentar conteos exactos. No se ampliaron ACL. Los workers de
pre-resolucion y proyeccion Slack ya estaban habilitados antes de esta tarea.

La respuesta `ab967308b2a94b33bfd688b0a1cb0b23`, completada a
`2026-09-18T19:43:51.114933+00:00`, resuelve ese inventario mediante Management
API con `read_only=true`, credenciales administrativas existentes retenidas en
el servidor y proyecto verificado contra runtime:

- Ledger: `20260912000100`, `20260914000100`, `20260916000100` y
  `20260917000100` presentes. Fingerprints seleccionados: checkout V2 5/5,
  pre-resolucion 3/3 y abstencion 4/4.
- RPC inspeccionadas: `service_role` con ejecucion; `anon` y `authenticated`
  sin ella. No constituye certificacion completa del esquema ni de todas las ACL.
- Catalogo del scope: una entrada activa, un default activo y todas aprobadas.
- Conteos puntuales cero: pre-resoluciones elegibles, eventos elegibles aun no
  sembrados, cota superior de proyecciones Slack V3 pendientes y checkout en
  estado ambiguo. No abarcan todas las colas ni garantizan futuros conteos.
- El lookup historico en `webhook_events.external_event_id` no encontro el
  identificador: sus ceros derivados no acreditan un destinatario nuevo. La
  consulta corregida de links es `da16453cbe72483fa153f83660e9653e`.

No se aplicaron migraciones, ampliaron permisos runtime ni cambiaron flags.

La comparacion Git entre el SHA desplegado de bridge y la base de esta tarea
solo cambia `chatwoot.py`, sus tests, el contrato del monitor y el documento de
acceso piloto; no incorpora nuevas migraciones. Hay ademas una correccion de
checkout en revision en su propio worktree, para pasar `expected_inbox_id` al
sender. Se integrara por separado antes de fijar el SHA del release compartido.

## Revision y bloqueos de integracion

Checkout esta publicado en PR #158, commit
`2d4cf24ba4fdb78049b314c0fcf48d1ed76156a2`; propagacion del inbox y regresion
revisadas. Las 39 pruebas focales pasaron. La suite canonica obtuvo 2125
aprobadas y tres fallos ATT1 por permisos de traversal del usuario restringido;
el operador revalido checkout/payment/ATT1 como UID 10000: 49 aprobadas.
Esto resuelve esos fallos de entorno, no dispensa otros checks de CI.

Monitor se publico en PR #159, commit
`ba85fc6e34524b9af55aac0f091259e1396554a7`, solo pruebas y dos documentos.
Su nueva prueba integrada cubre recuperacion, deduplicacion y pausa previa al
envio. Revalidacion administrativa scanner/webhook/ATT1: 203 aprobadas.
CI `verify` paso y se integro a las 20:32:25 UTC mediante merge commit
`5e9d1cbdc1f168a4b8cd8dad2b95faf36444acb1`. El operador verifico el SHA
revisado contenido en `origin/main` y el claim terminal `merged`, conservando
el worktree. Checkout sigue pendiente; no hubo deploy ni E2E externo.

La integracion `2d75dfe5029c4e938c61619fcf6aa58b` quedo bloqueada por el check
SQL de PR #158: `operator_correlation_resolution_command_invalid` al preparar
una resolucion. El diagnostico independiente encontro dos lecturas distintas de
`clock_timestamp()`: default de `prepared_at` y calculo de `expires_at`, frente
a un trigger que exige exactamente diez minutos entre ambos. Afecta nuevas
preparaciones de ambas acciones; un resultado focal favorable no elimina la
carrera. La reproduccion administrativa `306e9ce73c2547e3aa34e782a6d62f8a`
paso en ambos SHAs, sin reproducir el fallo temporal de CI.

La ampliacion `967a728c7c0243b7bf81dd53ba6ba3f3` autoriza migration forward
`20260918000100_operator_correlation_prepared_at.sql` y su validador SQL, con
preflight 0 a las 20:09:32 UTC. Se prepara regresion deterministica y una unica
captura de timestamp; no se reescribe una migracion aplicada ni relaja el trigger.
El inventario y el test de release-readiness siguen reservados por daily-feedback;
sus cambios previos se preservan. La integracion requiere resolver esa frontera,
no ignorar preflight ni alterar los paths ajenos.

La solicitud de E2E V3 `0eb8c4737c7243488823475904b7ae67` quedo bloqueada
antes de efectos por ese fallo SQL. No hubo fixtures, invocacion del modelo,
admision de webhook ni publicacion de tarjeta. Una reanudacion debe referenciar ese resultado de
cero efectos y repetir las precondiciones; no se presume ejecutada.

La regresion deterministica ya reprodujo `23514` antes del parche y paso con
la migracion nueva: asociacion, cierre, replay, evidencia obsoleta, rechazo de
falsificacion y cero efectos. En el alias estable y con `TZ=UTC` solo para el
proceso local, la suite SQL avanzo hasta `validate_acl_hardening.mjs`: se detuvo
por el fingerprint faltante de la migracion nueva. No es suite SQL aprobada.
Dos pruebas Python de metadata tambien siguen fallando porque los paths
compartidos no estan actualizados; no se oculta ese gate ni se declara
publicable la migracion.

La inspeccion parcial de commits de daily-feedback confirma que esa dependencia
no es una actualizacion aislada de inventario: incorpora contexto privado del
lead, RPC V2, scheduler e interfaz. Sus nueve cambios pendientes no se revisaron.
La revision automatica rechazo las lecturas adicionales de DDL por exceder el
scope autorizado de estos tres frentes. Se solicito a Dan ampliacion explicita
solo para revisar codigo, sin datos productivos, integracion ni despliegue.
La solicitud administrativa `62589d97ea5e4dc4be88fea726eecf85` queda retenida
mediante aviso `49e4d55a0bca4008bf940cbe9359f28a`; no usar un snapshot alternativo
para eludir el rechazo. No se abandono ni modifico ese trabajo ni se aprobo una
excepcion al preflight disjunto.

Un bloqueo de ejecucion afecto a los worktrees durante la validacion: la mascara
ACL de `/opt/hermes` anulaba traversal para `codex`. El operador restauro solo
ese permiso y verifico lectura en los cuatro espacios, manteniendo secretos y
Docker denegados. Evidencia administrativa:
`responses/operator-traverse-repair-20260918.json`. No se reintentaron efectos
externos. La orden de integracion del monitor, que no habia llegado a ejecutarse,
se emitio despues como `45eb4856ed8143faa6089aec4f96636b`.

El operador agrego bind mounts estables de los mismos worktrees, no clones,
bajo `/var/lib/codex-development/workspaces/`; `workspaces.json` contiene
`stable_worktree` por tarea. Claims/preflight administrativo mantienen rutas
canonicas. Desde el alias de coordinacion se comprobo `pwd` y lectura del
manifiesto. Python se ejecuta con `PYTHONPATH=src .venv/bin/python -m pytest`
para no depender de rutas editables antiguas. La revalidacion local del modulo
ATT1 completo como `codex` obtuvo **10 passed in 34.53s**, exit 0: los fallos
anteriores de traversal no persistieron por esa via autorizada.

## Orden de ejecucion y limites

Coordinar una unica revision de bridge para los frentes que lo comparten.
Verificar esquema y dependencias antes del release. Los releases deben partir
de un SHA remoto integrado y verificado, con rollback e inventario posterior.
El despliegue de codigo y la habilitacion de efectos son operaciones distintas.

Monitor y links se prueban en serie: comparten bridge, gates y sender.
Correlaciones comparte Hotmart/Supabase con links; cualquier cambio de esas
fronteras requiere reserva antes de editar e integracion serial.
No ampliar destinatarios para facilitar pruebas. No reintentar una entrega
incierta sin reconciliar su estado real. No efectuar compras ni pagos por Dan.

Para links, activar `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED` permite remitentes
del scope y no conserva la restriccion exclusiva de `ALLOWED_WHATSAPP_JID`.
Ademas exige `HUMAN_HANDOFF_PROJECTION_ENABLED`, cuyo worker consume trabajo
compartido. Una canary sin ingress publico no garantiza por si sola aislamiento
de esos efectos. No se habilitaron esos gates; se requiere entorno aislado o una
solucion de cohortes previamente disenada y reservada.

La inspeccion alternativa de la referencia configurada del destinatario fue
rechazada por la revision automatica de permisos antes de ejecutar. No obtuvo
ID de operacion ni accedio a valores. Se detuvo, sin trasladar la misma consulta
a otra via. Su reanudacion requiere aclaracion/autorizacion del alcance permitido;
ni esa referencia ni el contacto historico bloqueado se consideran elegibles.

## Acciones humanas pendientes de preparar

La autorizacion de inspeccion de la dependencia esta pendiente. Todavia no se
solicita ejecutar pruebas manuales: primero se deben fijar casos y scope.
Las dependencias previsibles son un inbound y comprobacion de recepcion en
telefono de prueba, comparacion/confirmacion en interfaz Slack y compra
controlada con evidencia autoritativa de SCK en Hotmart. Se consolidaran en una
sola lista con precondiciones y resultado esperado cuando los casos esten listos.

Esta evidencia no sustituye los registros individuales de cada feature ni
declara aprobados tests que aun dependen de efectos externos o del operador.
