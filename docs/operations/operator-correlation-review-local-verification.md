# Verificación local: consulta de correlaciones pendientes

- **Fecha UTC:** 2026-08-24
- **Estado:** Evidencia local; no prueba migración, deploy ni configuración en producción
- **Worktree:** `feat/copilot-unresolved-correlations-read`
- **Base:** `origin/main@02ff1168dec905b92b71826b8aa611f7c326234a`

## Suite Python

```text
uv run pytest
1058 passed, 1 warning
```

El warning corresponde a la deprecación existente de `fastapi.testclient` con `httpx`; no es un fallo del corte.

## Stack SQL PGlite

```text
npm test
exit 0

acl_hardening=OK
service_entrypoints=38
operator_correlation_review_read=OK
operator_correlation_review_pii_masking=OK
operator_correlation_review_acl=OK
```

La prueba aplicó el stack canónico, creó fixtures `resolved`, `unmatched` y `ambiguous`, verificó filtro tenant/funnel, masking, exclusión de `resolved`, delta durable cero, RPC permitida a `service_role`, lectura directa bloqueada y RPC rechazada para `anon`.

La regresión adversarial agregó deliberadamente una intención de otro tenant como
candidata de una correlación Lancemos. La proyección excluyó su email, teléfono y
metadatos porque el candidato no coincidía también en tenant, funnel, producto y oferta.
El masking SQL verificó además que `a@example.com` sale como `***@example.com`.

## HTTP real local

Se levantó Uvicorn en loopback y un mock PostgREST RPC separado, ambos fuera del repositorio y sin credenciales productivas. Resultado observado:

```json
{
  "automation_blocked": true,
  "candidates": 2,
  "count": 1,
  "detail": 200,
  "health": 200,
  "list": 200,
  "pii_raw_absent": true,
  "unauthorized": 401
}
```

Los procesos temporales se detuvieron después de la prueba.

## Instalación del perfil

El instalador se ejecutó contra un home temporal fuera del repositorio:

```json
{
  "env_created": false,
  "file_mode": "0600",
  "installed_files": 8,
  "plugin_present": true,
  "profile_mode": "0700"
}
```

No se instalaron skills, memoria ni credenciales.

Una falla inyectada durante la copia a staging dejó el perfil activo intacto y permitió
repetir la actualización. El plugin rechazó un redirect cross-origin antes de crear un
nuevo request con el bearer.

## Pendiente antes de producción

- merge autorizado;
- migración en Supabase Cloud;
- bearer y scope configurados fuera de Git;
- bridge desplegado default-off y luego habilitado de forma controlada;
- instalación privada del Profile Copilot;
- `hermes plugins doctor <path-or-id> --ci` en el host que sí tiene Hermes;
- postflight SQL y HTTP Cloud;
- consulta conversacional real del operador.
