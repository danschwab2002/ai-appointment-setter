# Arquitectura inicial

## Flujo del primer hito

```text
WhatsApp -> Evolution API -> Chatwoot -> POST /webhooks/chatwoot -> captura privada
```

El primer receptor no llama a un modelo ni envía mensajes. Su objetivo es obtener un evento real y confirmar el contrato de Chatwoot 4.13.0 con la integración de Evolution API 2.3.7.

## Restricción de prueba

Solo se aceptará para procesamiento el contacto cuyo identificador de WhatsApp sea exactamente:

`5492916424279@s.whatsapp.net`

La restricción será aplicada por código antes de cualquier futura invocación a Hermes. No será una instrucción de prompt.

## Próximo flujo

```text
Chatwoot -> bridge -> sales-agent (Hermes) -> Chatwoot -> Evolution API -> WhatsApp
```
