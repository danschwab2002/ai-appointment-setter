# Contrato WABA de template y readiness por etapas V1

- **Estado:** histórico y reemplazado por V2 para readiness controlado
- **Versión:** 1
- **Interfaz:** `scripts/verify_waba_staged_readiness.py`
- **No habilita:** conexión productiva, envío, activación, migración ni creación de templates
- **Compatibilidad:** contrato histórico de par primer contacto + follow-up; el corte single-touch de dos variables usa [V2](waba-template-readiness-v2.md)

## Frontera

El verificador consume por `stdin` un snapshot JSON sanitizado y compara el scope activo con tres enteros positivos provistos por argumentos: account esperado, inbox nuevo esperado e inbox anterior. Un ID booleano, faltante, no positivo o ambiguo falla cerrado. La salida nunca repite IDs ni campos desconocidos.

Todos los gates usan booleanos JSON exactos. `true`, `false` y campo ausente son estados distintos; strings, `0`, `1` y `null` no sustituyen una atestación.

## Niveles

Los niveles son acumulativos y no intercambiables:

1. `ready_for_observational_inbound`: canal oficial conectado y scoped, inbox anterior rechazado, Evolution fuera del scope y todo efecto apagado. No requiere Team, template, pago, schema remoto ni handoff.
2. `ready_for_controlled_template`: suma pago operativo, selección inequívoca y doble aprobación de templates, contrato de variables exacto, destinatario allowlisted, presupuesto de un envío, backlog elegible cero y rollback.
3. `ready_for_supervised_pilot`: suma scope durable publicado pero inactivo, schema remoto, stops por compra/opt-out, política y Conversation Release aprobadas, monitoreo, cohorte/presupuesto acotados y kill switch. `handoff_enabled` debe ser un booleano exacto: `true` exige owner, `false` no lo exige y cualquier valor no booleano bloquea con `handoff_enabled_invalid`.

`highest_ready_level` es el nivel acumulativo más alto listo o `null`. Cada nivel devuelve `status`, `ready` y `reasons` ordenados. Exit code `0` significa que al menos inbound observacional está listo; `1`, que ningún nivel lo está; `2`, input/argumentos inválidos.

## Contrato de templates V1

Primer contacto y follow-up son selecciones separadas. Cada uno debe tener:

- nombre canónico presente;
- aprobación vigente de Meta;
- aprobación explícita del negocio;
- idioma y categoría confirmados;
- exactamente un placeholder de body `{{1}}`.

El runtime implementado hoy comparte idioma, categoría y esquema de variables entre ambos templates y sólo acepta `MARKETING` o `UTILITY`. Por eso `category_runtime_supported=true` exige una de esas categorías y `pair_runtime_compatible=true` sólo puede atestarse si ambos coinciden en categoría, idioma y esquema. Una categoría no soportada falla con `template_category_runtime_unsupported`; un mismatch del par falla con `template_pair_runtime_mismatch`. No se adapta el payload, no se usa freeform y no se elige otro template como fallback.

`provider_mode_compatible=true` exige provider durable `waba` y modo `approved_template`. `waba + freeform` y provider no WABA + `approved_template` son incompatibles.

## Snapshot mínimo

Las secciones son `channel`, `runtime`, `template`, `controlled_template`, `supervised_pilot` y `evidence`. Los nombres de campos y reason codes quedan fijados por el verificador y sus pruebas. Los digests son sólo presencia (`*_present=true`); el snapshot que se conserve no debe contener secretos, teléfonos, nombres de templates, copy, IDs externos ni payloads.

## Compatibilidad

Agregar gates requiere una nueva versión o un cambio backward-compatible que permanezca fail-closed. Renombrar niveles/reasons o relajar un booleano exacto es breaking. La aprobación o conexión externa no cambia por sí misma el runtime ni eleva un nivel: primero debe existir una lectura nueva y sanitizada.

El verificador actual conserva inbound observacional, pero bloquea readiness controlado V1 con `template_contract_version_unsupported`. No debe usarse V1 para atestar el sender single-touch actual.
