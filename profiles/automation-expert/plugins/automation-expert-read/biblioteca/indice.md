# La biblioteca del Automation Expert

Lo que hay que saber de cada plataforma para entender un pedido y armar un flujo. No trae nada de una instancia (qué productos y ofertas tiene sale del almacén y de `list_flows`) ni de una persona. Cada tema se pide con `read_library(tema)`.

| tema | qué tiene |
|---|---|
| `hotmart` | los eventos que llegan de Hotmart, cuáles disparan un flujo y cuáles solo se guardan, y por qué el producto se reconoce por su ID |
| `ghl-formularios` | cómo llega el formulario de GHL (la intención con permiso de WhatsApp) y qué mail usa |
| `chatwoot-whatsapp` | las plantillas aprobadas, su categoría, la ventana de 24 h y las etiquetas |

Los eventos, en una línea cada uno:

- `compra` (Hotmart `PURCHASE_APPROVED`): dispara un flujo nativo, después de cortar lo que la persona tenía abierto.
- `carrito` (Hotmart `PURCHASE_OUT_OF_SHOPPING_CART`): el carrito abandonado.
- `pago_fallido` (Hotmart `PURCHASE_CANCELED` con un método de la tabla del manifiesto): el pago rechazado.
- `ficha_impresa` (Hotmart `PURCHASE_BILLET_PRINTED`) y `ficha_vencida` (Hotmart `PURCHASE_EXPIRED`): la ficha de pago (OXXO y parecidos), se guarda para su seguimiento.
- `intencion` (el formulario de GHL o de la landing): el primer contacto.
