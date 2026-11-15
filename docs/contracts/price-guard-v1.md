# Contrato: la guarda de precio (price-guard-v1)

- **Estado:** escrito antes del código (MP-01 del plan de multiproducto, E-553, 2026-11-13). Describe la interfaz que va a construir MP-04 y que usan MP-03, MP-07, MP-11, MP-13, MP-19 y MP-24; no dice que nada esté construido, desplegado ni activado.
- **Contrato:** `price-guard-v1`, versión `0.1.1` (propuesta); historial al final.
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
4. **El segundo intento es compartido:** si el primero falla por la guarda y por otra regla (el formato), la corrección las dice juntas. El producto en foco ya no usa el segundo intento: con la rama B de `conversation-focus-v1` (0.2.0, decisión de Dan del 2026-11-14), un aviso del agente sobre otro producto no repite el turno; si el último intento lo trae, sale la pregunta fija (`conversation-focus-v1` §5.6). Si el primero falla por otra regla y el segundo por la guarda, se deriva con `price_not_in_catalog` (manda la regla del último intento).

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

## 12. Lo que fijó MP-04 al construirla (E-564, 2026-11-14)

Propuesta escrita junto con el código; la evidencia de que funciona queda para `docs/operations/` tras la corrida en seco (MP-24) y la subida que prenda el modo.

- **Código:** `src/bridge/price_guard.py` (el extractor `extract_amounts`, los permitidos `PriceGuard.allowed_for_turn`, la comparación `PriceGuard.blocked`, la corrección `correction_detail`), `src/bridge/hermes.py` (la regla `price_not_in_catalog` en el bucle de intentos, `retry_correction(..., price_detail=...)`), `src/bridge/agent_provenance.py` (`price_guard` en `context_added`, sin migración: la columna es `jsonb`) y `src/bridge/app.py` (el modo, la guarda armada desde el manifiesto, la derivación y la nota).
- **El reply de una derivación no se mira.** Al derivar el bridge no manda el `reply` (F-033); bloquearlo solo costaría un llamado y le pisaría al agente su `reason_code` (por ejemplo, el aviso de seguridad de `safety_risk`). Se aparta de §4 («todas las decisiones»), a propósito.
- **Las secciones `## Promesas prohibidas` y `## No confirmado` de un texto no dan permitidos.** La campaña Black de ATT1 (r3, capturada el 13/11) dice «Citar el precio tachado de USD 97» entre sus promesas prohibidas: leída entera, dejaba pasar USD 97.
- **Una cuota sin moneda de menos de 10 no es un monto** («2x1», «4 x 4»).
- **El contexto del turno** (§3.2) cuenta sin `messages` ni `known_fields`: lo que dijo la persona y lo que el agente capturó de ella no se permite.
- **El resultado `failed` del procesador** lleva `reason = "price_not_in_catalog"` cuando el último intento lo bloqueó la guarda (con un formato inválido en el último intento, manda el formato: `invalid_agent_output`), y `payment_link_requested` / `payment_link_product` si la propuesta pedía el link.
- **La nota del link que no salió** es una nota privada de Chatwoot con texto fijo, una por derivación, con el marcador `[supportmagician-guarda-precio:<handoff_request_id>]`. La nota de derivación de siempre muestra el motivo como código (`price_not_in_catalog`): una frase en `inbound_handoff_reason_sentence` necesita una migración y no va en este encargo.
- **`outcome`:** `passed` si ningún intento tuvo un bloqueo, `retried_ok` si un intento se bloqueó y el segundo se entregó, `handoff` si el turno terminó sin respuesta (por la guarda, por el formato o porque Hermes no contestó, después de al menos un intento evaluado).
- **La corrección** nombra los montos con miles con punto y decimales con coma (`MXN 1.204,31`), hasta 12 permitidos; sin ningún permitido dice «no escribas ningun monto».
- **Formas que lee además del §2:** el código pegado al signo (`USD$97`, `MXN$1,397`, `COL$`, `U$S`, `1397 $MXN`), `dls`/`dlls`, `1397$`, y el código escrito después de un `$` (`$1,397 USD` es USD, no `$`).
- **Lo que no es un monto, además:** un «N x M» o «N meses de M» seguido de una unidad que no es plata (minutos, días, meses, cuotas, sesiones…), el total de cuotas («1 de 12 cuotas»), un año como cuota sin moneda («3 meses de 2026»), y «12 de 166» sin «cuotas», «meses», «pagos» o «mensualidad» a 40 caracteres.
- **Lo que no lee (límite conocido):** miles con espacio (`$1 397`), «100 mil pesos», «1,5 millones». No se leen como monto, así que tampoco se bloquean.
- **Un intento que la guarda no miró** (una derivación, una salida que no es un objeto) va en `attempts` con `"skipped": true`.

## 13. Historial

- 0.1.0 (2026-11-13, E-553, #460; completado por MP-04 en E-564, #464): el contrato y lo que fijó MP-04 (§12).
- 0.1.1 (2026-11-14, E-570): §4.4 alineado con la rama B de `conversation-focus-v1` 0.2.0: el segundo intento ya no lo comparte el cambio de producto. Solo documentación; el código de MP-04 no cambia (no implementa el foco).
