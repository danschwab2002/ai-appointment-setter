# Evidencia local de correlación Hotmart ↔ intención — 2026-08-20

- **Estado:** Evidencia operativa local
- **Alcance:** stack canónico + migración `20260820000100` en PostgreSQL 17.10 desechable
- **Cloud:** no aplicado
- **Outbound:** no ejecutado

## Corte ejercitado

```text
purchase_intent observado
→ webhook_event Hotmart procesable
→ wrapper de admisión + identidad canónica
→ scope product.id ↔ hotlink
→ correlación durable
→ resolved | unmatched | ambiguous | conflict
→ actualización fail-closed de purchase_intent
```

El probe recreó una base vacía, aplicó baseline más 26 migraciones canónicas y
eliminó sus fixtures lógicos al descartar la base. No usó credenciales, PII real ni
servicios externos.

## Evidencia

Comando canónico:

```text
uv run python tests/sql/followup_engine/real_postgres_hotmart_intent_correlation.py
```

Resultado:

```text
hotmart_intent_correlation_migrations=26
hotmart_intent_payload_identity_binding=OK
hotmart_intent_resolved_abandonment=OK
hotmart_intent_purchase_supersedes_abandonment=OK
hotmart_intent_unmatched=OK
hotmart_intent_ambiguous=OK
hotmart_intent_conflict=OK
hotmart_intent_resolved_email_only=OK
hotmart_intent_zero_effects=OK
hotmart_intent_rolling_expand_shim=OK
hotmart_intent_python_sql_identity_parity=OK
hotmart_intent_acl_and_immutability=OK
HOTMART_PURCHASE_INTENT_CORRELATION_REAL_POSTGRES_OK
```

Inventarios sobre esa misma base:

```text
schema fingerprints missing=0
total canonical migrations=26
PostgreSQL public functions=125
PGlite public functions=89
service-role entrypoints=35
api ACL leaks=0
trigger service-role leaks=0
allowlist mismatches=0
```

Suites adicionales:

```text
uv run pytest -q                         PASS — 946 tests
npm test                                PASS
validate_pilot_boundary.mjs             PASS
validate_pilot_boundary_runtime.mjs     PASS
```

## Cero efectos

Después de todos los fixtures de correlación:

```text
recovery_cases=0
followup_sequences=0
scheduled_actions=0
```

La migración no habilita workers, dispatcher, sender, AgentBot, WhatsApp ni email.
`activation_authorized` permanece en `false` para todos los casos ejercitados.
El probe usa teléfonos Hotmart formateados, correlaciona contra la representación
canónica y comprueba que un replay con otra identidad falla sin mutar el ledger.
También demuestra que una réplica vieja puede usar el shim histórico y obtener la
misma correlación atómica, sin crear efectos comerciales.

## Límites

Esta evidencia prueba SQL, cliente RPC, compatibilidad con el stack y fronteras ACL
en local. No prueba todavía:

- migración aplicada en Supabase Cloud;
- evento oficial fresco emitido por Hotmart;
- correlación de la submission preview del 2026-08-20;
- comportamiento de un submit completo de la landing.
