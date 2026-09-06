# Johanna: autoridad precheckout de seis landings — evidencia productiva

- **Fecha:** 2026-09-06
- **Estado:** PR #103 integrado; migración y bridge desplegados; E2E de seis rutas y limpieza selectiva completos; sin backfill ni envío real
- **Base del candidato:** `df0faaa6cba3250622018691c3bdcebd57cf7c5f`
- **Commit revisado:** `590e0f345a35c9122be9050ce4598d60db361362`
- **Merge commit:** `d6813409508c1c0994697fcf38f20162342a47b9`
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
..........                                                               [100%]

node tests/sql/followup_engine/validate_johanna_six_landing_precheckout.mjs
JOHANNA_SIX_LANDING_PRECHECKOUT_SQL_OK

node tests/sql/followup_engine/validate_acl_hardening.mjs
acl_hardening=OK positive_control_leaks=6 public_functions=149 service_entrypoints=63

uv run pytest
1503 passed, 1 warning in 62.87s

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
SQL quedaron verdes antes de integrar `main`.

## Postflight de producción

La lectura posterior a la ejecución manual del SQL confirmó:

```text
20260831000200 tracked = true
20260831000300 tracked = true
johanna_precheckout_landing_offers = 6
scopes Hotmart exactos = 6
bindings de timer exactos = 6
RPC con pair guard = 4
runtime = inactive / generation 0
```

El redeploy de EasyPanel partió de `origin/main`. El primer smoke todavía alcanzó
la réplica anterior: sólo `ads-a` persistió y las otras cinco respuestas externas
fueron `delivered:false`; esa observación no se contó como éxito. La primera matriz
post-rollout llegó a las seis rutas, pero usó un nombre no contractual para el
campo de consentimiento y sólo acreditó transporte/persistencia. Después de
corregir el fixture a `whatsapp_contact_consent`, la matriz contractual comprobó:

```text
V1.1.0 invalid-phone: 6/6 delivered, 6 submissions, 6 intents, 6 pares exactos
V1.1.0 invalid-phone: 6/6 normalized_phone=NULL, contacto=false, activación=false
timers nuevos = 0
commands nuevos = 0
```

La compatibilidad V1.0.0 permaneció cubierta por el contrato desplegado y la
suite automatizada; no se presenta la matriz productiva V1.1.0 como una segunda
matriz E2E de V1.0.0.

`GET /health` y `GET /ready` respondieron `200`. Readiness informó
`precheckout_first_touch_ready` y cero due, reserved, request-started y
delivery-unknown; `pilot_boundary=disabled` y `automation_state=default_off`
mantuvieron imposible el efecto global.

## Reevaluación histórica y limpieza

La reevaluación controlada fue sólo lectura. Encontró 46 outcomes históricos
`scope_not_configured` para `mgbgpp19`: 24 `PURCHASE_APPROVED` y 22
`PURCHASE_OUT_OF_SHOPPING_CART`. Bajo el nuevo scope exacto de 24 horas, los 46
tenían cero candidatos; no se reescribieron correlaciones, no se crearon timers o
commands y no hubo contacto retroactivo.

La limpieza posterior eliminó por IDs exactos 14 submissions de diagnóstico/E2E
y 13 intents exclusivamente sintéticos. Un intent compartido por el diagnóstico
personal se preservó y se eliminó sólo su submission/link. El resultado
transaccional y el postflight read-only confirmaron:

```text
deleted submissions = 14
deleted exclusively-owned intents = 13
preserved shared intents = 1
deleted webhooks = 0
deleted purchases = 0
deleted commands = 0
target test submissions remaining = 0
request-started = 0
delivery-unknown = 0
```

Los totales globales de submissions e intents siguieron avanzando con tráfico
ajeno después de la limpieza; por eso no se usan como invariante del borrado. La
verificación se ancla al conjunto exacto de IDs objetivo y a las guardas
transaccionales que impidieron borrar webhooks, compras, commands e intents
compartidos.

La evidencia acredita navegador/relay → bridge → Supabase, autoridad cerrada de
seis pares, contratos V1.0.0/V1.1.0 y degradación segura del teléfono inválido. No
acredita activación de mensajes, aceptación WABA ni entrega física.
