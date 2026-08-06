# Incidente: timeout del webhook entrante de Chatwoot

- **Fecha:** 2026-08-06
- **Estado:** causa aislada; corrección implementada localmente; despliegue y E2E pendientes
- **Alcance:** `POST /webhooks/chatwoot`

## Evidencia productiva

El worker de Chatwoot intentó el endpoint configurado y agotó su lectura:

```text
[WebhookJob] Exception: ... Timed out reading data from server
Performed WebhookJob ... in 5133.51ms
```

La URL también respondió correctamente para eventos descartados rápidamente. Por
lo tanto, la evidencia no indica una URL sintácticamente inválida: el camino
conversacional mantenía abierta la solicitud mientras consultaba historial,
ejecutaba Hermes y preparaba el efecto externo, superando el límite aproximado de
cinco segundos de Chatwoot.

No se preservan payloads, contenido, JIDs, teléfonos ni credenciales en este
registro.

## Corrección implementada localmente

El receptor ahora:

1. autentica, filtra y captura el evento;
2. admite atómicamente trabajo recuperable en el volumen persistente;
3. devuelve HTTP 202 sin esperar a Hermes ni a Chatwoot;
4. procesa el trabajo mediante un worker iniciado con el bridge;
5. retoma admisiones después de reinicios;
6. conserva idempotencia de evaluación y envío durante replay;
7. usa backoff persistido sin agotamiento para historial transitorio y un máximo
   de ocho intentos para errores no clasificados.

La pausa por intervención humana usa la misma frontera asíncrona, evitando que una
segunda llamada a Chatwoot mantenga abierto el webhook original.

## Verificación local completada

Se cubrieron mediante pruebas:

- ACK dentro de un límite acotado aun cuando Hermes no termina;
- admisión durable antes del HTTP 202;
- procesamiento y respuesta fuera del request;
- replay automático después de reinicio;
- ausencia de duplicación al repetir el worker;
- pausa por intervención humana fuera del request;
- backoff después de un fallo externo;
- recuperación cuando el historial no está disponible o todavía no expone el
  mensaje trigger, sin agotamiento terminal para esos fallos transitorios;
- límite de 1 MiB antes de autenticar;
- supervisión y reinicio del loop de trabajo ante excepciones inesperadas;
- timeout de 120 segundos para que un handler bloqueado no detenga toda la cola;
- rechazo de `.work` y locks inseguros o enlazados simbólicamente;
- eliminación del payload del work item al completar;
- cleanup de workers aun cuando el lifespan termina por excepción;
- un único escaneo del inbox por ciclo, con revalidación directa bajo lock;
- escritura atómica y durable antes de `202 captured` en el modo sin Hermes.

La suite completa, `compileall` y `git diff --check` terminaron correctamente.
Además se levantó el mismo factory de Uvicorn usado por el contenedor, con las
dependencias externas apuntando a un puerto local cerrado. Un webhook firmado
real obtuvo:

```text
HTTP 202
status=accepted
elapsed=21.99 ms
work_items=1
estado posterior=admitted con backoff ante dependencia no disponible; attempts>=1
permisos=.work 0700; work item 0600
HTTP cuerpo >1 MiB=413 chatwoot_webhook_body_too_large
```

La prueba confirma que el ACK no espera la dependencia externa, que el fallo
transitorio no se vuelve terminal y que el límite de cuerpo se aplica. No
demuestra el despliegue productivo ni la entrega por WhatsApp.

## Verificación productiva pendiente

1. desplegar la revisión aprobada;
2. confirmar `/health` por HTTP;
3. enviar un único mensaje público entrante desde el JID autorizado;
4. comprobar que el `WebhookJob` termina antes del timeout;
5. comprobar una única respuesta del AgentBot en la conversación canónica;
6. comprobar pausa o continuidad comercial y ausencia de efectos duplicados.

## Contratos relacionados

- [Contrato de ingreso Chatwoot v1](../contracts/chatwoot-ingress-v1.md)
- [Arquitectura](../architecture.md)
