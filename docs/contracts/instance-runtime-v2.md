# Contrato: runtime de una instancia con manifiesto v2

- **Fecha:** 2026-09-28
- **Diseño:** [setter-producto-instalable-v1.md](../design/setter-producto-instalable-v1.md) §4.3 y §5, [ADR-0021](../decisions/0021-setter-producto-instalable.md)
- **Manifiesto:** [referencia-manifiesto.md](../referencia-manifiesto.md) · **Conocimiento:** [commercial-knowledge-v1.md](commercial-knowledge-v1.md)
- **Reemplaza, para una instancia con manifiesto v2:** la carga del binding de [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md). El binding durable en la base y su readiness siguen igual.

## Cómo lo lee el bridge

| Variable del servicio | Qué hace |
|---|---|
| `INSTANCE_MANIFEST_PATH` | Ruta al `instancia.toml` montado en el contenedor. El binding de la instancia sale de ahí. Es excluyente con `COMMERCIAL_ALLY_CONFIG_PATH` (el binding v1 en JSON) |
| `COMMERCIAL_KNOWLEDGE_ENABLED` | `true` carga el conocimiento de `agente.conocimiento`, relativo a la carpeta del manifiesto. Tiene que estar aprobado, o el bridge no arranca. Viaja como mensaje `system` en cada pedido a Hermes |

Con manifiesto, el bridge además exige:

- `HERMES_MODEL_NAME` igual a `agente.modelo`;
- que cada flag del runtime tenga su flujo declarado en `true`: el manifiesto es el techo de lo que el runtime puede hacer;
- respuestas automáticas solo con el conocimiento cargado;
- con el flujo `carrito` o `pago_fallido` en `true` y la salida por WABA, que `WABA_FIRST_TOUCH_TEMPLATE_NAME`, `WABA_PAYMENT_FAILURE_TEMPLATE_NAME` y `WABA_TEMPLATE_LANGUAGE` nombren la plantilla de ese flujo en `[plantillas]`. Las variables del cuerpo de esa plantilla salen de su `parametros` ([referencia-manifiesto.md](../referencia-manifiesto.md#plantillas)).
- con `META_FINAL_EFFECT_ENABLED` y `DURABLE_OUTBOUND_ENABLED` en `true`, el modo directo del dispatcher (`DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED=true`): la salida manda el cuerpo aprobado del catálogo y no un borrador de Hermes ([approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md)).

| Flag del runtime | Flujo que lo habilita |
|---|---|
| `CHATWOOT_AUTOMATED_REPLIES_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`, `PAYMENT_LINK_ENABLED` | `inbound` |
| `PORTABLE_HOTMART_RECOVERY_ENABLED` | `carrito` |
| `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED` | `pago_fallido` |
| `CONVERSATION_REACTIVATION_ENABLED` | `reactivacion` |
| `CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED` | `descuento` |
| `LEAD_PRECHECKOUT_ENABLED` | el evento `intencion` en `eventos` |
| `GHL_PRECHECKOUT_ADAPTER_ENABLED` | el evento `intencion` en `eventos` y `[adaptadores.ghl]` con al menos un formulario |
| `META_FINAL_EFFECT_ENABLED` | algún flujo que manda plantillas: `precheckout`, `carrito`, `pago_fallido`, `reactivacion` o `descuento` |

La guarda de medicación usa `guardas.terminos_sensibles` y `guardas.acciones_sensibles` del manifiesto en vez de la lista por defecto. `/ready` informa `instance_ally`, `instance_product_version` y `commercial_knowledge` (`v<versión>:<sha256 del bloque>`).

`GHL_PRECHECKOUT_ADAPTER_ENABLED` solo existe con manifiesto: prendido sin él, el bridge no arranca. Además exige `GHL_PRECHECKOUT_ADAPTER_TOKEN` de 32 caracteres o más, distinto de `LEAD_PRECHECKOUT_SECRET` y de `CHATWOOT_WEBHOOK_SECRET`, no arranca con `[flujos].precheckout` en `true` (el token es la única barrera; ver *Riesgos* del contrato del adaptador), y suma a `/ready` `ghl_precheckout_adapter: enabled:<n>-forms`. Apagado, nada de eso se evalúa. Contrato: [ghl-precheckout-adapter-v1.md](ghl-precheckout-adapter-v1.md).

El agente de una instancia usa el SOUL común del producto (`profiles/agente-comercial-comun/SOUL.md`) con el conocimiento de la instancia encima.

## Qué reemplaza del binding v1

- El candado `tenant_ref == "att1"` sobre `META_FINAL_EFFECT_ENABLED` aplica solo a un binding v1. Con manifiesto v2 el envío a Meta lo habilita un flujo de salida declarado en el repo de la instancia, que es igual de explícito y queda versionado, y para la salida del dispatcher además el modo directo.
- El descuento post-respuesta deja de estar reservado a `tenant_ref == "att1"`: lo habilita el flujo `descuento`.
