# Arquitectura inicial

## Flujo del primer hito

```text
WhatsApp -> Evolution API -> Chatwoot -> POST /webhooks/chatwoot -> captura privada
```

El primer receptor no llama a un modelo ni envía mensajes. Su objetivo es obtener un evento real y confirmar el contrato de Chatwoot 4.13.0 con la integración de Evolution API 2.3.7.

## Restricción de prueba

Solo se aceptará para procesamiento el contacto cuyo identificador de WhatsApp sea exactamente:

`12025550123@s.whatsapp.net`

La restricción será aplicada por código antes de cualquier futura invocación a Hermes. No será una instrucción de prompt.

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

## Flujo futuro de envío

```text
Chatwoot -> bridge -> agente-comercial (Hermes) -> controles determinísticos
         -> AgentBot de Chatwoot -> Evolution API -> WhatsApp
```

El flujo futuro no está implementado. Antes de enviar deberá volver a validar
pausa, elegibilidad, estado de conversación, idempotencia e identidad exclusiva
del AgentBot.

## Decisiones arquitectónicas

- [ADR-0001: Profile comercial como motor de razonamiento aislado](decisions/0001-commercial-profile-boundary.md)
