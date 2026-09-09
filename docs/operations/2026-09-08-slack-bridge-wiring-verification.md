# Verificación local del wiring Slack para Johanna y ATT1

- **Estado:** Implementado y verificado localmente
- **Alcance:** Wiring productivo default-off; sin despliegue ni efectos externos
- **Base:** `origin/main` en `c15d823458687d54c75c8605c9a9af1f44d6f435`
- **Rama:** `feat/slack-bridge-producer-wiring-v1`

## Resultado

El bridge construye un único `SlackConnectorProducer`, lo cierra con el lifecycle
de la aplicación y ejecuta una proyección durable post-commit para las
correlaciones no resueltas de Johanna o del runtime portable ATT1. La proyección
usa scopes exactos, deduplicación determinista, claims unitarios, leases, fencing
y reintentos recuperables. ATT1 exige el binding activo exacto antes del startup
y en cada claim.

El conector exige attestation explícita del tenant antes de leer o persistir el
comando. Rechazos terminales, conflictos o tenant mismatch detienen el worker;
fallos de polling, pérdida de liveness o falta del primer ciclo exitoso degradan
`/ready`.

## Evidencia ejecutada

- `uv run python scripts/agent_workspace.py preflight` — código 0.
- `git diff --check` — código 0.
- Suite Python completa `uv run pytest -q` — verde en la revisión independiente.
- Suite focal final de runtime, productor, conector, proyección y migración — 57
  pruebas verdes.
- `node validate_slack_correlation_projection.mjs` —
  `SLACK_CORRELATION_PROJECTION_SQL_OK`.
- `node validate_acl_hardening.mjs` — ACL sin fugas y allowlist consistente.
- Validadores SQL relacionados con runtime portable, correlación y resolución de
  operador — verdes.
- Dos revisiones independientes finales — aprobadas sin bloqueantes de seguridad,
  concurrencia o confiabilidad.

Una prueba async preexistente presentó una falla transitoria de tiempo durante
una corrida concurrente; la repetición aislada pasó. No se cambió ese subsistema.
El `npm test` agregado excedió el límite de tiempo; los validadores SQL modificados
y sus dependencias directas sí se ejecutaron en verde.

## Límites de esta evidencia

No se aplicó la migración a Supabase Cloud, no se desplegó ningún servicio y no
se enviaron mensajes reales ni sintéticos a Slack. Tampoco se usaron o rotaron
credenciales. La activación productiva requiere un corte posterior explícito con
secretos nuevos y verificación externa separada.
