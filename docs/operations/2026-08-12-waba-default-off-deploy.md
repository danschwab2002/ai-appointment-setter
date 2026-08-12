# Evidencia del deploy WABA default-off — 2026-08-12

## Alcance autorizado

Se publicó e integró PR #22 mediante merge commit
`f81a99098f1c0c4365411b25dccb8de13707bb45` y se desplegó el bridge en modo
exclusivamente observacional. No se autorizaron ni ejecutaron mensajes reales,
outbound, cohorte, migraciones remotas, handoff productivo ni cambios financieros.

## Cambios efectivos

- EasyPanel reconstruyó el servicio desde `main` y preservó su configuración
  anterior en un backup privado `0600`, fuera del contenedor efímero.
- El archivo candidato de environment fue eliminado después del deploy.
- Scope efectivo del bridge: Chatwoot Account `1`, Inbox WABA `6`, provider `waba`.
- Permanecen apagados replies, splitter, pausa humana, shadow, resolución, compra,
  perímetro piloto, dispatcher, outbound, opt-out durable/projection y handoff
  admission/projection.
- La configuración que podía autoiniciar opt-out projection quedó ausente.
- Evolution permanece desconectado (`close`) y su integración Chatwoot quedó
  `enabled=false`. Se conservan la instancia, su configuración respaldada y el
  inbox histórico `1`; no se borraron credenciales ni historial.

## Verificación real

- `GET /health`: `200`, `status=ok`.
- `GET /ready`: `200`, `status=ready`, `automation_state=default_off`,
  `reason_code=pilot_boundary_disabled`.
- El hash de `/app/src/bridge/app.py` coincide con el checkout integrado.
- La imagen efectiva fue creada el `2026-08-12T13:25:42Z`.
- Desde ese instante Chatwoot registró cero mensajes nuevos y cero outgoing
  públicos.
- Logs del bridge desde el deploy: cero errores, cero tracebacks, cero webhooks,
  cero marcadores Hermes y cero marcadores de reply.
- Volumen persistente: un único work item histórico `completed`; cero reply
  artifacts. La captura y shadow históricos se conservaron sin reprocesarlos.
- Inboxes `1` y `6` siguen existiendo. Inbox `6` conserva dos miembros humanos y
  configuración completa de `whatsapp_cloud`.
- Los dos backups productivos quedaron fuera de contenedores, con permisos
  privados; no se registraron sus contenidos ni nombres en esta evidencia.

## Readiness integral

El verificador sanitizado terminó deliberadamente con exit code `1`:

```json
{
  "blockers": [
    "handoff_projection_backlog_not_zero",
    "human_handoff_team_missing",
    "opt_out_projection_backlog_not_zero"
  ],
  "safe_for_controlled_inbound": false,
  "status": "blocked"
}
```

Los dos blockers de backlog representan un estado **no comprobable**, no un
backlog positivo: Supabase remoto todavía devuelve `404` para la tabla/RPC de
proyección porque las migraciones correspondientes no fueron autorizadas ni
aplicadas. Chatwoot tampoco tiene todavía un Team con miembros (`teams_count=0`).

## Resultado

El cutover técnico default-off y el aislamiento de Evolution fueron exitosos y
sin efectos observados. El runtime está sano pero el gate integral permanece
fail-closed. No se debe pedir todavía el inbound físico ni habilitar ningún efecto
hasta resolver Team y esquema remoto mediante autorizaciones separadas y obtener
`status=ready`.
