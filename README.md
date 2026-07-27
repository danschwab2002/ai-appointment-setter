# AI Appointment Setter

Puente conversacional entre Chatwoot, Hermes Agent y Evolution API para agentes comerciales por WhatsApp.

## Primer hito

Recibir webhooks `message_created` de Chatwoot, verificar su firma, aceptar exclusivamente mensajes entrantes del JID de prueba autorizado y capturar el payload para diseñar la integración real con Hermes.

## Stack conocido

- Chatwoot Community Edition 4.13.0
- Evolution API 2.3.7
- Hermes Agent (profiles especializados)
- Supabase Cloud (integración posterior)

## Estado

El receptor de captura está implementado. Todavía no invoca a Hermes ni responde por WhatsApp.

## Desarrollo

```bash
uv sync
uv run pytest
```

## Ejecutar localmente

```bash
cp .env.example .env
# Completar CHATWOOT_WEBHOOK_SECRET con el secreto entregado por Chatwoot.
set -a && . ./.env && set +a
uv run uvicorn bridge.app:build_app --factory --host 0.0.0.0 --port 8000
```

Verificación:

```bash
curl http://localhost:8000/health
```

## Docker Compose

```bash
docker compose up --build
```

La guía de conexión se encuentra en `docs/chatwoot-webhook.md`.
