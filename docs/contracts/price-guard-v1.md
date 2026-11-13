# Contrato: la guarda de precio (price-guard-v1)

- **Estado:** escrito antes del código (MP-01 del plan de multiproducto, E-553, 2026-11-13). Describe la interfaz que va a construir MP-04 y que usan MP-03, MP-07, MP-11, MP-13, MP-19 y MP-24; no dice que nada esté construido, desplegado ni activado.
- **Contrato:** `price-guard-v1`, versión `0.1.0` (propuesta).
- **Diseño:** «Multiproducto: el plan de implementación» (vault del OS, `coordinacion/etapa-1/multiproducto-plan-de-implementacion.md`, MP-04 y MP-24), la guarda de precio del diseño C (`multiproducto-diseno-c-libre.md` §5.2) y la decisión D4 de Dan del 2026-11-13 (`decisiones/2026-11-13-multiproducto-decisiones-d1-d8.md`): «tanto el aviso como la guarda desde el día 1», la guarda bloqueando, sin semana de «solo registra».
- **Código que lo va a implementar:** `src/bridge/price_guard.py` (nuevo: el extractor y la comparación), `src/bridge/hermes.py` (la guarda como regla del bucle de intentos, el mismo enchufe de la corrección de E-486: `retry_correction`, `hermes.py:373`, y `_MAX_PROPOSAL_ATTEMPTS = 2`, `hermes.py:55`, en `262d2f9`), `src/bridge/app.py` (los montos permitidos del turno y la derivación) y la procedencia del turno.
- **Flag:** `AGENT_PRICE_GUARD_MODE`, `off` | `bloquea`. Ausente = `off`. Con `off` el bridge no extrae nada y todo sale byte a byte como hoy. Otro valor: el bridge no arranca (`price_guard_configuration_invalid`). **No hay modo «registra»** (D4).

## 1. Qué resuelve

Que el agente nunca le diga a una persona un precio que nadie le enseñó. El prompt baja la probabilidad; la guarda la lleva a cero para lo que se puede medir, después del modelo y antes de entregar, igual que hoy pasa con las URLs (`url_in_reply`). Sirve sola en ATT1 y Maribel, sin ningún producto nuevo, y es la que deja dar de alta productos con el texto libre de `product-card-v1` sin que un monto inventado llegue a una clienta.

## 2. Qué es un monto

Un número que el texto ata a una moneda. El extractor (una función pura de `price_guard.py`, la misma que usa la validación de `product-card-v1` §4) devuelve una lista de `(monto, moneda)`:

- **Con código ISO 4217** antes o después: `USD 199`, `199 USD`, `MXN 1,397`, `1.397.000 COP`.
- **Con símbolo:** `$1,397`, `US$ 47`, `€ 50`. `US$` es `USD`; `$` solo es moneda `$` (sin código, ver §3).
- **Con palabra:** `199 dólares`, `1397 pesos`, `pesos mexicanos`, `pesos colombianos`. `dólares` es `USD`; `pesos mexicanos` es `MXN`, `pesos colombianos` es `COP`; `pesos` a secas es moneda `$`.
- **Cuotas:** `12 cuotas de $166`, `12 de 166`, `12x 166`, `12 pagos de 166 USD`: el monto es la cuota (`166`), no el número de cuotas.

**Separadores** (los de México y Colombia): un punto o una coma seguidos de **exactamente tres** dígitos son separador de miles (`1,397` y `1.397` son 1397; `1.397.000` es 1397000); seguidos de uno o dos dígitos, son decimales (`1397.50`, `1.397,50`). Se normaliza a decimal y se comparan valores: `USD 199.00` es `USD 199`.

**No es un monto:** un número sin moneda ni símbolo ni palabra de moneda («8 programas», «30 días», «12 meses»), un porcentaje («10 %», el cupón), una hora, una fecha, un número de conversación o de pedido.

## 3. Los montos permitidos de un turno

Un monto del `reply` pasa si está en los permitidos **de ese turno** con la misma moneda. Un monto con moneda `$` (sin código) pasa si el número está permitido en cualquier moneda. Los permitidos salen de lo que el agente recibió legítimamente en ese turno, más los datos estructurados:

1. **Los textos que el bridge le manda al modelo en ese turno**, pasados por el mismo extractor: el conocimiento base firmado (`commercial-knowledge-v1`), los segmentos del turno (`segment-knowledge-v1`), el bloque de la campaña viva (`agent-campaigns-v1`) y, desde MP-11, el render del producto en foco (`product-card-v1` §6). Por eso **no hace falta que el v9 de Maribel ni el v14 de ATT1 tengan sus precios en campos** (D3: no se tocan): se leen del mismo texto que lee el agente.
2. **El contexto del turno que arma el bridge** (la actividad de la persona, por ejemplo el monto de un pago rechazado). Default de este contrato, a confirmar con la corrida en seco (§7).
3. **Los datos estructurados:** `ProductCommerce.price`/`currency` del manifiesto (`src/bridge/instance_manifest.py:263`), `oferta.precio` y `oferta.precio_antes` de la campaña viva, y desde MP-11 `ofertas[].precio` y `ofertas[].otros_montos[].monto` del mínimo del producto en foco.
4. **`[guarda_precio] montos_extra`** del manifiesto de la instancia (§8).

**No son permitidos los montos que escribió la persona** en sus mensajes: el agente no confirma un monto que no está en el catálogo solo porque la clienta lo dijo. Si la corrida en seco (§7) muestra que esto bloquea respuestas buenas (por ejemplo, «sí, los 1.397 que pagaste»), se decide ahí, con el número medido.

Ejemplo: una campaña con «USD 199» en `se_puede_decir` deja el 199 USD permitido mientras esa campaña está en el turno. Una campaña que quiere decir un precio que no escribió en ningún lado no existe: el AE lo escribe.

## 4. Dónde corre y qué pasa con un bloqueo

La guarda corre **adentro del bucle de intentos** de la propuesta, después de que la propuesta es válida en su forma y antes de entregarla. Mira solo el `reply` (todas las decisiones: `ask_question`, `send_payment_link`, `qualified`, `disqualified`, `handoff`).

1. **Primer intento bloqueado:** la respuesta no sale. El segundo intento (el que ya existe) lleva la corrección de la guarda, con la regla `price_not_in_catalog` y los montos: «Tu respuesta anterior no se pudo usar: el monto USD 149 no está en lo que te pasamos; usá solo estos: USD 199, USD 1.361. Devolvé solo el objeto JSON.» (el texto exacto lo fija MP-04; lleva los montos bloqueados y los permitidos, nunca texto de la persona).
2. **Segundo intento sin bloqueo:** se entrega. La clienta recibe la respuesta buena con un llamado de más de latencia.
3. **Segundo intento también bloqueado:** la respuesta **no sale** y la conversación **se deriva** por el circuito de derivación de siempre, con `reason_code = "price_not_in_catalog"`. No hay tercer llamado.
4. **El segundo intento es compartido:** si el primero falla por la guarda y por otra regla (el formato, o el cambio de producto de `conversation-focus-v1` §5), la corrección las dice juntas. Si el primero falla por otra regla y el segundo por la guarda, se deriva con `price_not_in_catalog` (manda la regla del último intento).

**El link de pago no pasa por la guarda.** La guarda mira el texto; `send_payment_link`, la reserva y el link siguen igual. Si una respuesta bloqueada dos veces traía `decision = "send_payment_link"`, el link **no** sale en ese turno (el link solo sale junto con una respuesta que se entrega) y la nota de derivación dice que el agente pidió el link de tal producto, para que la persona del equipo lo mande.

## 5. Lo que queda registrado (sin datos personales)

En la procedencia del turno, una clave `price_guard`:

```json
{
  "mode": "bloquea",
  "attempts": [
    {"attempt": 1, "blocked": [{"monto": "149", "moneda": "USD"}]},
    {"attempt": 2, "blocked": []}
  ],
  "outcome": "retried_ok"
}
```

`outcome`: `passed` (el primero pasó), `retried_ok` (el segundo pasó), `handoff` (derivó). Con `off` no se escribe la clave. No se guarda el `reply` bloqueado ni texto de la persona: los montos de un catálogo no son datos personales. Un log por bloqueo: `agent_price_guard_blocked attempt=<n> conversation=<n> amounts=<n>` (la cuenta, no los montos ni el texto).

## 6. El aviso

Cada turno con un bloqueo (también los que el reintento salvó) se avisa en el canal de Slack de la aliada con el número de conversación de Chatwoot, los montos bloqueados y el `outcome`. Lo hace el script del OS de ofertas huérfanas (MP-03), que lee `price_guard` de la procedencia en solo lectura: un aviso por turno bloqueado. La derivación, además, avisa por su circuito de siempre: nadie tiene que mirar Chatwoot para enterarse.

## 7. La corrida en seco (MP-24), requisito para prender

Antes de pasar `AGENT_PRICE_GUARD_MODE` a `bloquea` en una instancia, se corre la guarda en seco sobre el historial real: un script del OS importa `price_guard.py` del árbol de la rama (sin red) y lo pasa por las respuestas salientes del bridge con precio de esa instancia (ATT1 bandeja 11, Maribel bandeja 13), con los textos que el agente recibió en cada turno (`agent_turn_provenance`, solo lectura). Informe: cuántas bloquearía, cuáles son errores reales del agente y cuáles falsos positivos, y los `montos_extra` que harían falta.

**Requisito de la subida que prende la guarda:** cero falsos positivos sobre respuestas que estaban bien, o los que haya resueltos con `montos_extra` (§8). Es la semana de «solo registra» hecha hacia atrás, con datos que ya existen.

## 8. `[guarda_precio] montos_extra`

Campo opcional nuevo del manifiesto de la instancia, para montos legítimos que no están en ningún texto (por ejemplo, una conversión de moneda que el agente hace siempre igual):

```toml
[guarda_precio]
montos_extra = [
  { monto = "47", moneda = "USD", que_es = "precio Black en dólares" },
]
```

`monto` decimal mayor que 0, `moneda` ISO 4217, `que_es` no vacío. No toca el conocimiento firmado (D3). Va en MP-04.

## 9. Vuelta atrás

`AGENT_PRICE_GUARD_MODE=off` en el entorno de la instancia y reinicio del bridge. Es operación (la hace la coordinadora con el OK de Dan); no hay migración que deshacer.

## 10. Lo que este contrato no hace

- No revisa productos ajenos en el link: esa guarda es de MP-13 (`conversation-focus-v1` §6).
- No revisa contenido sin números (un «incluye» inventado): eso lo cuida el examen de cinco respuestas (MP-19) y el texto que el agente recibe.
- No compara con Hotmart: la sincronización de precios es de MP-17; mientras el usuario no actualice el mínimo, la guarda sigue con el precio del mínimo.

## 11. Errores y códigos

`price_guard_configuration_invalid` (arranque), `price_not_in_catalog` (regla del intento y `reason_code` de la derivación), `agent_price_guard_blocked` (log).
