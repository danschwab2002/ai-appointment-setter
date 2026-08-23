# Contrato WABA single-touch de dos variables y readiness V2

- **Estado:** implementado localmente; aprobación/sincronización externa y E2E pendientes
- **Versión:** 2
- **Interfaz:** `scripts/verify_waba_staged_readiness.py`
- **No habilita:** conexión productiva, envío, migración, creación de templates ni activación
- **Compatibilidad:** readiness controlado exige `template.contract_version` igual al entero exacto `2`; V1/ausencia/otros tipos fallan cerrado

## Alcance

V2 representa el primer corte de carrito abandonado de Johanna como un único contacto aprobado por Meta. No modela compra fallida ni usa el segundo candidato como follow-up.

El runtime exige:

- un nombre canónico de template de primer contacto;
- aprobación vigente de Meta y aprobación explícita del negocio;
- idioma y categoría confirmados;
- categoría `MARKETING` o `UTILITY`;
- exactamente dos placeholders de body y en este orden:
  1. nombre de la persona;
  2. nombre de la oferta/producto;
- follow-up explícitamente deshabilitado;
- provider durable `waba` y modo `approved_template`;
- cero fallback a texto libre o Evolution.

Un nombre o producto ausente/vacío bloquea el sender antes de cualquier lookup, creación de contacto, creación de conversación o POST de mensaje.

## Snapshot de template V2

```json
{
  "template": {
    "contract_version": 2,
    "selection_unambiguous": true,
    "first_touch_meta_approved": true,
    "first_touch_business_approved": true,
    "followup_disabled": true,
    "first_touch_name_present": true,
    "language_present": true,
    "category_present": true,
    "category_runtime_supported": true,
    "body_placeholders_two_exact": true,
    "single_touch_runtime_compatible": true
  }
}
```

Salvo `contract_version`, todos los campos del bloque son atestaciones booleanas exactas. Strings, `0`, `1`, `null` o ausencia fallan cerrado. `contract_version` debe ser el entero JSON exacto `2`; floats como `2.0`, strings y booleanos producen `template_contract_version_unsupported` para readiness controlado.

## Reason codes V2

| Gate | Reason cuando no es `true` |
|---|---|
| `selection_unambiguous` | `template_selection_ambiguous` |
| `first_touch_meta_approved` | `first_touch_meta_not_approved` |
| `first_touch_business_approved` | `first_touch_business_not_approved` |
| `followup_disabled` | `followup_not_disabled` |
| `first_touch_name_present` | `first_touch_template_name_missing` |
| `language_present` | `template_language_missing` |
| `category_present` | `template_category_missing` |
| `category_runtime_supported` | `template_category_runtime_unsupported` |
| `body_placeholders_two_exact` | `template_placeholder_schema_mismatch` |
| `single_touch_runtime_compatible` | `single_touch_runtime_mismatch` |

Los niveles, gates de canal/runtime/control/piloto, sanitización, exit codes y significado de `highest_ready_level` permanecen iguales a V1.

## Configuración runtime

- `WABA_FIRST_TOUCH_TEMPLATE_NAME`: obligatorio al habilitar outbound WABA.
- `WABA_FOLLOWUP_TEMPLATE_NAME`: opcional; vacío significa follow-up bloqueado por el adapter.
- `WABA_TEMPLATE_LANGUAGE`: obligatorio.
- `WABA_TEMPLATE_CATEGORY`: obligatorio y limitado a `MARKETING` o `UTILITY`.

Los valores canónicos provienen de un readback autorizado de Meta/Chatwoot y viven en el secret store administrado. El snapshot versionado sólo conserva atestaciones sanitizadas.
