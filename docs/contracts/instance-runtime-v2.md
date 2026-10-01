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
- con el flujo `carrito`, `pago_fallido` o `precheckout` en `true` y la salida por WABA, que `WABA_FIRST_TOUCH_TEMPLATE_NAME`, `WABA_PAYMENT_FAILURE_TEMPLATE_NAME`, `WABA_PRECHECKOUT_TEMPLATE_NAME` y `WABA_TEMPLATE_LANGUAGE` nombren la plantilla de ese flujo en `[plantillas]`. Las variables del cuerpo de esa plantilla salen de su `parametros` ([referencia-manifiesto.md](../referencia-manifiesto.md#plantillas)).
- con `META_FINAL_EFFECT_ENABLED` y `DURABLE_OUTBOUND_ENABLED` en `true`, el modo directo del dispatcher (`DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED=true`): la salida manda el cuerpo aprobado del catálogo y no un borrador de Hermes ([approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md)).

| Flag del runtime | Flujo que lo habilita |
|---|---|
| `CHATWOOT_AUTOMATED_REPLIES_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`, `PAYMENT_LINK_ENABLED` | `inbound` |
| `PORTABLE_HOTMART_RECOVERY_ENABLED` | `carrito` |
| `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED` | `pago_fallido` |
| `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED` | `precheckout` |
| `CONVERSATION_REACTIVATION_ENABLED` | `reactivacion` |
| `CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED` | `descuento` |
| `LEAD_PRECHECKOUT_ENABLED` | el evento `intencion` en `eventos` |
| `GHL_PRECHECKOUT_ADAPTER_ENABLED` | el evento `intencion` en `eventos` y `[adaptadores.ghl]` con al menos un formulario |
| `META_FINAL_EFFECT_ENABLED` | algún flujo que manda plantillas: `precheckout`, `carrito`, `pago_fallido`, `reactivacion` o `descuento` |

La guarda de medicación usa `guardas.terminos_sensibles` y `guardas.acciones_sensibles` del manifiesto en vez de la lista por defecto. `/ready` informa `instance_ally`, `instance_product_version` y `commercial_knowledge` (`v<versión>:<sha256 del bloque>`).

`GHL_PRECHECKOUT_ADAPTER_ENABLED` solo existe con manifiesto: prendido sin él, el bridge no arranca. Además exige `GHL_PRECHECKOUT_ADAPTER_TOKEN` de 32 caracteres o más, distinto de todo otro secreto y valor de texto de la configuración del bridge, y suma a `/ready` `ghl_precheckout_adapter: enabled:<n>-forms`. Apagado, nada de eso se evalúa. Contrato: [ghl-precheckout-adapter-v1.md](ghl-precheckout-adapter-v1.md).

**El riesgo del adaptador de GHL cuelga del manifiesto, no del flag.** Con `[adaptadores.ghl]` en el manifiesto y sin la aceptación escrita del riesgo (`riesgo_aceptado_por`, `riesgo_aceptado_el` y `riesgo_contrato` en esa sección), el bridge no arranca con `flujos.precheckout` ni `flujos.pago_fallido` en `true`, esté o no prendido `GHL_PRECHECKOUT_ADAPTER_ENABLED`. Con `LANCEMOS_PILOT_BOUNDARY_ENABLED` tampoco arranca si el scope del piloto es de audiencia `consented_intent` o `consented_intent_in_cohort`, o si ese modo no se puede leer de la base (migración `20261001000300`); `/ready` repite la lectura y responde `503` con `ghl_adapter_risk_not_accepted` o `ghl_adapter_risk_audience_unavailable`. `/ready` suma `ghl_adapter_risk` (`accepted:<fecha>:<contrato>`, `not_accepted` o `no_adapter_section`) cuando aplica, sin el nombre de quien acepta. La matriz completa está en el contrato del adaptador, sección *La aceptación escrita del riesgo*.

**El primer contacto tras el formulario.** `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED` solo existe con manifiesto y exige, además del flujo `precheckout`: una entrada del formulario (`LEAD_PRECHECKOUT_ENABLED` o `GHL_PRECHECKOUT_ADAPTER_ENABLED`), `LANCEMOS_PILOT_BOUNDARY_ENABLED`, `DURABLE_DISPATCHER_ENABLED`, `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`, su scope del piloto (`LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY` y `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION`, distinto de `LANCEMOS_PILOT_SCOPE_KEY`), `WABA_PRECHECKOUT_TEMPLATE_NAME` igual a `plantillas.precheckout.nombre`, `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED` con `HOTMART_HOTTOK`, y `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`. Suma a `/ready` `portable_precheckout_first_contact` con el estado de ese scope, y responde `503` si el scope no está publicado para este flujo. `WABA_PRECHECKOUT_TEMPLATE_NAME` sin manifiesto impide arrancar. Contrato: [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md).

**Teléfonos.** Con manifiesto, el bridge trata como el mismo móvil las dos formas con que llega un número de México (`52` + 10 dígitos y `521` + 10) o de Argentina (`54` + 10 y `549` + 10): al resolver un evento de Hotmart, al identificar a quien escribe, al proyectar el opt-out y la derivación a Chatwoot y al elegir el destinatario de un primer contacto. Nada se reescribe al guardar. Sin manifiesto las comparaciones siguen siendo exactas. Para identificar a quien escribe, el bridge lee `channel_identities` con un `GET` que lleva las dos formas en la query, uno por cada mensaje entrante de un móvil de México o Argentina: el proxy delante de PostgREST no tiene que registrar la query ([instalar.md](../instalar.md#6-la-base-de-la-instancia), paso 6). Detalle: [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md#equivalencia-de-teléfonos-de-whatsapp-2026-10-01-migración-20261001000100).

**La respuesta a una plantilla de salida.** Con el bridge 1.3.0, la admisión entrante rechaza la conversación que abrió una plantilla del dispatcher (carrito, pago fallido o primer contacto): queda en `automation_status = 'enabled'` y la admisión solo toma una `draft_only` (`22000 inbound_canonical_conversation_conflict`), así que el agente no contesta ahí (H7). Lo resuelve 1.3.1 (migraciones `20261001000400` y `20261001000500`, sin publicar): con manifiesto, el bridge admite por `admit_portable_inbound_commercial_case_v1` en las tres llamadas del entrante (el mensaje, la reautorización y la readmisión después de reanudar). Esa RPC adopta la conversación de una plantilla del piloto aceptada por Chatwoot (la pasa a `draft_only` y deja el evento `inbound_adopted_template_conversation`) y delega en la v2. En una conversación adoptada, la marca de atención, la reanudación, la reactivación y el seguimiento con cupón buscan el `inbound_sales`. Sin manifiesto el bridge sigue llamando a `admit_inbound_commercial_case_v2` con los mismos argumentos. Contrato: [inbound-commercial-case-admission-v1.md](inbound-commercial-case-admission-v1.md) §3.1.

- **No se adopta** un contacto dado de baja, `blocked`, `restricted` o `do_not_contact` que responde a una plantilla vieja: lo atiende una persona en Chatwoot (decisión de Dan del 2026-10-01). Tampoco se adopta con un paso de recuperación pendiente ni con dos identidades del mismo móvil.
- **El respaldo para la baja sigue**, para lo que la adopción no toma. Si la admisión falla, el bridge mira igual el historial de la conversación y registra la baja (o reconcilia el stop de quien ya se había dado de baja). Si el rechazo es determinista (un `22xxx`, o los conflictos `inbound_external_conversation_owned_by_another_identity` e `inbound_external_conversation_ownership_ambiguous`), deja `chatwoot_cut_b_admission_rejected conversation=<id> reason=<código>` y el trabajo termina como `failed` tras los intentos acotados. Sin manifiesto todo fallo de la admisión se sigue reintentando, como antes.
- **Apagar el piloto no corta las respuestas.** Pausar o desarmar el scope del piloto frena los envíos, no la respuesta a una plantilla que ya salió. Para que el agente no conteste, se apaga `CHATWOOT_CUT_B_AGENT_ENABLED` (decisión de Dan del 2026-10-01).
- **Orden de despliegue:** las dos migraciones antes que el bridge 1.3.1. Sin la `000400`, PostgREST responde `404` (`PGRST202`) y cada entrante se reintenta sin tope hasta que se aplique.

Detalle por botón y límites: [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md#la-respuesta-a-la-plantilla-bridge-131-sin-publicar).

El agente de una instancia usa el SOUL común del producto (`profiles/agente-comercial-comun/SOUL.md`) con el conocimiento de la instancia encima.

## Qué reemplaza del binding v1

- El candado `tenant_ref == "att1"` sobre `META_FINAL_EFFECT_ENABLED` aplica solo a un binding v1. Con manifiesto v2 el envío a Meta lo habilita un flujo de salida declarado en el repo de la instancia, que es igual de explícito y queda versionado, y para la salida del dispatcher además el modo directo.
- El descuento post-respuesta deja de estar reservado a `tenant_ref == "att1"`: lo habilita el flujo `descuento`.
