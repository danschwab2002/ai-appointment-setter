# Evidencia de creación y envío a revisión de plantillas Meta — ATT1

- **Estado:** Registrado por declaración del operador y evidencia visual; aprobación externa parcial
- **Fecha:** 2026-09-12
- **Alcance:** cuatro plantillas WABA de ATT1 creadas en Meta y enviadas al proceso de revisión
- **No implica:** sincronización con Chatwoot, actualización de configuración/runtime, publicación de política económica, activación de outbound ni autorización para contactar personas reales

## Resultado informado

El operador confirmó que creó y envió a revisión las cuatro plantillas previstas para ATT1. La captura aportada muestra cuatro entradas de categoría `Marketing`, todas con última modificación `12 sept. 2026`.

| Identidad prevista | Uso | Idioma visible | Estado visible |
|---|---|---|---|
| `att1_interes_precheckout_01` | Primer contacto después de precheckout autorizado | `Spanish (MEX)` | `Activa: calidad p…` |
| `att1_carrito_abandonado_01` | Primer contacto por carrito abandonado confirmado | `Spanish (MEX)` | `Activa: calidad p…` |
| `att1_compra_fallida_01` | Primer contacto por compra fallida confirmada | `Spanish (MEX)` | `Activa: calidad p…` |
| `att1_descuento_10_post_respuesta_01` | Mensaje posterior a inbound con nombre de programa y código de descuento variables | `English` | `En revisión` |

La interfaz trunca visualmente los nombres de las plantillas de interés, carrito y descuento; la tabla conserva las identidades previstas en el diseño. `att1_compra_fallida_01` sí aparece completo. Antes de configurar el runtime deberán obtenerse por readback autorizado los nombres completos y estados efectivos de Meta.

## Esquema acordado de la plantilla de descuento

La plantilla de descuento es reutilizable para los productos de ATT1 y no queda acoplada a `Alimenta Tu Tiroides`:

1. `{{1}}`: nombre del programa;
2. `{{2}}`: código de descuento.

El porcentaje permanece en 10 %, sin vencimiento, urgencia ni escasez, y la plantilla sólo es elegible después de al menos una respuesta inbound posterior al primer contacto autorizado.

## Observación pendiente

La plantilla de descuento figura con idioma visible `English`, mientras las otras tres figuran como `Spanish (MEX)` y el contrato candidato de ATT1 espera `es_MX`. Esta diferencia debe reconciliarse en Meta antes de sincronizar la plantilla o habilitar cualquier sender. El registro no presume que el body haya sido clasificado con el idioma correcto ni que el estado `En revisión` termine en aprobación.

## Gate siguiente

Antes de cualquier activación se requiere:

- readback autorizado de Meta con nombre completo, idioma, categoría, componentes, variables y estado de cada plantilla;
- aprobación efectiva de la plantilla de descuento o corrección/reenvío si Meta rechaza el idioma;
- sincronización y verificación en el inbox WABA de Chatwoot;
- actualización explícita del mapeo técnico de descuento a `{{1}} = programa` y `{{2}} = cupón`;
- conservación de todos los gates de consentimiento, compra, opt-out, takeover, presupuesto y efecto final hacia Meta.
