# Agente comercial: de GLM 5.2 a GLM 5.3 Flash por OpenRouter

- Fecha: 2026-09-30.
- Tipo: evidencia operativa. Describe mediciones y un cambio de configuración hechos el
  2026-09-30 entre las 17:36 y las 19:30 UTC. No es contrato ni arquitectura.
- Alcance: el `config.yaml` del profile `agente-comercial` en `infra_hermes`. **No cambia código
  del bridge**, que el 2026-09-30 declaraba `GIT_SHA` = `f7dd22720327f27da7be86d79de51b2e1c6b3cad`,
  ni ningún otro profile.
- Claim: `claude-estado-modelo-glm-5-3-flash-v1` (este documento).
- Antecedente: `docs/operations/2026-09-25-agente-comercial-glm-5-2-release.md`.

Este documento no contiene teléfonos, nombres de leads, textos de sus mensajes ni secretos.
Los identificadores son de Chatwoot (conversación, mensaje).

## 1. El cambio

Lo pidió Dan el 2026-09-30.

| Dato | Antes | Después |
|---|---|---|
| `model.provider` | `openrouter` | `openrouter` |
| `model.default` | `z-ai/glm-5.2` | `z-ai/glm-5.3-flash` |
| `fallback_providers` | `openrouter / anthropic/claude-sonnet-4.6` | sin cambio |
| md5 del `config.yaml` | `cfa245b67b5d898e82387be8917cc9d6` | `0b2264d0889ac5b0b245304440633af5` |

- **Hora:** 2026-09-30 18:16:07 UTC.
- **Cómo se hizo:** se reemplazó una sola línea. El script abortaba si la línea
  `  default: z-ai/glm-5.2` no aparecía exactamente una vez. Permisos y dueño se conservaron
  (`-rw------- hermes`).
- **Respaldo del archivo anterior:**
  `/opt/data/profiles/agente-comercial/private-backups/config.yaml.2026-09-30-181607`.
- **Cómo se vuelve atrás:**
  1. Dentro de `infra_hermes`, como usuario `hermes`:
     `cp /opt/data/profiles/agente-comercial/private-backups/config.yaml.2026-09-30-181607 /opt/data/profiles/agente-comercial/config.yaml`.
     Va sin `-p`, para que cambie el `mtime`, que es lo que invalida la caché.
  2. No hace falta reiniciar el gateway (ver abajo).
  3. Se verifica con el md5 `cfa245b67b5d898e82387be8917cc9d6` y con `model=z-ai/glm-5.2` en la
     próxima línea `API call` del `agent.log` del profile.

### No hizo falta reiniciar el gateway

El api_server de Hermes (`nousresearch/hermes-agent:v2026.8.31`) resuelve el modelo **en cada
pedido**:

- `gateway/run.py::_resolve_gateway_model` y `_load_fallback_model` leen el `config.yaml` con
  una caché que se invalida por `(mtime_ns, size)` del archivo.
- El proceso del gateway del profile siguió siendo el mismo antes y después: pid 1301, con más
  de cinco días de uptime.

Esto corrige una nota del 2026-09-25 que quedó fuera del repositorio («el `config.yaml` sí exige
reinicio»). `docs/operations/2026-09-25-agente-comercial-glm-5-2-release.md` §1 registra que ese
día se reinició, sin medir si hacía falta.

**Lo que no se midió:** si el resto del `config.yaml` también se relee en caliente (por ejemplo,
`platforms`).

### Verificado en producción

**Pedido de verificación, 18:16:53 UTC.**

- **Qué se mandó:** un pedido con la forma exacta del bridge (`model: agente-comercial`, sin
  `provider`), con el contexto real de la conversación 172 hasta el mensaje 2314, en el que el
  lead pide el enlace.
- **A dónde:** directo al api_server de Hermes (`/v1/chat/completions`), sin pasar por el
  bridge. No se le envió nada al lead.
- **La línea del `agent.log`:**

  ```
  2026-09-30 18:16:53,622 INFO [api-b2b80af806d3fcd9] agent.conversation_loop: API call #1: model=z-ai/glm-5.3-flash provider=openrouter in=5481 out=159 total=5640 latency=2.3s
  ```

- **La propuesta:** válida para `_parse_agent_proposal` + `_is_valid_proposal`, con
  `send_payment_link / payment_link_requested` (la misma decisión que dio producción para ese
  mensaje) y sin URL en el texto.

**Primer turno real, 18:36:12 UTC.**

- **El modelo:** el `agent.log` registra `API call #1: model=z-ai/glm-5.3-flash
  provider=openrouter in=5566 out=161`.
- **El resultado en el shadow dir:** `completed`, **sin `attempts`** (válido al primer intento),
  con `send_payment_link / payment_link_requested`. Es el único resultado escrito entre el
  cambio y las 19:29 UTC.
- **El enlace:** el lector de estado del recuperador lo muestra emitido para la conversación 214
  a las 18:36 UTC (`lead_intent`, atribución `full`) y en estado **`ENTREGADO`**.

## 2. Qué pasa por este modelo

Todo cliente del bridge que pide `model: agente-comercial` corre con el modelo del
`config.yaml`. Según los flags del contenedor el 2026-09-30:

| Cliente (`src/bridge/…`) | Activo | Qué hace |
|---|---|---|
| `hermes.py` (`HermesShadowProcessor`) | sí | la propuesta del agente por turno |
| `lead_first_name.py` | sí (`LEAD_FIRST_NAME_INFERENCE_ENABLED`) | el primer nombre para el saludo |
| `correlation_preresolution.py` | sí (`CORRELATION_PRERESOLUTION_ENABLED`) | la pre-resolución de correlación |
| `recovery_agent.py` | **no** | `RESOLUTION_WORKER_ENABLED`, `DURABLE_DISPATCHER_ENABLED` y `DURABLE_OUTBOUND_ENABLED` en `false` |

**No pasa por este modelo:**

- **El partidor de respuestas** (`reply_splitter.py`, `CHATWOOT_REPLY_SPLITTER_ENABLED=true`
  leído el 2026-09-30) manda su propio modelo en cada pedido:
  `HERMES_REPLY_SPLITTER_PROVIDER=anthropic` y `HERMES_REPLY_SPLITTER_MODEL_NAME=claude-haiku-4-5-20251001`.
  Sigue autenticando con la credencial OAuth de Anthropic del gateway.
- **El `title_generation` auxiliar de Hermes** usa el mismo Haiku.

## 3. Cómo se midió antes del cambio

### Retención cero de datos (ZDR)

La cuenta de OpenRouter tiene ZDR activado, y un modelo que no lo cumple devuelve 404. Se hizo
un pedido directo a `z-ai/glm-5.3-flash` con la key de producción, desde el contenedor del
bridge: **HTTP 200, servido por Together.**

### El harness

Se armó un harness y un auditor adversarial por cliente.

- **El pedido:** el cuerpo de producción, sin cambios salvo `"provider": "openrouter"` + `"model":
  <id>`. El api_server honra ese override por pedido sin tocar el `config.yaml`.
- **El contexto:** armado con las funciones del código desplegado.
- **La evaluación:** con el parser y el validador del código desplegado.

**Fidelidad del contexto en la propuesta por turno:**

- En los 8 casos recientes, el digest del contexto reconstruido es **igual** a
  `agent_turn_provenance.context_digest` de producción. El harness lo verificó 8 de 8 y el
  auditor, de forma independiente, 5 de 5.
- Los `prompt_tokens` de GLM 5.2 en el harness son iguales al `in=` que registró producción
  para esos turnos.
- En un caso (`rec-199-consejo`) se corrió además un brazo de control con el pedido exacto de
  producción (`model: agente-comercial`, sin override). En sus 2 corridas limpias dio el mismo
  `in=6034` y la misma decisión (`ask_question`) que el override.

⚠ **Un defecto de método que encontraron los auditores.** El api_server deriva la sesión de
sha256(system + primer mensaje `user`). Los harnesses mandaban los dos modelos del mismo caso a
la vez, así que el segundo pedido esperaba la reserva de la sesión y Hermes le recargaba la
transcripción previa.

- **Cuánto contaminó:** 11 de 72 pedidos de la propuesta por turno y 16 de la pre-resolución
  llevaron un prompt distinto del de producción.
- **Cómo se ve:** en el `in=` inflado. Por ejemplo, 9.816 contra 6.035 (`rec-199-consejo`) o
  9.137 contra 5.599 (`rec-203`) en el harness, y hasta 18.344 contra 6.034 en la re-corrida
  del auditor.
- **Cómo se corrigió:**
  - Las cifras rotuladas «pedidos limpios» excluyen los contaminados, identificados por el `in=`
    del `agent.log`. Las rotuladas «corrida completa del harness» los incluyen.
  - El auditor de la pre-resolución volvió a correr en serie.
  - El de la propuesta por turno no: 7 de sus 29 pedidos también se contaminaron, y los
    descartó al contar.

**Efecto colateral:** hubo unos 335 pedidos al gateway del profile. Los que llevaban contexto de
producción quedaron registrados en el `state.db` de Hermes, en las mismas sesiones que los turnos
reales.

- **No cambian respuestas futuras:** Hermes recarga la transcripción de una sesión solo cuando
  un pedido espera su reserva, y producción no manda dos pedidos a la vez sobre la misma sesión.
- **Sí ensucian cualquier auditoría de esas sesiones:** tienen intercambios de laboratorio,
  incluidos algunos de GLM 5.3 Flash anteriores a las 18:16 UTC.

## 4. Resultados

### Propuesta por turno

Doce casos:

- **Cuatro del 2026-09-24**, con la decisión que dio producción: el lead pide el enlace; pide
  descuento y cuotas; pide más información; y no puede entrar al curso.
- **Ocho turnos reales del 28 al 30/09**, que producción ya había contestado con GLM 5.2.

| Métrica | GLM 5.2 | GLM 5.3 Flash |
|---|---|---|
| Propuestas válidas (pedidos limpios, harness + auditoría) | **43 / 43** | **38 / 40** |
| Decisión y `reason_code` iguales a producción (corrida completa del harness) | 33 / 36 | 34 / 36 |
| Promete seguimiento humano con `human_handoff_confirmed=false`, que el SOUL prohíbe (pedidos limpios) | ~7 / 43 | 2 / 38 |
| Latencia media (pedidos limpios del harness) | 3,84 s | 5,27 s |
| Tokens de salida, media (corrida completa del harness) | 454 | 238 |

**Cómo decide:**

- Las fallas de decisión de GLM 5.2 están todas en el mismo caso: un pedido de consejo personal
  con un agente humano en el historial.
  - Ahí GLM 5.2 oscila entre `ask_question` y `handoff`; producción dio `handoff`.
  - GLM 5.3 Flash dio `handoff / policy_requires_human` 4 de 4.
- GLM 5.3 Flash hizo una vez una pregunta de orientación donde el SOUL manda derivar (acceso al
  curso): 1 de 7.

**Contrato de salida.** GLM 5.3 Flash lo rompe distinto que GLM 5.2:

- **Cómo falla:** responde en prosa de chat, sin el objeto JSON (`unparseable`).
- **Dónde:** pasó en un caso emocional (duelo), 2 de 6 pedidos limpios de ese caso. Fuera de
  ese caso, sus propuestas salieron válidas.
- **Comparación:** GLM 5.2 dio 0 inválidas en esta medición.
  - En producción, el listado del shadow dir del 2026-09-30 tiene 34 resultados posteriores al
    cambio a GLM 5.2 (del 2026-09-25 23:26 al 2026-09-30 16:07 UTC). Todos son `completed` y
    ninguno tiene `attempts`.
  - Los otros ~90 del directorio son anteriores: de Sonnet 4.6, o de antes de que existiera el
    reintento, así que no podían llevar `attempts`.

**Calidad del texto:**

- GLM 5.3 Flash copió al lead, 2 de 35 veces, una frase interna del SOUL: «el checkout informa
  una garantía de 7 días».
- Ninguno de los dos modelos metió voseo, lenguaje de piloto, URLs ni precios distintos de
  USD 49.

**Latencia:** ningún pedido se acercó al timeout de 60 s del bridge. El máximo fue 24,3 s de
latencia de API, en GLM 5.3 Flash.

**Caché de OpenRouter:**

- GLM 5.2 cachea en todos los pedidos entre ~89 % y ~98 % de los tokens de entrada.
- GLM 5.3 Flash cachea menos: en general ~2.300 tokens (~40 %), y a veces casi todo.
- El costo no se midió.

### Primer nombre

- **Validez:** 52 / 52 válidas con GLM 5.3 Flash y 49 / 49 con GLM 5.2. Ningún caso salió del
  contrato.
- **Nombre de pila compuesto de dos palabras poco común:** GLM 5.3 Flash lo corta 3 de 9 veces
  (deja la primera palabra o la segunda). GLM 5.2 acierta 9 de 9.
- **Dos nombres de pila:** GLM 5.3 Flash devuelve los dos 2 de 8 veces (la regla pide uno).
- **El resto de los casos:** no hay una diferencia que la muestra alcance a separar.
- **Trazabilidad:**
  - `lead_first_name_inferences.model_name` guarda `agente-comercial`, no el modelo que contestó.
    Para saber qué modelo infirió una fila hay que comparar su `created_at` con el 2026-09-30
    18:16:07 UTC.
  - La tabla conserva la primera respuesta por `name_key` (`on conflict … do nothing`), así que
    un error de GLM 5.3 Flash queda fijo para ese nombre.

### Pre-resolución de correlación

- **Diez casos reales:** mismo resultado con los dos modelos en todos. En pedidos limpios,
  21 / 21 de GLM 5.3 Flash y 31 / 31 de GLM 5.2 son válidos.
- **Formato:** GLM 5.2 envuelve el JSON en un bloque de código 8 de 32 veces; GLM 5.3 Flash,
  0 de 32. El parser tolera las dos formas.
- **Referencia de producción:** no hay contra qué comparar dentro de la lectura permitida. Las
  tablas de resultados de producción están revocadas para `service_role`.

### `recovery_agent`

Está apagado, así que el cambio no lo toca.

- **En el laboratorio, ante el mismo pedido:** GLM 5.3 Flash redacta el mensaje 19 de 28 veces;
  GLM 5.2, solo 2 de 28.
- **El resto:** deriva o sale inválido. GLM 5.2 devuelve el formato de la propuesta por turno;
  GLM 5.3 Flash traduce las claves o pasa el tope de 120 caracteres de `strategy`.
- **Antes de prender ese consumidor hay que resolver el choque** entre su pedido y la regla del
  SOUL que dice que el agente solo responde mensajes entrantes.

## 5. Qué pasa cuando una propuesta sale inválida

Con el bridge desplegado (`GIT_SHA` = `f7dd227` leído el 2026-09-30; contiene el PR #182,
mergeado el 2026-09-25):

1. **Reintento en el bridge.** `hermes.py` hace **un** reintento (`_MAX_PROPOSAL_ATTEMPTS = 2`)
   con la key `<digest>-retry-2`. Es una muestra independiente: el reintento no ve la salida
   mala.
2. **Si fallan los dos intentos:** el resultado queda `failed / invalid_agent_output` con
   `attempts: 2`, y `process_chatwoot_work` sale sin responder ni derivar.
3. **Rescate por el monitor de trabadas.** El mensaje del lead queda último en una conversación
   abierta.
   - **Quién lo toma:** `ChatwootStalledConversationMonitor` (`CHATWOOT_STALLED_MONITOR_ENABLED=true`
     leído el 2026-09-30, con los valores por defecto del código).
   - **Cuándo:** solo si la conversación sigue dentro de la ventana de 24 h (`can_reply`), sin
     asignado, sin `automation_paused`, y con el mensaje de entre 120 s y 24 h de antigüedad.
   - **Cómo:** la readmite con la identidad `stalled-chatwoot:{conv}:{mid}`, distinta de la del
     webhook.
   - **La primera readmisión trae dos intentos nuevos**, porque esa identidad no tiene resultado
     guardado.
   - **Las readmisiones 2 y 3 (300 s entre una y otra) no vuelven a pedir la propuesta:**
     `admit_recovery` rearma la misma identidad, y `has_result` encuentra el `failed` guardado.
   - Un turno tiene, como mucho, **cuatro intentos**.

**Cuánto se pierde.** Con la tasa medida en el caso de duelo (1 de 3 por intento):

- un turno así pierde los dos primeros intentos ~1 de cada 9 veces;
- si el monitor lo readmite, pierde los cuatro ~1 de cada 81 veces.

**Que el monitor lo rescate no está verificado de punta a punta.**

## 6. Lo que este documento no acredita

- **La tasa de inválidas en producción.** Los números de §4 son de laboratorio; hasta las 19:29
  UTC hubo un solo turno real con el modelo nuevo. La tasa la van a dar los resultados con
  `attempts` o `failed` del shadow dir a partir del 2026-09-30 18:16 UTC.
- **El rescate por el monitor de trabadas** de un turno que falló dos veces.
- **El fallback** (`anthropic/claude-sonnet-4.6`). Nunca corrió.
- **Lo que pasa después de la propuesta:** el override de medicación y el partidor de
  respuestas no se evaluaron con GLM 5.3 Flash. La inyección del enlace se vio funcionar en un
  solo turno (§1).
- **Contextos con audios transcritos:** no se probaron.
- **El costo por turno:** no se midió.
