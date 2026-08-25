# Client Copilot: correlaciones pendientes bajo demanda

- **Estado:** Base aceptada e implementada localmente; no desplegada
- **Alcance:** MVP de lectura, explicación y detalle
- **Fuera de alcance:** resolución manual, IA de matching y notificación proactiva

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
- No existe tool mutante en el paquete.

## Despliegue pendiente

La implementación local no prueba aún:

1. migración aplicada en Supabase Cloud;
2. bridge desplegado con rutas habilitadas;
3. plugin instalado y validado con `hermes plugins doctor <path-or-id> --ci`;
4. prueba HTTP real contra el bridge;
5. consulta conversacional real del Profile Copilot.

Esos pasos requieren autorización de deploy y credenciales configuradas fuera de Git.

## Próxima decisión, no implementada

Antes de agregar mutaciones se debe aceptar un contrato separado para registrar una decisión humana auditable. Este corte no anticipa sus comandos, estados ni efectos.
