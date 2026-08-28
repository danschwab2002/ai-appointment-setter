# Client Copilot: correlaciones pendientes bajo demanda

- **Estado:** Lectura desplegada; extensión manual supervisada implementada localmente
- **Alcance:** lectura, explicación, detalle y aplicación de elección humana explícita
- **Fuera de alcance:** IA de matching, activación y notificación proactiva

## Problema

La correlación exacta ya persiste `unmatched`, `ambiguous` y `conflict`, pero el operador sólo podía descubrir esos casos mediante consulta técnica. Para el MVP se necesita visibilidad bajo demanda sin convertir el modelo en autoridad de identidad.

## Diseño implementado

```text
Profile Copilot
  -> list/get tools específicos
  -> bearer independiente
  -> bridge default-off
  -> RPC read-only por tenant + funnel
  -> proyección enmascarada
```

El SQL conserva autoridad sobre elegibilidad y ownership. El bridge valida shape, reason codes e invariantes; el Copilot sólo ordena y explica lo recibido.

## Alternativas descartadas

### Dar SQL o terminal al Copilot

Descartado porque ensancha acceso, expone PII y permite mutaciones accidentales.

### Leer tablas directamente con `service_role`

Descartado porque las ACL vigentes revocan correctamente `SELECT` sobre `purchase_intents` y el ledger. Abrir esas tablas degradaría la frontera existente.

### Resolver con LLM, fuzzy matching o heurísticas

Descartado por decisión de producto. Un typo aparentemente obvio continúa siendo evidencia ambigua hasta una decisión humana explícita.

### Notificar proactivamente cada pendiente

Descartado para este corte. Los casos permanecen vivos y el operador los consulta cuando lo necesita.

## Fronteras

- El SQL devuelve sólo casos `manual_handoff_required=true` del tenant/funnel exactos.
- Un caso sin scope atribuible queda fuera fail-closed.
- El masking sucede antes de salir de PostgreSQL.
- El endpoint sólo existe cuando está habilitado y exige bearer propio.
- El plugin no conoce `service_role`.
- Las tools mutantes sólo preparan/aplican una elección humana y usan bearer separado.
- `confirm_correlation_resolution` pasa por aprobación manual nativa de Hermes.
- La resolución queda en ledger separado y no muta correlación ni intención.

## Despliegue de la extensión pendiente

La implementación manual local no prueba aún:

1. migración aplicada en Supabase Cloud;
2. bridge desplegado con rutas habilitadas;
3. plugin `0.2.0` instalado y validado en el Profile efectivo;
4. prueba HTTP real contra el bridge;
5. consulta conversacional real del Profile Copilot.

Esos pasos requieren autorización de deploy y credenciales configuradas fuera de Git.

## Decisión posterior, fuera de alcance

El contrato manual supervisado fue aceptado e implementado localmente. Cualquier
promoción posterior desde el binding manual hacia lifecycle, activación o efectos
requiere una decisión y un contrato separados; V1 mantiene todo eso bloqueado.
