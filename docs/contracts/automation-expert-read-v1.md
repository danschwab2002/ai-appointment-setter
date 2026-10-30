# Contrato: la lectura del Automation Expert (v1)

- **Estado:** aceptado como contrato conceptual el 2026-10-15. La versión 1.1.0 suma lo que fijó la implementación de los lectores (la rama `feat/claude-automation-expert-read-v1`, apagada por defecto): ver «Lo que fijó la implementación (1.1.0)». La 1.2.0, del 2026-10-25, la construye el PR 5 del hito 4 (migración `20261025000100`), escrita antes del merge: ver «Lo que suma el motor nativo (1.2.0)». La 1.3.0, del 2026-10-30, la construye el PR del Automation Expert del mapa M16 (E-208), escrita antes del merge y sin migración propia: ver «Quién ejecuta cada flujo de negocio (1.3.0)». El contrato no describe nada desplegado ni activado. Lo que dice de ATT1 es lo medido, con su fecha y su evidencia.
- **Fecha:** 2026-10-15
- **ADR:** [ADR-0025](../decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md) §4 y §7, y [ADR-0017](../decisions/0017-central-slack-operations-connector.md)
- **Contrato:** `automation-expert-read-v1`, versión `1.3.0`
- **Alcance:** lo que el Automation Expert de la 1a le pregunta al bridge y lo que recibe. Son dos preguntas: qué flujos hay, y qué le pasó o le va a pasar a un lead, y por qué. Fija las rutas, la clave, la marca de la instancia, la lista cerrada de motivos, los huecos, las tres funciones de la base, la lectura de Chatwoot y las reglas de los datos personales. Proponer o cambiar un flujo, el dibujo de un flujo y la app de Slack van en sus propias piezas (ver «Fuera de este contrato»).
- **Etapas:** las del roadmap macro del producto, como en [flow-definition-v1.md](flow-definition-v1.md). La 1a es el Automation Expert que lee y explica los flujos; la 1b, los flujos como datos, con su motor y su ciclo de versiones; y la 1c, los flujos nuevos. La Etapa 0 bis alinea a ATT1 y al producto con el ADR-0025 antes de llevar la 1a a ATT1. Incluye la declaración de Hermes con su instalador y su chequeo, el Hermes propio de ATT1 con el agente comercial, cubierto por el respaldo y las alertas, ese chequeo en verde contra ATT1 y lo viejo de ATT1 bajado.
- **Citas:** las citas de código son de `origin/main` en `1b83191` (1.8.0). Las migraciones de `supabase/migrations/` se citan por su nombre. `stack.yaml`, `despliegue/traefik/setter-att1.yaml`, `despliegue/gateway/nginx.conf`, `despliegue/respaldo/README.md`, `despliegue/alertas/README.md`, `instancia.toml`, `generar.py`, y el `.env.example` y el `README.md` que se dicen «de la instancia», son de la instancia ATT1 (`setter-instancia-att1`), en `b1cdefe`. Las de Hermes (`plugins/`, `gateway/`, `agent/`, `hermes_cli/` y `toolsets.py`) son del repo público `NousResearch/hermes-agent` en `29112be`, Hermes 0.21.0 (la imagen `v2026.8.31`). En este contrato, «el esquema» es [flow-definition-v1.md](flow-definition-v1.md), y sus secciones se citan por su título. «Hoy» es ese código, la instancia ATT1 y lo medido en att1-db y en Chatwoot al 2026-10-15.
- **Relacionados:** [flow-definition-v1.md](flow-definition-v1.md), [operator-correlation-review-v1.md](operator-correlation-review-v1.md) (el molde), [lead-first-name-inference-v1.md](lead-first-name-inference-v1.md), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md), [conversation-followup-discount-v1.md](conversation-followup-discount-v1.md), [conversation-reactivation-v1.md](conversation-reactivation-v1.md), [executable-human-handoff-v1.md](executable-human-handoff-v1.md), [agent-prompt-provenance-v1.md](agent-prompt-provenance-v1.md), [slack-operations-connector-v1.md](slack-operations-connector-v1.md) y el diseño [automation-expert-configuration-experience.md](../design/automation-expert-configuration-experience.md).

## Para qué

El Automation Expert vive en Hermes, en el profile `automation-expert` del Hermes propio de la instancia, como el de ATT1 (ADR-0025 §4). No lee la base: le pregunta al bridge a través de un plugin. Este contrato es el acuerdo entre los dos. Dice qué puede preguntar, qué recibe exactamente y con qué reglas. Con él se construyen los dos lectores del bridge, el plugin y los casos de prueba de la 1a. Está pensado para que nada de eso se rehaga en la 1b. Cuando los flujos pasen a ser definiciones guardadas, cambia solo de dónde sale la respuesta, y la forma sigue igual.

## En corto

- Contesta dos preguntas: «¿qué flujos hay?» y «¿qué le pasó o le va a pasar a este lead, y por qué?».
- Solo lee. Son rutas nuevas del bridge, apagadas por defecto con un flag, tres funciones de solo lectura en la base y una lectura de los mensajes de Chatwoot con el cliente que el bridge ya tiene.
- Usa una clave propia, distinta de todos los secretos que carga el bridge.
- La instancia la fija el bridge con su manifiesto: la bandeja de Chatwoot, el producto de Hotmart, el aliado y el embudo. Nunca el tenant solo, que es el mismo para varias instancias. Lo que no se ata a la instancia queda afuera.
- Los flujos llegan en el formato del esquema, con versión, el estado de su control, la prioridad y lo que pasó en la semana. Cada valor dice de dónde salió. El agente que contesta va aparte, porque no es un flujo.
- La historia junta todo lo de una persona en una línea de tiempo, con dos fuentes: la base de la instancia y los mensajes de Chatwoot, sin su contenido. Cada momento dice cuándo pasó, en qué flujo, qué pasó y por qué, con un motivo de la lista cerrada de este contrato. También dice si el dato está guardado o es deducido.
- A un lead se lo busca por número de conversación, por link de pago o por nombre, nunca por teléfono ni mail. Se lo nombra con su nombre de pila y el número de conversación.
- Lo que el sistema no guarda llega como un hueco con un código fijo, y el agente no lo rellena. Si Chatwoot no contesta, la historia sale igual, con la parte de la base y su hueco.
- Lo que devuelve el bridge vive solo en la sesión del agente (decisión de Dan del 2026-10-15). Las rutas no se publican en internet, y adentro del servidor el token es la única barrera.

## Las dos preguntas

Las dos respuestas vienen en el mismo sobre:

```json
{
  "contract": "automation-expert-read-v1",
  "contract_version": "1.1.0",
  "instance": {"tenant_ref": "…", "ally_ref": "…", "funnel_ref": "…",
               "chatwoot_inbox_id": "…", "hotmart_product_id": "…", "product_version": "…"},
  "source": "projection",
  "generated_at": "<hora UTC del bridge>",
  "database_at": "<now() de la función>",
  "bridge_git_sha": "…",
  "gaps": [{"code": "…", "scope": "flow:<flow_key> | lead | instance", "what": "…", "why": "…"}],
  "data": {},
  "page": {"limit": 50, "next_cursor": null}
}
```

- `contract_version` usa versionado semántico. El plugin rechaza un major distinto de 1.
- `instance` repite la marca de instancia que usó el bridge, para que se vea cuál fue.
- `source` vale `projection` en la 1a, porque el bridge arma el flujo con la configuración de hoy, y `stored_definition` en la 1b. Mientras los flujos se migran de a uno, vale `mixed`, y cada flujo trae el suyo.
- `gaps` lista los huecos con su código (ver «Lo que el lector no sabe»).

### 1. «¿Qué flujos hay?»

**Qué pregunta la persona en Slack.** Por ejemplo: «¿qué flujos tiene ATT1?», «¿cómo funciona el carrito?», «¿quién le gana a quién?» o «¿cuántos carritos salieron esta semana?».

**Qué pide el plugin.** La herramienta `list_flows` llama a `GET /internal/automation-expert/v1/flows`. Para un solo flujo, `list_flows(flow_key)` llama a `GET /internal/automation-expert/v1/flows/{flow_key}`. `flow_key` sale de una lista cerrada. Hoy son cinco: `precheckout`, que es el primer contacto, `carrito`, `pago_fallido`, `reactivacion` y `descuento`, que es el cupón. El manifiesto declara además `inbound` (`instance_manifest.py:68`), pero el esquema dice que el agente que contesta no es un flujo (esquema, «Lo que no entra en la v1, y dónde va»), así que va aparte. Los flujos de la 1c entran a la lista cuando existan.

**Qué devuelve el bridge.** Devuelve `data.flows[]`, con los cinco flujos, prendidos y apagados. Cada uno trae estos campos:
- `flow_key`.
- `version`: en la 1a, las dos fuentes de las que sale el flujo. Para el carrito es `{"scope": "att1-recuperacion v5", "policy": "att1-recuperacion-un-toque v1"}`, y el pago fallido comparte ese scope. Si la configuración vive en variables o en el manifiesto, como la del cupón, va `null` con el hueco `env_value_unversioned`. La versión de la definición, `definition.version`, la asigna el ciclo al publicar, en la 1b (esquema, «La versión, el control y el techo»). En la 1a vale 1, con `definition.estado` en `publicada`, como el esquema escribe los flujos de hoy. La versión que corre de verdad va en este campo, `version` (ver «Tres elecciones técnicas, con su valor por defecto»).
- `status`: `on`, `paused`, `off` o `not_configured`, con el motivo y desde cuándo. Los tres primeros son los estados del control del esquema: prendido, pausado y apagado (esquema, «La versión, el control y el techo»). `not_configured` no está en el esquema: es un flujo de la lista que la instancia no tiene configurado. En la 1b, todo flujo guardado tiene su control, porque nace apagado. Hoy el control es uno por scope y no por flujo: el carrito y el pago fallido comparten el suyo, así que tienen el mismo `status`. El cupón y la reactivación no tienen pausa: están en `on` o en `off` según su flag. En `detail` va el estado crudo del piloto, por ejemplo `armed`. El piloto tiene cuatro estados: `armed` se muestra como `on`, `paused` como `paused`, e `inactive` y `closed` como `off`. `inactive` es como nace el control y como queda al cambiar de versión, y `closed` es final (`20260810000100_lancemos_pilot_boundary.sql:130-170` y `:404`). Si el cupón corre en modo prueba, porque `CONVERSATION_FOLLOWUP_ONLY_PHONE` está puesto, `detail` vale `test_single_phone`, sin el valor.
- `priority`: la prioridad de la definición (`prioridad.le_gana_a` y `prioridad.no_arranca_mientras`, en el esquema) más `le_ganan`, que el bridge calcula mirando a los otros flujos y marca como derivado. Así la regla vive en un solo lugar y la respuesta no se contradice. Hoy esas reglas están en el código, y la procedencia lo dice.
- `usage`: lo consumido hoy contra los topes, si el flujo los tiene. Trae el día local, los arranques del día y del total, el tope diario, el tope total y la bolsa que comparte, que sale de `topes.bolsa`.
- `observed`: lo que pasó en los últimos 7 días completos de CDMX, solo en conteos y medianas. Cuántos disparos llegaron, cuántos arrancaron y cuántos aceptó Chatwoot. Cuántos se frenaron y cuántos se rechazaron al entrar, los dos por motivo. Cuántos esperaron al horario, y la mediana del disparo al envío. Para el primer contacto suma cuántas veces le ganó otro flujo y la mediana del formulario al evento que ganó. Con eso el agente explica con un número medido, como pide el esquema, en vez de omitirlo o inventarlo.
- `definition`: las cinco piezas de la decisión de Dan del 2026-10-14, en el formato del esquema y con su `esquema: setter-flujo/v1`. Las piezas son qué lo dispara, quién entra, los pasos, cuándo termina, y el horario y los topes. Su forma interna la fija el esquema, no este contrato, y una clave que el esquema no tiene rechaza la definición entera. Por eso las barreras que el esquema deja en código, como la baja o la toma humana, no se escriben en `entra` ni en `termina`. Lo que hoy cada flujo mira a su manera, la proyección lo dice con procedencia `code` (ver «Lo que sale de afuera de la base»).
- `provenance`: de dónde salió cada valor. Es un mapa que va de la ruta dentro del flujo (JSON Pointer) a `{from, ref}`. `from` puede ser `database`, `manifest`, `env` o `code`. Con eso el agente puede decir «esto vive en una variable del bridge» o «esto es una regla en código».

Además de los flujos, la respuesta trae tres bloques:
- `data.in_code[]`: lo que corre en código y no es un flujo. Son el agente que contesta, el monitor de conversaciones trabadas y la reanudación, cada uno con su estado y su procedencia.
- `data.events_without_flow[]`: los eventos que llegan y no disparan nada. Hoy son las fichas de pago, impresas y vencidas, que en ATT1 fueron de OXXO, Mercado Pago, Yape y Nequi. El bridge las guarda en `failed` con `unsupported_persisted_event_type` y no hace nada más (`worker.py:2395-2402`). Cada uno trae el tipo, qué pasa hoy, cuántos llegaron en los 7 días, cuándo llegó el último y la procedencia `code`.
- `data.shared`: en la 1a no trae la puerta única, porque todavía no existe. Hoy existe una parte, la reevaluación del motor y la autorización de los tres arranques (`authorize_lancemos_pilot_request_start`), y el cupón y la reactivación no pasan por ella (esquema, «La puerta, chequeo por chequeo»). Cada flujo tiene sus barreras, y no coinciden. Por ejemplo, la pausa del piloto frena el primer contacto, el carrito y el pago fallido, pero no el cupón ni la reactivación (esquema, «La versión, el control y el techo»; `conversation-followup-discount-v1.md:126`). Si la respuesta mostrara una puerta común, el agente diría que pausar el piloto frena el cupón. La puerta entra en `data.shared` cuando la construya la 1b. El tope por persona tampoco existe en ATT1: `pilot_proactive_contact_caps` tiene 0 filas, y `data.shared` lo dice así. En el esquema es `puerta.tope_por_persona`, que en ATT1 vale `sin_tope`. Que cuente también los envíos del cupón y de la reactivación es de la puerta, no de hoy.

**Ejemplo (recortado).** El carrito de ATT1:

```json
{
  "flow_key": "carrito",
  "version": {"scope": "att1-recuperacion v5", "policy": "att1-recuperacion-un-toque v1"},
  "status": {"state": "on", "detail": "armed", "since": "2026-10-13T00:20:37Z"},
  "usage": {"local_date": "<día de CDMX>", "starts_today": "<n>", "daily_cap": 80,
            "starts_total": "<n>", "total_cap": 1000, "pool": "att1-recuperacion"},
  "observed": {"window_days": 7, "triggers": "<n>", "starts": "<n>",
               "accepted_by_chatwoot": "<n>", "rejected_on_entry": {"flujo_pausado": "<n>"}},
  "priority": {"le_gana_a": ["precheckout"], "no_arranca_mientras": ["pago_fallido"],
               "le_ganan": [], "derived": ["le_ganan"]},
  "source": "projection",
  "definition": {"esquema": "setter-flujo/v1", "version": 1, "estado": "publicada"},
  "provenance": {
    "/definition/version": {"from": "code",
      "ref": "docs/contracts/flow-definition-v1.md «Los siete flujos de validación»"},
    "/priority/le_gana_a": {"from": "code",
      "ref": "20261001000200_portable_precheckout_first_contact.sql:296-313"},
    "/priority/no_arranca_mientras": {"from": "code",
      "ref": "20260930000300_pilot_scope_audience_mode.sql:837-846"},
    "/usage/daily_cap": {"from": "database", "ref": "pilot_scope_versions att1-recuperacion v5"}
  }
}
```

En Slack, el agente lo diría así: «Hay cinco flujos, y cuatro están prendidos: el primer contacto, el carrito, el pago fallido y el cupón. La reactivación está apagada. Aparte, el agente contesta a quien escribe. El carrito y el pago fallido comparten la v5: 80 arranques por día y 1000 en total. Si a una persona le toca el primer contacto y llega su carrito o su pago fallido, gana ese flujo, aunque no mande nada. Esta semana pasó <n> veces. Esa regla hoy está en el código. Las fichas de pago llegan, pero todavía no disparan nada».

### 2. «¿Qué le pasó o le va a pasar a este lead, y por qué?»

**Qué pregunta la persona en Slack.** Por ejemplo: «¿qué le pasó a la conversación #N?», «le mandamos este link, ¿qué pasó?», «¿por qué no le salió el cupón?» o «¿le va a llegar algo más?».

**Qué pide el plugin.** Usa una sola herramienta, `lead_history`, con una de tres llaves: `conversation`, `link` o `name`. Si ya se eligió entre candidatos, usa `lead_ref`. Del link, el plugin saca el identificador de la emisión y manda solo ese (`issuance`): el resto del link, con su `fbclid`, no sale del plugin. Hace dos llamadas:
1. `POST /internal/automation-expert/v1/leads/lookup`, con la llave en el cuerpo. Devuelve un `lead_ref`, hasta 5 candidatos o `too_many_candidates`.
2. `GET /internal/automation-expert/v1/leads/{lead_ref}/history?limit=50&cursor=…`.

La búsqueda va por POST solo para que lo que escribió la persona viaje en el cuerpo y quede fuera de la URL (ver «Datos personales»). No escribe nada.

**Qué devuelve el bridge.**
- `data.lead`: `lead_ref`, el id opaco del contacto en la base de la instancia (`contacts.id`, un uuid). Trae también `label`, los números de sus conversaciones con el link a cada una en Chatwoot, e `identity`. `identity` lleva solo sí o no: si tiene teléfono, si tiene mail, si autorizó WhatsApp y si se dio de baja.
- `data.past[]`: los momentos, en orden. Cada uno trae:
  - `at` (UTC) y `local_at` (la hora en la zona del flujo, hoy CDMX);
  - `kind`, del catálogo de abajo;
  - `flow_key` y `flow_version`, este último con la misma forma que `version` en la primera pregunta;
  - `reason`, el motivo tomado de la lista cerrada de «Los motivos», más abajo, o `null`;
  - `reason_code`, el código crudo de hoy, y `explanation`, una frase de un catálogo fijo del bridge, o `null`;
  - `certainty`, que vale `stored` o `inferred`;
  - `attached_by`, el camino por el que se ató a la persona (ver «Cómo se ata cada fuente a la persona y a la instancia»);
  - `evidence`, con `{table, id}`.

  Si corresponde, el momento suma `decision` (en `stopped`), `winner_flow_key` (el flujo que ganó), `count` (cuántas veces se repitió) o `link_source` (con qué link compró: el de la plantilla, el del agente o el del cupón). Los momentos que salen de Chatwoot suman `actor_type`, `delivery_status` y `private`, según el caso (ver el catálogo y «La parte de Chatwoot»).
- `data.upcoming[]`: lo que todavía puede pasar. Trae las acciones vivas, cada una con `due_at`, `expires_at`, el paso, lo que se revisa antes de mandar y `expected`, lo que se espera que pase: `{decision, reason, certainty: "inferred"}`. Hace falta porque, entre que llega el carrito y la hora del primer contacto, el primer contacto sigue vivo. Sin `expected`, el agente diría que sale a tal hora, y se va a cancelar. Para el primer contacto, `expected` se calcula con `_portable_precheckout_stop_reason`, que es `STABLE` y de solo lectura (`20261001000200_portable_precheckout_first_contact.sql:193-205`). Para el resto, con la parte guardada del orden de la reevaluación de hoy: el plazo, la baja, la compra, la toma humana y el horario. Con la toma humana, lo que se espera hoy del carrito y del pago fallido es `pause`, con el motivo `tomada_por_el_equipo`, y no que el recorrido termine. Si no se puede calcular, vale `unknown`. `upcoming` trae también la ventana del cupón, como `inferred`, y la arma el bridge: el último mensaje de la persona en Chatwoot, tomado con el mismo criterio que el barrido (ver `coupon_not_sent`), desde el que el esquema cuenta la espera, más la espera mínima y la máxima del cupón (`CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS` y `CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS`, por defecto 24 h y 72 h, `app.py:1203-1206`), dentro de su horario. Si Chatwoot no se pudo leer, usa el último turno del agente, que aproxima ese mensaje. Viene siempre completa, hasta 20 elementos.
- `data.now`: el estado de hoy, con lo que guarda la base. Dice si compró, si se dio de baja, si tiene una derivación sin atender, si una persona tomó la conversación (`conversations.human_takeover`) y cuántas acciones vivas tiene.

**El catálogo de momentos.** `kind` sale de esta lista cerrada:
1. `form_received`: llegó el formulario de la landing. Sale de `webhook_events` (`PRECHECKOUT_FORM_SUBMITTED`) y de `purchase_intents` (`submitted_at`, `landing_ref`, `offer_ref`).
2. `first_contact_planned` y `first_contact_not_planned`: se programó, o no, el primer contacto. Sale de `portable_precheckout_first_contact_plans` (`outcome`, `reason_code`, `scope_version`).
3. `hotmart_event`: llegó un carrito, un pago fallido o una compra. Sale de `webhook_events` (`event_type`, `received_at`, `processing_status`) y de `hotmart_purchase_intent_correlations` (`outcome`, `matched_by`). Si el evento no abrió un caso, entra solo atado por identidad (decisión de Dan del 2026-10-15).
4. `rejected_on_entry`: el evento llegó y no entró a ningún flujo. Sale de `webhook_events.processing_error`, por ejemplo `pilot_scope_rejected:pilot_runtime_not_armed`. Como no abrió un caso, entra solo atado por identidad (decisión de Dan del 2026-10-15).
5. `entered_flow`: se abrió un caso en un flujo. Sale de `recovery_cases` (`source`, la política) y de `pilot_recovery_case_bindings` (`scope_key`, `scope_version`, `audience_mode`).
6. `merged_into_open_case`: el evento se sumó a un caso abierto en vez de abrir otro. Sale de los eventos `*_aggregated` de `conversation_events`. En ATT1 hay uno, `payment_failure_aggregated`.
7. `scheduled`: quedó programado un paso. Sale de `scheduled_actions` (`anchor_type`, `step_key`, `due_at`, `expires_at`).
8. `deferred_by_hours`: le tocaba salir fuera de horario y se corrió. Sale de `conversation_events`, con la reevaluación `defer` y el motivo `business_window_closed`.
9. `stopped`: se frenó un paso. Sale de `conversation_events`, con la reevaluación `cancel`, `pause` o `escalate`, que también frenan la acción (`20260805000300_per_case_conversation_anchor.sql:284-445`), y de `scheduled_actions.terminal_reason`. Lleva la `decision` y nombra el flujo que ganó, si lo hubo.
10. `start_authorized`: el piloto autorizó el arranque y gastó cupo del día. Sale de `pilot_outbound_request_authorizations` (scope, versión, `local_budget_date`). Desde `20261022000300`, también de `flow_engine_request_authorizations` cuando lo autorizó el motor: la evidencia es esa tabla, el código `flow_engine_request_start_authorized` y la frase «El motor autorizó el arranque y gastó cupo del día.».
11. `start_rejected`: intentó arrancar y no lo autorizaron. Es deducido: son intentos `reserved` sin autorización. Sale como un solo momento, con `count`.
12. `accepted_by_chatwoot`: Chatwoot aceptó la plantilla. Sale de `followup_delivery_attempts` (`outcome`, `accepted_at`) y de `messages.delivery_status`. Dice que salió, y nada sobre si llegó.
13. `delivery_uncertain_or_failed`: el envío quedó incierto o falló. Sale de `followup_delivery_attempts`, con un resultado distinto de aceptado, y de los `failed` del cupón y de la reactivación. En ATT1 hoy no hay ninguno.
14. `meta_delivery`: lo que Meta dijo de una plantilla que salió de la base: entregada (`delivered`), leída (`read`) o fallida (`failed`). `sent` quiere decir que Meta no confirmó la entrega: no dice que llegó ni que falló. Sale de Chatwoot, del estado del mensaje de esa plantilla, que el bridge encuentra por su id (ver «La parte de Chatwoot»). Lleva `delivery_status`, y cada estado tiene su frase fija en `explanation`. La de `sent` dice que Meta no confirmó la entrega y que, en ATT1, antes del 2026-10-05 a las 11:41Z ese estado no distingue nada. La de `failed` dice que Meta no la entregó. Si Chatwoot guardó el código de Meta, el momento lleva `meta_error_code`, solo en dígitos, y la frase fija de ese código; si no lo guardó, va con el hueco `meta_failure_reason_not_persisted` y el agente no supone una causa (1.1.0). Va con la hora del envío, justo después del momento que lo registra (`accepted_by_chatwoot`, `coupon_sent` o `reactivation`), porque Chatwoot da el estado de hoy y no la hora en que cambió. No es `delivery_uncertain_or_failed`: ese es un envío que Chatwoot no aceptó, o del que no se sabe si salió.
15. `replied_and_adopted`: contestó a una plantilla y el agente tomó la conversación. Sale de `conversation_events` (`inbound_adopted_template_conversation`) y de `commercial_cases` (`inbound_sales`).
16. `agent_replied`: el agente contestó un turno. Sale de `agent_turn_provenance`: la hora, el resultado (`completed` o `failed`), el modelo y la versión del bridge. Nunca el contenido. Los mensajes de ese turno salen aparte, como `conversation_activity`: el turno dice cómo se contestó, y cada mensaje, cuándo salió y si llegó.
17. `conversation_activity`: un mensaje de la persona, del agente o del equipo, o la nota que el bridge deja al derivar, con su hora y el tipo de autor (`actor_type`), nunca con su contenido. Los salientes llevan `delivery_status`, salvo una nota privada, que sale con `private: true` y sin texto. Sale de Chatwoot (ver «La parte de Chatwoot»). Las plantillas de la base no salen acá, sino como `meta_delivery`, y los mensajes de actividad de Chatwoot no salen.
18. `link_issued`: se emitió un link de pago. Sale de `checkout_link_issuances` (`source_kind`, `offer_resolution`, `attribution_resolution`). Nunca devuelve la URL.
19. `coupon_sent`: salió el cupón. Sale de `conversation_followup_events` (`status`, `regime`, `inbound_age_seconds`, `checkout_issuance_id`).
20. `coupon_not_sent`: por qué no salió el cupón. Es deducido: compara las condiciones del cupón con lo guardado y con los mensajes de Chatwoot, que son los que mira su barrido: si la persona nunca contestó, si escribió último la persona o alguien del equipo, y si su último mensaje es demasiado nuevo o demasiado viejo (`followup_discount.py:469-517`). De Chatwoot usa lo mismo que el barrido: solo los mensajes públicos de tipo 0, 1 o 3, ordenados por el id de Chatwoot y no por la hora, con el agente reconocido solo por `sender.type` (`followup_discount.py:478-504`; `reactivation.py:244-278`). Una nota privada, como la de una derivación, no cuenta, y un audio de la persona sí. Si Chatwoot no se pudo leer, usa la ventana que dan los turnos del agente. Si no alcanza, es un hueco.
21. `reactivation`: salió una reactivación. Sale de `conversation_reactivation_events`. En ATT1 está apagada y tiene 0 filas.
22. `handed_off`, `handoff_attended` y `resumed`: se derivó a una persona, una persona lo atendió, o se levantó la pausa. Sale de `human_handoff_requests` (el motivo principal y el detalle, `status`, `attended_at`) y de `conversation_resume_events`. Nunca devuelve la nota privada.
23. `opted_out`: se dio de baja. Sale de `contact_opt_out_events` y de las `contact_authorizations` denegadas.
24. `purchased`: compró. Sale de `checkout_link_issuances.purchased_at`, que dice con qué link compró, y de `purchase_intents.lifecycle_state`. Si no compró con un link nuestro, la hora sale del `PURCHASE_APPROVED` atado por identidad (decisión de Dan del 2026-10-15), como `inferred`.
25. `expired` y `closed`: venció un paso o se cerró el caso. Sale de las `scheduled_actions` vencidas, de `followup_action_expired` y de `recovery_cases` (`status`, `closed_at`).
26. `other`: un tipo de evento que el catálogo no conoce. Sale con el tipo crudo, si cumple la forma de los códigos, y suma el hueco `unknown_event_kind`.

**Las reglas de la historia.** Varios registros de la base engañan si se leen tal cual. El lector los traduce así:
- **Un bucle, un momento.** Un arranque rechazado deja, en cada vuelta del lease, un `followup_action_reevaluated` con `decision=execute` y un intento `reserved`. Los 5 carritos de la v4 suman 144 intentos y 288 «execute» cada uno, y ninguno tiene una autorización ni un mensaje. La historia lo muestra como un solo `start_rejected`, con `count` y la primera y la última vez.
- **El motivo de un rechazo se deduce en este orden.** Primero, si la versión del caso era distinta de la activa en ese momento: `pilot_control_events` guarda cada cambio de versión. Ese caso no tiene motivo en la lista, así que va con `reason` en `null`. Después, si el control no estaba `armed`. `pilot_control_events` guarda también cada cambio de estado (`to_state`, `20260810000100_lancemos_pilot_boundary.sql:512-519`), y solo `paused` es una pausa (`flujo_pausado`). Si estaba `inactive` o `closed`, va con `reason` en `null`. Por último, si las autorizaciones del día local ya estaban en el tope del scope (`tope_del_flujo`). Si nada de eso lo explica, va el hueco `start_rejection_not_persisted`.
- **`expired_after_worker_crash`** es la etiqueta que pone el barrido a toda acción vencida sin lease, haya habido o no una caída (`20260803000100_followup_engine_v1.sql:2555-2604`). Pasa a `vencio`, como el primer chequeo de la puerta, y el porqué lo da el rechazo deducido del arranque.
- **`sequence_exhausted`** quiere decir «salió el mensaje y el flujo terminó». Ningún caso de ATT1 llega a `won` (0 de 392): el caso de un solo toque se cierra cuando se acepta la plantilla.
- **Una toma humana hoy no termina el carrito ni el pago fallido.** La reevaluación decide `pause` con `human_takeover_active` (`20260805000300_per_case_conversation_anchor.sql:297-306`): cancela la acción y deja el caso `paused` (`:425-438`). Ese caso sigue abierto, junta los carritos nuevos de la misma oferta y frena al primer contacto. La historia lo muestra así, con un `stopped` en `pause` y el motivo `tomada_por_el_equipo`, y no como un final. El primer contacto, en cambio, cancela su caso. Que la toma humana termine el recorrido de todos los flujos es de la 1b (ADR-0007 §7; esquema, «Lo que cambia respecto de hoy»). En ATT1 todavía no pasó: ninguna reevaluación decidió `pause`.
- **La compra sale de los links.** Nunca sale de `recovery_cases.won_at` ni del estado del webhook. Un `PURCHASE_APPROVED` en `failed` con `purchase_correlation_case_not_found` es una venta sin un caso abierto del recuperador. Hay 34 así: 2 tienen un link nuestro comprado, y 32 se atan a la persona por identidad. Cuando no compró con un link nuestro, el canal no se sabe.
- **Aceptado por Chatwoot quiere decir que salió, no que llegó.** La entrega la confirma Meta, y la base no la guarda: lo que Meta dijo lo trae `meta_delivery`, desde Chatwoot. Si Chatwoot no se pudo leer, el agente no dice que llegó ni que no llegó. En el caso A, Meta entregó la plantilla del carrito, y el cupón quedó en `sent`: Meta nunca confirmó su entrega, y aun así la persona compró con su link.
- **La cancelación del primer contacto pasa cuando le toca salir,** y no cuando llega el evento que gana. En los 158 primeros contactos cancelados así, la cancelación llegó una mediana de 18 minutos después del carrito o del pago fallido, y como mucho 58 minutos después. En ese rato, `upcoming` muestra el primer contacto con `expected` en `cancel`. El momento `stopped` muestra las dos horas y el flujo que ganó. En la base, contra esos 158, salieron 13 primeros contactos.
- **Las fichas no aparecen en ninguna historia.** La base las guarda sin correlación, sin caso y sin fila de identidad (`20261012000100_portable_payment_failure_context_and_slip_events.sql:24-31` y `:216-227`). Toda historia lleva el hueco `payment_slip_not_attached`, y la respuesta de flujos las cuenta en `events_without_flow`.
- **El motivo crudo pasa por una forma fija.** `reason_code` sale solo si cumple `^[a-z][a-z0-9_:]{0,79}$`. Si no la cumple, sale `null` con el hueco `unknown_reason_code`, nunca el texto. `terminal_reason`, los motivos de `conversation_events.data` y el `reason_code` de `pilot_control_events` son texto libre en la base (`20260803000100_followup_engine_v1.sql:195` y `20260810000100_lancemos_pilot_boundary.sql:294`). El bridge limpia solo `processing_error` (`supabase.py:178-181`): un error con un teléfono adentro llegaría tal cual al modelo y a Slack.
- **Un `reason_code` fuera del catálogo** deja el momento con `explanation` y `reason` en `null`, y suma el hueco `unknown_reason_code`. La historia sale igual. En esto se aparta a propósito del molde, donde un código desconocido hace fallar la lectura (`operator-correlation-review-v1.md:148`). Una línea de tiempo junta muchas tablas, y perderla entera por un código es peor que marcarlo.

**Los motivos.** `reason` sale de una lista cerrada, que vive en este contrato. El esquema deja en la puerta el motivo de cada chequeo y en «Cuándo termina» las salidas, y manda acá la lista con la que la 1a contesta «por qué no le llegó» (esquema, «Lo que no entra en la v1, y dónde va»). El esquema nombra diez motivos: `vencio`, `baja`, `compro`, `ficha_vencida`, `le_gano:<flujo>`, `tomada_por_el_equipo`, `contesto`, `flujo_apagado`, `ya_no_entra` y `sin_horario`. Los otros once los fija este contrato: vienen de la lista de una versión anterior del esquema, sin verificar. Cada motivo dice qué quiere decir, de qué chequeo o salida del esquema sale y cómo lo ve el lector en la 1a: guardado, deducido o como hueco, y si hoy no pasa, lo dice. Los chequeos son los doce de la puerta (esquema, «La puerta, chequeo por chequeo»).

En el esquema, cuando el disparo no arranca, es porque la puerta lo frenó al llegar el disparador. Ahí corre los doce chequeos y, además, mira la repetición, `no_arranca_mientras` y si otro flujo le gana. El disparo queda registrado con su motivo, que es uno de estos o el de un chequeo, como `baja` o `compro`. En la 1a, los rechazos al entrar de Hotmart se atan a la persona solo por identidad (decisión de Dan del 2026-10-15).
- `ya_en_curso`: la repetición no deja arrancar otro. La persona ya tiene un recorrido de este flujo en curso, o no pasó `otra_despues_de` desde su último envío aceptado o incierto. Sale de `topes.repeticion`. En la 1a, guardado en el primer contacto (`precheckout_contact_already_planned`, que cubre los dos casos). En el pago fallido, el disparo se suma al caso abierto (`merged_into_open_case`). En el carrito deja el mismo código que el motivo siguiente.
- `no_arranca_mientras:<flujo>`: la persona tiene en curso un recorrido de ese flujo, con la misma clave de repetición, y el disparo se pierde. Sale de `prioridad.no_arranca_mientras`. En la 1a, el carrito deja `pilot_case_binding_conflict`, que es también el código de su repetición (esquema, «Lo que cambia respecto de hoy»). El lector no sabe cuál de los dos fue, así que sale con `reason` en `null`. En ATT1 todavía no pasó.
- `le_gano:<flujo>`: llegó el disparo de un flujo que le gana, para la misma intención de compra, aunque ese flujo no mande nada, o ese flujo tiene un recorrido en curso en el mismo producto. Sale de `prioridad.le_gana_a` del flujo que gana. En la 1a, guardado (`superseded_by_provider_event`).
- `no_entra:<condición>`: la persona no cumple una condición de `entra` cuando llega el disparo, por ejemplo el permiso. Sale de la parte de permiso del chequeo 6 y del chequeo 8, cuando se sabe que no la cumple. Si no se sabe, es `identidad:<motivo>`. En la 1a, guardado (`pilot_audience_intent_unresolved`).
- `flujo_pausado`: el flujo está pausado. El disparo no arranca nada y no vuelve cuando el flujo se prende. Sale del chequeo 7. En la 1a, guardado como `pilot_runtime_not_armed`, el mismo código que deja un control `inactive` o `closed`. El lector mira en `pilot_control_events` en qué estado estaba, y solo `paused` es este motivo.

Un flujo apagado no deja motivo, porque su disparo ni se registra. En la 1a pasa lo mismo con el flag del flujo apagado: el evento vuelve `ignored` y no queda en la base (hueco `hotmart_ignored_not_persisted`).

Cuando el envío espera, el recorrido sigue en curso y todavía no manda:
- `fuera_de_horario`: está fuera de la franja, en la zona del flujo, y espera a la próxima apertura. Sale del chequeo 9. En la 1a, guardado (`business_window_closed`). Del cupón no queda nada: fuera de horario, el barrido no lee.
- `flujo_pausado`: el de arriba, con un recorrido que ya estaba en curso. Espera sin mandar hasta que el flujo se prenda o el recorrido venza. Sale del chequeo 7. En la 1a, deducido del control, porque hoy el arranque se rechaza y se reintenta sin guardar por qué (ver «Las reglas de la historia»).
- `tope_del_flujo`: se gastó el cupo del flujo sobre su bolsa. Espera sin gastar cupo hasta que se libere o venza. Sale del chequeo 11. En la 1a, deducido de las autorizaciones del día, sin un caso real todavía.
- `tope_por_persona`: la persona llegó al tope por persona de la instancia. Sale del chequeo 11. En la 1a no pasa, porque ATT1 no tiene renglón del tope. Si lo tuviera, el rechazo quedaría solo en el log, como hoy cualquier arranque rechazado (hueco `start_rejection_not_persisted`).

Cuando la versión del recorrido no es la activa (chequeo 7), no hay motivo en la lista: qué pasa con quien está a mitad de un flujo lo decide el contrato del ciclo. Hoy el arranque se rechaza y se reintenta hasta vencer (`pilot_scope_version_mismatch`). En la 1a el lector lo deduce y lo muestra con `reason` en `null`, y al vencer, con `vencio`.

Cuando no sale:
- `identidad:<motivo>`: no se sabe si el teléfono se puede leer, si es de una sola persona, si la conversación es del inbox de la instancia o si hay un permiso vigente, y se escala. Sale del chequeo 6. En la 1a, en el carrito, en el pago fallido y en el primer contacto ya planificado, que pasa por la misma reevaluación (`20261001000200_portable_precheckout_first_contact.sql:1639-1646`), es un `stopped` en `escalate` (`identity_not_authorized` o `contact_authorization_unknown`). Al planificar el primer contacto, es un `first_contact_not_planned`. En ATT1 hubo uno así: el mail y el teléfono del formulario llevaban a más de un contacto (`precheckout_contact_ambiguous`, `20261001000200_portable_precheckout_first_contact.sql:366-442`). Esos tres códigos salen por ahora con `reason` en `null`, como los demás sin traducción, hasta que el claim fije el `<motivo>` de cada uno.
- `meta:<regla>`: no cumple una regla de Meta. Fuera de la ventana de 24 h solo sale una plantilla aprobada, en su idioma, con todas sus variables y un nombre que pase el filtro. Sale del chequeo 10. En la 1a, el envío frenado deja su código, como `approved_template_mismatch`, y sale con `reason` en `null` hasta que el claim confirme la traducción. Lo que el cupón saltea por esto no se guarda (hueco `followup_skip_not_persisted`). En ATT1 todavía no hubo ninguno.
- `envio_incierto`: no se sabe si el envío salió. No se reintenta: se reconcilia o se escala. Sale del chequeo 12. En la 1a, guardado como `delivery_uncertain_or_failed`. En ATT1 todavía no hubo ninguno.

Cuando el recorrido termina:
- `vencio`: llegó el plazo. Sale del chequeo 1 y de la salida `vence`, o del `hasta` con un silencio. En la 1a, guardado (`expired` y `expired_after_worker_crash`).
- `baja`: una baja en cualquiera de las dos formas del móvil, un contacto bloqueado o restringido, o un permiso denegado. Sale del chequeo 2. En la 1a, guardado (`contact_opted_out` y, al entrar, `pilot_audience_consented_intent_prior_opt_out`).
- `compro`: la persona compró, o hay una compra que podría ser suya. Sale del chequeo 3 y de la salida `compra`. En la 1a, guardado (`intent_purchased`, `purchase_by_identity` e `intent_purchase_ambiguous`, que es una compra aprobada que no se pudo atribuir a una sola intención, `20261001000200_portable_precheckout_first_contact.sql:231-240`).
- `ficha_vencida`: llegó el vencimiento de la ficha. Sale del chequeo 3 y de la salida `ficha_vencida`. En la 1a no pasa: ningún flujo de hoy tiene esa salida, y las fichas no se atan a nadie (hueco `payment_slip_not_attached`).
- `le_gano:<flujo>`: el de arriba, con el recorrido que ya estaba esperando. Sale del chequeo 3. En la 1a, guardado (`superseded_by_provider_event`), con el flujo que ganó.
- `tomada_por_el_equipo`: una persona tiene la conversación. Es una derivación sin atender, una conversación tomada o pausada por el equipo, o una con la automatización apagada. Sale del chequeo 4. En la 1a, guardado en el primer contacto, que hoy sí termina, porque cancela su caso (`precheckout_conversation_handoff`). El carrito y el pago fallido hoy no terminan: la reevaluación decide `pause` (`human_takeover_active`) y su caso queda `paused`. La historia los muestra con este motivo y con `decision: pause` (ver «Las reglas de la historia»).
- `contesto`: la persona escribió después del disparo, si el flujo tiene la salida `contesta`, o después del primer envío aceptado, en cualquier flujo. Sale del chequeo 5. En la 1a pasa solo en el cupón, que saltea sin guardarlo (`lead_wrote_meanwhile`). El lector lo deduce con los mensajes de Chatwoot, tomados con el mismo criterio que el barrido (ver `coupon_not_sent`), donde se ve que la persona escribió después del último mensaje del agente, o con los turnos del agente si Chatwoot no se pudo leer. Si no alcanza, es el hueco `followup_skip_not_persisted`. El carrito, el pago fallido y el primer contacto mandan un solo mensaje y no miran la respuesta antes de mandarlo, así que en ellos no pasa.
- `flujo_apagado`: el flujo se apagó con el recorrido en curso. Sale del chequeo 7. En la 1a no se ve: hoy un flujo se apaga con su flag, que no deja historia en la base (hueco `env_value_unversioned`).
- `ya_no_entra`: dejó de cumplirse una condición que cambia con el tiempo: el silencio y su edad, la adopción de la conversación, la intención viva o el consentimiento. Sale del chequeo 8. En la 1a, guardado en el primer contacto, con dos códigos: `precheckout_intent_not_live` y `precheckout_authorization_lost`, que es el que el esquema nombra para este chequeo (`20261001000200_portable_precheckout_first_contact.sql:215`, `:228`, `:1611` y `:1630`). El segundo todavía no pasó en ATT1. Se escribe como en el esquema, sin sufijo. Qué condición dejó de cumplirse lo dicen `reason_code` y su frase en `explanation`, no el código del motivo (ver «Tres elecciones técnicas, con su valor por defecto»).
- `sin_horario`: no queda ninguna apertura antes de vencer. Sale del chequeo 9. En la 1a, si pasa, aparece como un `stopped` en `escalate`, porque hoy ese caso se escala. En ATT1 todavía no pasó.
- `completo`: ya se mandaron todos los pasos. Sale de «Cuándo termina», de las salidas que quedan en código. En la 1a, guardado (`accepted_by_chatwoot` y `policy_exhausted`).

Así se traducen los códigos de hoy. Todos aparecen en att1-db, salvo `human_takeover_active` y `precheckout_authorization_lost`, que todavía no pasaron. En los rechazos al entrar se mira lo que va después de `pilot_scope_rejected:`.
- `superseded_by_provider_event` pasa a `le_gano:<flujo que ganó>`.
- `business_window_closed` pasa a `fuera_de_horario`.
- `pilot_runtime_not_armed` pasa a `flujo_pausado` si el control estaba `paused` en ese momento, según `pilot_control_events`. Si estaba `inactive` o `closed`, sale con `reason` en `null`.
- `pilot_audience_intent_unresolved` pasa a `no_entra:permiso`, y `precheckout_intent_not_live` y `precheckout_authorization_lost`, a `ya_no_entra`.
- `intent_purchased`, `purchase_by_identity` e `intent_purchase_ambiguous` pasan a `compro`.
- `contact_opted_out` y `pilot_audience_consented_intent_prior_opt_out` pasan a `baja`.
- `precheckout_contact_already_planned` pasa a `ya_en_curso`.
- `precheckout_conversation_handoff` y `human_takeover_active` pasan a `tomada_por_el_equipo`. El segundo va con `decision: pause`, porque hoy el carrito y el pago fallido no terminan.
- Los vencimientos (`expired` y `expired_after_worker_crash`) pasan a `vencio`.
- `accepted_by_chatwoot` y `policy_exhausted` pasan a `completo`.

Los demás códigos de hoy, como `pilot_contact_not_in_cohort`, `precheckout_contact_ambiguous` o `payment_failure_correlation_unresolved`, salen con `reason: null` y su `reason_code` hasta que esta lista les dé un motivo. La lista se confirma en el claim, leyendo dónde nace cada código, y la frase de cada uno se escribe ahí. Así, cuando la 1b guarde estos motivos, no se rehacen el plugin, el SOUL ni los casos de prueba.

**Ejemplo (el caso A, anonimizado y recortado).** Las horas van relativas a T0, la hora del formulario, y en la respuesta real son UTC. Cada momento lleva además `local_at`, `explanation`, `attached_by` y `evidence`. Los momentos de Chatwoot salen de sus metadatos, medidos el 2026-10-15 (ver «De dónde sale»).

```json
{
  "contract": "automation-expert-read-v1",
  "contract_version": "1.1.0",
  "source": "projection",
  "data": {
    "lead": {"lead_ref": "<uuid>", "label": "<nombre de pila> · conversación #<n>"},
    "past": [
      {"at": "T0", "kind": "form_received", "flow_key": "precheckout", "certainty": "stored"},
      {"at": "T0 + 41 min", "kind": "deferred_by_hours", "flow_key": "carrito",
       "flow_version": {"scope": "att1-recuperacion v4", "policy": "att1-recuperacion-un-toque v1"},
       "reason": "fuera_de_horario", "reason_code": "business_window_closed", "certainty": "stored"},
      {"at": "T0 + 46 min", "kind": "accepted_by_chatwoot", "flow_key": "carrito",
       "certainty": "stored"},
      {"at": "T0 + 46 min", "kind": "meta_delivery", "flow_key": "carrito",
       "delivery_status": "delivered", "certainty": "stored"},
      {"at": "T0 + 60 min", "kind": "stopped", "flow_key": "precheckout", "decision": "cancel",
       "reason": "le_gano:carrito", "reason_code": "superseded_by_provider_event",
       "winner_flow_key": "carrito", "certainty": "stored"},
      {"at": "T0 + 91 min", "kind": "conversation_activity", "actor_type": "prospect",
       "certainty": "stored"},
      {"at": "T0 + 92 min", "kind": "replied_and_adopted", "flow_key": "carrito",
       "certainty": "stored"},
      {"at": "T0 + 92 min", "kind": "conversation_activity", "actor_type": "ai_agent",
       "delivery_status": "delivered", "certainty": "stored"},
      {"at": "T0 + 1 d + 98 min", "kind": "coupon_sent", "flow_key": "descuento",
       "reason_code": "link_sent_no_purchase", "certainty": "stored"},
      {"at": "T0 + 1 d + 98 min", "kind": "meta_delivery", "flow_key": "descuento",
       "delivery_status": "sent", "certainty": "stored"},
      {"at": "T0 + 2 d + 5 h", "kind": "purchased", "link_source": "coupon",
       "certainty": "stored"}
    ],
    "upcoming": [],
    "now": {"purchased": true}
  },
  "gaps": [
    {"code": "payment_slip_not_attached", "scope": "instance",
     "what": "si imprimió una ficha de pago",
     "why": "la base guarda las fichas sin atarlas a nadie; se mira en Hotmart"}
  ]
}
```

En Slack, el agente lo diría así: «<nombre de pila> (conversación #N) llenó el formulario un poco después de las 8 de la mañana de CDMX, y el primer contacto quedó para una hora después. A los 41 minutos llegó su carrito. Como era antes de las 09, la plantilla esperó y salió a las 09, y Meta se la entregó. El primer contacto se canceló a la hora en que le tocaba salir, porque ganó el carrito. Unos 45 minutos después de la plantilla contestó, y el agente tomó la conversación y le mandó un link, que también le llegó. Al día siguiente, después de 24 h de silencio, salió el cupón. Meta no confirmó que le llegara, pero dos días después del formulario compró con el link del cupón».

## Cómo se nombra a un lead

**Cómo se lo pide.** Con una de tres llaves:
- **El número de conversación de Chatwoot,** el que Brayan y Dan ven en la URL. Es la llave exacta. Está guardado en cinco lugares de la base, y en los casos medidos coinciden entre sí.
- **El link de pago pegado.** El plugin saca de ahí el identificador de la emisión, que está adentro del link en 44 de 44 casos, y manda solo ese. El bridge rechaza una URL con 422 `invalid_lookup`, y nunca guarda ni devuelve la URL, porque lleva atribución personal.
- **El nombre.** Se compara por igualdad sobre el nombre normalizado: en minúsculas, sin tildes y con los espacios colapsados. También vale un prefijo de tres letras o más, con `%` y `_` escapados, para que nadie recorra la base de a cinco probando letras sueltas. Si hay más de un candidato, devuelve hasta 5, cada uno con su nombre de pila, su número de conversación y su última actividad, y el agente pregunta cuál es. Si hay más de cinco, contesta `too_many_candidates` sin listarlos. Ante un empate nunca elige solo. El nombre completo está cargado en 294 de 295 contactos, con 293 distintos: es casi único, pero no está garantizado.

Una entrada con forma de teléfono o de mail se rechaza con 422 `identifier_not_allowed`, tanto en el plugin como en el bridge. Para contar los dígitos, primero se sacan los espacios, los guiones, los puntos, los paréntesis y el `+`: siete dígitos o más es un teléfono. El servidor nunca busca por esos campos. Si alguien arranca desde un teléfono, Chatwoot ya busca por teléfono y por mail, y el número de conversación que da es la llave exacta (decisión de Dan del 2026-10-15).

**Cómo se lo nombra.** `label` es «<nombre de pila> · conversación #N». El nombre de pila sale de la misma cadena del saludo de las plantillas: el nombre inferido con confianza o, si no hay, la primera palabra del nombre completo. Nunca cae al nombre completo. Si la persona escribió primero el apellido, esa primera palabra es el apellido, y la cadena no lo puede evitar. La guarda es la de las palabras de nombre de la cadena, que admite solo letras, apóstrofos y guiones (`lead_first_name.py:67`). No es `template_greeting_name_is_safe`, que admite emoji y los caracteres invisibles TAG, de U+E0020 a U+E007F (`lead_first_name.py:80` y `:443-451`): con ellos se puede esconder texto que el modelo lee y la persona no ve. Si el nombre no pasa la guarda, va solo el número. Nunca se muestran el nombre completo, el teléfono ni el mail, tampoco enmascarados. En eso se aparta del molde del copiloto, que enmascara (`operator-correlation-review-v1.md:152`). Es decisión de Dan del 2026-10-15, con el límite del apellido aceptado.

**Todo lo de una persona.** La historia junta todos los casos, conversaciones y links del contacto. Hace falta: 163 contactos tienen dos casos y 2 tienen tres, y alguno llega a 4 conversaciones. En el caso A, una sola persona juntó tres casos (el formulario, el carrito y la venta conversacional), dos links y una compra.

## Las reglas

### Solo lectura

- Las rutas son `GET`, más una búsqueda por `POST` que no escribe.
- Las tres funciones de la base son `STABLE` (`provolatile = 's'`). El nombre no alcanza: hay funciones con prefijo de lectura que son `VOLATILE`, como `get_agent_turn_provenance_v1`. Y `STABLE` tampoco alcanza solo: Postgres no lo verifica hacia adentro, y una función `STABLE` puede llamar a otra que escriba. Por eso una prueba corre las tres adentro de `begin transaction read only`. Si alguna escribe, sola o a través de otra función, la prueba falla.
- No hay ninguna tabla nueva ni ningún permiso nuevo sobre tablas.
- Las herramientas del plugin no escriben, y la 1a no tiene herramientas de cambio.
- El lector no guarda los rechazos al arrancar: guardarlos va con la 1b (decisión de Dan del 2026-10-15; ver «Lo que decidió Dan el 2026-10-15»). Tampoco marca nada como leído ni deja rastro en la base.
- A Chatwoot le hace solo `GET` a los mensajes de una conversación: el mismo pedido que el bridge ya hace antes de cada turno del agente (`app.py:4336-4340`).
- Falta confirmar que PostgREST corra las funciones en una transacción de solo lectura (ver «Lo que se confirma al implementar»). La prueba de arriba no depende de eso.

### Clave propia

- El plugin manda `Authorization: Bearer <AUTOMATION_EXPERT_READ_TOKEN>`. El bridge lo compara en tiempo constante, con `hmac.compare_digest`, igual que el token del copiloto (`app.py:5713-5732`).
- El token tiene al menos 32 caracteres. Tiene que ser distinto de todos los secretos que carga el bridge (`Settings`, `app.py:466-681`): el de Hotmart, los de las landings y de GHL, los de Chatwoot, la key de `service_role`, `HERMES_API_KEY` (la del agente comercial) y los del copiloto, entre otros. Si repitiera uno, quien tiene ese secreto podría leer la historia de cualquier lead: por ejemplo, Hotmart. Hoy el bridge compara solo el token de lectura del copiloto con el de escritura (`app.py:2948-2986`). Para este token, al arrancar lo compara con `compare_digest` contra cada secreto, y no arranca si coincide con alguno.
- Cada instancia tiene su propio valor, generado por `generar.py` de la instancia.
- La clave sirve para entrar al bridge. El bridge sigue leyendo con su cliente de PostgREST, que entra como `service_role` (`supabase.py:1571-1589`). El plugin nunca recibe la key de `service_role`, igual que el del copiloto (`operator-correlation-review-v1.md:171`).
- La clave vive solo en el `.env` del profile `automation-expert`, nunca en el entorno del contenedor de Hermes. Con un gateway por profile, como corre ATT1, Hermes busca un secreto en el profile y, si no está, en el entorno del proceso (`agent/secret_scope.py:176-190`). Si el token estuviera en el entorno del contenedor, lo heredaría también el agente comercial, el que habla con los leads. El `.env` del profile entra al respaldo de R2, como hoy las keys del comercial (`despliegue/respaldo/README.md:11` de la instancia).
- Cada pedido deja en el log una línea de acceso sin datos personales: la ruta, el `lead_ref`, la cantidad de momentos y el código de respuesta.

### La instancia (el tenant)

- La fija el bridge, nunca el pedido. Con el flag prendido, el bridge exige el manifiesto (`INSTANCE_MANIFEST_PATH`) y arma con él la marca de la instancia: la cuenta y la bandeja de Chatwoot, `tenant_ref`, `ally_ref` y `funnel_ref`, el producto de Hotmart y el `inbound_scope_key` (`instance_manifest.py:176-195`). Le suma el `scope_ref` de los turnos del agente, que sale de `AGENT_PROVENANCE_SCOPE_REF` (`app.py:806`). Se la pasa a las funciones, y ningún parámetro del pedido la cambia.
- El tenant solo no separa instancias. `tenant_ref` y `tenant_key` valen `lancemos`, que es la agencia y no la instancia. ATT1 y Johanna ya lo comparten (`20260825000500_johanna_mvp_full_activation.sql:480`; `.env.example:54` y `:109` de la instancia), y ATT2 también lo va a compartir. En att1-db, las 7 versiones de scope tienen `tenant_key` `lancemos` y bandeja 11, y las 461 intenciones están en `lancemos` y `att1`. El CHECK de `tenant_key` solo pide que no esté vacío (`20260903000300_commercial_ally_payment_failure_recovery.sql:3-7`).
- Los flujos se filtran por la bandeja y el producto de cada scope (`pilot_scope_versions.chatwoot_inbox_id` y `external_product_id`, `20260810000100_lancemos_pilot_boundary.sql:8-36`), comparados con los del manifiesto. Nunca por `tenant_key` solo.
- La mayoría de las tablas de la historia no tiene columna de instancia: `recovery_cases`, `scheduled_actions`, `conversation_events`, `followup_delivery_attempts` y `messages`, entre otras. Y la bandeja no está donde se la buscaría para un lead: `conversations.commercial_context` guarda solo el número de conversación (`20260816000200_inbound_commercial_case_draft_only.sql:134-136`), y `channel_identities` no tiene bandeja.
- Por eso la historia se arma cerrada por defecto, como pide el molde (`operator-correlation-review-v1.md:41`). Cada elemento entra solo si se ata a la instancia por algún camino con marca, y lo que no se ata queda afuera. Los caminos están en «Cómo se ata cada fuente a la persona y a la instancia». Hoy no se pierde nada: las 199 conversaciones de ATT1 tienen al menos un ancla, 194 de ellas por el scope del caso.
- Una conversación o un lead que no se ata a la instancia da 404, igual que uno que no existe.
- En ATT1, además, la base es propia y está en una red interna (`stack.yaml:37`, `:52` y `:63`). El contrato no se apoya en eso, porque tiene que seguir valiendo con varias instancias en una base.
- La marca de instancia supone una bandeja por instancia en los caminos que atan por bandeja: los links, las derivaciones y las bajas. Si ATT2 usara la misma bandeja que ATT1, esos caminos no alcanzan solos. Se revisa al diseñar ATT2.

### El flag

- Son dos variables: `AUTOMATION_EXPERT_READ_ENABLED`, en `false` por defecto, y `AUTOMATION_EXPERT_READ_TOKEN`. No hay variables de instancia, porque salen del manifiesto.
- Sigue la convención del copiloto en los cinco lugares de `app.py`: `Settings`, `from_env`, la validación al arrancar, las rutas registradas adentro del `if` del flag y `PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES` (`app.py:176-243`, `:651`, `:1694-1720`, `:2948-2986` y `:5713-5732`). Además toca `.env.example`, `compose.yaml` y `tests/test_deployment_config.py`, con el default en `false`.
- Si el flag no entra a `PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES`, ATT1 no arranca con el flag prendido (`ATT1 runtime capabilities are not portable`, `app.py:2839-2858`).
- Con el flag apagado, las rutas no existen y dan 404.
- Con el flag prendido, el bridge no arranca si falta el manifiesto o PostgREST, o si el token no cumple las reglas de la clave.
- `/ready` suma `automation_expert_read: "enabled"` solo cuando el flag está prendido. Así la activación se verifica por presencia, sin token y sin datos.

**Antes de prenderlo, el profile cumple esto, y una prueba del profile lo verifica:**
- `SLACK_ALLOWED_USERS` con los IDs de Brayan y de Dan. Sin lista, Hermes niega, y eso está bien (`gateway/authz_mixin.py:497-502`).
- `allowed_channels` con un único valor centinela que no sea un canal real. Vacío no sirve: en el adaptador de Slack, vacío quiere decir «todos los canales» (`plugins/platforms/slack/adapter.py:9194-9209`). Si alguien invita la app a un canal y Brayan o Dan la mencionan ahí, la historia de un lead saldría en el canal, que es lo que prohíbe el ADR-0017 (`docs/decisions/0017-central-slack-operations-connector.md:19-20`). El centinela deja afuera también los mensajes directos de grupo: Hermes trata solo al mensaje directo 1:1 como privado, y a uno de grupo le aplica `allowed_channels`, como a un canal (`plugins/platforms/slack/adapter.py:6313-6321` y `:6447-6450`).
- `unauthorized_dm_behavior: ignore`.
- Que solo Dan pueda usar los comandos de administración, como `/model` (`allow_admin_from`, a confirmar en el adaptador de Slack). Si no, un usuario permitido puede cambiar el modelo, y lo que devuelve el bridge termina en un proveedor sin ZDR.
- Un modelo con ZDR.
- Las plataformas del profile llevan solo `automation_expert_read`. Con esa lista, Hermes no suma la terminal, la memoria, las tareas programadas ni la delegación (`hermes_cli/tools_config.py:2816`). Sin ella, Slack trae acceso total (`toolsets.py:516-517`).
- La revisión en segundo plano, apagada (`auxiliary.background_review.enabled: false`, `agent/background_review.py:291-300`).

Si algún día se permite un canal, la etiqueta pasa a ser solo el número.

### Exposición

- Las rutas van bajo `/internal/automation-expert/v1/`. Nunca van bajo `/internal/operator/correlations/`, porque el Traefik de ATT1 publica ese prefijo a internet para el conector de Slack (`despliegue/traefik/setter-att1.yaml:11` y `:16`). Ningún router apunta al prefijo nuevo.
- Para verificarlo hay dos pruebas. Desde afuera del servidor, un pedido a la ruta nueva por el host público de ATT1 tiene que dar 404 del proxy. Y un `grep` de `setter-att1_att1-bridge` en `/etc/easypanel/traefik/config/` del VPS, solo leyendo, no tiene que encontrar un router hacia el prefijo nuevo.
- Adentro del servidor, el token es la única barrera. El bridge escucha en `0.0.0.0:8000` (`Dockerfile:24`) y está en tres redes: `interna`, `easypanel` y `hermes` (`stack.yaml:83-88`). Cualquier contenedor de `easypanel` llega directo al bridge, sin pasar por Traefik. El Hermes de ATT1 está solo en `hermes` y llega por `http://att1-bridge:8000`: el `/health` dio 200 el 2026-10-14 a las 22:54Z. Como segunda barrera se podrían aceptar estas rutas solo desde la subred de `hermes`; es opcional y está a medir.
- El Automation Expert contesta solo por mensaje directo, a Brayan y a Dan. Es decisión de Dan del 2026-10-15, que confirmó la recomendación del análisis de la Etapa 1 del 2026-10-14, y la configuración del profile de arriba es la que lo hace cumplir.
- Un canal de Slack, aunque sea privado, uno por persona o uno solo con Brayan y Dan, es un cambio de configuración con su propia aprobación: suma el ID de ese canal a `allowed_channels`, y la etiqueta pasa a ser solo el número. Un canal público, abierto a todo el workspace, no entra con este contrato: recibiría la historia de los leads, y el ADR-0017 dice que el canal no debe recibir PII, cuerpos de conversación ni payloads de proveedores (`docs/decisions/0017-central-slack-operations-connector.md:19-20`, «Contexto»). Si se quiere, va como decisión aparte, que tiene que resolver antes ese choque.

### Dónde queda lo que devuelve el bridge

- Hermes guarda la sesión con los resultados de las herramientas. Su revisión en segundo plano relee cada turno y escribe en la memoria y en las skills del profile, que se inyectan en las sesiones siguientes (`agent/background_review.py:1-16`). Así, un nombre de pila con su compra podría quedar «recordado» y aparecer en otra charla.
- El volumen de Hermes entra al respaldo de R2 desde el 2026-10-14 (`despliegue/respaldo/README.md:11` de la instancia), que tiene un candado de 90 días (`:26`): lo que entra no se puede borrar aunque la persona pida la baja.
- Todo pasa además por el proveedor del modelo.
- El molde pide lo contrario: la identidad no se incorpora a metadata, replay ni sesiones locales (`operator-correlation-review-v1.md:155-156`).

La regla: lo que devuelve el bridge vive solo en la sesión. El profile no tiene los toolsets de memoria ni de skills, tiene apagada la revisión en segundo plano y usa un modelo con ZDR. Sus sesiones se guardan poco tiempo y quedan fuera del respaldo (decisión de Dan del 2026-10-15). Lo que no se pueda cumplir queda escrito como riesgo aceptado por Dan. Sacar las sesiones del respaldo se acuerda con la Etapa 0 bis, porque su respaldo suma hoy todo el volumen de Hermes.

### Datos personales

- Nunca salen el teléfono, el mail (tampoco enmascarados), el nombre completo, el contenido de los mensajes, la URL de un link, la nota privada de una derivación ni un payload crudo. La lista de columnas está en «Lo que nunca sale de la base», y la de los campos de Chatwoot, en «La parte de Chatwoot».
- La función arma la respuesta con claves nombradas (`jsonb_build_object`), nunca con `to_jsonb` de una fila entera. El bridge valida la forma antes de contestar.
- De Chatwoot, el bridge se queda con la hora, el tipo de autor, el estado de entrega, si es una plantilla de la base, el `id` del mensaje y si es una nota privada (`private`). Todo lo demás lo descarta adentro del proceso, antes de armar la respuesta: el contenido; los adjuntos (`attachments`), donde un audio de la persona trae su transcripción (`transcribed_text`) y los links al archivo (`data_url` y `thumb_url`); y el nombre, el mail y el teléfono, que vienen en el `sender` de cada mensaje y en el `meta.contact` de la respuesta. Arma la proyección copiando campo por campo, nunca pasando un mensaje entero, y no escribe en el log nada de lo que descarta. Este recorte pasa en el bridge y no en PostgreSQL, porque Chatwoot no está en la base.
- Todo valor que no sale de un catálogo pasa por una forma fija antes de salir. Para los motivos es `^[a-z][a-z0-9_:]{0,79}$`. Para las refs de landing y de oferta, la forma que tienen en el manifiesto. Si no la cumple, sale `null` con su hueco, nunca el texto.
- Ningún dato de una persona viaja en una URL, ni del plugin al bridge ni del bridge a PostgREST. uvicorn escribe en el log del bridge la URL de cada pedido, porque el contenedor arranca sin `--no-access-log` (`Dockerfile:24`). Por eso la búsqueda va por POST y las URL llevan solo ids opacos.
- Una entrada con forma de teléfono o de mail se rechaza con 422 `identifier_not_allowed`. El bridge ya tiene esas formas en `followup_discount.py:83-84`, pero la del teléfono exige el `+` y no admite separadores. Hay que ampliarla: se sacan los espacios, los guiones, los puntos y los paréntesis, y siete dígitos o más, con o sin `+`, es un teléfono. Un número de conversación de siete dígitos o más también se trata como teléfono.
- Las rutas validan los parámetros a mano, como el copiloto con `case_id: str`, y devuelven 422 con códigos cerrados. Con parámetros tipados, FastAPI devuelve el valor recibido en el cuerpo del 422, y `app.py` no tiene un manejador propio de `RequestValidationError`. El plugin descarta los cuerpos de error (`tools.py:94-99`), pero el bridge no se apoya en eso.
- El log del bridge anota solo `error_type`, nunca el cuerpo, la llave de búsqueda ni el token. La línea de acceso lleva solo ids opacos.
- `auto_explain` escribe en el log de att1-db los parámetros completos de toda sentencia que tarde 10 s o más (`auto_explain.log_min_duration=10000` y `log_parameter_max_length=-1`), y la búsqueda manda el nombre como parámetro. Hoy no llega a pasar, porque `authenticator` corta a los 8 s. La regla: no subir el `statement_timeout` de ese rol. La otra opción es poner `auto_explain.log_parameter_max_length` en 0 en la instancia.
- A Slack llegan el nombre de pila y el número de conversación (decisión de Dan del 2026-10-15). Lo que la persona escribe en el mensaje directo queda en Slack y en la sesión de Hermes, y este contrato no lo cubre.

### Huecos

- Lo que el sistema no guarda llega en `gaps[]`, con un código de la lista de «Lo que el lector no sabe». El agente lo nombra tal cual y no lo rellena.
- Lo deducido sale como un momento con `certainty: "inferred"`, y la base de la deducción va en `evidence`.
- Nada se recorta en silencio: si la respuesta no entra en los límites, da error.
- Lo que no se pudo leer de Chatwoot tampoco se recorta en silencio, pero no da error: la historia sale con la parte de la base y el hueco `chatwoot_not_read`.

### Errores y límites

Errores:
- Con el flag apagado: 404, porque la ruta no existe.
- Sin Bearer o con otro: 401 `automation_expert_authentication_required`, con `WWW-Authenticate: Bearer`. La respuesta no dice si la ruta existe.
- Con un parámetro inválido: 422 (`invalid_flow_key`, `invalid_lead_ref`, `invalid_lookup`, `identifier_not_allowed`, `limit_out_of_range` o `invalid_cursor`).
- Con un flujo o un lead que no existe, o que no se ata a esta instancia: 404 (`flow_not_found` o `lead_not_found`). No distingue entre los dos casos.
- Con una falla de la base, una forma inválida, una respuesta demasiado grande o demasiados pedidos a la vez: 503 (`automation_expert_read_unavailable`, `automation_expert_response_too_large` o `automation_expert_busy`). El log anota solo `error_type`.
- Una falla de Chatwoot no da error: la historia sale igual, con su hueco (ver «Huecos»).

Límites (son una propuesta, falta medirlos con datos reales):
- Flujos: hasta 20, sin paginar, con un cuerpo de hasta 256 KiB.
- Historia: `limit` de 1 a 100, 50 por defecto, con un cursor opaco que guarda el momento y su id. Con dos fuentes, el bridge ordena juntos los momentos de la base y los de Chatwoot, por su hora y su id, y recién ahí corta la página. La función devuelve la parte entera de la base y el bridge corta la página (1.1.0). `upcoming` llega hasta 20 y siempre completo. Una búsqueda devuelve hasta 5 candidatos; con más, `too_many_candidates`.
- En vuelo: hasta dos pedidos a la vez en estas rutas, y con más, 503 `automation_expert_busy`. El agente no satura nada, porque Hermes corre en serie las herramientas de un plugin (`agent/tool_dispatch_helpers.py:48-61` y `:175-176`). Pero un token filtrado podría ocupar las 10 conexiones de PostgREST que comparte todo el bridge (`PGRST_DB_POOL`, `stack.yaml:51`).
- Chatwoot: hasta 5 s para toda la lectura de una historia, y hasta 10 páginas de 20 mensajes por conversación (ver «La parte de Chatwoot»).
- Tiempos: la función apunta a 5 s, y el corte que existe hoy es el `statement_timeout` de 8 s de `authenticator`. El cliente de PostgREST del bridge corta a los 10 s (`supabase.py:1579-1584`) y el plugin a los 15 s. Chatwoot se lee después de la base, con su propio tope: con los 8 s de la base y los 5 s de Chatwoot, la historia contesta antes de los 15 s del plugin.

### El plugin

- Se llama `automation-expert-read`, y su toolset, `automation_expert_read`. Tiene dos herramientas: `list_flows(flow_key?)` y `lead_history(conversation? | link? | name? | lead_ref?, limit?, cursor?)`.
- Copia el cliente del molde (`profiles/client-copilot/plugins/operator-correlation-review/tools.py`). Lee la URL y el token del `.env` del profile (`AUTOMATION_EXPERT_API_URL` y `AUTOMATION_EXPERT_API_TOKEN`). No sigue redirecciones (`:34-39`) y corta a los 15 s. Mapea los errores a códigos cerrados (`:81-104`) y valida los parámetros antes de salir a la red (`:198-218`).
- Tiene dos cambios respecto del molde. El primero: acepta `http` solo hacia el nombre exacto del bridge que le da la instalación por variable, con puerto explícito. En ATT1 es `http://att1-bridge:8000`. En cualquier otro caso exige `https`, como el molde (`:42-62`). No alcanza con «un nombre de una sola etiqueta»: en el contenedor de Hermes de ATT1, un nombre que Docker no conoce se busca después en el dominio de la tailnet (`ndots:0`), y si ahí hay una máquina con ese nombre, el plugin le mandaría el token por `http`. El bridge también usa una lista exacta para hablarle a Hermes (`app.py:843-850`). El segundo cambio: rechaza una respuesta cuyo `contract_version` no tenga major 1, con el código `automation_expert_contract_unsupported`.
- Como defensa extra, tapa lo que parezca un mail o un número largo, pero solo en los campos de texto libre: `label`, `explanation` y el `what` y el `why` de los huecos. Nunca toca los campos que ya se validaron como uuid, fecha o número de conversación. Si tapara siete dígitos seguidos en todo, rompería cerca del 15 % de los uuid, y el agente no podría pedir esa historia.
- No lleva hooks en la 1a. Si alguno hiciera falta y tiene que frenar, devuelve `block` con un `message`: en `pre_tool_call`, Hermes 0.21.0 acepta `block`, `approve` y `modify`, ignora en silencio cualquier otro valor y descarta un `block` sin `message` (`hermes_cli/plugins.py:6650-6673`). Por eso el `deny` del copiloto no hace nada. Al copiloto no le afecta, porque su herramienta vuelve a validar el pedido (`tools.py:240-244` y `:253-266`).
- El agente comercial nunca habilita el plugin, y lo lista como conocido y apagado: un toolset de plugin que no figura en `known_plugin_toolsets` arranca prendido (`hermes_cli/tools_config.py:2840-2858`). Una prueba verifica que, con el plugin instalado, el comercial siga con cero herramientas, como pide el análisis de la Etapa 1 del 2026-10-14.
- Como defensa extra, el plugin se niega a correr fuera del profile `automation-expert`. Falta medir cómo sabe en qué profile corre.

### Fuera de este contrato

- El dibujo del flujo en HTML: lo arma la skill de la 1a que dibuja un flujo, a partir de `definition`.
- Proponer o cambiar un flujo: va en el contrato del ciclo, en la 1b.
- Crear e instalar la app de Slack. Dan confirmó el 2026-10-15 que el Automation Expert se conecta a Slack y habla solo por mensaje directo con Brayan y con él. Las condiciones del profile para prender el flag sí están en este contrato.
- La excepción al ADR-0017 por esa app propia. El ADR deja el bot token de Slack solo en el conector central (`docs/decisions/0017-central-slack-operations-connector.md:32-33` y `:55`). La excepción va a Juan, cerrada y con default, como la de las alertas de ATT1 (`despliegue/alertas/README.md:27` de la instancia), y no frena la 1a.
- Lo que la persona escribe en el mensaje directo.
- Dos hallazgos para el producto, que este contrato no resuelve:
  - `anon` y `authenticated` tienen `SELECT` sobre 13 tablas: 11 centrales y las 2 del arranque proactivo (`proactive_lead_bootstrap_targets` y `proactive_lead_identity_bootstrap_commands`). `anon` también sobre `conversation_reactivation_events` y `conversation_resume_events`. Todas tienen RLS y ninguna tiene policies (medido en att1-db el 2026-10-15). Hoy las protegen solo RLS sin policies y que PostgREST esté en la red interna.
  - 16 de las 194 plantillas del motor están `failed` en Chatwoot (del 2026-10-06 al 2026-10-14), y att1-db las cuenta como aceptadas, porque el bridge no escucha `message_updated`. Chatwoot sí guardó el código de Meta (corregido el 2026-10-15 a las 20:07Z, ver «Lo que fijó la implementación (1.1.0)»).

## Lo que el lector no sabe

Cada hueco tiene un código fijo:
- `start_rejection_not_persisted`: el motivo de un arranque rechazado, cuando no se puede deducir. Queda solo en el log del bridge (`durable_request_start_rejected`, `worker.py:1554`). Se deducen la versión, la pausa o el tope diario. Si nada de eso lo explica, va el hueco. Hoy los rechazos quedan solo en el log, y la 1a no suma esa escritura: guardarlos va con la 1b, y ahí el hueco desaparece (decisión de Dan del 2026-10-15; ver «Solo lectura»).
- `case_creation_failure_not_persisted`: por qué no se pudo crear el caso de un carrito (`create_recovery_case_failed`). Hay 4 en ATT1, y ninguno coincidió con un caso abierto de la misma persona. La causa queda solo en el log del bridge. El agente dice «no arrancó por una falla al crear el caso; la causa queda en el log».
- `chatwoot_not_read`: la parte de Chatwoot de una conversación, es decir, la entrega de sus plantillas y su actividad. Sale cuando Chatwoot no contesta, contesta con error o pasa el tope de tiempo, y cuando la conversación pasa el tope de páginas o trae mensajes de otra bandeja. Va con `scope: "lead"`, y dice qué conversación y por qué. La historia sale igual con la parte de la base, y el agente no dice si una plantilla llegó ni si no llegó.
- `meta_failure_reason_not_persisted`: por qué Meta no entregó una plantilla que quedó en `failed`, cuando Chatwoot no guardó el código. Va con `scope: "lead"`. Desde la 1.1.0, si `content_attributes.external_error` empieza con el código de Meta, sale solo ese código en dígitos (`meta_error_code`, `^\d{3,6}$`) con una frase fija del bridge, y el resto de `content_attributes` se descarta como antes. El agente no supone una causa que el código no dice.
- `unanswered_reply_not_persisted`: por qué el agente no contestó un mensaje de la persona. Con Chatwoot se ve que escribió y que nadie le contestó, pero no el porqué. Pasa, por ejemplo, cuando tenía otro paso pendiente y su respuesta no se adoptó (regla 10 de «Lo que sale de afuera de la base»). La base no lo guarda.
- `agent_release_not_persisted`: con qué versión del prompt y del conocimiento contestó el agente cada turno. La columna existe (`agent_turn_provenance.release_id`), pero en ATT1 está vacía en los 91 turnos.
- `followup_skip_not_persisted`: por qué el cupón salteó a alguien. Se guardan solo los envíos que salieron (16 filas, todas `sent`). Los salteos van resumidos a `/ready` (`followup_discount.py:89-99`). El lector deduce el motivo con las condiciones del cupón y los mensajes de Chatwoot, o con los turnos del agente si Chatwoot no se pudo leer. Si no alcanza, declara el hueco.
- `hotmart_ignored_not_persisted`: los eventos de Hotmart que el bridge ignora al recibirlos no quedan en la base.
- `unattached_hotmart_event`: un evento de Hotmart que la identidad no ata a un solo contacto, porque sus datos llevan a dos contactos o porque un mismo dato lleva a varios (decisión de Dan del 2026-10-15). Va en la historia de cada contacto al que podría pertenecer, sin nombrar al otro. Medido el 2026-10-15 sobre los 344 eventos con identidad guardada, no pasa en ninguno. Los eventos cuyos datos no llevan a ningún contacto, hoy las 29 compras con `purchase_correlation_contact_not_found`, no son de ningún lead y no entran en ninguna historia. Con esa regla se atan a la persona los 93 carritos y pagos fallidos que no arrancaron, entre ellos los 24 que el correlador dejó `unmatched` (`identity_not_found`) y los 7 que marcó `conflict` (`email_phone_conflict`), y las 32 compras sin caso. Para entrar a la historia, además, cada uno tiene que atarse a la instancia (ver «Los eventos de Hotmart», en «Cómo se ata cada fuente a la persona y a la instancia»).
- `payment_slip_not_attached`: si la persona imprimió una ficha de pago (OXXO, Mercado Pago, Yape o Nequi) o se le venció. La base guarda las fichas sin correlación, sin caso y sin fila de identidad: medido en att1-db el 2026-10-15 a las ≈02:13Z, 6 impresas y 2 vencidas. Las 2 vencidas llegaron entre las 22:51Z del 2026-10-14, cuando se midió para el esquema y no había ninguna, y las ≈02:13Z del 2026-10-15. Va en toda historia: para saberlo, se mira en Hotmart.
- `purchase_channel_not_persisted`: con qué link o por qué canal compró, cuando no fue con un link nuestro. Hay 47 intenciones `purchased` y solo 2 links comprados. La hora sí se guarda, en el `received_at` del `PURCHASE_APPROVED`, y sale cuando el evento se ata a la persona por identidad (decisión de Dan del 2026-10-15) y a la instancia.
- `env_value_unversioned`: un valor que sale de una variable o del manifiesto no tiene historia en la base. Se sabe cuánto vale hoy, pero no desde cuándo. Es el caso del cupón: sus tablas de política tienen 0 filas en ATT1, y su configuración vive en el manifiesto y en las variables `CONVERSATION_FOLLOWUP_*`.
- `unknown_reason_code`: un motivo que el catálogo no conoce, o que no cumple la forma de los códigos. El momento sale igual, con `explanation` y `reason` en `null`. Si no cumple la forma, también `reason_code` va en `null`.
- `unknown_event_kind`: un tipo de evento que el catálogo de momentos no conoce. Sale como `other`.

## Qué lee y de dónde

### Por qué hacen falta funciones en la base

El bridge lee por PostgREST como `service_role`. Ese rol tiene `BYPASSRLS`, así que lo que lo frena son los permisos y no RLS. Leer las tablas directo no alcanza, por cuatro razones medidas:
1. `service_role` no puede leer justo las tablas que contestan las dos preguntas. No tiene acceso a ninguna `pilot_*`, ni a `portable_precheckout_first_contact_plans`, `conversation_followup_events`, `human_handoff_requests`, `commercial_cases`, `checkout_link_issuances`, `purchase_intents` o `hotmart_purchase_intent_correlations`. Darle acceso rompería el modelo de permisos que el producto mantiene a propósito.
2. Las tablas que sí puede leer (`recovery_cases`, `scheduled_actions`, `conversation_events` y `webhook_events`) no tienen columna de instancia.
3. Esas tablas guardan datos personales en campos `jsonb` (`payload`, `context`, `data` y `metadata`). Conviene recortarlos adentro de la base, como hace el copiloto.
4. Diez lecturas sueltas son diez fotos tomadas en momentos distintos, y una función da una sola foto de la base. Chatwoot queda afuera de esa foto: se lee después y aparte (ver «La parte de Chatwoot»).

Las funciones de lectura que ya existen cubren pedazos sueltos: el estado del piloto, el modo de audiencia, el binding del aliado y la política del cupón. Ninguna arma los flujos ni la historia. Dos de ellas no sirven: `get_followup_execution_context` devuelve el teléfono y el mail del comprador, y `get_portable_recent_payment_failure_v1` se pide por teléfono.

### Las tres funciones

Van en una sola migración, `20261015000200_automation_expert_read_v1.sql`. Las tres reciben `p_instance`, la marca de la instancia que arma el bridge con el manifiesto y sus variables, nunca con el pedido. Su forma la fija «Lo que fijó la implementación (1.1.0)».
- `read_automation_expert_flows_v1(p_instance jsonb) returns jsonb`: devuelve las versiones, el estado, los topes, el consumo del día, lo observado en 7 días, la audiencia de cada scope y los eventos sin flujo. Por dentro reusa `get_lancemos_pilot_runtime_status`, `get_portable_precheckout_pilot_runtime_status` y `get_lancemos_pilot_scope_audience_mode`, que son `STABLE`. La audiencia sale como modo y cantidad, por ejemplo `{"mode": "single_test_phone", "value_present": true}`, nunca con los miembros de la cohorte ni con un teléfono de prueba.
- `find_automation_expert_lead_v1(p_instance jsonb, p_conversation bigint, p_issuance_ulid text, p_name text) returns jsonb`: recibe exactamente una llave y devuelve un `lead_ref`, hasta 5 candidatos o `too_many_candidates`.
- `read_automation_expert_lead_history_v1(p_instance jsonb, p_lead_ref uuid) returns jsonb` (1.1.0: sin `p_limit` ni `p_cursor`, porque la página la corta el bridge): devuelve la parte de la base de la historia: la línea de tiempo, `upcoming`, `now` y los huecos. Devuelve además, con cada plantilla, el id de su mensaje en Chatwoot y, con el contacto, los números de sus conversaciones atadas a la instancia. Con eso el bridge suma la parte de Chatwoot.

Las tres cumplen lo mismo:
- Son `STABLE SECURITY DEFINER` y fijan `search_path = pg_catalog, public, pg_temp`, como las del copiloto (`operator-correlation-review-v1.md:70`).
- Hacen un `REVOKE` explícito de `public`, `anon` y `authenticated`, y dan `GRANT EXECUTE` solo a `service_role`. El `REVOKE` tiene que ser explícito porque los privilegios por defecto de `public` le dan `EXECUTE` a `anon` y a `authenticated` sobre toda función nueva (medido en `pg_default_acl`).
- No suman ningún permiso sobre tablas.

El bridge llama por GET a las dos funciones que no reciben datos de una persona, y por POST a la búsqueda, con la llave en el cuerpo. Así nada personal queda en una URL, tampoco en el gateway de la base.

### Cómo se ata cada fuente a la persona y a la instancia

`lead_ref` es `contacts.id`. Cada fuente entra a la historia solo si se ata a la persona y a la instancia. Lo que no se ata queda afuera y, si corresponde, suma un hueco.
- **Los casos** (`recovery_cases`, con sus acciones, eventos, intentos y `recovery_case_events`): a la persona por `contact_id`; a la instancia por `pilot_recovery_case_bindings` y la bandeja y el producto de su scope.
- **El caso comercial** (`commercial_cases`): a la persona por `contact_id`; a la instancia por su `inbound_scope_key`, comparado con el del manifiesto, o por el caso del que sale.
- **El formulario y la intención** (`purchase_intents`, que no tiene `contact_id`): a la persona por el plan del primer contacto (`portable_precheckout_first_contact_plans.contact_id`), por el binding de audiencia del caso o por un link (`checkout_link_issuances.purchase_intent_id`); a la instancia por `tenant_ref` y `funnel_ref`.
- **Los links** (`checkout_link_issuances`): a la persona por `contact_id`; a la instancia por `chatwoot_inbox_id`.
- **La derivación** (`human_handoff_requests`): a la persona por su conversación; a la instancia por `chatwoot_inbox_id`.
- **La baja** (`contact_opt_out_events`): a la persona por `contact_id`; a la instancia por `canonical_inbox_id`.
- **El cupón y la reactivación:** a la persona y a la instancia por su conversación, que tiene que estar anclada por alguno de los caminos de arriba.
- **Los turnos del agente** (`agent_turn_provenance`): a la persona por `external_conversation_id`, el número de Chatwoot; a la instancia por `scope_ref`, que en ATT1 es `att1-agent-bot`.
- **Los mensajes de Chatwoot:** a la persona por su conversación, que tiene que estar atada por alguno de los caminos de arriba; a la instancia por la bandeja de cada mensaje (`inbox_id`), comparada con la del manifiesto (ver «La parte de Chatwoot»).
- **Los eventos de Hotmart:** si abrieron un caso, por el caso. Si no, como un rechazo al entrar o una compra sin caso nuestro, solo por identidad (decisión de Dan del 2026-10-15). La función compara la identidad normalizada del evento (`hotmart_purchase_intent_event_identities`) con `contact_points`: el mail (`normalized_email`) con los de tipo `email` (`lower(btrim(normalized_value))`), y el teléfono por sus últimos 10 dígitos con los de tipo `phone`, que así resuelve las dos formas del móvil de México. Ata el evento cuando los datos que cruzan llevan a un solo contacto, aunque el otro dato no cruce con nadie. Si llevan a dos contactos, o un mismo dato lleva a varios, el evento va al hueco `unattached_hotmart_event`. A la instancia se atan por su correlación: `hotmart_purchase_intent_correlations.scope_id` apunta a un scope con `tenant_ref`, `funnel_ref` y el producto de Hotmart (`20260820000100_hotmart_purchase_intent_correlation.sql:15-59`). Pero `scope_id` admite nulo (`:59`), y no se midió cuántos de los eventos atados por identidad lo tienen (ver «Lo que se confirma al implementar»). Una compra sin correlación se ata por el producto del evento, comparado adentro de la función con el del manifiesto; falta medir dónde está ese campo en cada tipo de evento. Nunca se devuelven los valores, y el momento sale como `inferred`, con `attached_by: "identity"`.
- **Las fichas:** no se atan por ningún camino. Van como hueco.

Medido en att1-db el 2026-10-15: las 199 conversaciones de ATT1 tienen al menos un ancla de instancia. 194 se anclan por el scope del caso, 60 por el caso comercial entrante, 29 por un link y 13 por el plan del primer contacto. Ninguna queda sin ancla.

### Qué lee cada una

- **La de los flujos** lee `pilot_scope_versions`, `pilot_runtime_controls`, `pilot_control_events` (para saber desde cuándo), `pilot_outbound_request_authorizations` y, desde `20261022000300`, `flow_engine_request_authorizations` con `pool` = `scope_key` (para el consumo del día y los arranques de lo observado: los dos caminos gastan la misma bolsa), `pilot_proactive_contact_caps`, `followup_policy_versions` y `commercial_ally_runtime_bindings`. Para `observed` y `events_without_flow` cuenta además en `webhook_events`, `portable_precheckout_first_contact_plans`, `scheduled_actions`, `conversation_events` y `followup_delivery_attempts`, en la ventana de 7 días, y devuelve solo conteos y medianas.
- **La de la búsqueda** lee, según la llave:
  - para la conversación, `channel_identities.external_conversation_id` y `conversations.commercial_context`;
  - para el link, `checkout_link_issuances.issuance_ulid`;
  - para el nombre, `contacts.full_name` y `lead_first_name_inferences`. Busca con el nombre que le pasan, pero devuelve solo el de pila.

  En los tres casos, el resultado se ata a la instancia antes de salir.
- **La de la historia** lee las tablas del catálogo de momentos y estas, cada una para algo puntual:
  - `contacts`, solo para el nombre de pila y los sí o no de `identity`;
  - `contact_points`, para saber si existe un teléfono o un mail y para comparar adentro la identidad de un evento de Hotmart (decisión de Dan del 2026-10-15);
  - `conversations`, para la pausa;
  - `recovery_case_events`, para atar los eventos a su caso;
  - `agent_turn_provenance`, para la hora, el resultado, el modelo y la versión del bridge de cada turno;
  - para atar por identidad los eventos de Hotmart (decisión de Dan del 2026-10-15), `hotmart_purchase_intent_event_identities` y `hotmart_purchase_intent_scopes`;
  - para sumar la parte de Chatwoot (decisión de Dan del 2026-10-15), el id en Chatwoot de cada plantilla: `messages.external_message_id` en las del motor, y `provider_message_id` en `conversation_followup_events` y `conversation_reactivation_events`.

### Lo que nunca sale de la base

Nunca salen:
- `webhook_events.payload`, `recovery_cases.context`, `scheduled_actions.metadata` y `human_handoff_requests.private_note_body`;
- los valores de `contact_points` y de `hotmart_purchase_intent_event_identities`, y el teléfono, el mail y el nombre completo de `contacts`;
- `channel_identities.external_user_id` y `username`, y `contact_opt_out_events.external_user_id`;
- `scheduled_actions.error_message` y `recovery_cases.identity_resolution_error`;
- `conversation_followup_events.coupon_code` y `failure_reason`;
- `checkout_link_issuances.checkout_url_final`, `original_sck`, `source_value` y `sck_value`;
- `agent_turn_provenance.context_added`, que guarda los campos conocidos del lead, la bandera de derivación y la forma del historial (`20260928000100_agent_prompt_provenance_v1.sql:97-140`);
- el contenido de `messages`.

De `conversation_events.data` salen solo las claves de una lista cerrada, como `decision` y `reason_code`, y pasan por la forma fija de los motivos.

### La parte de Chatwoot

Es decisión de Dan del 2026-10-15. La historia suma lo que la base no tiene de la conversación. De ella, la base guarda la primera respuesta a una plantilla (`inbound_adopted_template_conversation`, 55 en total) y cada turno del agente, con su hora y su resultado (91 turnos en 47 conversaciones, desde el 2026-10-05). 42 de las 55 conversaciones adoptadas tienen turnos. `messages` tiene solo las 194 plantillas del motor, y `conversations.last_inbound_at` está vacío en las 199 conversaciones. La entrega de cada plantilla tampoco está: la guarda Chatwoot, y el bridge no escucha su actualización (`message_updated` no aparece en `src/`). Esta parte la arma el bridge, después de llamar a la función.

**Cómo lee.** Con el cliente que ya tiene, `ChatwootClient.get_conversation_messages` (`src/bridge/chatwoot.py:1536-1605`). Pide `GET /api/v1/accounts/{cuenta}/conversations/{número}/messages`, con el token de API de control del bridge (`CHATWOOT_CONTROL_API_ACCESS_TOKEN`, `app.py:1356-1358` y `:3287-3298`) en el header `api_access_token`. Pagina hacia atrás con `before`, de a 20 mensajes. Es el mismo pedido con el que el bridge arma hoy el historial que le pasa al agente (`app.py:4336-4340`). El plugin nunca ve ese token.

**Qué conversaciones.** Solo las que la función devuelve para el contacto, que ya están atadas a la instancia. Cada mensaje trae su bandeja (`inbox_id`). Si alguno no es la bandeja del manifiesto, esa conversación no entra y suma el hueco. Es la comprobación que hace `validate_conversation_authority` antes de tocar una conversación (`chatwoot.py:1277-1314`), hecha sobre los mensajes y sin otro pedido.

**Qué toma de cada mensaje.** Cuatro cosas:
- la hora (`created_at`);
- el tipo de autor;
- el estado de entrega (`status`: `sent`, `delivered`, `read` o `failed`), solo en los salientes que no son una nota privada;
- si es una plantilla de la base, que se sabe por el cruce de ids de más abajo.

Para clasificarlo usa además `id`, `message_type`, `private` e `inbox_id`, el `id` del remitente solo para compararlo con el AgentBot y, en una nota privada, si el contenido lleva la marca de la derivación (ver «Cómo reconoce a cada autor»). De todo eso, a la respuesta llegan solo el `id` del mensaje, en `evidence`, y `private`, en las notas.

**Qué descarta siempre.** Lo descarta adentro del bridge, antes de armar la respuesta: `content`, `content_attributes`, `source_id`, `attachments`, el `sender` entero salvo su `type` y su `id`, que se usa solo para compararlo con el AgentBot y no sale, y el `meta` de la respuesta de Chatwoot. En un mensaje entrante, `sender` trae el nombre completo, el mail y el teléfono de la persona. En uno del equipo, trae el nombre de quien lo escribió. Y `meta.contact` trae otra vez el nombre, el mail y el teléfono de la persona. Así lo muestra el fixture de esta API de la bandeja 9 (`tests/fixtures/chatwoot_conversation_messages_inbox_9_conv_173_20260926.json`, Chatwoot v4.13.0, leído solo por sus claves). Un mensaje con adjuntos trae además `attachments`. En un audio de la persona, ahí van `transcribed_text`, la transcripción de lo que dijo, y los links al archivo, `data_url` y `thumb_url`. Así lo muestra un fixture de la bandeja 11 (`tests/fixtures/chatwoot_audio_trigger_inbox_11_conv_1_20261006.json`, leído solo por sus claves), y el bridge ya lee esos adjuntos para transcribir audios (`audio_transcription.py:94-102`).

**Cómo reconoce a cada autor.** Con las reglas con las que el bridge arma hoy el historial del agente (`app.py:1869-1886`), más una para la nota de la derivación. Los valores son los cuatro de `messages.actor_type` de la base, `prospect`, `ai_agent`, `human_agent` y `system` (`supabase/baseline/20260803_public_schema.sql:97`), más `other`:
- `prospect`, la persona: un entrante (`message_type` 0) con `sender.type` `contact`;
- `ai_agent`, el agente: un saliente (`message_type` 1) con `sender.type` `agent_bot` y el `sender.id` del AgentBot de la instancia (`CHATWOOT_AGENT_BOT_ID`, `app.py:1353`);
- `system`, el propio bridge: una nota privada (`message_type` 1, `private` en `true`) cuyo contenido lleva la marca `[supportmagician-handoff:`. Es la nota que el bridge deja al derivar, con esa marca al final (`worker.py:615-627`; `chatwoot.py:1456` y `:1490-1510`). El bridge mira la marca antes de descartar el contenido, y la marca no sale;
- `human_agent`, el equipo: cualquier otro saliente (`message_type` 1 o 3) con `sender.type` `user`;
- `other`: cualquier otro saliente, por ejemplo de otro AgentBot. No dice de quién es.

Lo que el bridge publica con su token de control figura en Chatwoot como de Dan, porque ese token es el de su usuario (`docs/decisions/0024-la-revision-diaria-en-una-instancia-autohospedada.md:60`; `docs/operations/2026-10-05-att1-entrante-e2e.md:96-97`). La marca es lo único que distingue la nota de la derivación. Un mensaje de ese usuario sin la marca puede ser de Dan o del bridge, y sale como `human_agent`.

Un mensaje cuenta aunque no traiga texto, como un audio de la persona. El historial del agente saltea los mensajes sin texto (`app.py:1864-1865`), pero la proyección no copia ese filtro: si lo copiara, `contesto` y la ventana del cupón se equivocarían.

**Las notas privadas y la actividad.** Una nota privada (`private` en `true`) sale como `conversation_activity`, con `private: true` y sin texto: de `system` si lleva la marca de la derivación, y si no, de `human_agent`. Así el agente no dice que alguien del equipo dejó una nota cuando la dejó el bridge al derivar. La derivación misma la cuenta `handed_off`, desde la base. Los mensajes de actividad (`message_type` 2, sin remitente) no salen. Son el registro de Chatwoot, como una asignación o un cambio de estado, y su texto lleva el nombre de la persona del equipo: la captura del fixture lo tuvo que tapar. Sin el texto no dicen nada. En la bandeja 11 hubo 150 en los 14 días previos al 2026-10-15.

**Cómo se pega la entrega a cada plantilla.** Por el id del mensaje en Chatwoot, que la base ya guarda: `messages.external_message_id` en las plantillas del motor, y `provider_message_id` en las del cupón (`followup_discount.py:1058-1083`) y de la reactivación. La función devuelve ese id con cada plantilla, y el bridge suma un `meta_delivery` con el estado que tiene ese mensaje en Chatwoot. Medido el 2026-10-15: las 194 filas de `messages` de att1-db son las plantillas del motor, todas con `actor_type` `ai_agent`, `delivery_status` `accepted` y `external_message_id`, y las 194 son ids de mensajes de la bandeja 11. Según Chatwoot, 118 están `read`, 40 `delivered`, 20 `sent` y 16 `failed` (del 2026-10-06 al 2026-10-14). La historia va a mostrar esas 16 como `meta_delivery` en `failed`, con el código de Meta que guardó Chatwoot (1.1.0). att1-db las cuenta como aceptadas, porque el bridge no escucha `message_updated` y `messages.delivery_status` nunca pasa de `accepted`, aunque la columna admite `delivered`, `read` y `failed` (`supabase/baseline/20260803_public_schema.sql:92-100`). El cruce del cupón no se midió.

**Qué estado, y de cuándo.** Chatwoot da el estado de hoy de cada mensaje, no la hora en que cambió: la API no trae una hora de actualización (las claves de los dos fixtures). Por eso `meta_delivery` va con la hora del envío y dice el estado que tenía cuando el bridge lo leyó. `sent` quiere decir que Meta no confirmó la entrega: no dice que llegó ni que falló. En la bandeja 11 hay estados `delivered` o `read` recién desde el 2026-10-05 a las 11:41Z (311 mensajes al 2026-10-15), así que en un mensaje anterior `sent` no permite distinguir nada.

**Los momentos.** Cada plantilla de la base suma un `meta_delivery`, y cada otro mensaje que no es de actividad, un `conversation_activity`. Los dos llevan `certainty: "stored"`, `attached_by: "conversation"` y `evidence` con `{"table": "chatwoot.messages", "id": <id del mensaje>}`.

**Cuando Chatwoot no contesta.** La historia sale igual con la parte de la base, y la conversación que no se pudo leer suma el hueco `chatwoot_not_read`. Pasa si Chatwoot no contesta, contesta con error o pasa el tope de tiempo, si la conversación pasa el tope de páginas o si trae mensajes de otra bandeja. Una conversación entra entera o no entra, nunca a medias, como ya permite el cliente (`require_history_boundary`, `chatwoot.py:1597-1598`). Para la ventana del cupón, `coupon_not_sent` y el motivo `contesto`, el lector vuelve a los turnos del agente.

**Los topes, a confirmar en el claim.** La propuesta es de 5 s para toda la lectura de Chatwoot de una historia, y de hasta 10 páginas por conversación, es decir, 200 mensajes. El lector usa su propio tope, porque el cliente de hoy corta a los 15 s por pedido y lee hasta 100 páginas (`chatwoot.py:1554-1561`). Con los 8 s de la base, la historia contesta antes de los 15 s del plugin. No se midieron el largo de las conversaciones de la bandeja 11 ni lo que tarda Chatwoot en contestar.

**Las pruebas.** Son la 13 y la 14 de «Las pruebas que fija el contrato».

### Lo que sale de afuera de la base

Para los flujos, el bridge suma lo que la base no tiene. Cada valor lleva su procedencia.
- **Del manifiesto:** `[flujos]`, `[plantillas]` y la zona.
- **De las variables:** la configuración del cupón y de la reactivación, solo desde una lista cerrada. Entran las esperas, los horarios, los topes, los nombres de plantilla, y el código del cupón y el nombre del producto (`CONVERSATION_FOLLOWUP_COUPON_CODE` y `CONVERSATION_FOLLOWUP_PRODUCT_NAME`, `app.py:1193-1196`), que la persona ya ve en la plantilla. Nunca entra un secreto. De los teléfonos de prueba (`CONVERSATION_FOLLOWUP_ONLY_PHONE` y `PRECHECKOUT_TEST_PHONE_E164`) y del JID permitido (`ALLOWED_WHATSAPP_JID`) dice solo si están puestos (`app.py:416-418`, `:889` y `:999-1004`).
- **Del código:** las reglas fijas de convivencia. Son la lista del esquema, «Lo que pasa hoy en ATT1, y el campo que lo escribe»:
  1. el carrito y el pago fallido le ganan al primer contacto, y no al revés, aunque no manden nada (`20261001000200_portable_precheckout_first_contact.sql:296-313`);
  2. de día, a quien le falla el pago y después abandona le llegan las dos plantillas, con unos 40 minutos de diferencia, y cada reintento de tarjeta posterior al envío abre otro caso y manda otra plantilla (aceptado por Dan el 2026-10-09, al prender el pago fallido en ATT1; esquema, «Lo que pasa hoy en ATT1, y el campo que lo escribe», punto 2);
  3. de noche sale una sola: el carrito no arranca mientras el pago fallido espera a las 09 (`20260930000300_pilot_scope_audience_mode.sql:837-846`). Sale del código y no tiene un caso real;
  4. al revés no pasa: un pago fallido que llega con un carrito en curso abre su propio caso (`20260904000100_att1_payment_failure_single_initial_contact.sql:87-98`). También sale del código y falta medirlo;
  5. el carrito y el pago fallido comparten los topes;
  6. hay un solo primer contacto en curso por persona, y ninguno en las 24 h siguientes a un envío aceptado o incierto (`20261001000200_portable_precheckout_first_contact.sql:836-862`);
  7. el cupón sale una vez por conversación, no por persona, y solo en las conversaciones adoptadas;
  8. la compra corta la recuperación;
  9. una conversación derivada o pausada frena distinto a cada flujo. El primer contacto mira todas las conversaciones del contacto. El carrito y el pago fallido miran solo la de su caso, que no existe antes del primer envío. El cupón mira su conversación y la derivación de su caso. La reactivación mira la derivación de su caso y entra a propósito en las pausadas (esquema, «Lo que pasa hoy en ATT1, y el campo que lo escribe», punto 9). La proyección dice cuál mira qué;
  10. la respuesta a una plantilla no se adopta mientras la persona tenga otro paso pendiente.

  La proyección las carga donde el esquema las pone: `prioridad.le_gana_a`, `prioridad.no_arranca_mientras`, `topes.bolsa`, `topes.repeticion`, `entra.contesto_a` y `termina.si`. Cada una lleva su procedencia y su línea: `code`, salvo la bolsa, que hoy es el `scope_key` de la base (`database`). La 2 y la 4 son lo que pasa con esos campos, sin uno propio. La 9 y la 10 no tienen campo: el esquema las deja en código, en el chequeo 4 de la puerta y en la regla de adopción.
- **Un dato medido:** Hotmart avisa el carrito entre 30 y 40 minutos después de lo último que la persona hizo en el checkout (medido en ATT1 el 2026-10-06; esquema, «Qué lo dispara»). Va en el disparador del carrito, con su fuente.

### Los compañeros de la migración

Van en el mismo claim que la migración:
- `tests/sql/followup_engine/validate_acl_hardening.mjs`: las tres firmas se suman a la lista, y el conteo pasa de 124 a 127 (`:243`), con su comentario en el historial (`:225-243`).
- `scripts/supabase_acl_inventory.sql` y `scripts/supabase_schema_inventory.sql`, con la huella de la migración.
- La allowlist de `verify_effective_acls` (`tests/sql/followup_engine/validate_handoff_postgres.py:432`): la función de la historia entra con el comentario «solo lee esa tabla», porque lee `human_handoff_requests`.

La migración va después de la última del repo, `20261015000100`, que entró con #232 el 2026-10-15 a las 01:56Z. Si Dan aprueba la propuesta del 2026-10-09 que cancela el envío que supera el tope por persona, la migración de esa propuesta va antes, y esta, detrás.

### Las pruebas que fija el contrato

1. Una instancia no ve los flujos ni los leads de otra. Se prueba con dos instancias que comparten el tenant `lancemos` y tienen bandejas y productos distintos, en la misma base. Es la primera prueba que se escribe.
2. Las tres funciones son `STABLE`, solo las ejecuta `service_role`, y corren adentro de `begin transaction read only` sin error.
3. Ninguna respuesta lleva teléfono, mail, nombre completo, contenido ni la URL de un link. Se prueba con un valor canario en cada columna `text` y `jsonb` de cada tabla que leen las funciones: ninguno puede aparecer en la salida.
4. El bucle del lease sale como un solo momento, con su `count`.
5. Un `reason_code` desconocido no tumba la historia, y uno que no cumple la forma sale en `null`.
6. Con el flag apagado, las rutas dan 404. Con el manifiesto de ATT1 y el flag prendido, el bridge arranca. Con el token igual a cualquier otro secreto del bridge, no arranca.
7. Una entrada con forma de teléfono o de mail da 422, en el plugin y en el bridge, también con espacios, guiones o paréntesis.
8. El plugin no sigue redirecciones, rechaza `http` hacia cualquier host que no sea el nombre configurado del bridge, mapea bien el 401, el 404, el 422 y el 503, y no tapa los uuid.
9. Los casos A y B, como fixtures anonimizados y con las horas relativas, con su parte de la base y su parte de Chatwoot, dan la historia esperada. Mientras el primer contacto del caso B sigue vivo, `upcoming` lo muestra con `expected` en `cancel`.
10. Cada código de la lista de motivos tiene su `reason` y su frase.
11. Desde afuera del servidor, la ruta nueva da 404 del proxy, y la configuración de Traefik del VPS no tiene un router hacia el prefijo nuevo.
12. Con el plugin instalado, el comercial sigue con cero herramientas. El profile del Automation Expert tiene solo `automation_expert_read`, sin memoria ni skills, con la revisión en segundo plano apagada, los dos usuarios permitidos y el centinela de canales.
13. La proyección de Chatwoot se prueba con un fixture capturado de la bandeja 11 que tenga mensajes `delivered`, `read` y `failed`, un audio de la persona, una nota privada del equipo, la nota de una derivación y mensajes de actividad. Hoy hay fixtures de mensajes de la bandeja 11 con `delivered` y `read`, y uno con audios de la persona y la nota de una derivación (`tests/fixtures/chatwoot_audio_trigger_inbox_11_conv_1_20261006.json`), pero ningún fixture del repo trae un mensaje `failed`. La prueba pone un valor canario en `content`, `content_attributes`, `source_id`, cada campo de `sender` y de `meta.contact`, y en `transcribed_text`, `data_url` y `thumb_url` de los adjuntos. Ninguno puede aparecer en la respuesta: nunca contenido, nombre, mail ni teléfono. Verifica también:
    - que la nota del equipo salga sin texto, de `human_agent`, y que la de la derivación, escrita por el usuario del token de control y con la marca `[supportmagician-handoff:`, salga de `system`;
    - que la actividad no salga, y que el audio cuente como un mensaje de la persona, aunque no traiga texto;
    - que cada plantilla de la base tenga su `delivery_status`, y que la que está en `failed` lleve su `meta_error_code` en dígitos y nada del texto de Meta;
    - que una nota posterior al último mensaje de la persona no cambie el motivo deducido de `coupon_not_sent` ni de `contesto`;
    - que un mensaje de otra bandeja deje afuera su conversación.
14. Con Chatwoot caído, con un error, más lento que el tope o con una conversación que pasa el tope de páginas, la historia sale con la parte de la base y el hueco `chatwoot_not_read`, dentro del tope de tiempo.

## Lo que decidió Dan el 2026-10-15

Dan tomó seis decisiones sobre este contrato el 2026-10-15, hacia las 12:40Z. Ese mismo día confirmó además las dos recomendaciones del análisis de la Etapa 1 del 2026-10-14 que el contrato asume: a las ≈12:55Z, que el Automation Expert habla solo por mensaje directo, y a las ≈15:25Z, que los rechazos al arrancar no se guardan en la 1a, sino en la 1b. Cada decisión ya está aplicada en el resto del contrato. Acá van como reglas, cada una con su razón y lo que destraba. Al final están las tres elecciones técnicas que dejó la alineación con el esquema, que no son de Dan.

### 1. La historia ata por identidad los eventos de Hotmart que no abrieron un caso

- **La regla.** La función compara, adentro de la base, la identidad normalizada del evento (`hotmart_purchase_intent_event_identities`) con `contact_points`. El mail (`normalized_email`) se compara con los de tipo `email` (`lower(btrim(normalized_value))`). El teléfono se compara por sus últimos 10 dígitos con los de tipo `phone`, y así se resuelven las dos formas del móvil de México. La consulta de cobertura con la que se opera ATT1, fuera de los repos, ya compara el teléfono así, por sus últimos 10 dígitos. El evento se ata cuando los datos que cruzan llevan a un solo contacto. Si llevan a dos contactos, o un mismo dato lleva a varios, el evento va al hueco `unattached_hotmart_event`. Nunca devuelve los valores, y el momento sale como `inferred`, con `attached_by: "identity"`. El cupón ya decide la compra de un modo parecido, por el mail o por los últimos 9 dígitos (`conversation-followup-discount-v1.md:31`).
- **Lo medido.** En att1-db, el 2026-10-15 a las ≈12:50Z y solo leyendo, sobre los 344 eventos de Hotmart con identidad guardada:
  - en 308, el mail y el teléfono dan el mismo contacto;
  - en 7 cruza un solo dato, 6 el mail y 1 el teléfono, y el otro dato no da ningún contacto;
  - en 0, el mail y el teléfono dan dos contactos distintos, y en 0, un mismo dato da varios;
  - 29 no dan ningún contacto: son las 29 compras con `purchase_correlation_contact_not_found`.

  Los 93 carritos y pagos fallidos que no arrancaron se atan todos a la persona. 89 se atan por los dos datos: los 86 que el piloto rechazó al entrar y 3 pagos fallidos con `payment_failure_correlation_unresolved`. Los otros 4, los `create_recovery_case_failed`, se atan solo por el mail. Las 32 compras sin caso (`purchase_correlation_case_not_found`) también se atan todas a la persona: 29 por los dos datos, 2 solo por el mail y 1 solo por el teléfono. Los 7 carritos que el correlador marcó `conflict` van al mismo contacto: su conflicto es con el formulario, no entre contactos. Si se atan además a la instancia, que es lo que los hace entrar a la historia, no se midió (ver «Lo que se confirma al implementar»).
- **Un criterio por defecto, que Dan puede revertir.** Es más amplio que la recomendación que Dan aprobó, que pedía que el mail y el teléfono dieran el mismo contacto. Un dato que no cruza con nadie no impide atar el evento, así que se atan también los 7 que cruzan por un solo dato. Con la regla estricta, que pide que crucen los dos, quedarían afuera 4 carritos y 3 compras: se atarían 89 y 29, en vez de 93 y 32.
- **La razón.** Sin esto, el agente ve una historia sin carrito y puede decir que no lo hubo.
- **Qué destraba.** Que «¿por qué no le llegó el carrito?» tenga respuesta. De los 93 que no arrancaron, solo 3 se atan a una persona por un camino guardado, y por identidad se atan a la persona los 93. De las 34 compras sin un caso nuestro, las 32 que no se compraron con un link nuestro se atan a la persona, y salen con su hora si se atan también a la instancia.

### 2. La historia lee también Chatwoot

- **La regla.** La historia junta dos fuentes: la función de la base y una proyección de los mensajes de Chatwoot que arma el bridge, sin su contenido. Cómo se lee, qué se toma y qué se descarta está en «La parte de Chatwoot».
- **La razón.** Dan eligió leer también Chatwoot. Es distinto de la recomendación, que era leer solo la base en la 1a y sumar la entrega como versión 1.1 si Brayan la pedía. Dan no dio el motivo.
- **Qué cambia.**
  - Dos momentos que eran huecos pasan a existir: `meta_delivery`, lo que Meta dijo de cada plantilla, y `conversation_activity`, los mensajes de la persona, del agente y del equipo, con su hora y su tipo de autor.
  - Los huecos `meta_delivery_not_read` y `conversation_not_read` desaparecen.
  - Aparece el hueco `chatwoot_not_read`, para cuando Chatwoot no contesta o tarda. La historia sale igual, con la parte de la base.
  - Aparece también `meta_failure_reason_not_persisted`, para la plantilla en `failed` cuyo código de Meta no está en Chatwoot.
  - La ventana del cupón, `coupon_not_sent` y el motivo `contesto` se deducen con los mensajes, y no solo con los turnos del agente.
- **Lo que trae de nuevo.** Una segunda fuente, que se puede caer o tardar, y un recorte de datos personales que pasa en el bridge y no en PostgreSQL. Lo cubren el hueco, los topes de tiempo y de páginas, y las pruebas 13 y 14, con un fixture capturado de la bandeja 11.
- **Qué destraba.** Que el agente diga si una plantilla llegó, si la persona la leyó o si falló, y cuándo escribieron la persona y el equipo. Por ejemplo, de las 194 plantillas del motor, Chatwoot tiene 16 en `failed`, y att1-db las cuenta como aceptadas. La historia las va a mostrar como `meta_delivery` en `failed`.

### 3. No se busca a un lead por teléfono ni por mail

- **La regla.** Se busca por número de conversación, por link o por nombre. Lo que parece un teléfono o un mail se rechaza con 422 `identifier_not_allowed`, con la misma regla en el plugin y en el bridge.
- **La razón.** Chatwoot ya busca por teléfono y por mail, y el número de conversación que da es la llave exacta. Así el bridge nunca recibe un teléfono ni un mail, y el agente nunca le pide uno a nadie.
- **Qué destraba.** La búsqueda, cerrada en tres llaves.

### 4. En Slack, el lead se nombra «<nombre de pila> · conversación #N»

- **La regla.** El nombre de pila sale de la cadena del saludo de las plantillas, con la guarda de las palabras de nombre y sin caer nunca al nombre completo. Si el nombre no pasa la guarda, va solo el número.
- **El límite, aceptado.** Si no hay un nombre inferido con confianza, va la primera palabra del nombre completo, que puede ser el apellido si la persona lo escribió primero. Medido en att1-db el 2026-10-14 a las 22:59Z, 134 contactos tenían un nombre inferido con confianza. El 2026-10-15, los contactos eran 295.
- **La razón.** Con el número solo, una charla con Brayan sobre tres leads se vuelve difícil de seguir.
- **Qué destraba.** El campo `label` y la búsqueda por nombre, que necesita algo para elegir entre candidatos.

### 5. Lo que devuelve el bridge vive solo en la charla

- **La regla.** El profile del Automation Expert no tiene memoria ni skills, tiene apagada la revisión en segundo plano y usa un modelo con ZDR. Sus sesiones se guardan poco tiempo y quedan fuera del respaldo de R2. Cómo se hace cada cosa en Hermes está a medir, y lo que no se pueda cumplir queda escrito como riesgo aceptado por Dan.
- **La razón.** Que la historia de un lead no aparezca después en otra charla, ni quede 90 días con candado en R2 cuando la persona pide la baja.
- **Lo que falta.** Acordarlo con la Etapa 0 bis, porque su respaldo suma hoy todo el volumen de Hermes (`despliegue/respaldo/README.md:11` de la instancia).
- **Qué destraba.** Que el instalador del profile sepa qué configurar.

### 6. La 1a no lleva un rol de base propio

- **La regla.** El rol propio va con la 1b, cuando el Automation Expert empiece a proponer cambios. En la 1a alcanza con funciones de solo lectura que solo ejecuta `service_role`.
- **La razón.** Un rol propio, por ejemplo `automation_reader` sin `BYPASSRLS`, aísla más, pero pide cuatro cosas: firmar otro JWT con el secreto de PostgREST, darle membership a `authenticator`, sumar un segundo cliente en el bridge y extender los validadores, que hoy cuentan con tres roles de API.
- **Qué destraba.** Que el claim de los lectores siga el molde del copiloto, sin tocar la autenticación de PostgREST.

### Las dos recomendaciones del análisis de la Etapa 1 que el contrato asume

**Solo por mensaje directo: confirmada.** Dan la confirmó el 2026-10-15 a las ≈12:55Z: el Automation Expert se conecta a Slack y habla solo por mensaje directo con Brayan y con él. El contrato fija el mensaje directo, y la configuración del profile lo hace cumplir (ver «El flag» y «Exposición»). Dan dudó entre dos formas: un canal privado de Brayan y otro suyo, o una especie de canal público donde esté el Automation Expert y Brayan le pueda hablar.
- Si un canal privado de cada uno quiere decir el espacio privado de cada uno con el Automation Expert, eso ya es el mensaje directo confirmado, y no cambia nada. Hermes llama al mensaje directo 1:1 «a private conversation with a single human» (`plugins/platforms/slack/adapter.py:6313-6321`).
- Un canal de Slack, aunque sea privado, uno por persona o uno solo con Brayan y Dan, es un cambio de configuración con su propia aprobación: suma el ID del canal a `allowed_channels`, y la etiqueta pasa a ser solo el número.
- Un canal público, abierto a todo el workspace, no entra con este contrato. Recibiría la historia de los leads, y el ADR-0017 dice que el canal no debe recibir PII, cuerpos de conversación ni payloads de proveedores (`docs/decisions/0017-central-slack-operations-connector.md:19-20`, «Contexto»). Si se quiere, va como decisión aparte, que tiene que resolver antes ese choque.
- El centinela de `allowed_channels` deja afuera también los mensajes directos de grupo (ver «El flag»).
- La app propia es una excepción al ADR-0017 (ver «Fuera de este contrato»). Va a Juan, cerrada y con default, como la de las alertas de ATT1, y no frena la 1a, como recomienda el análisis.
- Destraba el despliegue de la 1a y la prueba de punta a punta con Brayan.

**No guardar los rechazos al arrancar en la 1a: confirmada.** Dan la confirmó el 2026-10-15 a las ≈15:25Z: guardarlos va con la 1b.
- **Qué quiere decir.** A veces un mensaje programado está por salir y el piloto no lo autoriza: el flujo está pausado, se gastó el cupo del día o cambió la versión. El bridge lo vuelve a intentar en la vuelta siguiente y anota el porqué solo en su log (`durable_request_start_rejected`, `worker.py:1540-1562`). La base no lo guarda. Guardarlo sería escribir en la base: una escritura nueva del worker y una migración más.
- **Un caso real.** Los 5 carritos de la v4 hicieron 144 intentos cada uno, sin una autorización ni un mensaje, y la base no dice por qué. El lector lo deduce de la versión (ver «Las reglas de la historia»).
- **Cómo lo vería Brayan en Slack.** Si pregunta por qué no salió un carrito, a veces el agente va a poder decir el motivo, como el cambio de versión de esos 5 carritos. Otras veces va a contestar que no salió y que el motivo no quedó guardado. Si se guardaran los rechazos, tendría siempre el motivo.
- **La recomendación.** No guardarlos en la 1a, que solo lee. El lector deduce el motivo con lo que ya se guarda, que es la versión, la pausa o el tope del día. Si no puede, lo muestra como el hueco `start_rejection_not_persisted`. Guardarlos es una pieza aparte del producto, con la 1b, y el esquema aprobado ya la pone ahí (esquema, «Lo que no entra en la v1, y dónde va»).
- **Lo que decidió Dan.** Se guardan con la 1b. Cuando estén guardados, el hueco `start_rejection_not_persisted` desaparece.

### Tres elecciones técnicas, con su valor por defecto

No son decisiones de Dan. Las dejó la alineación con el esquema, y cada una va con el valor por defecto que se aplicó. Se pueden revisar en el claim.
1. **`ya_no_entra` va sin sufijo,** como en el esquema aprobado. Qué condición dejó de cumplirse lo dicen `reason_code` y su frase en `explanation`, no el código del motivo. Así `reason` usa el mismo código que la puerta.
2. **Un rechazo con el piloto en `inactive` o `closed` va con `reason` en `null`.** Ninguno de los dos estados es una pausa, que es `flujo_pausado`. Y el arranque rechazado no termina el recorrido, que es lo que dice `flujo_apagado`: se reintenta hasta que algo lo frene o venza. `inactive` es como nace el control y como queda al cambiar de versión, y `closed` es final.
3. **`definition.version` vale 1, y `definition.estado`, `publicada`.** Así escribe el esquema los cuatro flujos que corren hoy y la reactivación como está construida: tal como corren, con `version: 1` y `estado: publicada` (esquema, «Los siete flujos de validación»). Un comentario dice de dónde sale cada uno hoy: el scope y la política en el carrito, el pago fallido y el primer contacto, las variables en el cupón, y «como está construida» en la reactivación. La versión que corre de verdad va en `version` del flujo, por ejemplo `{"scope": "att1-recuperacion v5", "policy": "att1-recuperacion-un-toque v1"}`. La procedencia de `/definition/version` dice `code`, con esa sección del esquema. Nunca va `null`, que el esquema no admite: pide un entero mayor que 0.

## Lo que fijó la implementación (1.1.0)

Los lectores se construyeron en la rama `feat/claude-automation-expert-read-v1`, apagados por defecto. Lo que cambió o se cerró respecto de la 1.0.0:

- **Chatwoot sí guarda por qué Meta no entregó una plantilla.** La medición del 2026-10-15 al mediodía leía `content_attributes ->> 'external_error'`, pero Chatwoot guarda `content_attributes` como un JSON adentro de un texto (`json_typeof` da `string`), y así la clave no aparece. Desenvolviéndolo, el 2026-10-15 a las 20:07Z, solo leyendo y con los números largos y los mails tapados, las 18 fallidas del AgentBot de la bandeja 11 tienen el código de Meta:
  - 131049, 11, entre el 2026-10-10 y el 2026-10-13: Meta frena los mensajes de marketing a una persona que recibe muchos;
  - 131026, 4: no se pudo entregar;
  - 130472, 2: el número está en un experimento de Meta;
  - 131042, 1, el 2026-10-06: un problema de pago de la cuenta.

  La API de mensajes trae `external_error` con valor en las dos fallidas del fixture capturado (`tests/fixtures/automation_expert/chatwoot_messages_inbox_11_convs_50_17_52_20261015.json`). Por eso `meta_delivery` en `failed` lleva `meta_error_code`, solo los dígitos que abren `external_error` (`^\d{3,6}`), y una frase fija del bridge por código. Nunca sale el texto de Meta. El cambio es menor: el plugin sigue aceptando todo major 1.
- **La forma de `p_instance`:** `tenant_ref`, `ally_ref`, `funnel_ref`, `chatwoot_account_id`, `chatwoot_inbox_id`, `hotmart_product_id`, `inbound_scope_key`, `agent_scope_ref` (el `AGENT_PROVENANCE_SCOPE_REF` del bridge), `time_zone` y `scopes`, la lista de los scopes que corre el bridge (`LANCEMOS_PILOT_SCOPE_*` y `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_*`), cada uno con su grupo, su clave y su versión. Las funciones validan cada campo y fallan con `22023` (`automation_expert_invalid_instance`) si alguno no cumple su forma. Un scope cuya bandeja o cuyo producto no son los del manifiesto sale con `bound_to_instance: false` y sin datos.
- **La función de los flujos lee las tablas del piloto directo,** sin reusar `get_lancemos_pilot_runtime_status`: esa función devuelve un veredicto para el arranque y no el consumo ni la historia del control. La audiencia sale como el modo del scope, sin la cohorte. El control es uno por scope (`pilot_runtime_controls` tiene la clave primaria en `scope_key`).
- **La historia devuelve toda la parte de la base** de un lead, hasta 2000 momentos. Si hay más, falla con `54000` (`automation_expert_response_too_large`). El bridge suma la parte de Chatwoot, ordena todo por la hora, el origen (la base, la entrega de Meta, la actividad), la tabla y el id, y corta la página. El cursor es ese orden, en base64url, y cada página vuelve a leer las dos fuentes. Además de la línea de tiempo, la función devuelve `adopted_conversations`, para la ventana del cupón, y `gap_counts`, los huecos que solo la base puede contar.
- **Un evento de Hotmart sin caso se ata a la instancia por su producto** (`payload.data.product.id`, comparado adentro de la función con el del manifiesto). Medido en att1-db el 2026-10-15 a las ≈20:05Z: el campo está en todos los eventos de Hotmart de ATT1 (66 compras, 282 carritos, 10 pagos fallidos, 8 fichas impresas y 2 vencidas). El evento sale solo con su tipo, su estado y el código de su error. El payload no sale de la función.
- **El rechazo al entrar con `pilot_runtime_not_armed`** lleva `control_state`, el estado del control del scope de la recuperación en ese momento, sacado de `pilot_control_events`. Solo `paused` se traduce a `flujo_pausado`.
- **Los rechazos al arrancar se deducen en el orden del arranque:** primero la versión activa (el último `pilot_scope_version_activated` antes del primer intento), después el estado del control de esa versión, después el tope total y por último el del día. Los dos topes son de la bolsa: cuentan las autorizaciones del `scope_key`, de todas sus versiones. Así lo hace `authorize_lancemos_pilot_request_start`, y lo dicen `pilot_total_budget_exhausted` y `pilot_daily_budget_exhausted`.
- **El cupón se deduce con el criterio de su barrido.** Se toma el último mensaje público por id, y la persona tiene que haber contestado alguna vez y nadie del equipo tiene que haber escrito después. La ventana va de 24 a 72 h desde el último mensaje de la persona, dentro de su horario. Los códigos son los del barrido (`last_message_inbound`, `last_message_not_from_agent`, `team_replied`, `never_replied`, `inbound_too_recent` e `inbound_too_old`), y los momentos salen como `coupon_not_sent`, `inferred`.
- **El flag** (`AUTOMATION_EXPERT_READ_ENABLED`) entra a las capacidades portables. Con el flag prendido, el bridge exige el manifiesto, PostgREST, un token de 32 caracteres o más distinto de todo otro valor de su configuración y un `AGENT_PROVENANCE_SCOPE_REF` con forma de slug. `/ready` suma `automation_expert_read: enabled`.
- **Las rutas** corren de a dos pedidos a la vez. El tercero recibe 503 `automation_expert_busy`. El log de acceso lleva solo la ruta, el `lead_ref`, la cantidad y el código.

- **Lo que cambió la revisión adversarial del 2026-10-15** (privacidad, corrección contra este contrato y pruebas que no falsifiquen):
  - **El nombre de pila:**
    - el respaldo es siempre una sola palabra, y el espacio duro cuenta como espacio;
    - un nombre con dígitos no sale;
    - la guarda mira cada carácter, así que un superíndice o un número en círculo no es una letra.
  - **La búsqueda por nombre** acepta un prefijo de tres letras o más solo sobre la primera palabra. El resto tiene que coincidir en palabras enteras desde el principio. Así nadie reconstruye un apellido letra por letra.
  - **Un número de conversación** entra a la búsqueda solo si la conversación está atada a la instancia, aunque su contacto lo esté. La última actividad de un candidato mira solo esta instancia.
  - **Los códigos con siete dígitos seguidos** no salen, aunque cumplan la forma de los motivos. Las refs de la landing y de la oferta y el modelo tampoco.
  - **El estado del control en un momento** es el último cambio de estado o la última activación de versión del scope. La activación deja el control `inactive`, y por eso un rechazo entre la activación y el arranque no es `flujo_pausado`.
  - **El flujo que ganó** (`le_gano:<flujo>`) es el primer carrito o pago fallido de la persona después de su formulario, con caso o atado por identidad.
  - **`upcoming` deduce lo que frena el arranque** con el estado de hoy:
    - la versión activa, el control y los dos topes;
    - la toma humana, que es la marca, `paused_human` o la automatización en `paused`, `disabled` o `restricted`;
    - el horario con el minuto de cierre entero, como el motor.
  - **Un formulario sin contacto** (no planificado antes de crearlo) se ata por identidad, con la misma regla que los eventos de Hotmart, y sale `inferred`.
  - **El cupón:**
    - `claimed` es un envío incierto, no un cupón enviado;
    - un `failed` no impide la ventana siguiente, porque el barrido reintenta;
    - la ventana del cupón mira la compra, la baja y la toma humana;
    - `coupon_not_sent` lleva una hora estable entre páginas.
  - **El estado del carrito y del pago fallido** sale de su flag propio (`PORTABLE_HOTMART_RECOVERY_ENABLED` y `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED`) además del piloto. Un flujo apagado no le gana a nadie (`le_ganan`).
  - **Nada se recorta en silencio:**
    - las conversaciones que pasan de cinco dejan su `chatwoot_not_read`;
    - una plantilla cuyo mensaje no aparece en las conversaciones leídas también deja un hueco;
    - un `upcoming` de más de 20 da 503.
  - **El cuerpo de la búsqueda** tiene un tope de 4 KiB. Un error inesperado da 503, y el log lleva solo su tipo.

Lo que sigue sin confirmar está en «Lo que se confirma al implementar».

## Lo que suma el motor nativo (1.2.0)

Escrito al construir el PR 5 del hito 4 (2026-10-25, migración `20261025000100_automation_expert_native_reads_v1.sql`), antes del merge. Describe la interfaz; no dice que nada esté desplegado ni activado. Los flujos nativos son los del contrato [flow-engine-v2.md](flow-engine-v2.md). Todo es aditivo: el plugin sigue aceptando todo major 1, y sin flujos nativos las respuestas son las de la 1.1.0 más `contract_version: 1.2.0` y `data.native` en la historia.

### Tres lecturas nuevas en la base

De solo lectura, `stable`, `security definer` con `search_path` fijo, y solo para `service_role`, como las tres de la 1.1.0. La marca de instancia es la misma `p_instance` (la ata `_flow_store_instance`, por `tenant_ref` y `ally_ref`). Ninguna devuelve un teléfono, un mail ni un contacto.

- **`read_automation_expert_native_flows_v1(p_instance)`**: los flujos con ruta nativa de la instancia, apagados incluidos, con el estado del control y desde cuándo, la versión activa y su definición, la bolsa y su zona, cuántos tiene la cohorte, los arranques del día en la zona de la bolsa y lo observado en los últimos 7 días corridos (recorridos que arrancaron, envíos aceptados por Chatwoot, pasos en curso y los rechazos del arranque por motivo).
- **`read_automation_expert_native_history_v1(p_instance, p_lead_ref)`**: los momentos de los recorridos nativos de la persona en la bandeja de la instancia, con la forma de los momentos de la historia (`kind`, `at`, `flow_key`, `flow_version`, `reason_code`, `certainty`, `attached_by`, `evidence` y `extra`), y `cohort_of`, las cohortes de prueba de las que es. `null` si no tiene ni lo uno ni lo otro. Los momentos: `entered_flow` (`flow_engine_native_started`, evidencia en `flow_engine_runs`), `scheduled` (`flow_engine_native_step_scheduled`, con `due_at`, `expires_at`, `step_key` y `status`), `start_authorized`, `accepted_by_chatwoot` o `delivery_uncertain_or_failed`, `stopped` o `expired`, y `purchased` cuando el recuperador ató una compra al caso del recorrido (nunca la que lo disparó).
- **`read_flow_sales_v1(p_instance, p_since)`**: las ventas por flujo del motor (el carrito con su ruta en motor y los nativos), desde `p_since` y a lo sumo 31 días (`22023 flow_sales_window_too_wide`). Ver «Las ventas por flujo».

### Lo que cambia en dos funciones de la 1.1.0

- **El anclaje de una persona** en `read_automation_expert_lead_history_v1` y `find_automation_expert_lead_v1` suma un camino: un recorrido nativo de la bandeja de la instancia (`flow_engine_native_runs.chatwoot_inbox_id`). Un comprador que solo recibió la bienvenida no se ataba a la instancia por ningún otro camino, y la historia daba `lead_not_found`. Es un reemplazo por ancla con su marca (`automation_expert_native_anchor_v1`), sobre la definición vigente; el resto de las dos funciones queda letra por letra.

### Lo que suma la respuesta

- **`list_flows`** suma los flujos nativos a `data.flows[]`, después de los cinco de siempre. Cada uno trae `native: true`, `product_ref`, `source: stored_definition`, `version: {definition: N}`, `status` con `state` en `on`, `paused`, `off` o **`cohort`** (el estado `cohorte` del control: corre solo para su cohorte de prueba) y el estado crudo en `detail`, `cohort: {members, max_members: 5, applies}`, `priority` desde la definición, `usage: {pool, pool_timezone, starts_today}`, `observed` y la definición guardada en `definition`, con su procedencia `database`. Con flujos nativos, el sobre dice `source: mixed`. `list_flows(flow_key)` acepta la clave de un flujo nativo (`^[a-z][a-z0-9_]{1,39}$`) y da `flow_not_found` si no existe.
- **`sales`**, en cada flujo que corrió en el motor (los nativos y, con su ruta en motor, el carrito): `runs_started`, `purchases_after_send`, `by_case`, `by_identity`, `people_after_send`, `purchases_before_send`, `by_step`, la ventana (`since` y `until`, los últimos 7 días), `rule` e `identity_window_days`.
- **`lead_history`** pone los momentos nativos en la misma línea de tiempo, con la clave de su flujo nativo en `flow_key` y `flow_version: {scope: null, policy: null, definition: N}`. Los momentos de los flujos de la 1.1.0 no cambian de forma. `data.native.cohort_of` lista las cohortes de la persona.
- **Los huecos nuevos:** `native_flows_not_read` (la lectura del motor nativo no contestó: una base sin la migración, o un error; la respuesta sale igual con lo de la 1.1.0), `flow_sales_not_read`, `flow_sales_motor_only` (las ventas de un flujo del legado cuentan solo lo que corrió en el motor) y `native_upcoming_not_projected` (el próximo paso de un flujo nativo sale como un momento `scheduled` con su `due_at`, no en `upcoming`).

### Las ventas por flujo

**La regla, `purchase_after_accepted_send_v1`, es un valor por defecto que decide Dan (D-080-1):**

- una venta es una compra aprobada de Hotmart de un producto de la instancia, contada una vez por transacción;
- va al recorrido al que el recuperador ya la ató (`by_case`: `recovery_cases.purchase_event_id`, que escribe la correlación de la compra al cerrar el caso abierto, y que es único). Desde `20261028000400` (E-169), una compra con el link de un caso también gana ese caso si ya estaba cerrado, como `sequence_exhausted` después del único envío de la bienvenida: esa venta va por `by_case` y no por identidad;
- si no la ató ningún caso, va por la persona (`by_identity`): al recorrido del motor de la misma persona (su mail en `contact_points`) con el último envío aceptado por Chatwoot en los 7 días antes de la compra. Sin esto, un flujo de un solo envío nunca vendería: la bienvenida cierra su caso al salir (medido en PGlite: el caso queda `sequence_exhausted` y la otra compra no se ata a él);
- la compra que disparó el recorrido no cuenta para ese recorrido;
- el toque (`by_step`) es el último paso aceptado antes de la compra; una compra atada a un caso sin ningún envío antes va aparte (`purchases_before_send`).

Es «compró después de recibir el mensaje», no que el mensaje causó la compra, y así lo dice el agente. No mira el `sck` del link ni el cupón: esa separación (con nuestro link, con nuestro cupón) es de una versión siguiente. Lo que corrió en el legado no entra (`flow_sales_motor_only`).

### La biblioteca del agente

Una herramienta nueva del plugin, `read_library(tema)`, sin red: sin tema devuelve el índice, y con `hotmart`, `ghl-formularios` o `chatwoot-whatsapp`, la página. Las páginas viajan con el plugin (`profiles/automation-expert/plugins/automation-expert-read/biblioteca/`) y no traen nada de una instancia ni de una persona. **Que vivan en el plugin y no en `docs/plataformas/` es un valor por defecto (D-080-2):** el plugin es lo que llega al Hermes de la instancia, y una copia en `docs/` envejecería aparte. Una prueba las coteja contra el código: los eventos que nombra existen en `hotmart.py`, la ruta del adaptador de GHL es la del bridge, los códigos de Meta están en el catálogo y los disparadores son los del esquema.

### Las pruebas que la fijan

- **PGlite** (`validate_automation_expert_native_reads.mjs`, al final de `npm test`): la forma de la migración (tres lecturas solo de `service_role`, cuatro funciones cambiadas con su marca, el resto y los permisos iguales, dos corridas y otro dueño abortan); la bienvenida del PR 4 en cohorte, con una compra de la cohorte que arranca el recorrido, el envío aceptado y otra compra de la misma persona; las tres lecturas sobre ese mundo; rojos a propósito: sin el parche del anclaje la persona no se encuentra, y sin el envío aceptado la compra no cuenta. Con `--emit-fixture` deja la salida de las tres en `tests/fixtures/automation_expert_native/`.
- **pytest** (`tests/test_automation_expert_native_reads.py`): las rutas con esa salida, ATT1 sin flujos nativos igual que en la 1.1.0, una base sin la migración con su hueco, la biblioteca contra el código.

## Quién ejecuta cada flujo de negocio (1.3.0)

Escrito al construir el PR del Automation Expert del mapa M16 (E-208, pasos P5 y P8, 2026-10-30), antes del merge: el bridge contesta `1.3.0` (`CONTRACT_VERSION`, `automation_expert_read.py`). Describe la interfaz; no dice que nada esté desplegado ni activado. No suma funciones a la base: usa cinco lecturas que ya existen y ejecuta `service_role`. Es aditivo: el plugin sigue aceptando todo major 1, y una base o un bridge sin lo de abajo contestan con su hueco.

**Por qué.** Con las claves nativas propias (D1 del mapa M16: un flujo nativo nunca usa la clave de un flujo del legado, `precheckout`, `carrito`, `pago_fallido`, `reactivacion`, `descuento` ni `ficha`; la base de E-207 reserva las cinco primeras, `FLOW_KEYS`), la clave ya no dice qué flujo de negocio hace un nativo, y el agente no sabía qué sigue en el legado ni por qué scope. La decisión de Dan del 30/10 (D9): *«cualquier usuario que no tenga un Claude Code pueda operar todo lo relacionado a los flujos mediante el Automation Expert»*.

### Lo que suma `list_flows`

- **`data.business_flows[]`**, uno por flujo de negocio con algo que lo haga, en este orden: `precheckout`, `carrito`, `pago_fallido`, `ficha`, `descuento`, `reactivacion` y `compra` (lo de después de la compra, que solo hace un nativo). Cada uno trae:
  - `business` y `name`;
  - `legacy`: el flujo del legado (`flow_key`, `state` y `detail` como en `data.flows[]`, `scope` con la forma `<clave> v<N>`, `route_executor` si tiene ruta del motor v1 (con varias rutas del mismo flujo, la de todas o `mixto`), y `sends`, que es `state == on`), o `null` si no tiene;
  - `native`: los flujos nativos que lo hacen, por su disparador (`dispara.evento`: `carrito`, `pago_fallido`, `ficha_impresa` o `ficha_vencida` es la ficha, `intencion` es el primer contacto, `compra`; `dispara.silencio.lo_ultimo`: `agente` es el cupón, `persona` la reactivación), cada uno con `flow_key`, `product_ref`, `state`, `version` y `cohort_members`;
  - `executor`: `legado`, `motor_v1` (el legado con su ruta en `motor`), `nativo`, `nativo_en_cohorte`, `legado_y_nativo`, `legado_y_nativo_en_cohorte` o `nadie`;
  - `double_case_guard`: `true` en los flujos de negocio con la guarda del doble caso en los dos sentidos, hoy el carrito (M1a, `20261030000100`) y el pago rechazado (E-207, `20261030000200`). Ahí, con legado y nativo a la vez, el nativo toma cada disparo que arranca y el legado manda solo los que el nativo no arranca (por ejemplo, con un caso del legado en curso): no sale un mensaje doble, y el `summary` lo dice así. Sin la guarda (`precheckout`, `ficha`, `descuento`, `reactivacion`), `legado_y_nativo` avisa que los dos le escriben a todos y puede salir un mensaje doble;
  - `summary`: la frase para contarlo, por ejemplo «Carrito abandonado: el legado sigue mandando por el scope maribel-recuperacion v4; el nativo carrito_nativo está en cohorte (le escribe solo a su cohorte de prueba).».
- **La ficha del legado** (C1 y sus recordatorios), como un flujo más de `data.flows[]` con `flow_key: ficha`, antes de los nativos, **solo si el manifiesto declara `[flujos].ficha`** (sin eso, la respuesta es la de la 1.2.0). Su estado sale del manifiesto, de `PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED` y de su scope (`get_portable_payment_slip_pilot_runtime_status`, con la clave y la versión de `LANCEMOS_PILOT_PAYMENT_SLIP_SCOPE_*`); su definición, `dispara: {evento: ficha_impresa}`, `entra: {permiso: formulario}`, el paso de `[plantillas].ficha` con `boton: {link: ficha}` y la salida por la compra. Los recordatorios (`[plantillas].ficha_vence_manana` y `ficha_vence_hoy`) van en `reminders`, con `cuando`, porque salen relativos al vencimiento de cada ficha y el esquema no tiene esa espera. Si un flujo nativo se llamara `ficha`, la ficha del legado no se muestra y queda el hueco `legacy_payment_slip_key_taken`: la lectura no da dos flujos con la misma clave.
- **El pago rechazado con scope propio.** Cuando el bridge lo planifica contra su scope (`PAYMENT_FAILURE_VARIANTS_ENABLED` o `PAYMENT_FAILURE_OWN_POLICY_ENABLED`, con `LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE_*`: la misma condición que `app.py`), `pago_fallido` muestra ese scope en `version`, `usage.pool` y `topes.bolsa`, y su estado sale de `get_portable_payment_failure_pilot_runtime_status`. Hasta la 1.2.0 se mostraba con el scope de la recuperación, que en Maribel no es el que lo ejecuta.
- **`engine_v1`** en un flujo del legado con ruta del motor v1 (una fila de `flow_engine_routes` con su `legacy_kind`): `routes` (producto, clave, `executor`, scope y estado del control), `divergences` (`kind`, `reason`, `version`, `count`, `first_at`, `last_at`) y `start_rejections` (`phase`, `reason`, `version`, `count`, `first_at`, `last_at`), de `read_flow_engine_routes_v1`, `read_flow_engine_divergences_v1` y `read_flow_engine_start_rejections_v1` (toda la historia, sin marcar nada como avisado). Un flujo sin ruta no lo trae. Los rechazos de los nativos siguen en su `observed.start_rejections`.
- **`list_flows(flow_key)`** filtra `business_flows` al que nombra esa clave, como legado o como nativo.
- **Los huecos nuevos:** `legacy_scope_not_read` (no contestó el estado del scope propio), `legacy_scope_runtime_only` (del scope propio se lee solo el estado: los topes, el horario y el vencimiento van en `null`, y la espera, que venía de la política de la recuperación, no está en los pasos; E-216), `legacy_payment_failure_repeat_not_in_schema` (el pago rechazado del legado junta los rechazos de una persona y un producto en un caso, y suma uno de las 72 h siguientes a un caso cerrado: el esquema no lo expresa y `topes.repeticion` va en `null`; E-216), `payment_slip_reminders_anchored_to_due_date`, `payment_slip_observed_not_read` (lo observado de la semana no cuenta la ficha todavía), `engine_v1_not_read` (la lectura del motor no contestó, o trajo una fila fuera del contrato: `/flows` sale igual, sin `engine_v1`), `engine_v1_truncated` (vinieron 50 divergencias o rechazos, el tope de la lectura, de la más vieja a la más nueva: puede haber más nuevos) y `engine_rejections_alertable_only` (la lectura del motor trae solo los rechazos que alertan, los de MOT-002).
- **El pago rechazado del legado lleva su espera** (la gracia de su política, como el primer contacto) cuando se lee la política de su scope (E-216).
- **El motivo** de una divergencia o un rechazo se acepta con la regla de la base (`^[a-z0-9_:]{1,80}$`), y una corrida de siete dígitos o más se muestra como `<numero>`.

### Lo que no cambia

No toca la base, ninguna función, permiso ni envío. Sin la ficha en el manifiesto, sin scope propio del pago y sin rutas del motor, `data.flows[]` es el de la 1.2.0 y se suma `business_flows`. Una lectura que falla deja su hueco y el resto sale igual.

### Las pruebas que la fijan

pytest, `tests/test_automation_expert_executors.py`: el ejecutor por flujo de negocio con los fixtures capturados de los flujos de ATT1, la bienvenida nativa y las lecturas del motor (`tests/fixtures/flow_engine/bridge_rpc_rows.json` y `native_bridge_rpc_rows.json`); un carrito nativo junto al del legado (doble) y con el legado pausado (nativo); la ficha desde su scope, apagada, sin leer y sin declarar; el pago con scope propio; el motor v1 con sus divergencias y rechazos, y una fila fuera del contrato que se rechaza; la ruta `/flows` y `/flows/{flow_key}` con las tres lecturas, y sin ellas, con su hueco. Y `tests/test_automation_expert_draw.py`: la ficha se dibuja sin errores.

## Lo que se confirma al implementar

- Falta confirmar que PostgREST 14.15 corra las funciones `STABLE` en una transacción de solo lectura, por GET y por POST. No se probó. La prueba 2 cubre la función, pero no lo que hace PostgREST.
- La función apunta a 5 s, pero un `SET` en la función no corta la sentencia que la llamó. El corte que existe hoy es el de 8 s de `authenticator`. Cómo bajar a 5 s se resuelve en el claim.
- No se midió el tamaño real de una historia ni el de la proyección de los flujos: los 256 KiB y los 100 momentos son una propuesta. Tampoco se midió si Hermes tiene un tope para el resultado de una herramienta, ni cuánto contexto ocupa una historia de 50 momentos con un modelo que todavía no está elegido.
- La deducción del rechazo al arrancar se comprobó solo para la versión, con los 5 carritos de la v4, y sin mirar el log que la confirmaría. Para el tope diario y la audiencia no hubo un caso real. Tampoco se midió cuánto aporta `pilot_control_events`, que tiene 214 filas: 194 autorizaciones, 14 cambios de estado, 5 activaciones de versión y 1 alta en la cohorte.
- Las 47 intenciones `purchased` contra 2 links comprados: no se midió por qué camino ni a qué hora quedaron así. Antes de prometer el momento «compró» sin un link nuestro, hay que leer el código que mueve `lifecycle_state`.
- No se midió por qué 13 de las 55 conversaciones adoptadas no tienen turnos en `agent_turn_provenance`.
- La causa de los 4 `create_recovery_case_failed` queda en el log del bridge, que no se leyó.
- La regla de identidad de los eventos de Hotmart (decisión de Dan del 2026-10-15) se midió solo contra la persona. Falta medir si los eventos atados por identidad se atan también a la instancia, que es lo que los hace entrar a la historia. De los 93 carritos y pagos fallidos, no se midió cuántos tienen una correlación con `scope_id`, que admite nulo (`20260820000100_hotmart_purchase_intent_correlation.sql:59`). Las 32 compras no tienen correlación en esa tabla: medido en att1-db el 2026-10-15, la tabla no tiene ninguna `PURCHASE_APPROVED`. Se atan a la instancia por el producto del evento, y falta medir dónde está ese campo. Para un carrito o un pago fallido sin `scope_id` hay que fijar un camino a la instancia; el candidato es el mismo de las compras. Resuelto en la 1.1.0: se atan a la instancia por el producto del evento (ver «Lo que fijó la implementación (1.1.0)»).
- Las reglas 3 y 4 de convivencia salen del código y del esquema, y no tienen un caso real en ATT1: los 4 `create_recovery_case_failed` no coincidieron con un caso abierto. Para la 3, tampoco hay ningún `pilot_case_binding_conflict`, el código con el que hoy el carrito deja ese disparo (esquema, «Lo que cambia respecto de hoy»).
- La plantilla de cada envío no está como columna. Sale del catálogo por `anchor_type`, es decir, del manifiesto y de las variables `WABA_*`. No se miró `messages.semantic_metadata`, para no tocar contenido.
- El esquema fija la forma interna de `definition` y de la prioridad. Deja en código la puerta, común a todos los flujos, con la regla de su tope por persona, y deja el número de ese tope como un dato de la instancia, fuera de la versión (`puerta.tope_por_persona`). Falta decidir una cosa que el esquema no cubre: dónde dice la proyección lo que hoy cada flujo mira a su manera y no tiene campo en el esquema, como la toma humana (la regla 9 de convivencia). Qué llevan `definition.version` y `definition.estado` en la 1a quedó como elección técnica, con su valor por defecto (ver «Tres elecciones técnicas, con su valor por defecto»).
- Que un contenedor de la red `easypanel` llegue al bridge sale de las redes declaradas en `stack.yaml`, y no se probó desde adentro. La barrera por la subred de `hermes` es opcional y está a medir.
- En Hermes 0.21.0 falta confirmar tres cosas: la clave de administración del adaptador de Slack (`allow_admin_from` está en los de Telegram y Discord), cómo sabe un plugin en qué profile corre y cómo se acorta la retención de las sesiones de un profile.
- Cuando un contacto tiene varias conversaciones, la propuesta es mostrarlas todas en la misma historia. Falta validarlo con Dan.
- De la parte de Chatwoot quedan cinco cosas sin medir:
  - si la API marca una plantilla. Los 20 mensajes del fixture de la bandeja 9 no traen `additional_attributes`, y la medición de la bandeja 11 reconoció las plantillas en la base de Chatwoot (`additional_attributes ? 'template_params'`). Por eso la proyección reconoce una plantilla solo por el cruce con la base. Se confirma con el fixture de la prueba 13;
  - el cruce de la entrega del cupón por `provider_message_id`: se midieron solo las 194 plantillas del motor;
  - los topes de 5 s y de 10 páginas: no se midieron el largo de las conversaciones de la bandeja 11 ni lo que tarda Chatwoot en contestar. Se confirman en el claim;
  - cómo se reparte el cursor de la historia entre la función y el bridge, ahora que el bridge ordena juntos los momentos de las dos fuentes, y si cada página vuelve a leer Chatwoot. Se fija en el claim; Resuelto en la 1.1.0: la función devuelve la parte entera de la base y cada página vuelve a leer las dos fuentes.
  - si en la bandeja 11 de Chatwoot hay conversaciones que la base no ata a ningún contacto. La historia lee solo las que la base ata.

## De dónde sale

**Cómo se armó.**
- Se midió el 2026-10-15, entre las ≈01:30 y las 02:31Z, solo leyendo, sobre cuatro fuentes: el código del producto, la instancia ATT1 en GitHub (`b1cdefe`), att1-db y los metadatos de Chatwoot (bandeja 11).
- El borrador salió de tres mediciones de solo lectura (el acceso a la base, la historia de un lead y la convención del bridge), un redactor, dos verificadores adversariales (seguridad y privacidad, y si contesta las preguntas de la 1a) y un corrector: se aplicaron 30 arreglos y se descartaron 5, con su motivo.
- Se escribió en paralelo con el esquema y después se alineó con su versión verificada. La lista cerrada de motivos, que el esquema deja a este contrato, quedó escrita acá, y las citas y los nombres de campo siguen al esquema final. Esa alineación pasó por un verificador y un corrector: encontraron 11 problemas, y no se descartó ninguno.
- Para aplicar las decisiones de Dan del 2026-10-15, hacia las 12:50Z se midió otra vez en att1-db y en la base de Chatwoot (bandeja 11), solo leyendo, con conteos y sin datos personales, y se leyó el código del producto en `1b83191`. La aplicación de las decisiones pasó por otro verificador y otro corrector: encontraron otros 11 problemas, y se aplicaron todos.
- Los casos van anonimizados y con las horas relativas.

**Las decisiones y las mediciones.** Están registradas fuera de este repo, y se citan por su fecha:
- Decisiones de Dan del 2026-10-15: hacia las 12:40Z, las seis de «Lo que decidió Dan el 2026-10-15», y a las ≈12:55Z, que el Automation Expert habla solo por mensaje directo. A las ≈15:25Z, que los rechazos al arrancar no se guardan en la 1a, sino en la 1b.
- Decisión de Dan del 2026-10-14 (≈00:20Z del 2026-10-15), al revisar el análisis de la Etapa 1: las cinco piezas de un flujo, la puerta única y la prioridad como dato. En esa revisión también le pareció bien la propuesta de este contrato de lectura.
- El análisis de la Etapa 1 del 2026-10-14: de ahí salen el alcance de este contrato, la prueba del comercial con cero herramientas, los dos lectores, el plugin, el despliegue en ATT1, las recomendaciones de hablar solo por mensaje directo y de no guardar los rechazos al arrancar en la 1a, y los choques con la Etapa 0 bis.
- Decisión de Dan del 2026-10-09, al prender el pago fallido en ATT1: las dos plantillas de día a quien le falla el pago y después abandona, y otro caso con otra plantilla por cada reintento de tarjeta posterior al envío.
- La v5 del carrito y el pago fallido, con 80 arranques por día y 1000 en total: `armed` en ATT1 desde el 2026-10-13 a las 00:20:37Z.
- La demora de Hotmart en avisar el carrito: medida en ATT1 el 2026-10-06.
- En att1-db, el 2026-10-14, cuando se midió para el esquema: a las 22:51Z no había ninguna ficha vencida, y a las 22:59Z, 134 contactos tenían un nombre inferido con confianza.
- En att1-db, el 2026-10-15, en cuatro sesiones con `default_transaction_read_only=on`:
  - ≈01:39Z: los roles y los permisos (`pg_roles`, `pg_auth_members` y `has_table_privilege`), las funciones (`pg_proc`: 239 en `public`, 122 con `EXECUTE` para `service_role` y ninguna para `anon` ni `authenticated`), las columnas de instancia, los privilegios por defecto (`pg_default_acl`) y los conteos;
  - ≈02:13Z: los rechazos al entrar y por qué camino se atan, los turnos del agente, las conversaciones adoptadas, los cupones contra los turnos, las fichas, y los tiempos de la cancelación del primer contacto;
  - ≈02:15Z: el logging de Postgres, la volatilidad de las funciones que se reusan, el tenant y la bandeja de los scopes, y las anclas de instancia de las 199 conversaciones;
  - 02:30:54Z: los 392 casos con 0 `won_at`, los nombres (295, 294 y 293), las conversaciones por contacto (4 como máximo), los casos por contacto (60, 163 y 2), las intenciones por estado (47 `purchased`), y las columnas de `hotmart_purchase_intent_event_identities` y de `agent_turn_provenance`.
- Las historias A y B: en att1-db y en Chatwoot (bandeja 11, solo metadatos), el 2026-10-15 entre las ≈01:30 y las 01:43Z. De esa medición sale también que `precheckout_authorization_lost` no aparece entre los motivos de las reevaluaciones, de los planes ni de las acciones, y que `hotmart_purchase_intent_correlations` no tiene ninguna `PURCHASE_APPROVED`.
- La medición para aplicar las decisiones, el 2026-10-15 hacia las 12:50Z, solo leyendo, con conteos y sin datos personales:
  - en att1-db, los 344 eventos de Hotmart con identidad guardada contra `contact_points`, las 194 filas de `messages` y la hora de las fichas vencidas;
  - en la base de Chatwoot, bandeja 11, los mensajes de 14 días por tipo, estado, tipo de autor, nota privada y plantilla, desde cuándo hay `delivered` y `read`, las fallidas y el cruce con las 194 plantillas del motor.

  De esta medición quedaron solo las consultas y la lista de los 194 ids. Las demás salidas no se guardaron, y sus números salen de un resumen escrito en el momento. La hora de las fichas vencidas no quedó en ningún lado, y por eso el contrato da solo el rango entre dos mediciones: después de las 22:51Z del 2026-10-14, cuando se midió para el esquema, y antes de las ≈02:13Z del 2026-10-15.
- La comparación por el mail y por los últimos 10 dígitos del teléfono es la misma que usa la consulta de cobertura con la que se opera ATT1, fuera de los repos.

**El producto:** el código se midió en `0cbb989`, que el 2026-10-15 avanzó a `1b83191` (#232, la 1.8.0). Lo de las decisiones de Dan del 2026-10-15, y lo que sumó la corrección posterior, se leyó en `1b83191`. `git diff --stat 0cbb989 1b83191` no muestra cambios en los archivos citados, salvo `pyproject.toml`, `validate_acl_hardening.mjs` y los dos inventarios de `scripts/`, que se citan como están en `1b83191`. En ese rango entraron además dos archivos nuevos: la migración `20261015000100`, que se nombra como la última del repo, y el fixture `tests/fixtures/chatwoot_daily_feedback_att1_conversations_20261015.json`, uno de los demás `tests/fixtures/chatwoot_*.json`, que no trae ningún mensaje `failed`. Se citan:
  - el molde, `docs/contracts/operator-correlation-review-v1.md` (:37-47, :41, :53-70, :96, :148, :152, :155-156 y :171);
  - `docs/contracts/conversation-followup-discount-v1.md` (:31 y :126) y `docs/contracts/lead-first-name-inference-v1.md`;
  - el plugin molde, `profiles/client-copilot/plugins/operator-correlation-review/tools.py` (:34-39, :42-62, :81-104, :94-99, :198-218, :240-244 y :253-271), y `tests/test_client_copilot_profile_package.py:230`;
  - `src/bridge/app.py` (:176-243, :416-418, :466-681, :651, :806, :843-850, :889, :999-1004, :1193-1196, :1203-1206, :1353, :1356-1358, :1694-1720, :1864-1865, :1869-1886, :2839-2858, :2948-2986, :3287-3298, :4336-4340 y :5713-5732), que no tiene manejador de `RequestValidationError`;
  - `src/bridge/supabase.py` (:178-181 y :1571-1589);
  - `src/bridge/instance_manifest.py` (:67-68 y :176-195);
  - `src/bridge/followup_discount.py` (:83-84, :89-99, :469-517, :478-504 y :1058-1083);
  - `src/bridge/reactivation.py` (:244-278), de donde el cupón toma el último mensaje público;
  - `src/bridge/audio_transcription.py` (:94-102);
  - `src/bridge/lead_first_name.py` (:1-27, :49-75, :67, :80 y :443-451);
  - `src/bridge/chatwoot.py` (:1277-1314, :1456, :1490-1510, :1536-1605, :1554-1561 y :1597-1598);
  - `src/bridge/worker.py` (:615-627, :1540-1562, :1554 y :2395-2402);
  - `supabase/baseline/20260803_public_schema.sql` (:92-100);
  - los fixtures `tests/fixtures/chatwoot_conversation_messages_inbox_9_conv_173_20260926.json` y `tests/fixtures/chatwoot_audio_trigger_inbox_11_conv_1_20261006.json`, leídos solo por sus claves, y los demás `tests/fixtures/chatwoot_*.json`, leídos solo por su bandeja y el estado de sus mensajes: ninguno trae un `failed`;
  - `docs/decisions/0017-central-slack-operations-connector.md` (:19-20, :32-33 y :55), `docs/decisions/0024-la-revision-diaria-en-una-instancia-autohospedada.md` (:60), `docs/decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md` (§4 y §7) y `docs/operations/2026-10-05-att1-entrante-e2e.md` (:96-97);
  - `Dockerfile` (:24) y `pyproject.toml` (:3, la 1.8.0);
  - las migraciones `20260803000100_followup_engine_v1.sql` (:195, :1233 y :2555-2604), `20260805000300_per_case_conversation_anchor.sql` (:284-445), `20260808000100_hotmart_purchase_approved.sql` (:190-215), `20260810000100_lancemos_pilot_boundary.sql` (:8-36, :130-170, :294, :404 y :512-519), `20260816000200_inbound_commercial_case_draft_only.sql` (:134-136), `20260820000100_hotmart_purchase_intent_correlation.sql` (:15-59), `20260825000500_johanna_mvp_full_activation.sql` (:480), `20260903000300_commercial_ally_payment_failure_recovery.sql` (:3-7), `20260904000100_att1_payment_failure_single_initial_contact.sql` (:87-98), `20260923000200_conversation_reactivation_v1.sql` (:93), `20260928000100_agent_prompt_provenance_v1.sql` (:97-140), `20260928000400_conversation_followup_discount_v1.sql` (:67-115), `20260930000300_pilot_scope_audience_mode.sql` (:837-846), `20261001000200_portable_precheckout_first_contact.sql` (:193-205, :215, :228, :231-240, :296-313, :366-442, :836-862, :1611, :1630 y :1639-1646) y `20261012000100_portable_payment_failure_context_and_slip_events.sql` (:24-31 y :216-227);
  - `tests/sql/followup_engine/validate_handoff_postgres.py` (:432-539) y `tests/sql/followup_engine/validate_acl_hardening.mjs` (:225-243, que exige 124);
  - `scripts/supabase_acl_inventory.sql` y `scripts/supabase_schema_inventory.sql`;
  - `git grep message_updated` en `src/`, que no da resultados.

**Hermes 0.21.0 (`29112be`):** la fuente es la del repo público `NousResearch/hermes-agent` en el commit `29112be` (la imagen `v2026.8.31`), y se leyó sin ejecutarla. Se citan `plugins/platforms/slack/adapter.py` (:6313-6321, :6447-6450 y :9194-9209), `gateway/authz_mixin.py` (:497-502), `agent/background_review.py` (:1-16 y :291-300), `agent/secret_scope.py` (:176-190), `agent/tool_dispatch_helpers.py` (:48-61 y :175-176), `hermes_cli/plugins.py` (:6622-6673), `hermes_cli/tools_config.py` (:2816 y :2840-2858) y `toolsets.py` (:516-517).

**La instancia ATT1 (`b1cdefe`):** `despliegue/stack.yaml` (:37, :43-51, :52, :63, :83-88, :147-150 y :153-158), `despliegue/traefik/setter-att1.yaml` (:11 y :16), `despliegue/gateway/nginx.conf` (:23-24), `despliegue/respaldo/README.md` (:11 y :26), `despliegue/alertas/README.md` (:27), `instancia.toml` (`[flujos]`), `.env.example` (:18, :54, :90, :98, :106 y :109-110) y `README.md` (:75, :244 y :508). El `resolv.conf` del contenedor de Hermes se leyó el 2026-10-15 a las ≈02:16Z.

**El esquema:** [flow-definition-v1.md](flow-definition-v1.md): «Qué lo dispara», «La versión, el control y el techo», «Cómo conviven los flujos», «Lo que pasa hoy en ATT1, y el campo que lo escribe», «La puerta, chequeo por chequeo» y «Cuándo termina» (de ahí salen los diez motivos que nombra el esquema), «Lo que cambia respecto de hoy», el carrito y los otros flujos de hoy en «Los siete flujos de validación», la definición formal y «Lo que no entra en la v1, y dónde va». El contrato se escribió contra una versión anterior del esquema, sin verificar, y después se alineó con esta. De la lista de motivos de esa versión salen los once códigos que el esquema final no nombra. El sufijo de `ya_no_entra` que traía esa lista ya no se usa (ver «Tres elecciones técnicas, con su valor por defecto»).
