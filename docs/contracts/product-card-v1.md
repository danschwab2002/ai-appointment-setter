# Contrato: el conocimiento de un producto nuevo (product-card-v1)

- **Estado:** escrito antes del código (MP-01 del plan de multiproducto, E-553, 2026-11-13). Describe la interfaz que van a construir MP-07, MP-11, MP-17, MP-18 y MP-20; no dice que nada esté construido, desplegado ni activado.
- **Contrato:** `product-card-v1`, versión `0.1.0` (propuesta).
- **Diseño:** «Multiproducto: el plan de implementación» (vault del OS, `coordinacion/etapa-1/multiproducto-plan-de-implementacion.md` §1.1-§1.3, E-551 reescrito en E-552), sobre el veredicto «C como esqueleto, con tres piezas de B y dos de A» (`multiproducto-comparacion.md` §3). Decisiones de Dan del 2026-11-13, D1-D8 (`decisiones/2026-11-13-multiproducto-decisiones-d1-d8.md`), resumidas en §9.
- **Depende de:** `price-guard-v1` (el extractor de montos valida el texto libre, §4), `commercial-knowledge-v1` (el conocimiento del principal, que no cambia), `agent-campaigns-v1` (el molde: borrador del AE, validación, página, lectura en vivo).
- **Código que lo va a implementar:** `src/bridge/agent_products.py` (validar, MP-07), `src/bridge/product_card.py` (render, MP-11), la migración de `agent_products` (MP-07) y la publicación con siembra (MP-08). Flag: `AGENT_PRODUCTS_ENABLED`, apagado por defecto; apagado, el bridge no llama a ninguna RPC de productos y todo sale byte a byte como hoy.

## 1. Qué es

El conocimiento de un producto que **no** es el principal de la instancia, para que el usuario lo sume solo con el Automation Expert y la página de aprobación, sin PR ni reinicio.

**No es una ficha con campos (D1).** Dan, 2026-11-13: «Lo importante es tener anotado qué información mínima tiene que existir sí o sí y que luego, fuera de eso, sea un texto libre». Por eso tiene tres partes:

1. **El mínimo obligatorio** (§2): solo lo que el código lee para rutear una compra, armar un link, cortar casos, contar clientas o correr la guarda de precio. El bridge lo valida y **rechaza** la propuesta si falta algo o no valida.
2. **El esqueleto recomendado** (§3): secciones sugeridas del texto libre. La validación solo **advierte**.
3. **El texto libre** (§4): el conocimiento en markdown, con tope de bytes y la regla del precio.

La pregunta para decidir si algo entra al mínimo es una sola: ¿lo lee el código para rutear, cobrar, cortar casos o la guarda? Si no, va al texto libre.

**Lo que no cubre:** el conocimiento del principal de ATT1 y de Maribel (D3: queda en su archivo firmado, `commercial-knowledge-v1`), los formularios de GHL por producto (D8: siguen en `[adaptadores.ghl]` del manifiesto, por PR) y el pack de varios productos de Hotmart con checkouts separados (un pack se vende como un producto de Hotmart).

## 2. El mínimo obligatorio

Un objeto JSON (`minimo`). El bridge rechaza la propuesta con el código de la columna «Error» y no la guarda como `validada`.

| Campo | Qué es | Quién lo usa | Validación | Error |
|---|---|---|---|---|
| `clave` | slug del producto (`forever-young`) | el foco (`conversation-focus-v1`), la etiqueta `cliente:<clave>`, `oferta.producto` de campañas, `producto:` de flujos; el `funnel_ref` se deriva de ella | `^[a-z0-9]+(-[a-z0-9]+)*$`, hasta 40 caracteres; única en la instancia; distinta de la clave del principal y de las de `[[productos]]` del manifiesto; no es una reservada (`principal`, `todos`, `ninguno`) | `product_key_invalid`, `product_key_taken` |
| `nombre` | cómo se llama el producto para la clienta | render, índice de productos, `product_names` de segmentos y campañas, link | texto no vacío, hasta 80 caracteres | `product_name_invalid` |
| `hotmart.producto_id` | ID del producto en Hotmart | `for_hotmart` (rutea compra, carrito, pago rechazado y ficha) | entero positivo; no es el de otro producto de la instancia (base, manifiesto o principal) | `hotmart_product_invalid`, `hotmart_product_taken` |
| `hotmart.hotlink` | el código del checkout de Hotmart | `send_payment_link` y la reserva del link | `[A-Za-z0-9]{4,32}`, el mismo de `_HOTLINK` del manifiesto (`src/bridge/instance_manifest.py:52`); el link lo arma solo el sistema | `hotlink_invalid` |
| `ofertas[]` | cada oferta que el recuperador atiende | link, catálogo de ofertas de la siembra (MP-08), guarda de precio, sincronización (MP-17) | al menos una | `offers_missing` |
| `ofertas[].codigo` | código de la oferta (`off=`) | link, ruteo de la compra, ofertas huérfanas | `[A-Za-z0-9]{4,32}`, el de `_OFFER_CODE` (`instance_manifest.py:51`); único en la instancia (ninguna oferta de otro producto, base o manifiesto, lo usa) | `offer_code_invalid`, `offer_code_taken` |
| `ofertas[].precio` | precio de lista de la oferta | guarda de precio, examen (MP-19), página (MP-20), sincronización | decimal mayor que 0, como texto (`"199"`, `"1397.00"`) | `offer_price_invalid` |
| `ofertas[].moneda` | moneda del precio | lo mismo | código ISO 4217 en mayúsculas (`USD`, `MXN`, `COP`) | `offer_currency_invalid` |
| `ofertas[].por_defecto` | la oferta que sale si nadie nombra otra | link | booleano; **exactamente una** en `true` | `default_offer_count` |
| `ofertas[].otros_montos[]` | opcional: `{monto, moneda, que_es}`. Cuotas («12 de 166»), precio tachado, precio en otra moneda o en otro país | guarda de precio (son montos permitidos) | `monto` decimal mayor que 0, `moneda` ISO 4217, `que_es` texto no vacío de hasta 80 caracteres; hasta 12 por oferta | `other_amount_invalid` |
| `no_se_puede_prometer[]` | lo que el agente nunca promete de este producto | render (va arriba del texto libre), examen, página | al menos un elemento; cada uno texto no vacío de hasta 200 caracteres; hasta 20 | `promises_missing`, `promise_invalid` |
| `relaciones` | opcional: `incluye_productos[]`, `no_se_vende_si_tiene[]`, `venta_cruzada[]` (§5) | corte de casos y pausa al comprar (MP-15), clienta por producto (MP-14) | cada elemento es la clave de un producto que existe en la instancia (base o manifiesto), distinta de la propia; sin ciclos en `incluye_productos` | `relation_unknown_product`, `relation_cycle` |
| `texto_libre` | el conocimiento en markdown | render (MP-11) | §4 | §4 |

Lo que **no** está en el mínimo, a propósito: qué incluye el producto, la garantía, el precio por país como tabla, las preguntas frecuentes, el tono. Todo eso va al texto libre. Un producto que no encaja en una forma fija (un evento de un día, un pack, una membresía) escribe lo suyo sin pelear con campos.

**Lo que se deriva y no se escribe:** el `funnel_ref` (de la clave), la etiqueta de Chatwoot `cliente:<clave>` y la del producto, y el binding de la base (lo siembra la publicación, MP-08, con el precio y la moneda de la oferta por defecto; el resto de las ofertas, una fila por oferta en el catálogo).

**Lo que sale de Hotmart:** cuando exista la lectura de Hotmart (MP-16), el ID, el hotlink, los códigos de oferta, el precio y la moneda los lee el sistema y se vuelcan al mínimo; el AE no los tipea. Mientras tanto, los da el usuario y la página los muestra al lado de lo leído.

## 3. El esqueleto recomendado

Secciones sugeridas del `texto_libre`, en este orden, como encabezados `##`:

1. **Qué es**
2. **Para quién es**
3. **Qué incluye**
4. **Precio y cómo se paga** (los montos, solo los del mínimo: §4)
5. **Garantía**
6. **Preguntas frecuentes**
7. **Qué hacer si ya es alumna**
8. **Tono**

La validación compara los encabezados `##` del texto libre con esta lista, sin distinguir mayúsculas ni acentos. Por cada sección que falta devuelve una **advertencia** `skeleton_section_missing:<seccion>` (por ejemplo `skeleton_section_missing:garantia`). Una advertencia **no** rechaza la propuesta: se guarda junto al producto (`advertencias`), la página de aprobación la muestra (MP-20) y el AE la usa como guía al armar el texto (MP-18). Secciones de más, otro orden u otros nombres no advierten nada.

## 4. El texto libre

Markdown, UTF-8. Validaciones, todas con rechazo:

| Regla | Error |
|---|---|
| Hasta **5.500 bytes** | `free_text_too_long` |
| No vacío | `free_text_empty` |
| Sin teléfonos, mails, secretos (claves, tokens) ni URLs de pago (`pay.hotmart.com`, `hotmart.com/…/checkout`, cualquier `?off=`) | `free_text_personal_data`, `free_text_secret`, `free_text_payment_url` |
| **Todo monto con moneda o símbolo que aparezca tiene que estar declarado** en `ofertas[].precio` u `ofertas[].otros_montos[].monto` de alguna oferta del mismo producto, con la misma moneda | `free_text_amount_not_declared:<monto> <moneda>` |

**La regla del precio** es la que hace posible que la guarda de precio bloquee desde el primer día (D4): para un producto nuevo, la única fuente de precios es el mínimo. Si el texto libre menciona un monto que no está declarado, la propuesta no valida y el AE lo corrige antes de que el producto exista. Así la guarda nunca bloquea un precio que el propio conocimiento enseñó. Qué cuenta como monto, cómo se normalizan los separadores y qué se compara está en `price-guard-v1` §2: el texto libre pasa por **el mismo extractor** que la guarda, con los montos del mínimo como permitidos. Un número sin moneda ni símbolo («8 programas», «30 días») no es un monto.

## 5. Las relaciones y la venta cruzada (D7)

- `incluye_productos[]`: lo que trae un pack. Al comprar el pack, el corte de casos y la pausa alcanzan también a esos productos (MP-15), y la persona cuenta como clienta de cada uno (MP-14).
- `no_se_vende_si_tiene[]`: a quien ya es clienta de alguno de esos productos no se le ofrece este (MP-14).
- `venta_cruzada[]`: **reservado, apagado.** Lista de `{producto, condicion}`: `producto` es la clave de un producto a cuyas clientas se les puede ofrecer este, y `condicion` un texto opcional de hasta 200 caracteres. Se valida (las claves existen) y se guarda desde MP-07; vacío por defecto.

**Mientras no exista el flag `AGENT_PRODUCT_CROSS_SELL_ENABLED`, `venta_cruzada` no cambia nada:** el render (§6) no la incluye, el índice de productos no la nombra y rige la regla del SOUL común (`profiles/agente-comercial-comun/SOUL.md:113` en `262d2f9`): «Otro producto: contestá y pedí su enlace sólo si ella lo pide, sin descuento». Esa regla vive **solo** en el SOUL; ningún módulo del bridge la repite, para que el flag futuro la reemplace en un solo lugar.

**Prenderla después** es un encargo propio, fuera del plan de multiproducto: el flag, una sección del SOUL común condicionada (cuándo ofrecer, sin descuento salvo campaña), el render de la relación en el índice y sus pruebas de conversación. **Sin migración ni cambio de este contrato**: el dato ya existe y `es_clienta_de(clave)` (MP-14) es lo que necesita para saber a quién ofrecerle qué.

## 6. El render que recibe el agente

Determinista y con huella (sha256 del texto renderizado), lo arma MP-11:

1. Una cabecera corta: el nombre, cada oferta con su precio y moneda (la por defecto marcada) y sus `otros_montos` con su `que_es`.
2. `no_se_puede_prometer`, como lista.
3. El texto libre, tal cual.

No renderiza `hotmart.*`, los códigos de oferta, `relaciones` ni las advertencias. **El render entero no pasa de 6.000 bytes**; si una propuesta lo supera, no valida (`render_too_long`). Va como `system` aparte, solo cuando el producto está en foco (`conversation-focus-v1`), dentro del tope del turno de 40.000 bytes (`MAX_TURN_SYSTEM_BYTES`, `src/bridge/segment_knowledge.py:56`). Los topes de 5.500 y 6.000 bytes son una propuesta: se confirman en MP-11 midiendo el turno con el conocimiento v9 de Maribel, el índice y un segmento lleno.

## 7. Ciclo de vida

El de las campañas del AE (`agent-campaigns-v1`): `borrador → validada → publicada → pausada | cerrada | vencida | descartada`, con `requested_by_kind = 'automation_expert'`, huella de lo validado y validación que vence a las 24 h. Publicar exige la validación vigente y la aprobación en la página; publicar siembra en la misma transacción el binding, el catálogo de ofertas, el scope de intención y la política de compra (lo que hoy imprime `scripts/provision_instance_product.py`), idempotente (MP-08). Pausar y cerrar pausan primero los flujos y las campañas del producto. Una revisión nueva (otra oferta, otro texto) es otra fila con su propia validación.

Una campaña viva **por producto**, no por instancia (D6): MP-08 cambia el índice `agent_campaigns_one_live` (`supabase/migrations/20261108005000_agent_campaigns_v1.sql:212`) a (instancia, producto).

## 8. Ejemplo

```json
{
  "clave": "forever-young",
  "nombre": "Black Pack Forever Young",
  "hotmart": {"producto_id": 1234567, "hotlink": "X00000000Y"},
  "ofertas": [
    {
      "codigo": "abcd1234",
      "precio": "199",
      "moneda": "USD",
      "por_defecto": true,
      "otros_montos": [
        {"monto": "1361", "moneda": "USD", "que_es": "precio tachado"}
      ]
    }
  ],
  "no_se_puede_prometer": [
    "resultados de salud o de peso en un plazo",
    "bonos fuera de la fecha del en vivo"
  ],
  "relaciones": {
    "incluye_productos": ["el-protocolo"],
    "no_se_vende_si_tiene": [],
    "venta_cruzada": []
  },
  "texto_libre": "## Qué es\n…\n## Precio y cómo se paga\nUSD 199 (antes USD 1.361), de por vida.\n…"
}
```

El ID, el hotlink y el código de oferta son inventados: el ejemplo no describe ningún producto dado de alta. Si el texto libre dijera «USD 149», la propuesta no validaría (`free_text_amount_not_declared:149 USD`). Si no tuviera `## Garantía`, validaría con la advertencia `skeleton_section_missing:garantia`.

## 9. Las decisiones que este contrato toma (Dan, 2026-11-13)

- **D1:** mínimo obligatorio + esqueleto que advierte + texto libre; la regla del precio en el texto (§2-§4).
- **D2:** quién decide de qué producto se habla: el sistema, con señales (`conversation-focus-v1`).
- **D3:** el conocimiento de Maribel y de ATT1 queda como está, en su archivo firmado; solo lo nuevo usa este contrato.
- **D4:** el aviso de ofertas huérfanas y la guarda de precio, los dos desde el día 1, la guarda bloqueando (`hotmart-unknown-offers-v1`, `price-guard-v1`).
- **D5:** el payload del evento huérfano se guarda 72 h, solo para reproceso (`hotmart-unknown-offers-v1`).
- **D6:** una campaña viva por producto (§7).
- **D7:** venta cruzada apagada, con el lugar reservado (§5).
- **D8:** formularios de GHL por producto, afuera.

## 10. Errores y advertencias

Rechazo: `product_key_invalid`, `product_key_taken`, `product_name_invalid`, `hotmart_product_invalid`, `hotmart_product_taken`, `hotlink_invalid`, `offers_missing`, `offer_code_invalid`, `offer_code_taken`, `offer_price_invalid`, `offer_currency_invalid`, `default_offer_count`, `other_amount_invalid`, `promises_missing`, `promise_invalid`, `relation_unknown_product`, `relation_cycle`, `free_text_empty`, `free_text_too_long`, `free_text_personal_data`, `free_text_secret`, `free_text_payment_url`, `free_text_amount_not_declared:<monto> <moneda>`, `render_too_long`.

Advertencia (no rechaza): `skeleton_section_missing:<seccion>`.

Los códigos son nombres fijos; ninguno lleva texto del usuario salvo el monto y la moneda de `free_text_amount_not_declared`, que salen del propio texto libre y no son datos personales.
