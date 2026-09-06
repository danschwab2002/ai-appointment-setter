# Johanna: autoridad precheckout de seis landings — evidencia local

- **Fecha:** 2026-09-06
- **Estado:** candidato local implementado; sin commit, deploy, backfill ni envío real
- **Base integrada:** `df0faaa6cba3250622018691c3bdcebd57cf7c5f`
- **Migración:** `20260831000300_johanna_six_landing_precheckout.sql`

## Alcance publicado

La tabla inmutable `johanna_precheckout_landing_offers` contiene exactamente:

| landing | offer |
|---|---|
| `ads-a` | `bxjge6zq` |
| `ads-b` | `mgbgpp19` |
| `ads-c` | `s1qfxm7m` |
| `org-a` | `jtt6fcsm` |
| `org-b` | `ecyu87q0` |
| `org-c` | `ulhzpw9a` |

La migración crea scopes Hotmart y bindings de timer por oferta exacta, sin
oferta comodín. Admisión, scheduling, reevaluación y autorización final vuelven
a consultar la relación. La creación/reutilización de la intención conserva el
landing y offer admitidos.

El scope agregado
`johanna-precheckout-delayed-first-touch-production / 1` sustituye como
autoridad efectiva al scope piloto 1/1. Su control permanece `inactive / 0`, tal
como exige readiness; el efecto externo continúa dependiendo de
`PRECHECKOUT_DELAYED_OUTBOUND_ENABLED`. El scope piloto histórico permanece
inmutable e inactivo sólo para auditoría.

## Guardas y no-replay

Los patches por marcador conservan las validaciones existentes de contrato,
consentimiento, teléfono, conflicto de identidad, opt-out, takeover humano,
compra/evento proveedor y estados `request_started` / `delivery_unknown`. La
migración no inserta commands ni outbound attempts, no reprograma timers y no
recorre submissions históricas.

## Evidencia ejecutada

```text
uv run pytest tests/test_johanna_six_landing_precheckout_migration.py -q
.......                                                                  [100%]

node tests/sql/followup_engine/validate_johanna_six_landing_precheckout.mjs
JOHANNA_SIX_LANDING_PRECHECKOUT_SQL_OK

node tests/sql/followup_engine/validate_acl_hardening.mjs
acl_hardening=OK positive_control_leaks=6 public_functions=149 service_entrypoints=63

uv run pytest
1500 passed, 1 warning in 57.75s

cd tests/sql/followup_engine && npm test
exit 0; incluyó JOHANNA_SIX_LANDING_PRECHECKOUT_SQL_OK y acl_hardening=OK
```

El validador PGlite ejerce los seis recorridos completos: admisión, intención,
timer, reevaluación, reserva y autorización final. También admite de forma
durable un V1.1 con teléfono no normalizable pero conserva contacto/activación
en `false`, rechaza un cruce landing/offer sin dejar efectos y verifica la
inmutabilidad de la relación y el contrato readiness `inactive / 0`.

El fingerprint compartido registra la nueva migración y mantiene reconocible el
estado histórico default-off de `20260829000500`. Las suites completas Python y
SQL quedaron verdes después de integrar `main`. Ninguna de estas pruebas locales
acredita DDL ni efectos de producción.
