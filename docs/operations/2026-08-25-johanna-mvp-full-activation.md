# Evidencia del candidato de activación completa del MVP de Johanna

- **Fecha:** 2026-08-25
- **Estado:** Implementación local verificada y aprobada; publicación y despliegue pendientes
- **Branch:** `feat/johanna-mvp-full-activation`
- **No incluyó:** mensajes reales, cambios productivos, migraciones remotas ni activación de flags

## Alcance ejercitado

- inbound comercial por remitente canónico dentro del account/inbox exacto;
- reautorización de identidad antes de historia, reply, assignment y nota privada;
- handoff que pausa sin emitir un reply adicional;
- parsing Hotmart exacto de `PURCHASE_CANCELED + CANCELLED + NO_FUNDS`;
- admisión durable de pago fallido seguida por command first-touch default-off;
- plantilla fija `johanna_compra_fallida_01`, un mensaje y cero follow-ups;
- presupuesto físico compartido con carrito por teléfono;
- replay exacto/concurrente, compra aprobada, opt-out, identidad ambigua y ACL;
- startup fail-closed del scope inbound `1/9 + libre-de-ansiedad-inbound v2`.

## Resultados

### Python focal vigente

```text
uv run pytest \
  tests/test_hotmart_webhook.py::test_payment_failure_resolved_case_sends_approved_template_once \
  tests/test_hotmart_webhook.py::test_payment_failure_rejects_scope_mismatch_before_rpc \
  tests/test_johanna_mvp_activation_migration.py::test_payment_failure_admission_is_exact_and_durable \
  tests/test_deployment_config.py::test_deployment_declares_johanna_full_mvp_flags_default_off -q

resultado: PASS
```

### Python completo vigente

```text
uv run pytest -q

resultado: PASS
```

### SQL canónico

```text
cd tests/sql/followup_engine && npm test

resultado: PASS
JOHANNA_PAYMENT_FAILURE_DURABLE_REVIEW_OK
acl_hardening=OK
service_entrypoints=45
```

El tracer SQL vigente cubre command `started`, replay, finalización
`outbound_accepted`, presupuesto compartido, compra aprobada antes de
`request_started`, opt-out durable, propiedad ambigua del teléfono y convergencia
concurrente en una sola fila.

### PostgreSQL 17 disposable

Se aplicaron baseline y todas las migraciones, incluida `20260825000500`, sobre
PostgreSQL `17.10` local, loopback-only y disposable. El probe físico ejecutó
admisión, begin, replay, metadata de plantilla, finalización y ACL dentro de una
transacción con rollback:

```text
POSTGRES17_JOHANNA_PAYMENT_FAILURE_OUTBOUND_OK
```

El cluster y el archivo temporal del probe fueron eliminados después de la
ejecución.

### Remediación de revisión adversarial

La primera revisión independiente devolvió `request_changes` por cuatro rutas no
cubiertas. Se agregaron regresiones RED→GREEN para:

- replay terminal `outbound_accepted` y `delivery_unknown` con HTTP 200 y cero
  efectos adicionales;
- reconciliación transaccional de command y caso hacia `outbound_accepted`;
- claim de opt-out con account, inbox y teléfono canónicos, seguido por
  reautorización Chatwoot inmediatamente antes del macro;
- serialización de `begin` con el writer durable de opt-out.

La última condición se ejecutó con dos sesiones PostgreSQL `17.10`: el writer
real mantuvo `chatwoot-opt-out-user` durante una transacción, `begin` esperó y,
después del commit, devolvió
`johanna_payment_failure_hotmart_auto_contact_blocked`. El estado final sanitizado
fue `stops=1`, `commands=0`, `case_status=pending_human_review`.

### HTTP real del entrypoint productivo

Se arrancó localmente el comando del contenedor con todos los efectos default-off:

```text
uvicorn bridge.app:build_app --factory --host 127.0.0.1 --port 18081
```

Resultado por TCP real:

```text
/health status=200 body={"status":"ok"}
/ready status=200 body={"status":"ready","pilot_boundary":"disabled","automation_state":"default_off","reason_code":"pilot_boundary_disabled"}
```

También se ejecutaron `uv build`, `git diff --check` y el preflight coordinado con
resultado PASS. El proceso HTTP se cerró después del probe.

## Límites de la evidencia

- No demuestra publicación Git, CI remota, migración en Supabase Cloud ni despliegue EasyPanel.
- No demuestra tráfico Hotmart real del evento soportado.
- No demuestra reply inbound real posterior al release.
- No demuestra aceptación física de `johanna_compra_fallida_01`; no se realizó un
  envío real durante esta implementación.
- La revisión residual independiente reprodujo las cuatro correcciones y devolvió
  `approve`, sin release blockers.
- La evidencia productiva previa del carrito abandonado no fue repetida y el contacto ya usado no recibió nuevos mensajes.
