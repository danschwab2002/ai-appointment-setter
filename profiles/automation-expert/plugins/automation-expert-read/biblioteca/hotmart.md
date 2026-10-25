# Hotmart

**Cómo llega.** Hotmart manda un webhook por evento a `POST /webhooks/hotmart` del bridge, en la versión 2.0.0 del formato (`src/bridge/hotmart.py`, `EVENT_VERSION`). El bridge lo guarda en la base y el worker lo procesa después.

**Los eventos que importan** (`src/bridge/hotmart.py`):

- `PURCHASE_APPROVED`, la compra aprobada. Primero corta los casos abiertos de la persona (deja de mandarle la recuperación) y después arranca los flujos nativos que dispara la compra, como la bienvenida (`src/bridge/worker.py`, el paso de la compra). Una venta sin caso del recuperador también los dispara.
- `PURCHASE_OUT_OF_SHOPPING_CART`, el carrito abandonado: el disparador del carrito.
- `PURCHASE_CANCELED`, la compra cancelada: con un método de pago de la tabla del manifiesto es el pago rechazado; si no, se guarda y no dispara nada.
- `PURCHASE_BILLET_PRINTED` y `PURCHASE_EXPIRED`, la ficha de pago (OXXO y parecidos) que se generó o que venció sin pagarse: se guardan para el seguimiento de la ficha.
- Cualquier otro tipo queda en la base como `failed` con `unsupported_persisted_event_type` y no dispara nada (lo lista `list_flows` en `events_without_flow`).

**El producto, siempre por su ID.** Un evento es de un producto de la instancia por el ID de Hotmart (`data.product.id`), nunca por el nombre: el nombre puede venir distinto (en ATT1 llegó uno con un espacio al final). Una compra del bump o del upsell es de otro producto y no cuenta como compra del principal.

**Qué trae una compra.** El mail y el teléfono del comprador, el nombre (`data.buyer.name`), el producto, la oferta y la transacción. El teléfono y el mail nunca se muestran.

**Lo que no sabe el bridge.** Por qué Hotmart rechazó un pago más allá del motivo que manda, y lo que pasó en el panel de Hotmart que no llegó por webhook.
