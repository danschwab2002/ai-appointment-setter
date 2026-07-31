# Arquitectura inicial

## Flujo del primer hito

```text
WhatsApp -> Evolution API -> Chatwoot -> POST /webhooks/chatwoot -> captura privada
```

El primer receptor no llama a un modelo ni envía mensajes. Su objetivo es obtener un evento real y confirmar el contrato de Chatwoot 4.13.0 con la integración de Evolution API 2.3.7.

## Restricción de prueba

Solo se acepta para procesamiento el contacto cuyo identificador de WhatsApp
coincide exactamente con `ALLOWED_WHATSAPP_JID`. El valor es configuración
sensible del despliegue y no se documenta en el repositorio.

La restricción se aplica por código antes de invocar Hermes; no depende de una
instrucción de prompt.

## Flujo sombra disponible

```text
Chatwoot -> bridge -> historial canónico de Chatwoot
                   -> API Server de agente-comercial
                   -> validación JSON
                   -> archivo privado en SHADOW_DIR
```

El historial se trunca en el ID canónico del mensaje que originó el webhook.
Los mensajes posteriores no forman parte de esa evaluación. Si el ID no aparece
en la lectura acotada, el bridge falla cerrado y no invoca Hermes.

Este flujo no vuelve a Chatwoot, Evolution API ni WhatsApp. La propuesta se
conserva únicamente para evaluación. Un delivery con resultado terminal se
trata como duplicado. Si existe la captura pero falta el resultado terminal, el
bridge reintenta síncronamente con la misma `Idempotency-Key` antes de responder.

## Flujo de envío implementado

```text
WhatsApp -> Evolution API -> Chatwoot -> bridge
         -> agente-comercial (Hermes) -> controles determinísticos
         -> AgentBot de Chatwoot -> Evolution API -> WhatsApp
```

El flujo fue validado E2E con el WhatsApp autorizado. Hermes genera una propuesta
estructurada; el bridge vuelve a consultar Chatwoot y conserva la decisión final.
Antes de publicar valida pausa, intervención humana, JID canónico, trigger,
avance de conversación, idempotencia e identidad exclusiva del AgentBot.

La autorización se repite inmediatamente antes del `POST`. La respuesta creada
se acepta sólo si coincide en conversación, dirección, visibilidad, contenido,
AgentBot y marcador idempotente.

## Decisiones arquitectónicas

- [ADR-0001: Profile comercial como motor de razonamiento aislado](decisions/0001-commercial-profile-boundary.md)
- [ADR-0002: Detección y señalización de intervención humana](decisions/0002-human-takeover-detection.md)

## Estado operativo

El registro de validación E2E, despliegue y supervisión durable del gateway se
encuentra en [Registro operativo del 2026-07-31](operations/2026-07-31-production-readiness.md).
