# Contrato provisional de submission pre-checkout V1

- **Estado:** Implementado y verificado localmente; no desplegado
- **Assurance:** `synthetic_provisional`; no observado del proveedor
- **Endpoint:** `POST /webhooks/precheckout`
- **Versión externa:** `1.0.0-emulated`
- **Decisión:** [ADR 0011](../decisions/0011-provisional-precheckout-adapter-boundary.md)
- **Diseño:** [correlación de intención](../design/joana-precheckout-intent-correlation-v1.md)

## 1. Alcance

Este contrato permite desarrollar desde la recepción del formulario mientras se desconoce su
payload real. Es una interfaz emulada y reemplazable, no una afirmación sobre el proveedor.

El corte implementado termina en:

```text
request autenticado
→ validación y normalización
→ submission durable
→ purchase_intent waiting_for_purchase
```

No implementa timer, consulta Hotmart, clasificación, recuperación, template, conversación ni
mensaje. Toda intención creada por esta versión conserva:

```json
{
  "provisional": true,
  "provider_observed": false,
  "activation_authorized": false
}
```

## 2. Transporte y autenticación

- Header: `X-PRECHECKOUT-TOKEN`.
- Secreto aislado: `PRECHECKOUT_FORM_TOKEN`.
- Feature flags: `PRECHECKOUT_FORM_ENABLED=false` y
  `PRECHECKOUT_TEST_MODE_ENABLED=false`.
- La configuración de deployment sólo acepta ambos flags en `true` si existe
  `PRECHECKOUT_TEST_PHONE_E164` canónico y coincide exactamente con
  `ALLOWED_WHATSAPP_JID`.
- Edad máxima: `PRECHECKOUT_MAX_AGE_SECONDS`, default `300`.
- Body máximo: 64 KiB.
- El token se compara en tiempo constante antes de leer el body.
- Supabase es obligatorio para aceptar una submission.
- tenant, funnel, landing, producto y oferta se comparan contra scope server-side; el token no
  convierte esos campos aportados por el caller en autoridad.

El endpoint puede ejercitarse localmente por inyección explícita de `Settings` y, una vez
desplegado, mediante el gate test-only anterior. No existe una ruta de activación general para
este V1. El teléfono se revalida después de parsear y antes de llamar a Supabase; cualquier otro
destinatario recibe `403` y produce cero efectos.

## 3. Payload emulado

```json
{
  "id": "form-submit-fixture-0001",
  "event": "PRECHECKOUT_FORM_SUBMITTED",
  "version": "1.0.0-emulated",
  "created_at": "2026-08-14T22:15:00Z",
  "lead": {
    "full_name": "Lead de Prueba",
    "phone_e164": "+12025550123"
  }
}
```

El fixture ejecutable está en
`tests/fixtures/precheckout_form_submission_v1.json`. Todos sus datos son sintéticos.

## 4. Validación y normalización

Obligatorio:

- `id` no vacío;
- evento y versión exactos;
- timestamp ISO-8601 con timezone;
- nombre y teléfono E.164.

El caller no posee tenant, funnel, landing, producto, oferta ni consentimiento. El adapter los
liga desde configuración server-side. En el scope provisional actual se usan los refs observados
`F106691755G` y `bxjge6zq`; siguen pendientes de ratificación como IDs canónicos del proveedor.
El contrato emulado es exacto: campos extra de primer nivel o dentro de `lead` se rechazan antes
de persistir. Esto evita capturar PII o semántica no declarada.

Normalización:

- teléfono: E.164 de entrada → dígitos canónicos internos;
- timestamp: UTC;

Email y país no son requeridos ni inventados. El adapter real deberá mapearlos sólo si el
payload observado los entrega y su semántica queda demostrada.

Como la compleción todavía no prueba qué aceptación persiste el backend, la representación
canónica fija `terms_accepted=false`, `privacy_accepted=false`,
`whatsapp_contact=false` y `activation_authorized=false`. La captura del copy se conserva como
versión de evidencia server-side, no como prueba de consentimiento individual.

## 5. Admisión e idempotencia

La RPC `admit_precheckout_form_submission` ejecuta en una transacción:

1. valida otra vez assurance y campos canónicos;
2. serializa por submission ID e identidad comercial;
3. replay exacto del ID → `duplicate` y devuelve la misma intención;
4. mismo ID con contenido diferente → registra `semantic_conflict` una sola vez por fingerprint;
5. replay del mismo conflicto → reutiliza la evidencia; una colisión de fingerprint con payload
   diferente falla cerrado;
6. crea la submission append-only;
7. anexa submissions repetidas a una única intención viva para
   tenant/funnel/teléfono/producto/oferta;
8. crea `purchase_intent` en `waiting_for_purchase` sólo si no existe una viva.

Los tipos JSON y el consentimiento se revalidan dentro de PostgreSQL. `service_role` no tiene
DML directo sobre las cuatro tablas del agregado; sólo puede ejecutar la RPC de admisión.

No se reinicia una espera ni se concede opt-in por anexar un submit. Esta versión sólo admite
consentimiento y activación en `false`; ningún valor del caller puede promoverlos. Las demás
políticas de repetición no están implementadas.

## 6. Respuestas

| HTTP | `status` | Significado |
|---:|---|---|
| 202 | `received` | submission e intención admitidas |
| 200 | `duplicate` | replay exacto |
| 200 | `conflict` | mismo ID con semántica diferente; cero efectos |
| 400 | — | JSON o payload inválido |
| 403 | — | teléfono distinto del único destinatario de prueba |
| 401 | — | token inválido o submission fuera de ventana |
| 413 | — | body mayor a 64 KiB |
| 503 | — | feature apagado, secreto/Supabase ausente o persistencia no disponible |

Las respuestas nunca devuelven email, teléfono ni nombre. Los outcomes admitidos incluyen
`test_only=true`, `generalizable=false` y conservan `activation_authorized=false`.

Este gate sólo admite el JID controlado; no demuestra consentimiento ni permite ampliar el
outbound. La autorización de un request-start pertenece separadamente al perímetro durable del
piloto (cohorte, estado, budget y reautorización), no a este receptor.

## 7. Compatibilidad futura

Cuando se observe el formulario real:

```text
payload real
→ adapter específico del proveedor
→ misma representación canónica
→ misma RPC o una versión explícitamente migrada
```

No se debe ensanchar silenciosamente este parser para aceptar formas ambiguas. Se agregará un
adapter versionado con fixtures del payload real sanitizados, tests de compatibilidad y un gate
explícito antes de marcar `provider_observed=true` o autorizar activación.