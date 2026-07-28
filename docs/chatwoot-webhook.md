# Conectar Chatwoot al receptor

## Endpoint

Una vez desplegado el bridge con HTTPS, la URL será:

```text
https://<dominio-del-bridge>/webhooks/chatwoot
```

## Configuración de Chatwoot 4.13.0

1. Ir a **Settings → Integrations → Webhooks**.
2. Crear un webhook con la URL anterior.
3. Seleccionar únicamente `message_created` para la primera prueba.
4. Copiar el secreto que muestra Chatwoot una vez creado.
5. Configurar ese valor como `CHATWOOT_WEBHOOK_SECRET` en el bridge.
6. Reiniciar el bridge.

Chatwoot firma el cuerpo crudo mediante los headers:

- `X-Chatwoot-Signature`
- `X-Chatwoot-Timestamp`
- `X-Chatwoot-Delivery`

## Prueba permitida

El bridge solo captura mensajes públicos entrantes cuyo identificador de
remitente coincida con `ALLOWED_WHATSAPP_JID`. Para la integración actual, el
valor canónico se lee desde `conversation.meta.sender.identifier`; únicamente
se usa `conversation.contact_inbox.source_id` como compatibilidad con payloads
que no incluyen el metadata del remitente.

Cualquier otro JID recibe HTTP 200 con estado `ignored` y no se persiste. El
valor autorizado es configuración sensible del despliegue y no debe copiarse a
documentación ni logs.

## Resultado esperado

Un evento aceptado devuelve HTTP 202:

```json
{"status":"captured","delivery_id":"..."}
```

Los payloads aceptados quedan bajo `CAPTURE_DIR` con permisos `0600`. El nombre del archivo es un SHA-256 del delivery ID para evitar path traversal y no revelar identificadores.

La estructura sanitizada y las diferencias observadas entre webhook y API se
documentan en [`research/chatwoot-observed-contract.md`](research/chatwoot-observed-contract.md).
