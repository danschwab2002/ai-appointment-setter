# Plan de verificacion del Stalled Conversation Monitor V1

- Estado: en revision; preparacion documental con evidencia offline.
- Fecha de verificacion: 2026-09-18.
- Base de codigo inspeccionada: `0a79688645624a3ed641cfba3245b48feea414d2`.
- Claim: `codex-stalled-verification-prep-v1`, recurso `verification:chatwoot-stalled-monitor`.
- Rama: `docs/codex-stalled-verification-prep-v1`.
- E2E real, estado productivo y activacion recurrente: pendientes, no verificados aqui.

## Alcance y fuentes

Este procedimiento prepara la verificacion del comportamiento ya definido en el
[contrato del monitor](../contracts/chatwoot-stalled-conversation-monitor-v1.md).
La [arquitectura](../architecture.md) describe su admision al mismo inbox durable
del webhook; no se propone otro pipeline ni se modifica el contrato.

La preparacion inicial de este documento tuvo scope exclusivamente documental.
La habilitacion operativa posterior del 18-09-2026 amplio el claim y autorizo
implementacion/configuracion/deploy/E2E mediante el operador administrativo;
sus limites y evidencia estan en [verificacion operativa](codex-stalled-monitor-e2e.md).
Los procedimientos siguientes siguen condicionados a scope efectivo e identidad
de prueba verificados, sin ampliar destinatarios por esa autorizacion.
La afirmacion del contrato "implementado y desplegado; default-off" es contexto
documental: no demuestra el estado efectivo del runtime al momento de esta revision.

Aplican la [gobernanza documental](../documentation-governance.md), el
[runbook multiagente](multi-agent-development-runbook.md) y las restricciones del
[piloto de acceso remoto](codex-remote-access-pilot-v1.md).

## Habilitacion administrativa observada

El worktree autorizado es
`/opt/hermes/projects/ai-appointment-setter-codex-stalled-verification`, accesible
tambien como `/home/codex/projects/stalled-monitor-verification`; la vista del
integrador es `/opt/data/projects/ai-appointment-setter-codex-stalled-verification`.
Se verificaron rama, HEAD, estado inicial limpio, lectura del claim en
`implementing` y permiso de escritura del directorio documental.

El comprobante del integrador
`/var/lib/codex-development/receipts/stalled-verification-preflight.json`
registra preflight inicial con `exit_code=0` a las
`2026-09-18T18:31:37.542465+00:00`, para esta base, claim y worktree.
Habilita la primera edicion; no sustituye el preflight final previo a entrega
y commit. El agente restringido no ejecuta el coordinador global ni modifica
los metadatos Git compartidos.

La lectura del registro confirmo `merged` para
`chatwoot-stalled-activity-message-compat-v1` y `codex-remote-access-pilot-v1`.
Esa reconciliacion fue realizada por el integrador; no acredita un despliegue.

## Evidencia offline

Entorno: usuario `codex`, worktree y base indicados arriba, `.venv` propio
preparado por el integrador con `uv sync --locked`. Las pruebas adicionales
usan dependencias ya instaladas, transportes simulados, `TestClient` en proceso
y almacenamiento temporal. No se cargaron secretos ni se consultaron servicios
productivos.

| Grupo | Procedencia | Resultado | Que acredita |
| --- | --- | --- | --- |
| Scanner, seleccion `stalled` de `tests/test_chatwoot.py` | Reportado por el integrador en el handoff; no reejecutado en esta preparacion | 30 passed, 63 deselected, 2.30 s | Seleccion canonica, actividades, tipos invalidos, paginacion, identidad y exclusiones bajo mocks |
| Monitor en `tests/test_webhook.py` y logs sanitizados | Ejecutado en esta tarea | 8 passed, 93 deselected, 1 warning, 4.04 s | Admision estable tras recrear el monitor, cooldown/cap, recuperacion tras error, wiring de readiness y default-off, redaccion de errores |
| Sender, cuatro funciones seleccionadas de `tests/test_chatwoot.py` | Ejecutado en esta tarea | 13 passed, 0.39 s | Actividad posterior compatible, bloqueo de respuesta obsoleta, autoridad canonica y reconsulta despues del autorizador |

Comando reportado por el integrador:

```sh
uv run --locked pytest tests/test_chatwoot.py -k stalled
```

Comandos ejecutados desde el worktree autorizado:

```sh
/home/codex/.local/bin/uv run --locked --offline --no-sync pytest \
  -p no:cacheprovider \
  tests/test_webhook.py tests/test_chatwoot_stalled_monitor_logging.py \
  -k stalled

/home/codex/.local/bin/uv run --locked --offline --no-sync pytest \
  -p no:cacheprovider \
  tests/test_chatwoot.py::test_sends_an_idempotent_agent_bot_reply_after_later_chatwoot_activity \
  tests/test_chatwoot.py::test_does_not_send_a_stale_reply_after_the_conversation_advanced \
  tests/test_chatwoot.py::test_final_reply_boundary_revalidates_full_conversation_authority \
  tests/test_chatwoot.py::test_rechecks_chatwoot_again_after_pre_send_authorizer_and_before_post
```

El warning es `StarletteDeprecationWarning` por el uso de `httpx` en
`starlette.testclient`; no se actualizaron dependencias. Estos resultados no
equivalen a suite completa, CI, HTTP real, entrega al canal ni validacion de
credenciales. La prueba de reinicio recrea objetos sobre el mismo almacenamiento
temporal; no acredita recuperacion de un servicio desplegado. Los estados 503
antes del primer scan y despues de un error deben verificarse explicitamente
en el procedimiento futuro; observar `healthy` por si solo no los demuestra.

## Condiciones previas al E2E futuro

El operador no inicia el E2E hasta contar con:

1. Autorizacion operativa de Dan del 18-09-2026 registrada mediante el operador.
   Antes de ejecutar se concretan entorno, ventana, contacto sintetico y efectos
   permitidos dentro de ese alcance; queda pendiente verificar identidad y
   aislamiento, no solicitar otra autorizacion general para el E2E.
2. Claim de ejecucion separado y preflight vigente del integrador. Debe fijarse
   el commit remoto verificado, la revision realmente desplegada y cualquier
   diferencia respecto de la base de esta evidencia.
3. Cuenta, inbox y JID exacto de prueba documentados en un registro privado;
   los controles tambien deben ser sinteticos y estar autorizados. El gate de
   multiples remitentes no debe ampliarse para facilitar la prueba.
4. Estado inicial de readiness, flags efectivos, colas y conversaciones de prueba
   relevado por el operador autorizado, sin copiar secretos ni cuerpos a Git.
   Para empezar, el monitor debe estar apagado. Si no lo esta, detener la
   preparacion y acordar la contencion antes de ejecutar.
5. Un mecanismo acordado para dejar el inbound de prueba pendiente sin afectar
   webhooks ni workers de otras conversaciones. Fijar la correlacion que permita
   atribuir la recuperacion al monitor y no a una entrega normal del webhook.
6. Un responsable presente con capacidad de detener admisiones y contener los
   envios ya encolados. Apagar solo el monitor no demuestra que un worker haya
   dejado de procesar trabajo admitido.
7. Ventana y presupuesto numericos de scans, recuperaciones y envios. Registrar
   los valores efectivos de intervalo, edad, paginas, cooldown y limite de
   admisiones; no asumir que los defaults son la configuracion desplegada.

## Matriz de aceptacion futura

Todos los casos siguientes estan pendientes de ejecucion real. Los casos con
payload malformado, fallos o carreras inducidas deben prepararse en un entorno
de prueba controlado; no se inyectan en conversaciones productivas.

| Caso | Preparacion/observacion | Criterio de aceptacion |
| --- | --- | --- |
| E0: baseline | Monitor apagado; lectura HTTP autorizada de `/ready` y conteos iniciales | Campo `chatwoot_stalled_monitor=disabled`; prerequisitos sanos antes de avanzar |
| E1: recuperacion atribuible | Inbound sintetico elegible dentro de la ventana; sin trabajo normal pendiente que pueda responderlo | Admision `stalled-chatwoot:<conversation_id>:<message_id>` y una sola respuesta logica atribuible a ese trigger |
| E2: actividad posterior | Igual a E1 con una actividad publica valida `message_type=2` posterior | Se recupera el inbound; la actividad no se toma como trigger ni lo oculta |
| E3: conversacion avanzada | Aparece un inbound u outbound conversacional posterior al trigger original, incluso entre admision y envio | Cero respuestas al trigger obsoleto; un inbound nuevo es otro trigger y no prueba duplicacion |
| E4: controles de autoridad | Casos separados con asignacion humana, pausa, contacto bloqueado, estado no abierto, sin permiso de respuesta o identidad fuera del scope | Cero envios nuevos por el monitor en cada control; volver a comprobar antes del POST |
| E5: repeticion | Repetir scans y, solo si se autorizo, reiniciar conservando el almacenamiento durable tras E1 | Sin nueva respuesta al mismo trigger; ninguna duplicacion de una misma parte |
| E6: recuperacion acotada | Fallo terminal controlado, transcurso del cooldown y nuevo scan | Sin readmision anticipada; ninguna admision por encima del limite efectivo. El limite de admisiones no es un limite global de mensajes |
| E7: falla de scan | Paginacion incompleta/inestable o error de scan, en entorno de prueba | Sin admisiones de un scan incompleto; readiness 503 hasta un scan completo sano; errores sanitizados |
| E7b: tipo invalido | Registro publico con tipo ausente, booleano, no entero o desconocido, en entorno de prueba | Conversacion excluida y respuesta bloqueada; esa exclusion por si sola no exige readiness 503 |
| E8: cierre | Terminar la ventana aprobada y conciliar admisiones, partes y conteos | Cero delta de envios del monitor en controles, ninguna entrega ambigua sin resolver y evidencia de contencion final |

Para E1/E2 se fija antes de iniciar el numero esperado de partes de la respuesta.
Preferir un caso de una parte: un unico POST aceptado y una respuesta visible.
Si se permite multipart, una respuesta logica puede contener varios mensajes;
se verifica cada parte por trigger e indice, sin confundir multipart con reintento.

Se conserva un baseline por control y se comparan los conteos al cierre. Si
otros productores envian durante la ventana y no puede atribuirse el delta, el
resultado es inconcluso; no se declara exito. El tiempo de observacion debe
cubrir los scans repetidos y el cooldown acordados, ademas de la latencia del
worker y canal. No se reemplaza la observacion con una espera arbitraria corta.

## Parada y registro de resultados

Detener ante scope ambiguo, error persistente de readiness, respuesta duplicada,
envio a un control, perdida de trazabilidad, exceso de presupuesto o entrega
incierta. El operador autorizado aplica el mecanismo de contencion acordado y
comprueba tambien el trabajo ya admitido. No reenviar a ciegas una entrega
incierta, borrar el ledger ni limpiar colas para obtener un resultado favorable.

La evidencia del E2E debe registrar autorizacion, operador, entorno, SHA
desplegado, tiempos UTC, configuracion no secreta, caso y resultado
`aprobado|fallido|inconcluso|no ejecutado`, conteos antes/despues, correlacion
sanitizada de trigger/admisiones/partes y estado final de la contencion.
Referencias reales, payloads y PII permanecen en `data/` privado y excluido de
Git; el documento versionado contiene solo aliases sinteticos y agregados.

Un E2E aprobado no autoriza por si mismo la activacion recurrente. Esa decision
requiere revision de la evidencia y autorizacion explicita por separado.

## Cierre de esta preparacion

La preparacion inicial cambio solo este documento. La continuacion operativa
registra evidencia separada en [verificacion operativa](codex-stalled-monitor-e2e.md).
No cambia la arquitectura ni una interfaz por documentar estas comprobaciones.
Antes de publicar, el integrador debe revisar el archivo completo, verificar el
scope incluido el archivo nuevo no trackeado, ejecutar un preflight final y
administrar commit, push, PR y transicion a `review` segun el runbook.
El preflight final y la publicacion estan pendientes al redactar este documento.
La suite canonica y CI se reportan separadamente si el integrador los ejecuta;
no se infieren de los grupos de tests anteriores. Ningun resultado de esta
preparacion declara el monitor validado en produccion.
