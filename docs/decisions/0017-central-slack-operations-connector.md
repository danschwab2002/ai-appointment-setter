# ADR-0017: Conector central de operaciones Slack

- **Estado:** Aceptada
- **Fecha:** 2026-09-07
- **Estado de implementación:** Implementado y verificado; despliegue y activación pendientes
- **Contrato:** [Slack Operations Connector V1](../contracts/slack-operations-connector-v1.md)

## Contexto

Johanna y ATT1 necesitan publicar avisos operativos en una app y canal Slack
compartidos. Copiar el bot token a cada bridge ampliaría innecesariamente la
frontera de credenciales, acoplaría los bridges al API de Slack y permitiría que
cada caller eligiera copy o routing.

Una publicación puede quedar ambigua después de iniciar el request. Reintentar
ese resultado automáticamente podría duplicar el mensaje. El canal tampoco debe
recibir PII, cuerpos de conversación ni payloads de proveedores.

## Decisión

Se crea un servicio independiente `supportmagician-slack-connector`:

```text
bridge Johanna ── bearer johanna ─┐
                                  ├─→ conector durable ─→ Slack API ─→ C0C0YEACVT2
bridge ATT1 ───── bearer att1 ─────┘
```

1. Sólo el conector posee `SLACK_BOT_TOKEN` y el Channel/Team ID.
2. Cada bridge usa una credencial interna diferente; el tenant se deriva de esa
   credencial.
3. El contrato acepta eventos tipados, nunca texto, bloques, canal o tenant.
4. El servidor renderiza las 49 plantillas catalogadas.
5. Una base SQLite en volumen persistente conserva admisión, deduplicación,
   claims, `request_started`, resultado y binding de hilo.
6. V1 se despliega con exactamente una réplica y un worker. Un lock de instancia
   bloquea un segundo proceso sobre el mismo volumen.
7. Todo resultado no verificable después de `request_started` termina en
   `delivery_unknown` y no se reintenta automáticamente.
8. `auth.test` debe confirmar el workspace antes de iniciar el worker.
9. Ingreso, publicación e interacciones tienen controles separados y default-off.
10. Interactivity, Events API, Incoming Webhooks y Socket Mode permanecen fuera
    de V1.

## Consecuencias

- Los bridges no pueden publicar arbitrariamente ni filtrar el bot token.
- Los replays exactos son seguros y los conflictos semánticos fallan cerrado.
- La operación requiere un volumen persistente y prohíbe scaling horizontal.
- Una publicación `delivery_unknown` requiere reconciliación humana; no hay retry
  ciego.
- Para escalar a varias réplicas habrá que migrar el ledger a PostgreSQL con
  leases y fencing equivalentes.
- Los bridges siguen siendo responsables de producir una notificación estable
  desde su evento autoritativo; el adaptador `SlackConnectorProducer` implementa
  el transporte y preserva idempotencia.

## Alternativas descartadas

- **Bot token en cada bridge:** amplía el blast radius y duplica configuración.
- **Incoming Webhook:** no ofrece la frontera ni evolución necesaria para estado
  e interactividad.
- **Endpoint `/send`:** permitiría copy, bloques o routing elegidos por el caller.
- **Memoria del proceso:** pierde admisiones en restart.
- **Retry automático tras timeout:** puede duplicar un efecto ya aplicado.
- **Varias réplicas con SQLite:** no ofrece coordinación segura entre hosts.
