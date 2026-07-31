# Registro operativo: flujo E2E y gateway durable

- **Fecha:** 2026-07-31
- **Estado:** validado mediante ejecución real
- **Alcance:** respuesta automática controlada por WhatsApp y operación durable del gateway Hermes `agente-comercial`

## Resumen

El sistema quedó operativo para responder desde un AgentBot de Chatwoot a un único WhatsApp autorizado. Chatwoot conserva la propiedad canónica de la conversación; Hermes genera contenido y el bridge mantiene la decisión determinística de autorizar, bloquear y publicar cada respuesta.

La prueba E2E confirmó una respuesta recibida en el mismo WhatsApp controlado. El gateway comercial dejó de depender de un proceso iniciado con `nohup`: ahora se ejecuta como servicio por perfil bajo s6, se recupera después de un redeploy y fue capaz de reiniciarse automáticamente después de una terminación controlada.

## Flujo validado

```text
WhatsApp controlado
→ Evolution API
→ Chatwoot
→ POST /webhooks/chatwoot
→ appointment-bridge
→ Hermes / agente-comercial
→ reautorización canónica
→ AgentBot de Chatwoot
→ Evolution API
→ WhatsApp controlado
```

## Límites de responsabilidad

### Chatwoot

Es la fuente canónica de:

- conversaciones y contactos;
- historial público;
- identidad de mensajes entrantes, humanos y AgentBot;
- etiquetas, incluida `automation_paused`;
- estado actual necesario para autorizar una respuesta.

### Hermes

El profile aislado `agente-comercial`:

- recibe un contexto acotado de una sola conversación;
- genera exclusivamente una propuesta estructurada;
- no decide la autorización final;
- no publica directamente en Chatwoot o Evolution API;
- no posee las credenciales de control de Chatwoot.

### Appointment bridge

El bridge:

- verifica firma, timestamp y delivery del webhook;
- limita el flujo al JID configurado;
- recupera el estado canónico desde Chatwoot;
- invoca Hermes sólo cuando el trigger es elegible;
- valida estrictamente la propuesta de Hermes;
- reautoriza inmediatamente antes del envío;
- publica exclusivamente con la identidad AgentBot configurada;
- falla de forma cerrada ante estado incompleto, ambiguo o inconsistente.

## Invariantes de seguridad confirmados

- Sólo se procesa el JID exacto configurado en `ALLOWED_WHATSAPP_JID`.
- El JID se vuelve a verificar contra la conversación canónica.
- El trigger debe ser un mensaje público, entrante y enviado por el contacto.
- Una nota privada no se interpreta como intervención humana pública.
- Una respuesta humana pública tiene precedencia y detiene la automatización.
- La etiqueta `automation_paused` bloquea la respuesta antes del `POST`.
- La autorización se repite dentro de la sección crítica, inmediatamente antes de publicar.
- La identidad idempotente es `conversation_id + trigger_message_id`; distintos delivery IDs para el mismo trigger no crean respuestas adicionales.
- Los reintentos consultan Chatwoot antes de considerar una respuesta como duplicada.
- Una respuesta sólo se acepta si coincide en conversación, dirección, visibilidad, contenido, AgentBot y marcador idempotente.
- Las lecturas canónicas usan la credencial de control; la creación del mensaje usa exclusivamente la credencial del AgentBot.
- HTTP sin TLS sólo se permite para loopback o para el DNS interno exacto `hermes`.
- Modo sombra y respuestas automáticas permanecen desactivados por defecto en código; el despliegue debe habilitarlos explícitamente.

## Estado de despliegue verificado

### Appointment bridge

- El endpoint público `/health` respondió HTTP 200.
- El procesamiento Hermes y las respuestas automáticas están habilitados explícitamente en el despliegue controlado.
- La URL interna de Hermes es `http://hermes:8643/v1`.

### Hermes comercial

- Profile: `agente-comercial`.
- API Server: puerto interno `8643`.
- `/health` respondió HTTP 200.
- `/v1/models` sin credencial respondió HTTP 401.
- El bridge autenticado pudo consultar el modelo esperado.

### Supervisión de procesos

El startup del servicio Hermes quedó configurado como:

```bash
exec /init /opt/hermes/docker/main-wrapper.sh --profile agente-comercial gateway run
```

Con `HERMES_DASHBOARD=1`, el contenedor resultante mantiene:

```text
/init → s6-svscan (PID 1)
├── dashboard en 9119
└── gateway-agente-comercial en 8643
```

Se verificó que:

- `/init` quedó como PID 1 mediante `s6-svscan`;
- el slot `/run/service/gateway-agente-comercial` existe y está `up`;
- no queda ningún proceso `gateway run --no-supervise`;
- el dashboard continúa disponible para el profile predeterminado y para administrar perfiles;
- los logs del gateway se conservan bajo el volumen persistente;
- el gateway vuelve a iniciar después de un redeploy;
- al terminar controladamente el proceso hijo, s6 creó un PID nuevo y `/health` volvió a HTTP 200 sin intervención manual.

## Verificaciones realizadas

### Suite y artefactos locales

- Suite completa: `79 passed`.
- Compilación Python: exitosa.
- `git diff --check`: exitoso.
- Build con `uv`: exitoso.
- Revisión independiente final: sin bloqueos.

### Prueba HTTP aislada

El arnés sin contacto con servicios reales confirmó:

```text
primera entrega   → reply_sent
reintento         → reply_duplicate
POST Chatwoot     → 1
toma humana       → automation_paused
mensaje posterior → reply_blocked
```

### Prueba E2E real

La prueba controlada confirmó:

- recepción del webhook por el bridge;
- invocación exitosa a Hermes;
- creación de una única respuesta AgentBot;
- entrega efectiva en el mismo WhatsApp autorizado;
- tono y comportamiento comercial esperados.

No se registran en este documento el número autorizado, payloads, tokens, claves ni contenido privado de conversaciones.

## Incidentes de validación y comportamiento esperado

### Funciones desactivadas en el primer despliegue

Una prueba inicial se detuvo después de capturar el webhook porque `HERMES_SHADOW_ENABLED` y `CHATWOOT_AUTOMATED_REPLIES_ENABLED` permanecían en sus valores seguros por defecto. Al habilitarlos explícitamente y redeployar el bridge, Hermes quedó disponible para el flujo real.

### Conversación pausada

Otra prueba llegó hasta una propuesta válida de Hermes, pero el bridge no publicó porque Chatwoot conservaba la etiqueta `automation_paused`. La lectura canónica confirmó JID y trigger válidos, sin mensajes humanos posteriores ni duplicados AgentBot. El bloqueo fue el comportamiento fail-closed esperado. Después de una reanudación explícita de la conversación controlada, la prueba E2E fue exitosa.

### Gateway temporal

Para la primera prueba se inició el gateway mediante `nohup` y se escribió su salida en `/tmp`. Ese mecanismo no sobrevivía a redeploys ni ofrecía reinicio por crash. Se retiró después de migrar al supervisor oficial s6 y comprobar la autorecuperación.

### Entrypoint reemplazado por EasyPanel

Aunque la imagen oficial declara `/init` como entrypoint, el campo `Comando` del despliegue lo reemplazaba por `sh -c`, dejando `dash` como PID 1. El startup actual usa `exec /init ...`, restaurando el diseño oficial sin crear otro contenedor ni compartir `/opt/data` entre gateways.

## Operación

- Para pausar una conversación, debe aplicarse `automation_paused` o intervenir públicamente desde una identidad humana de Chatwoot.
- Quitar `automation_paused` es una reanudación explícita; no debe hacerse de forma automática.
- No deben ejecutarse dos contenedores gateway contra el mismo `/opt/data`.
- El dashboard y el gateway son servicios distintos: el dashboard permanece en `9119`; el bridge consulta exclusivamente el gateway comercial en `8643`.
- Un health check administrado sobre `8643` puede incorporarse más adelante como defensa adicional ante procesos vivos pero no responsivos. No es un bloqueo actual: el crash, el redeploy y la recuperación HTTP ya fueron ejercitados.

## Documentación relacionada

- [Arquitectura](../architecture.md)
- [Contrato comercial](../commercial-brief.md)
- [Webhook de Chatwoot](../chatwoot-webhook.md)
- [ADR-0001: profile comercial](../decisions/0001-commercial-profile-boundary.md)
- [ADR-0002: intervención humana](../decisions/0002-human-takeover-detection.md)
- [Contrato observado de Chatwoot](../research/chatwoot-observed-contract.md)
- [Spike de compatibilidad AgentBot](../../spikes/001-chatwoot-agentbot-compatibility/README.md)
