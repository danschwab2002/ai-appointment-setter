# Verificación local de resolución manual de correlaciones — 2026-08-24

## Alcance

Evidencia sanitizada de la implementación local de resolución manual supervisada.
No constituye autorización ni evidencia de despliegue.

## Base

- Branch: `feat/operator-correlation-manual-resolution`
- Base inicial: `fd965d081054fc73a6bafb0575e7a7eee73b5592`
- Migración candidata: `20260824000200_operator_correlation_manual_resolution.sql`
- Profile candidato: `client-copilot` package `0.2.0`

## Evidencia local

- `uv run pytest`: `1072 passed, 1 warning` preexistente de TestClient.
- `npm test`: exit `0`.
- Probe SQL manual:
  - prepare no inserta resolución;
  - replay de prepare con igual UUID/fingerprint devuelve el mismo comando;
  - igual key con semántica distinta falla cerrado;
  - confirm exacto inserta una resolución;
  - replay de confirm devuelve la misma resolución;
  - drift del candidato bloquea confirmación;
  - outcome determinístico y `purchase_intents` no cambian;
  - cero timers, activaciones o efectos.
- ACL full-stack: `108` funciones públicas, `40` entrypoints `service_role`, cero leaks/desajustes.
- Plugin Doctor: `4` tools, `1` hook, import y registro correctos.
- HTTP TCP loopback real:
  - prepare sin bearer: `401`;
  - prepare autenticado: `200`;
  - confirm autenticado: `200`;
  - ambos responses conservaron `automation_blocked=true`.
- `git diff --check`: exit `0`.
- `agent_workspace.py preflight`: exit `0`.
- Scan estático de líneas agregadas: cero secretos hardcodeados, shell injection, `eval/exec`, pickle o SQL formateado.

## Preflight read-only en Supabase Cloud

La fixture sintética existente continúa apta para una prueba posterior:

- caso presente: `1`;
- outcome: `ambiguous` / `multiple_candidates`;
- `manual_handoff_required=true`;
- vínculo determinístico de purchase ausente;
- candidatos durable/scoped: `2/2`;
- todos en `waiting_for_purchase`;
- todos con `activation_authorized=false`;
- tenant/funnel coinciden con el scope del piloto.

No se consultó ni registró PII.

## No ejecutado

- No se aplicó la migración en Supabase Cloud.
- No se desplegó una imagen del bridge.
- No se instaló el Profile `0.2.0` efectivo.
- No se configuró bearer de escritura ni actor productivo.
- No se preparó ni confirmó una resolución real.
- No se habilitó worker, timer, dispatcher, outbound ni activación.

La prueba productiva controlada requiere autorización separada de merge/deploy y debe
mantener todos los efectos apagados.

## Revalidación de integración — 2026-08-28

La implementación preservada se integró sobre
`221d3c0dac3a3d91526d533b85e89ffff9c5ba55`, manteniendo las migraciones posteriores
del piloto y sin activar efectos.

- `uv run pytest`: `1164 passed, 1 warning` preexistente de TestClient.
- `npm test`: exit `0` sobre el stack completo.
- Probe SQL de resolución manual: prepare, confirm, replay, stale guard,
  owner-forgery guard y cero efectos: PASS.
- ACL full-stack: `121` funciones públicas, `50` entrypoints `service_role`,
  cero leaks/desajustes.
- Inventario cronológico: `20260824000200` presente una vez antes de las
  migraciones `20260825`–`20260827`.

Esta revalidación sigue siendo evidencia local. No prueba publicación, merge,
aplicación en Supabase Cloud ni despliegue del Bridge.
