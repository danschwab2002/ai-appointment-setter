# Agente comercial: GLM 5.2 por OpenRouter, SOUL sin lenguaje de piloto e imagen de Hermes pinneada

- Fecha: 2026-09-25.
- Estado: los tres cambios estan **en produccion y verificados** en las horas y con
  las mediciones que se listan abajo.
- Claims: `claude-soul-comercial-sin-piloto-v1` (merged), `claude-estado-glm-soul-imagen-v1` (este documento).
- PRs: [#181](https://github.com/danschwab2002/ai-appointment-setter/pull/181), mergeado en `98fd848` el 2026-09-25 15:37:23 UTC.
- Alcance: profile `agente-comercial` del gateway de `infra_hermes`. No toca el bridge,
  que sigue en `4fad449`, ni ningun otro profile.

## 1. El agente comercial dejo de depender de una cuenta OAuth de Dan

**Antes**, medido el 2026-09-24 19:20 UTC en `/opt/data/profiles/agente-comercial/config.yaml`:
primario `anthropic / claude-sonnet-4-6` por un token OAuth (`ANTHROPIC_TOKEN`), fallback
`openai-codex / gpt-5.6-sol` por OAuth de dispositivo. La premisa de que el primario era
OpenAI resulto falsa: los profiles con OpenAI de primario eran `client-copilot` y
`client-copilot-correlation-review`.

**Desde el 2026-09-25 15:42 UTC**: primario `openrouter / z-ai/glm-5.2`, fallback
`openrouter / anthropic/claude-sonnet-4.6`. La `OPENROUTER_API_KEY` la cargo Dan como
variable del servicio `infra_hermes` en EasyPanel. Backup del archivo anterior en
`/opt/data/profiles/agente-comercial/private-backups/config.yaml.2026-09-25-154230`.

Verificado en el log del profile: los turnos posteriores al reinicio registran
`API call #1: model=z-ai/glm-5.2 provider=openrouter`.

El fallback se eligio de otro proveedor upstream a proposito, para que una caida de Z.AI
no arrastre tambien al suplente. **No se probo**: provocarlo exige un fallo real de GLM.

### Como se probo antes de cambiarlo

Con el override por request del api_server, que acepta `provider` y `model` en el cuerpo de
`POST /v1/chat/completions` y, con `provider` explicito, falla cerrado en vez de caer a las
credenciales globales. Eso permitio ejecutar el agente entero (SOUL, skills y tools del
profile) contra otro modelo **sin tocar `config.yaml` ni afectar a produccion**.

Cuatro casos reales del inbox 9, con el contexto que arma el bridge campo por campo:

| Caso | Mensajes del lead | Lo que decidio Claude en produccion |
|---|---|---|
| A (conv 172) | "Enviame el enlace" | `send_payment_link / payment_link_requested` |
| B (conv 173) | pregunta por descuento y cuotas | `handoff / commercial_exception` |
| C (conv 174) | "Hola, quiero mas info" | `ask_question / johanna_e2e_response` |
| D (conv 174) | no puede entrar al curso | `handoff / policy_requires_human` |

GLM coincidio **4 de 4** con Claude, antes y despues del cambio de SOUL.

⚠ La primera corrida dio un falso rojo en el caso A: el contexto de prueba no llevaba
`payment_link_action`, que el bridge agrega a todo contexto cuando `PAYMENT_LINK_ENABLED=true`,
y el SOUL manda el enlace solo si ese bloque viene habilitado. Un contexto de prueba que no
es identico al de produccion campo por campo mide otra cosa.

### El riesgo que quedo medido

**1 propuesta invalida en 37 llamadas** a GLM con este SOUL: JSON con coma final y sin
`captured_fields` ni `missing_fields`. Claude Sonnet 4.6 llevaba **0 en 91** shadows de
produccion. Una propuesta invalida se persiste como
`{"status": "failed", "reason": "invalid_agent_output"}`, que el bridge trata como terminal:
no la vuelve a pedir, y esa persona se queda sin respuesta.

La respuesta cruda quedo guardada en
`tests/fixtures/hermes_glm_truncated_proposal_20260925.json` y es el insumo del reintento
propuesto en el PR #182. **Mientras ese PR no se despliegue, el riesgo sigue abierto.**

## 2. El SOUL dejo de hablarle al lead de una prueba privada

El SOUL describia al agente como parte de "una prueba privada por WhatsApp con un unico
usuario autorizado" y repetia "para esta prueba" y "de esta release". GLM lo repetia al
lead. El PR #181 saco esas once frases conservando cada afirmacion que los tests del profile
exigen, incluida la advertencia de que la brand voice es provisional y no esta ratificada
por Johanna.

Desplegado al runtime el 2026-09-25 15:39 UTC copiando desde `origin/main`, no editando el
archivo en el servidor:

| Dato | Valor |
|---|---|
| md5 en el runtime | `ba54b79cd2dd8a294c01fad5d6b37ec9` |
| Tamanio | 17.685 bytes |
| Backup del anterior | `private-backups/SOUL.md.2026-09-25-153953` (md5 `4c4425f1…`) |
| Gateway | reiniciado con `/command/s6-svc -r /run/service/gateway-agente-comercial`, `api_server=connected` a los 20 s |

Verificado despues del deploy con los mismos cuatro casos: Claude 4/4 y GLM 4/4.

## 3. La imagen de Hermes quedo fijada por tag

El 2026-09-25 14:39 UTC un redespliegue de `infra_hermes` para cargar la variable nueva
trajo `nousresearch/hermes-agent:latest`, que ese dia era la 0.21.5. **Desde la 0.21.4
Hermes exige un unico gateway por host**, y el contenedor levanta cuatro servicios s6, uno
por profile. El gateway del profile `default` gano, se declaro STANDALONE y los otros tres
murieron al arrancar; s6 no los reintento.

El mensaje literal quedo en `/opt/data/logs/gateways/agente-comercial/current`:

> `Profile 'agente-comercial' does not get a gateway of its own.`
> `Exactly one gateway per host is the inbound process for every profile.`

**El agente comercial estuvo 21 minutos sin atender** (14:39 a 15:00 UTC). No entro ningun
lead en esa ventana, verificado contra la base de Chatwoot: cero mensajes entrantes en el
inbox 9 entre las 14:30 y las 15:00.

Se resolvio fijando `nousresearch/hermes-agent:v2026.8.31` (0.21.0, upstream `29112bef`).
La migracion a 0.21.5 con gateway unico (`hermes gateway migrate --multiplex`) es una tarea
propia y no urgente; hay que probarla con un profile de prueba antes.

⚠ `gateway_state.json` **mintio durante veinte minutos**: conservo `running` con el pid del
proceso muerto. La verdad estaba en `ps` (buscar `hermes -p agente-comercial gateway run`) y
en el log de s6 del profile.

⚠ El otro servicio con "hermes" en el nombre, `att1-production_att1-product-hermes`, corre
una imagen propia de EasyPanel (`easypanel/att1-production/att1-product-hermes`, construida
el 2026-09-07) y **no tiene el binario `hermes`**: no le aplica este riesgo. Verificado el
2026-09-25.

## 4. Lo que este documento no acredita

- **El fallback nunca corrio.** Que este declarado no prueba que funcione.
- **La tasa de invalidas de GLM en produccion no se midio**: desde el cambio de modelo y
  hasta el cierre de esta medicion no entro ningun lead, asi que los 37 datos son de
  laboratorio. El campo `attempts` que agrega el PR #182 es lo que va a permitir medirla.
- **El bridge no se toco** en ninguno de los tres cambios.
