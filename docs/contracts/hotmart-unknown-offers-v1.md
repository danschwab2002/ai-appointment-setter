# Contrato: las ofertas huérfanas de Hotmart (hotmart-unknown-offers-v1)

- **Estado:** escrito antes del código (MP-01 del plan de multiproducto, E-553, 2026-11-13). Describe la interfaz que van a construir MP-02 (el registro), MP-03 (el aviso, en el OS) y MP-17 (el reproceso); no dice que nada esté construido, desplegado ni activado.
- **Contrato:** `hotmart-unknown-offers-v1`, versión `0.1.0` (propuesta).
- **Diseño:** «Multiproducto: el plan de implementación» (vault del OS, `coordinacion/etapa-1/multiproducto-plan-de-implementacion.md`, MP-02, MP-03 y MP-17, y el hallazgo 2 de §2), la oferta huérfana del diseño C (`multiproducto-diseno-c-libre.md` §10.2) y las decisiones D4 y D5 de Dan del 2026-11-13 (`decisiones/2026-11-13-multiproducto-decisiones-d1-d8.md`).
- **Código que lo va a implementar:** `src/bridge/hotmart_unknown_offers.py` (nuevo), `src/bridge/app.py` (los dos primeros puntos de entrada y `/ready`), `src/bridge/worker.py` (el tercero), `src/bridge/supabase.py`, una migración con la tabla y sus dos RPC. En el OS: `.claude/scripts/integrations/ofertas_huerfanas.py` (el aviso).
- **Flags:** `HOTMART_UNKNOWN_OFFERS_ENABLED` (el registro; MP-02) y `HOTMART_UNKNOWN_REPROCESS_ENABLED` (el reproceso; MP-17). Los dos apagados por defecto. Apagados, el bridge no llama a ninguna RPC nueva y todo sale byte a byte como hoy.

## 1. Qué resuelve

Hoy, un evento de Hotmart de un producto o una oferta que la instancia no declara se pierde: sale con 200 y, en el mejor caso, una línea de log. El 11/11 eso dejó 14 de 24 compras del Black de ATT1 sin bienvenida, porque la oferta no estaba declarada (E-422) y nadie se enteró hasta mirar a mano. Este contrato hace que cada oferta huérfana **quede registrada** (sin datos personales), **se avise** en el canal de la aliada apenas aparece y, con el producto o la oferta ya dados de alta, **se reprocese** dentro de las 72 h.

Sirve solo en ATT1 y Maribel, sin ningún producto nuevo. Se prende en la misma subida que la guarda de precio (D4).

## 2. Los tres puntos de entrada

Una oferta huérfana sale hoy por tres lugares distintos (en `262d2f9`). Un registro que mire uno solo reportaría cero en las dos aliadas reales, que declaran un solo producto.

| # | Punto | Cuándo pasa | Dónde | `entrada` |
|---|---|---|---|---|
| 1 | **Sin ruta** | La instancia declara dos productos o más y el evento es de un producto que ninguno declara (`product_not_declared`), o de una oferta no declarada en un carrito, pago fallido o ficha (`offer_not_declared`) | `product_routes.py:171-203` (`for_hotmart` devuelve `HotmartUnrouted`); `app.py:13602-13616` (log `hotmart_event_unrouted`, respuesta `ignored`) | `sin_ruta` |
| 2 | **Una sola ruta, el parser descarta** | La instancia declara un solo producto: `for_hotmart` devuelve siempre el principal (`product_routes.py:188-189`) y el parser descarta el evento de otro producto o de otra oferta (carrito: `hotmart.py:220-227`; compra: `hotmart.py:283`) | `app.py:13935-13940` (`invalid_purchase_payload`), `app.py:14019-14024` y `:13632` (`invalid_cart_abandonment_payload`) | `parser_descarta` |
| 3 | **El despachador frena la oferta** | El evento se admitió, pero el envío de un flujo durable lleva una oferta que ningún producto declara (E-432) | `worker.py:3218-3240` (log `durable_followup_offer_not_declared`, motivo `recipient_offer_not_declared`, `worker.py:471`) | `despacho` |

**Un payload roto de verdad no deja fila.** En el punto 2, se registra solo si el payload es legible y trae un ID de producto y, si corresponde, un código de oferta, y el descarte es **porque** ese producto o esa oferta no son los del principal. Un payload sin ID de producto, sin transacción o sin comprador sigue siendo `invalid_*_payload` y no es una oferta huérfana.

**Lo que no cubre:** un evento de **otra cuenta** de Hotmart. El hottok es uno por instancia (`app.py:1544`) y el webhook lo exige antes de leer el cuerpo (401 `invalid_token`, `app.py:13478`): ese evento nunca llega a ninguno de los tres puntos. Sumar cuentas es otro contrato.

## 3. Qué cambia en la respuesta a Hotmart

Nada. Los tres puntos siguen respondiendo y terminando como hoy (`ignored` con su motivo, o el envío que no sale con `recipient_offer_not_declared`). El registro es un efecto de costado: si la llamada al registro falla, se loguea `hotmart_unknown_offer_record_failed` y el evento sigue su camino de siempre.

## 4. El registro (sin datos personales)

Tabla `hotmart_unknown_offers`, una fila por (tenant, producto de Hotmart, oferta, tipo de evento):

| Columna | Qué es |
|---|---|
| `tenant_ref` | la instancia |
| `hotmart_product_id` | el ID del producto del evento |
| `offer_code` | el código de oferta, o null si el evento no lo trae |
| `event_type` | `PURCHASE_APPROVED`, `PURCHASE_OUT_OF_SHOPPING_CART`, el pago fallido, la ficha… (el tipo de Hotmart) |
| `motivo` | `product_not_declared` u `offer_not_declared` |
| `entradas` | los puntos de §2 por los que apareció (`sin_ruta`, `parser_descarta`, `despacho`) |
| `conteo` | cuántos eventos distintos |
| `primera_vez`, `ultima_vez` | timestamps |
| `resuelta_en` | null mientras la oferta siga huérfana; se completa cuando se publica un producto u oferta que la cubre (MP-08) o a mano |

**Sin payload, sin mail, sin teléfono, sin nombre, sin transacción.** El ID del producto y el código de oferta son del catálogo de la aliada, no de la persona.

RPC (ejecutables solo por `service_role`, grants en `do $roles$ … if to_regrole(…)` como el resto de la cola):

- `record_hotmart_unknown_offer_v1(tenant, product_id, offer_code, event_type, motivo, entrada, event_ref_sha256)`: upsert idempotente. El mismo evento (`event_ref_sha256`, el hash del ID del evento de Hotmart o de la acción del despacho) repetido no suma al conteo; Hotmart reintenta y el despachador también.
- `list_hotmart_unknown_offers_v1(tenant, solo_abiertas boolean default true)`: las filas, para el aviso, `/ready` y `recuperador_estado.py`.

## 5. El aviso (MP-03, en el OS)

Un script del OS (`ofertas_huerfanas.py`) lee el registro de cada instancia en solo lectura (por el registro `instancias.py`), cada hora, y postea en el canal de Slack de la aliada con el bot de su conector, como `meta_salud.py` y `revision_por_vencer.py`:

- **Un aviso por oferta huérfana, la primera vez que aparece** (estado en un json al lado del script), con el producto de Hotmart, la oferta, el tipo de evento, el conteo y si es **una oferta nueva de un producto conocido** o **un producto desconocido**. Con el AE de productos (MP-18), el aviso dice qué pedirle para darla de alta.
- Un segundo aviso solo si la oferta sigue abierta y el conteo cruzó un umbral (default 10, a revisar con el uso).
- El mismo script avisa también cada turno bloqueado por la guarda de precio (`price-guard-v1` §6).

`/ready`, con `HOTMART_UNKNOWN_OFFERS_ENABLED` prendido, suma `hotmart_unknown_offers: {abiertas: <n>}`; apagado, `/ready` no cambia.

## 6. El reproceso (D5, MP-17)

Para que la bienvenida, el carrito o el pago rechazado salgan tarde pero salgan, el payload del evento huérfano se guarda **72 h, solo para reprocesarlo** cuando se da de alta el producto o la oferta (D5: tiene datos personales, por eso la retención corta y el uso único):

- Tabla aparte, `hotmart_unknown_offer_events`: la fila del registro, el ID del evento, el payload y `expira_en = recibido + 72 h`. Un barrido borra lo vencido; nada lo lee salvo el reproceso. RLS sin DML directo para los roles de la API.
- Al publicar un producto u oferta que cubre una oferta abierta (MP-08), los eventos guardados y sin vencer de esa oferta vuelven a entrar al webhook interno como si llegaran ahora, una sola vez cada uno, y se borran al terminar.
- Lo guarda solo `HOTMART_UNKNOWN_REPROCESS_ENABLED`. Apagado, MP-02 no guarda payload en ningún lado.

## 7. Errores y logs

- `hotmart_unknown_offer_recorded entrada=<…> motivo=<…> product_id=<n> offer=<…>` (info; sin datos personales).
- `hotmart_unknown_offer_record_failed entrada=<…>` (warning; el evento sigue su camino).
- `/ready`: `hotmart_unknown_offers` (§5).
- Los motivos existentes (`hotmart_event_unrouted`, `invalid_purchase_payload`, `invalid_cart_abandonment_payload`, `recipient_offer_not_declared`) no cambian de nombre.

## 8. Las decisiones que este contrato toma

- **D4 (Dan, 2026-11-13):** el aviso desde el día 1, en la misma subida que la guarda de precio.
- **D5 (Dan, 2026-11-13, el default):** el payload del evento huérfano, 72 h y solo para reproceso.
- **Default de este contrato:** los tres puntos de entrada registran en la misma tabla, con su `entrada`; un payload roto no es una oferta huérfana; el aviso es uno por oferta.
