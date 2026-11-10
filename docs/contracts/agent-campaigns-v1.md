# Contrato: las campañas del Automation Expert como dato (v1)

- **Estado:** escrito al construir E-355, el 2026-11-08, antes de cualquier merge o despliegue. Describe la interfaz; no dice que nada esté desplegado ni activado.
- **Contrato:** `agent-campaigns-v1`, versión `1.0.0`.
- **Alcance:** opción B de `documentacion/2026-11-08-campanas-por-el-ae-alcance.md` (fuera del repo). La frase que tiene que ser cierta: «Le pedís la campaña al AE, la aprobás en una página y queda andando, sin subir nada al servidor».
- **Migración:** `20261108005000_agent_campaigns_v1.sql`.
- **Código:** `src/bridge/agent_campaigns.py` (la forma, el armado del segmento y el catálogo vivo), `agent_campaign_api.py` (las rutas del AE), `campaign_approval.py` (la página), `campaign_check.py` (el comando del juez), `campaign_inbound_entry.py` (la recarga) y `automation_expert_chatwoot.py` (el macro). La herramienta del agente: plugin `automation-expert-change` 0.6.0.
- **Flag:** `AGENT_CAMPAIGNS_ENABLED`, apagado por defecto. Apagado, no existen las rutas de las campañas, la página de una campaña ni la creación de un macro (`POST .../chatwoot/macros`), `automation-expert-chatwoot-v1` sigue en `1.0.0` y no se lee la base. Lo que queda instalado igual es el profile del AE: la skill y las herramientas. Con el flag apagado `list_campaigns` contesta `campaigns_not_enabled` y la skill frena ahí, antes de crear nada.
- **Lo que cambia aunque el flag esté apagado:** una persona dada de baja no recibe la oferta ni el cupón de una campaña: se le contesta normal, como pide el SOUL común. Vale para la entrada (`CAMPAIGN_INBOUND_ENTRY_ENABLED`) y para el segmento que ya tenía por la etiqueta (`AGENT_SEGMENTS_ENABLED`), sea una campaña de la base o un segmento del manifiesto. Es la conducta de los segmentos desde E-311, corregida acá: antes la etiqueta puesta antes de la baja le seguía abriendo el bloque y el cupón. Solo cambia algo donde haya segmentos prendidos con una etiqueta vigente; el registro de operación dice que ATT1 y Maribel los tienen apagados (ATT1 el 06/11, `segmentos: agente=False entrada=False`), sin verificar en vivo al escribir esto.

## Lo que entra y lo que no

Entra quien llega con la **palabra clave** o responde que sí a la **pregunta de entrada**; el cupón va en el link. Con `AGENT_CAMPAIGN_LABEL_PATHS_ENABLED` (E-411) entra también por el carrito, el pago rechazado y la intención de su oferta (`codigo_oferta`, `campaign-entry-v1` §4b), por la herencia de la etiqueta a otra conversación de la misma persona y por el `etiquetar` de un flujo (`segment-labels-v1` §7b). **No entra:** el envío a una lista y el juez adentro del bridge (va por comando nuestro). No se construyó el §8b de `campaign-inbound-entry-v1`.

## El recorrido

```
borrador ─validar─▶ validada ─publicar (el sí del dueño)─▶ publicada ◀─pausar/reanudar─▶ pausada
   │                  │  ▲                                  │
   └─descartar        │  └ rehacer (preguntas del juez)     └─ termina la vigencia ─▶ terminada
                      └─24 h sin publicar─▶ vencida
```

1. El AE mira primero `list_campaigns` (con `campaigns_not_enabled`, frena sin crear nada), conversa y arma la campaña; la mira con `preview_campaign` (valida y renderiza **sin guardar**). Etiqueta y macro con `create_chatwoot_label` y `create_chatwoot_label_macro` (plan B: los crea el operador y el id va en `macro_etiqueta`).
2. `propose_campaign` guarda el borrador; `validate_campaign` revisa la instancia y que el macro sume exactamente la etiqueta. Sin bloqueos, la base la congela 24 horas con su huella.
3. El **control de calidad** (el juez de `segment_check`, 3 corridas, hasta 15 minutos) lo corre el operador: `python -m bridge.campaign_check <id> --instancia <carpeta> --soul <SOUL.md>`. El resultado queda en la fila. Limpio: la página muestra el botón y el dueño tiene otras 24 horas. Con preguntas: el agente las rehace con el dueño (`propose_campaign` sobre la validada) y se corre otra vez. Con `--criterio frenos` (`campaign_gate.py`, E-364a) frena solo lo que contradice un valor o una promesa escrita: la fila suma `criterio`, `razon`, `campo` y `citas` en cada item que frena, y `avisos` (más `avisos_total`) con lo que no frena. `limpio` pasa a querer decir que nada frena. Sin la opción frena cualquier observación, con dos cambios de E-364a que valen con o sin ella: el juez ve los flujos y plantillas de la campaña (los busca con la etiqueta; puede sumar observaciones) y 2 de 3 corridas válidas sin nada que frene da `sin_veredicto`. `--grabar` deja la captura cruda y `--sin-anotar` no escribe nada en la base.
4. **El sí del dueño**: `Aprobar y publicar` en `/flow-approval/c/{id}`, con la misma sesión de Slack y la misma lista de aprobadores que los flujos. Sin ese sí no se publica; el agente no tiene ninguna herramienta para publicar.
5. Publicada, el bridge la lee con caché corta (30 s, o ya mismo después de tocar el botón).

## El `chequeo_sha`

Cada campaña lleva el suyo, calculado con la misma función que el cargador de archivos (`segment_check.calcular_chequeo_sha`), sobre el contenido, el render del conocimiento base, los segmentos del manifiesto que se pisan y la versión de las reglas. Se recalcula al publicar y en cada recarga. **Un `chequeo_sha` que no calza en una campaña de la base no se sirve, avisa y el bridge sigue** (decisión de Dan del 2026-11-08): un log `agent_campaign_not_served`, una vez por campaña y motivo al canal de alertas si la instancia tiene el conector de Slack, y la página lo dice. Se arregla volviendo a correr el comando del juez (`record_agent_campaign_check_v1` acepta una publicada o pausada para volver a certificar). Con preguntas abiertas sobre una publicada o pausada, la base no pisa su control limpio: contesta `agent_campaign_check_not_clean` y guarda las preguntas en `agent_campaign_events` (`agent_campaign_checked`, hasta 10, recortadas a 300 caracteres); el comando las imprime y sale con 1.

La regla del 06/11 que frena el arranque queda solo para los archivos del manifiesto (`segment-knowledge-v1`).

## Una sola campaña viva por instancia

Un índice único parcial (`status = 'publicada'`) y `approver_publish_agent_campaign_v1` lo hacen cumplir: publicar con otra viva contesta `agent_campaign_live_exists` (la pausada no retiene el lugar). Una viva cuya vigencia ya terminó se marca `terminada`, con su `agent_campaign_finished` en el historial, y no retiene el lugar (al publicar otra o al reanudar). Los precios por país van adentro de la campaña. Corregir una publicada es pausarla y armar la versión siguiente.

## El catálogo vivo

`LiveSegmentCatalog` tiene la misma interfaz que `SegmentCatalog` (`segments`, `select`, `blocks_for`, `readiness`): sus consumidores no cambian. Si la base no contesta, se sirve lo último bueno (o solo el manifiesto, si nunca leyó) y `/ready` lo muestra (`agent_campaigns`: `served=N,not_served=N,refresh=ok|failing:N`). Si el cambio de una campaña llega mientras el modelo piensa la respuesta de un turno, la entrada del turno ignora esa respuesta sin romperlo (`segment_entry_answer_ignored`). Una instancia que lee segmentos del manifiesto sigue igual.

## Las funciones de la base

| Rol | Funciones |
|---|---|
| `automation_expert_change` | `save_agent_campaign_draft_v1`, `record_agent_campaign_validation_v1`, `get_agent_campaign_v1`, `list_agent_campaigns_v1` |
| `flow_change_approver` (con la sesión viva) | `approver_get_agent_campaign_v1`, `approver_publish_agent_campaign_v1`, `approver_pause_agent_campaign_v1`, `approver_resume_agent_campaign_v1`, `approver_discard_agent_campaign_v1` |
| `service_role` | `get_live_agent_campaigns_v1`, `operator_get_agent_campaign_v1`, `record_agent_campaign_check_v1` |

Las tablas (`agent_campaigns`, `agent_campaign_events`) no tienen permisos para la API.

## Las rutas del AE

Bajo `/internal/automation-expert/v1`, con `AUTOMATION_EXPERT_CHANGE_TOKEN`: `POST /campaigns/preview`, `POST /campaigns`, `POST /campaigns/{id}/validate`, `GET /campaigns?estado=abiertas|todas&limit=`, `GET /campaigns/{id}`. Validar exige `SETTER_VERSION` y `GIT_SHA` en la imagen.

## La oferta propia de la campaña (E-410)

- **`oferta.codigo_oferta`**: la oferta de Hotmart de la campaña, si tiene una propia (el `off=` del link de pago de su página). Con `AGENT_CAMPAIGN_OFFER_LINK_ENABLED`, el link del agente en la campaña sale con esa oferta (`segment-knowledge-v1`, «La oferta de la campaña en el link»).
- **Bloqueo `campaign_offer_unknown`** (campo `oferta.codigo_oferta`): el código no es una oferta declarada del producto de la campaña en el manifiesto (`[[hotmart.ofertas]]` del principal, o las de `[[productos]]` si hay `oferta.producto`). La oferta se declara en `instancia.toml` y se siembra en el catálogo de la base con `scripts/add_instance_offers.py` **antes** de armar la campaña.
- **Aviso `campaign_offer_not_verified`**: el bridge no ve si la oferta está activa en Hotmart ni en el catálogo de la base; eso lo dice la reserva al emitir.
- **Bloqueo `campaign_offer_entry_taken`** (E-411, campo `oferta.codigo_oferta`): la oferta, o la landing de esa oferta, ya es de una `[[campanas]]` del manifiesto. Gana el manifiesto.
- **`oferta.frenar_descuento_carrito`** (`true`; `false` no se guarda): la campaña frena el descuento del carrito aunque no traiga cupón (E-410 C8, `segment-knowledge-v1`).
- **Una campaña publicada no se edita**: para sumarle la oferta, se pausa y se arma la versión siguiente con el campo, que pasa otra vez por el juez y la aprobación. `CHECK_RULES_VERSION` no cambió con E-410 (D4): el `chequeo_sha` de una campaña sin el campo es el mismo y sigue sirviéndose.

## Lo que el arranque exige

`AGENT_CAMPAIGNS_ENABLED=true` pide `AGENT_SEGMENTS_ENABLED`, `CAMPAIGN_INBOUND_ENTRY_ENABLED`, `AUTOMATION_EXPERT_CHANGE_ENABLED` y `FLOW_APPROVAL_ENABLED`. Con eso, `CAMPAIGN_INBOUND_ENTRY_ENABLED` puede arrancar con cero segmentos declarados.

## Lo que no se verificó

- Que Chatwoot v4.13.0 acepte `POST /macros` con la clave de control de una instancia real: se leyó su código (ver `chatwoot_v4_13_macro_create_documentacion_20261108.json`). Plan B: lo crea el operador y el id va en `macro_etiqueta`.
- Que el cupón exista en Hotmart: no hay verificación; la página le pide al dueño que lo confirme.
