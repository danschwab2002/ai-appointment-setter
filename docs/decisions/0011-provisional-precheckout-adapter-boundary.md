# ADR 0011 — Frontera provisional para el formulario pre-checkout

- **Estado:** Aceptada
- **Fecha:** 2026-08-14
- **Implementación:** Código y migración local presentes; no desplegada ni activada

## Contexto

El funnel de Joana contiene un formulario entre la landing y Hotmart. Se conocen visualmente
nombre, teléfono y correo, pero no existe todavía un webhook observado ni un contrato del
constructor. Esperar esa integración bloquearía el desarrollo de intención, correlación e
idempotencia; tratar una forma inventada como verdad del proveedor crearía acoplamiento y riesgo.

## Decisión

Se implementa una frontera externa emulada y versionada:

```text
payload `1.0.0-emulated`
→ adapter provisional
→ representación canónica provider-independent
→ admisión durable atómica
```

La representación creada por este adapter siempre queda marcada:

```text
provisional=true
provider_observed=false
activation_authorized=false
```

El receptor está apagado por defecto. Usa un token aislado, liga
tenant/funnel/landing/producto/oferta desde configuración server-side y termina en una
`purchase_intent` sin crear efectos. El payload real se incorporará mediante otro adapter; no se
hará depender el dominio de los nombres de campos del proveedor.

## Consecuencias

### Refinamiento aceptado — 2026-08-16

El contrato emulado se redujo al dato de negocio mínimo confirmado para construir downstream:
nombre y teléfono. `id`, evento y timestamp siguen siendo metadatos técnicos del fixture. El
scope comercial se liga server-side y el adapter fija toda evidencia de consentimiento y
activación en `false`; email, país y consentimiento no se inventan ni se exigen al payload.
Esto no cambia el gate de reemplazo por un adapter observado.

### Refinamiento aceptado — 2026-08-17

Para acelerar el E2E del piloto sin convertir el contrato emulado en autoridad productiva, la
configuración de deployment permite habilitar el receptor únicamente con un gate `test_only`:
`PRECHECKOUT_TEST_MODE_ENABLED=true` y un teléfono E.164 que coincida exactamente con el único
`ALLOWED_WHATSAPP_JID`. El handler vuelve a verificar ese teléfono antes de persistir. Cualquier
otro destinatario produce cero efectos. Este refinamiento no cambia los flags durables
`provider_observed=false` y `activation_authorized=false`, no concede consentimiento y no
autoriza un request-start; esa autoridad sigue perteneciendo al perímetro durable del piloto.

### Positivas

- permite TDD y desarrollo downstream sin esperar al constructor del formulario;
- limita el reemplazo futuro al borde de entrada;
- preserva idempotencia y conflictos desde el primer tracer;
- impide presentar datos sintéticos como evidencia productiva.

### Costos y límites

- el contrato emulado puede diferir del payload real;
- no prueba autenticación, redirect ni propagación a Hotmart;
- la migración debe cambiar de estado explícitamente antes de producción;
- no autoriza timer, lookup, clasificación ni mensajes.

## Alternativas descartadas

1. **Esperar el webhook real:** reduce trabajo potencialmente descartable, pero bloquea el
   tracer central del MVP.
2. **Acoplar dominio al payload supuesto:** acelera el primer endpoint, pero hace que un cambio
   del proveedor afecte identidad, persistencia y workflow.
3. **Persistir sólo JSON crudo:** conserva evidencia, pero no establece invariantes de
   identidad, idempotencia ni una intención durable utilizable.

## Gate para reemplazo

Antes de adoptar un adapter real se requiere payload observado sanitizado, autenticación
confirmada, mapping completo, pruebas de replay/conflicto y verificación HTTP. Sólo entonces una
versión posterior podrá declarar `provider_observed=true`; la activación seguirá siendo un gate
separado.