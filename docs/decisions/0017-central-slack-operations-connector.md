# ADR-0017: Conector central de operaciones Slack

- **Estado:** Aceptada
- **Fecha:** 2026-09-07
- **Estado de implementación:** Implementado y verificado; despliegue y activación pendientes
- **Contrato:** [Slack Operations Connector V1](../contracts/slack-operations-connector-v1.md)

## Contexto

Johanna y ATT1 necesitan publicar avisos operativos mediante una misma app de
Slack, pero en canales exclusivos por aliado. El canal `C0C0YEACVT2`, aunque
temporalmente conserve otro nombre visible, pertenece exclusivamente a Johanna.
ATT1 tendrá otro Channel ID y no puede usar el canal de Johanna como fallback.
Copiar el bot token a cada bridge ampliaría innecesariamente la
frontera de credenciales, acoplaría los bridges al API de Slack y permitiría que
cada caller eligiera copy o routing.

Una publicación puede quedar ambigua después de iniciar el request. Reintentar
ese resultado automáticamente podría duplicar el mensaje. El canal tampoco debe
recibir PII, cuerpos de conversación ni payloads de proveedores.

## Decisión

Se crea un servicio independiente `supportmagician-slack-connector`:

```text
bridge Johanna ── bearer johanna ─┐                         ┌─→ canal Johanna
                                  ├─→ conector durable ─→ Slack API
bridge ATT1 ───── bearer att1 ─────┘                         └─→ canal ATT1
```

1. Sólo el conector posee `SLACK_BOT_TOKEN`, el Team ID y el mapa durable de
   Channel IDs por tenant.
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
10. El canal se selecciona exclusivamente desde un mapa server-owned derivado del
    tenant autenticado. Dos tenants no pueden compartir Channel ID y un tenant sin
    canal configurado falla cerrado antes de admitir el evento.
11. Interactivity V2 verifica firma, workspace, canal, mensaje y allowlist de
    operador antes de leer o mutar un caso. Events API, Incoming Webhooks y Socket
    Mode permanecen fuera de alcance.

## Consecuencias

- Los bridges no pueden publicar arbitrariamente ni filtrar el bot token.
- Un error o ausencia de configuración de ATT1 no puede enviar mensajes al canal
  de Johanna; tampoco existe un canal global de respaldo.
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
