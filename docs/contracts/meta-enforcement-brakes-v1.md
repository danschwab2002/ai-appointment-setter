# Contrato: los frenos ante Meta (v1)

- **Estado:** escrito al construir F1, el 2026-11-27, antes de cualquier merge o despliegue. Describe la interfaz propuesta; no dice que nada esté desplegado ni activado.
- **Contrato:** `meta-enforcement-brakes-v1`, versión `1.0.0`.
- **Alcance:** F1 del mapa «Frenos ante Meta, calentamiento del masivo, alerta por plantilla y texto literal» del 2026-11-27 (fuera del repo), hallazgos H1 a H6 y decisiones D2 a D6.
- **Migración:** `20261127100000_meta_enforcement_brakes_v1.sql`.
- **Código:** `src/bridge/meta_enforcement.py` (estado, sondeo, avisos, webhook y rutas), la marca del catálogo en `src/bridge/approved_templates.py` (`BRAKE_KEY`), el filtro de `ChatwootClient.with_catalog_filter`, el freno del masivo (`bulk_send_worker.py`), el de los reintentos (`meta_delivery.py`), el del pedido de utilidad (`template_request_api.py`) y el cableado en `src/bridge/app.py`.
- **Flag:** `META_ENFORCEMENT_BRAKES_ENABLED`, apagado por defecto. Apagado no existe ninguna ruta, no se lee la base, ningún envío cambia y `/ready` dice `meta_enforcement: disabled`. Prendido exige el manifiesto, `SUPABASE_SERVICE_ROLE_KEY`, `META_TEMPLATES_ACCESS_TOKEN` y `META_WABA_ID`.

## Las dos entradas (D2)

1. **El sondeo**, cada 15 minutos: `GET /{waba}/message_templates?fields=id,name,language,status,category,correct_category`, la lista entera (si no se lee hasta el final, no se compara). La base compara con lo último visto de cada plantilla. La primera lectura es línea de base: no recategoriza para atrás (D-F1.1).
2. **`/webhooks/meta`**, solo con `META_WEBHOOK_VERIFY_TOKEN` **y** `META_APP_SECRET` (con uno solo, el bridge no arranca):
   - `GET` con `hub.mode=subscribe`, `hub.verify_token` igual al configurado y `hub.challenge` (hasta 256 de `A-Za-z0-9_-`): contesta el challenge en texto. Cualquier otra cosa, `403`.
   - `POST` con `X-Hub-Signature-256: sha256=<HMAC-SHA256 del cuerpo crudo con el secreto de la app>`. Sin firma o con otra: `401` y no se lee nada. Cuerpo de más de 1 MB: `413`.
   - Solo cuenta el `entry` cuyo `id` es `META_WABA_ID`; otro se descarta y se cuenta (`other_account`).
   - `template_category_update`: aviso previo (`correct_category`, `category_update_timestamp`) o cambio hecho (`previous_category`, `new_category`). El idioma `en-US` de la doc se guarda como `en_US`, como el catálogo (D-F1.8).
   - `account_update` con `event: ACCOUNT_RESTRICTION`: `violation_info.violation_type` y los `restriction_info[].restriction_type`. Otros `event` se ignoran.
   - Se procesa en línea con un tope de 8 s: si la base no contesta, `503` y Meta reintenta (D-F1.7). Lo que la base rechaza por forma, `200` con `rejected`.

Hoy la app de Meta tiene el callback de Chatwoot y en 400 h no llegó ningún `template_category_update` ni `account_update` (H2): para que lleguen hace falta una app propia suscrita a esos campos con el callback en esta ruta. Los fixtures de las pruebas son los ejemplos de la documentación de Meta, **no capturados**.

## Qué decide la base

| Cambio | Efecto |
|---|---|
| UTILITY → MARKETING o AUTHENTICATION, hecho | la plantilla queda **retirada**; aviso `recategorizacion_hecha`; suma al contador |
| aviso previo (`correct_category` distinto) | solo aviso `recategorizacion_previa` (una vez en 7 días) |
| MARKETING → UTILITY | nada |
| solicitud del AE pedida UTILITY, aprobada como otra categoría | suma al contador, no retira (nunca fue de utilidad) |
| solicitud pedida UTILITY aprobada como UTILITY | el contador vuelve a 0 |
| `ACCOUNT_RESTRICTION` (salvo los dos de abajo) | la instancia queda **congelada**; aviso `congelamiento` |
| `UTILITY_TEMPLATE_ABUSE_UNBAN`, `UTILITY_TEMPLATE_ABUSE_RATE_LIMIT_RECOVERY` | solo aviso `restriccion_levantada_por_meta`; no descongela |

El mismo cambio en 7 días (o con la plantilla todavía retirada) no se registra dos veces; el mismo evento de cuenta con la misma hora es `repetido`. Al llegar el contador a 2 sale una vez el aviso `contador_dos`. La advertencia (`UTILITY_TEMPLATE_ABUSE` sin `restriction_info`) también congela, del lado prudente (D-F1.5).

## Dónde frena

- **Plantilla retirada:** ningún envío que inicia el bridge la manda. Los envíos leen el catálogo con un cliente de Chatwoot filtrado: la plantilla llega con `status: BRIDGE_BRAKED` y `bridge_meta_brake: template_recategorized`, `parse_approved_template` la rechaza con ese motivo y los demás lectores del catálogo, como a una no aprobada. Vale para el legado, el motor nativo (también con `permiso: formulario`, que acepta MARKETING: H6), los seguimientos, la reactivación y el primer contacto. El masivo la frena antes de arrancar y antes de cada lote (`frenado` con ese motivo). Los reintentos del 131049 y de la cuenta no la reintentan. El link del agente en una conversación abierta cae al texto.
- **Instancia congelada:** lo mismo para **todas** las plantillas, con el motivo `meta_account_restricted`, en todo lo que inicia el bridge. Las respuestas del agente dentro de una conversación abierta (y el link en el globo) siguen.
- **Último control:** el cliente filtrado también se niega a mandar con `send_first_message`, `send_followup_message` y las plantillas del AgentBot una plantilla frenada (`ChatwootProtocolError("meta_enforcement:<motivo>")`), para los envíos que no leen el catálogo.
- **Contador en 2 o más:** `POST /template-requests` y su validación con `categoria: UTILITY` contestan `409 meta_recategorization_streak`; MARKETING sigue.
- Sin ninguna lectura buena del estado (la base caída desde el arranque) no hay freno conocido y los envíos siguen como hoy; después, queda lo último bueno (D-F1.6). Refresco cada 60 s, y en el acto tras un webhook o una acción a mano.

## Las acciones a mano (D-F1.9)

Prefijo `/internal/automation-expert/v1`, `Authorization: Bearer <AUTOMATION_EXPERT_CHANGE_TOKEN>`; quien pide tiene que estar en `FLOW_APPROVAL_SLACK_APPROVERS` (`403 not_an_approver`). Existen solo con `AUTOMATION_EXPERT_CHANGE_ENABLED` y `FLOW_APPROVAL_ENABLED`; si no, `/ready` dice `manual=sin_aprobadores`.

- `GET /meta-enforcement` → `data.congelada`, `data.congelamiento`, `data.retiradas`, `data.recategorizaciones_seguidas`, `data.pedidos_de_utilidad_frenados`, `data.ultimo_sondeo`, `data.este_bridge`.
- `POST /meta-enforcement` con `{accion, motivo, requested_by: {slack_user}, plantilla?}`:
  - `congelar` (por el banner o el mail de Meta, mientras no llegue el webhook), `descongelar`, `liberar_contador`;
  - `liberar_plantilla` con `plantilla: {nombre, idioma}` (por ejemplo, Meta aprobó la apelación). Una que no está retirada: `404 meta_enforcement_template_not_retired`.
  - Repetir contesta `replayed: true` sin cambiar nada. La base guarda quién y por qué.

## Los avisos (D6, D-F1.11)

Texto fijo escrito por `notice_text`, por el bot del Automation Expert (`META_TEMPLATES_SLACK_BOT_TOKEN`) en el canal de la aliada (`[slack].canal`), el mecanismo de `template_status.py`. Sin token o sin canal esperan en la base. `not_in_channel` y los rechazos del token esperan sin gastar intento; otro rechazo reintenta hasta 5; uno que pudo haber llegado queda `incierto` y no se repite.

## `/ready`

`meta_enforcement`: `disabled` con el flag apagado; si no, `frozen=yes|no,retired=N,streak=N,last_poll=<iso|never>,refresh=ok|failing:N|never_read,webhook=on|sin_secreto,manual=on|sin_aprobadores`. Con el flag, además `meta_enforcement_worker`, `meta_enforcement_last_error`, `meta_enforcement_last_poll` y `meta_enforcement_notices`. Nunca 503: un freno no es una falla. Sin nombres de plantillas ni datos de nadie.

## La base

Siete tablas con RLS y sin permisos para la API (`meta_template_category_seen`, `meta_template_category_events`, `meta_template_retirements`, `meta_account_events`, `meta_account_freezes`, `meta_recategorization_streak_releases`, `meta_enforcement_notices`) y siete funciones de `service_role`: `record_meta_template_categories_v1`, `record_meta_template_category_webhook_v1`, `record_meta_account_event_v1`, `get_meta_enforcement_state_v1`, `act_on_meta_enforcement_v1`, `claim_meta_enforcement_notices_v1`, `finish_meta_enforcement_notice_v1`. Nada se borra; una retirada se libera una vez y un congelamiento se levanta una vez.
