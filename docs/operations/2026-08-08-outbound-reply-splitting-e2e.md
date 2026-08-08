# Evidencia operativa: división outbound de respuestas por WhatsApp

- **Fecha:** 2026-08-08
- **Estado:** validación E2E productiva exitosa
- **Alcance:** una conversación y un JID autorizados; activación controlada
- **Commit desplegado:** `5965f88` (`feat: split outbound replies durably`)

## Condiciones previas verificadas

Antes de activar la división se confirmó:

- `GET /health`: HTTP 200;
- `CHATWOOT_REPLY_SPLITTER_ENABLED=false` durante el preflight;
- allowlist y respuestas automáticas configuradas;
- `REPLY_DIR=/app/data/replies`;
- volumen Docker persistente, escribible, montado en `/app/data`;
- directorio de replies con modo `0700` y escritura + `fsync` exitosos;
- provider/model del divisor configurados;
- llamada sintética al modelo divisor vía Hermes: HTTP 200;
- cero manifiestos y claims previos;
- cero errores recientes.

Durante el preflight se detectó que el primer redeploy no tenía ningún mount
Docker. Se agregó un volumen administrado por EasyPanel en `/app/data` antes de
activar el feature. No había datos que migrar desde el filesystem efímero.

## Activación

La activación inicial controlada se realizó mediante una actualización directa
del servicio Swarm:

```text
CHATWOOT_REPLY_SPLITTER_ENABLED=true
```

El servicio convergió con una tarea estable, conservó el volumen y respondió
HTTP 200. Después del E2E, el operador guardó el mismo valor en la configuración
declarativa de EasyPanel y ejecutó un nuevo deploy.

El redeploy persistente confirmó:

```text
splitter_enabled=true
manifest_count=1
batch_claim_count=1
posting_journal_count=2
latest_status=completed
latest_part_count=2
canonical_marker_count=2
canonical_indices=[1,2]
unique_message_ids=true
no_duplicate_after_redeploy=true
recent_errors=0
recent_retry_warnings=0
```

Por lo tanto, el flag quedó activo de forma persistente y el lote E2E sobrevivió
la recreación del contenedor sin recalcular geometría ni duplicar mensajes. El
rollback con flag apagado continúa respetando manifiestos existentes.

## Caso E2E

Se envió un mensaje real desde el único WhatsApp autorizado. El flujo observado
fue:

```text
WhatsApp
→ Chatwoot
→ webhook durable
→ batching inbound
→ Hermes comercial
→ divisor de fronteras
→ claim + manifiesto durable
→ dos POST AgentBot ordenados
→ dos burbujas recibidas en WhatsApp
```

## Evidencia durable y canónica

Sin registrar texto, JID, nombres ni IDs, se verificó:

```text
manifest_status=completed
part_count=2
manifest_mode=0600
batch_hash_matches_filename=true
batch_claim_present=true
batch_claim_mode=0600
posting_journal_count=2
all_part_journals_present=true
canonical_part_count=2
canonical_indices=[1,2]
declared_counts=[2,2]
all_outgoing_public_agent_bot=true
created_at_ordered=true
inter_part_delay_seconds=3
recent_errors=0
recent_retry_warnings=0
```

La captura visual aportada por el operador confirmó dos burbujas separadas y en
orden. No se conserva la captura en el repositorio porque contiene conversación
y datos personales.

## Resultado

La vertical productiva de división outbound quedó validada para el caso
controlado. `CHATWOOT_REPLY_SPLITTER_ENABLED=true` quedó guardado en EasyPanel y
verificado después de un redeploy sin duplicados.

No se ejecutaron pruebas destructivas, reenvíos deliberados ni cambios sobre
conversaciones ajenas al JID autorizado.
