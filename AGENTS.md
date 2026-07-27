# Reglas del proyecto

## Alcance actual

Construir un receptor seguro para webhooks de Chatwoot antes de integrar el profile comercial de Hermes.

## Convenciones

- Python administrado con `uv`; no usar `pip` global.
- Desarrollo guiado por pruebas: ejecutar `uv run pytest`.
- Los secretos viven únicamente en `.env`, nunca en Git.
- Los payloads capturados y cualquier PII viven en `data/`, excluido de Git.
- No registrar tokens, firmas, API keys ni cuerpos completos en logs de aplicación.
- El agente comercial solo podrá actuar para el JID autorizado configurado.
- Antes de declarar una funcionalidad terminada, ejecutar pruebas y una verificación HTTP real.
