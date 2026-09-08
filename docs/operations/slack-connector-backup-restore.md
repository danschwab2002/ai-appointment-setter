# Backup y restore del ledger Slack

- **Estado:** Implementado y verificado localmente
- **Alcance:** SQLite V2 de una sola réplica

## Backup online consistente

El backup usa la API online de SQLite, valida `integrity_check=ok` y `user_version=2`, publica mediante rename atómico, fuerza `fsync` del archivo y directorio y deja permisos `0600`.

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

El restore rechaza un backup corrupto o de otra versión antes de reemplazar el destino y rechaza un destino cuyo instance lock esté tomado. La copia se valida, se fuerza a disco y reemplaza el destino mediante rename atómico. Un fallo previo al rename conserva el ledger anterior.

5. Arrancar exactamente una réplica con efectos inactivos.
6. Exigir `/ready` con `storage_ready=true` y revisar sólo los conteos sanitizados.
7. Resolver cualquier `delivery_unknown` antes de reactivar outbound.
