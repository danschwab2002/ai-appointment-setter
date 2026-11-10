# La campaña por la landing que vio la persona

- **Estado:** escrito al construir E-321, el 2026-11-03, antes de cualquier merge o despliegue. Describe la interfaz; no dice que nada esté desplegado ni activado. Va detrás de un flag apagado por defecto.
- **Contrato:** `campaign-entry-v1`, versión `1.0.0`.
- **Diseño:** `documentacion/2026-11-03-e319-diseno-campanas.md` (E-319, fuera del repo), adenda del 03/11 (la decisión de Dan sobre el carrito y D8).
- **Depende de:** `segment-knowledge-v1` (PR-A, E-311: `[[agente.segmentos]]` y la vigencia de cada segmento) y `segment-labels-v1` (E-316: el plan que pone la etiqueta de un segmento antes del primer envío de un flujo nativo, `NativeSegmentLabelPlan`).
- **Migración:** `20261103001700_flow_engine_native_campaign_origin_v1.sql` (una lectura nueva, solo de `service_role`).

## Para qué

Las landings de una campaña (Black November de ATT1) son otras que las de siempre. Dan, el 2026-11-03: el carrito de una campaña se identifica **por la landing que vio la persona**, no por un cupón (el carrito de Hotmart trae `offer.code` y no trae cupón, `tests/fixtures/hotmart_cart_abandonment_rejected_v1.json`). Medido en ATT1 del 04/10 al 03/11: el 87,4 % de los carritos (624 de 714) llegan cubiertos por una intención del formulario en las 24 h previas, que ya trae su landing.

Cuando la persona deja el formulario en una landing de la campaña, la intención guarda su `landing_ref`. Cuando después llega su carrito, la admisión portable lo correlaciona con esa intención y el recorrido nativo del carrito nace con ella (`flow_engine_native_runs.audience_purchase_intent_id`). Con este contrato, si esa landing es de una campaña vigente, **el motor le pone a la conversación la etiqueta del segmento de la campaña antes del primer envío del carrito**. Cuando la persona contesta, el webhook ya trae la etiqueta y el agente responde con el conocimiento de la campaña desde el primer turno.

La oferta propia de la campaña es el respaldo: si la campaña declara `ofertas` y el evento es de una de ellas, también la identifica, aunque no haya formulario (el 12,6 % restante). La landing manda sobre la oferta.

## 1. La declaración: `[[campanas]]` del manifiesto

```toml
[[agente.segmentos]]
clave = "black-november-2026"
etiqueta = "black-november"
conocimiento = "conocimiento/segmentos/black-november-2026-v1.toml"   # la vigencia va aca
prioridad = 10
macro_etiqueta = 41                    # obligatorio para una campaña

[[campanas]]
segmento = "black-november-2026"       # la clave de un [[agente.segmentos]] con macro
landings = ["bn-a", "bn-b"]            # landing_id de ofertas declaradas en [hotmart] o [[productos]]
ofertas = ["bnoffer1"]                 # opcional: los códigos de las ofertas propias de la campaña
```

- `segmento`: la clave de un segmento con `macro_etiqueta`. La campaña corre por ese segmento: su etiqueta, su conocimiento y su vigencia (`[vigencia]` del archivo del segmento, `hasta` exclusivo).
- `landings`: `landing_id` de ofertas declaradas (cada una de una sola oferta). La URL de cada landing ya va con su oferta en `[[hotmart.ofertas]]` (`url`), así que la campaña la nombra por su `landing_id`, que es lo que guarda la intención.
- `ofertas`: códigos de ofertas declaradas. Opcional.
- Al menos una landing o una oferta. Una landing, una oferta o un segmento no van en dos campañas. Cualquier error: el manifiesto no carga.

## 2. El flag

`FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED`, apagado por defecto y portable (capacidad `flow_engine_campaign_entry_enabled`). Prendido, el bridge no arranca sin `FLOW_ENGINE_ENABLED`, sin `AGENT_SEGMENTS_ENABLED` (la vigencia sale de ahí), sin el cliente de control de Chatwoot o sin `[[campanas]]`, ni si el macro de una campaña es el de la pausa, la reanudación, la baja o el de `cliente`. No pide `FLOW_ENGINE_SEGMENT_LABELS_ENABLED`: los flujos no tienen que nombrar la etiqueta.

Apagado, nada cambia: el despacho no lee nada nuevo y el plan es el de E-316 (o ninguno).

## 3. El despacho

En cada envío de un recorrido nativo, el plan de la etiqueta (`CampaignEntryLabels.plan`, en el lugar de `NativeSegmentLabels`, sin cambiar el despachador):

1. Si el flujo tiene `etiquetar` de un segmento (E-316, con su flag), manda ese: una sola etiqueta por camino.
2. Si no, lee `flow_engine_native_campaign_origin_v1(p_action_id)`: `{disparador, oferta, landing}`.
3. Solo con los disparadores que nacen de una intención: `carrito`, `pago_fallido`, `intencion`. La compra no (la persona ya es cliente).
4. Busca la campaña **vigente** de la landing; si no hay, la de la oferta. Una campaña por empezar, vencida o sin vigencia conocida no cuenta. Sin corte por horario.
5. Con campaña, el plan de E-316 (`NativeSegmentLabelPlan`): el macro del segmento antes de la plantilla, en la conversación que eligió el remitente; con el envío aceptado, un reintento si no salió. Falla blanda: una etiqueta que no sale, o una lectura que falla, no frena el envío.

Log: `campaign_entry_matched action_id=… segment=… label=… by=landing|oferta value=… trigger=…`, `campaign_entry_origin_unavailable action_id=… error=<tipo>`, y los de E-316 (`flow_engine_native_segment_label when=before_send|after_send`). Sin teléfono, mail ni nombre.

## 4. La lectura de la base

`public.flow_engine_native_campaign_origin_v1(p_action_id uuid) returns jsonb`, `stable`, `security definer`, solo `service_role`.

| Campo | De dónde sale |
|---|---|
| `disparador` | `flow_engine_native_runs.trigger_event` |
| `oferta` | `flow_engine_native_runs.offer_code` (la oferta del evento) |
| `landing` | `purchase_intents.landing_ref` de `audience_purchase_intent_id`, solo si la intención es real (`provider_observed`); si no, `null` |

Para una acción que no es de un recorrido nativo, `null`. No devuelve mail, teléfono, nombre ni payload. La prueba es `tests/sql/followup_engine/validate_flow_engine_native_campaign_origin.mjs` (la cadena entera en PGlite, con el formulario y el carrito capturado por las admisiones portables), que escribe el fixture del bridge `tests/fixtures/flow_engine/native_campaign_origin_rows.json`.

## 4b. Las campañas del Automation Expert (E-411, `AGENT_CAMPAIGN_LABEL_PATHS_ENABLED`)

Con `AGENT_CAMPAIGN_LABEL_PATHS_ENABLED` (apagado; exige `AGENT_CAMPAIGNS_ENABLED`) y `FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED`, la lista de campañas del despacho se lee en cada envío: primero las `[[campanas]]` del manifiesto y después las campañas del AE servidas que traen `oferta.codigo_oferta` (E-410).

- **Cómo se identifica** una campaña del AE: por su `codigo_oferta` (la oferta del recorrido) y por la landing de esa oferta en el manifiesto (el `landing_id` de la `[[hotmart.ofertas]]` con ese código, que es de una sola oferta). Sin `codigo_oferta`, la campaña no entra por este camino (D4 de E-411).
- **El manifiesto gana** (D3 de E-411): `match_campaign` no cambia y la lista llega con el manifiesto primero. Al crear la campaña, `campaign_offer_entry_taken` bloquea una oferta (o su landing) que ya es de una `[[campanas]]`; si igual pasa en vivo, la campaña se sigue sirviendo (bloque y palabra clave) sin la entrada por oferta (`agent_campaign_entry_skipped key=… reason=offer_taken`).
- **El arranque** no exige `[[campanas]]`. **Sin campañas** en la lista, el plan no lee el origen del recorrido: no hay lectura nueva.
- **No cambia la plantilla:** la etiqueta se pone en el `before_send` del envío nativo. A1, A2 y Z salen con su descuento salvo los frenos de `segment-knowledge-v1`, que deciden por las etiquetas y no frenan el carrito nativo (ver «Una campaña sin cupón que frena igual»).
- **El carrito de una oferta que la instancia no declara** no llega al motor (`pilot_offer_mismatch`): primero se declara la oferta (`add_instance_offers.py`).

## Lo que no hace

- No sirve para el carrito legado (no nativo): el plan de etiqueta es del motor nativo.
- No mira el primer contacto portable (no nativo) ni la conversación que abre la persona por inbound: ahí entran la palabra clave y la pregunta de entrada (E-317) y la herencia (E-316).
- No etiqueta por país ni por precio.
- No cuenta ventas por campaña: eso va por `offer.code` cuando la campaña tiene oferta propia (E-319 §5).

## Requisito de operación

El formulario de cada landing de la campaña tiene que llegar al bridge (adaptador de GHL, o el orgánico) y la landing tiene que estar declarada en la instancia como una oferta (`[[hotmart.ofertas]]` con su `landing_id` y su `url`), como pasó con la landing B el 2026-10-07. Una landing que no llega al bridge no deja intención, y su carrito solo se puede identificar por una oferta propia.
