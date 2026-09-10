# Backup y restore del ledger Slack

- **Estado:** Implementado y verificado localmente
- **Alcance:** SQLite V6 de una sola réplica

## Backup online consistente

El backup usa la API online de SQLite, valida `integrity_check=ok`,
`user_version=6`, el esquema exacto y los invariantes de los ledgers de
notificaciones, replay de interacciones, sesiones de revisión y proyecciones de
correlación. La validación incluye columnas, constraints, índices únicos, foreign
keys, estados y bindings caso↔tenant↔team↔canal↔mensaje. Después publica mediante
rename atómico, fuerza `fsync` del archivo y directorio y deja permisos `0600`.

Dentro del contenedor, con el servicio activo:

```text
python -m slack_correlation.store backup \
  --source /app/data/slack-connector.sqlite3 \
  --destination /app/data/backups/slack-connector.sqlite3
```

Copiar después el archivo terminado a almacenamiento externo cifrado. No copiar el `.sqlite3`, `-wal` o `-shm` activos con herramientas de filesystem como sustituto de este comando.

## Restore offline validado y atómico

1. Cerrar ingreso y outbound.
2. **Detener primero** el servicio; no usar rolling update.
3. Confirmar cero réplicas/procesos y preservar el volumen existente.
4. Ejecutar desde una tarea de mantenimiento que monte el mismo volumen:

```text
python -m slack_correlation.store restore \
  --source /app/data/backups/slack-connector.sqlite3 \
  --destination /app/data/slack-connector.sqlite3
```

El restore obtiene una instantánea consistente mediante la API de backup SQLite,
incluyendo estado confirmado en WAL. Admite únicamente V2–V6; V2–V5 se migran
en una transacción explícita sobre la copia temporal, donde DDL, copia de filas y
`user_version=6` se confirman o revierten juntos. Rechaza archivos corruptos,
objetos, índices, foreign keys o constraints inesperados y estados lógicos
imposibles antes de reemplazar el destino; esto incluye replay sin respuesta
coherente, sesiones sin command/job compatible y proyecciones que no enlazan una
notificación `COR-001/002/003` aceptada. También rechaza un destino cuyo instance
lock esté tomado.

Con el lock retenido, el restore hace checkpoint `TRUNCATE` y cierra el destino
anterior, elimina `-wal`/`-shm` y fuerza `fsync` del directorio **antes** de publicar.
Luego ejecuta rename atómico y vuelve a hacer `fsync` del directorio. Una caída
anterior al rename deja el ledger viejo checkpointed y válido; una caída posterior
al rename expone el restaurado sin sidecars obsoletos.

5. Arrancar exactamente una réplica con efectos inactivos.
6. Exigir `/ready` con `storage_ready=true` y revisar sólo los conteos sanitizados.
7. Resolver cualquier `delivery_unknown` antes de reactivar outbound.
