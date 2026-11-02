# Hotmart

**Cómo llega.** Hotmart manda un webhook por evento a `POST /webhooks/hotmart` del bridge, en la versión 2.0.0 del formato (`src/bridge/hotmart.py`, `EVENT_VERSION`). El bridge lo guarda en la base y el worker lo procesa después.

**Los eventos que importan** (`src/bridge/hotmart.py`):

- `PURCHASE_APPROVED`, la compra aprobada. Primero corta los casos abiertos de la persona (deja de mandarle la recuperación) y después arranca los flujos nativos que dispara la compra, como la bienvenida (`src/bridge/worker.py`, el paso de la compra). Una venta sin caso del recuperador también los dispara.
- `PURCHASE_OUT_OF_SHOPPING_CART`, el carrito abandonado: el disparador del carrito.
- `PURCHASE_CANCELED`, la compra cancelada: con un método de pago de la tabla del manifiesto es el pago rechazado; si no, se guarda y no dispara nada. Del rechazo salen los datos con los que el pago rechazado elige su versión (`crear-un-flujo`): el motivo que manda Hotmart da `tipo_de_rechazo` (saldo insuficiente es `sin_saldo`; cualquier otro, `banco`), y dos rechazos o más del mismo pago dan `rechazos: varios`.
- `PURCHASE_BILLET_PRINTED` y `PURCHASE_EXPIRED`, la ficha de pago (OXXO y parecidos) que se generó o que venció sin pagarse: se guardan para el seguimiento de la ficha.
- Cualquier otro tipo queda en la base como `failed` con `unsupported_persisted_event_type` y no dispara nada (lo lista `list_flows` en `events_without_flow`).

**El producto, siempre por su ID.** Un evento es de un producto de la instancia por el ID de Hotmart (`data.product.id`), nunca por el nombre: el nombre puede venir distinto (en ATT1 llegó uno con un espacio al final). Una compra del bump o del upsell es de otro producto y no cuenta como compra del principal.

**Qué trae una compra.** El mail y el teléfono del comprador, el nombre (`data.buyer.name`), el producto, la oferta y la transacción. El teléfono y el mail nunca se muestran.

**El pago rechazado, con sus versiones (cómo se escribe).**

Es el único flujo nativo con condiciones (`flow-engine-v2` §27). Un solo mensaje por persona: la condición elige cuál.

- **Se escribe como una cadena** al final de los pasos, después de la espera: una `condicion` con su `entonces`, y en el `si_no`, como único paso, la condición siguiente; el último `si_no` es «todos los demás». Hasta tres condiciones (cuatro versiones). Gana la primera que se cumple, así que el orden es el de ATT1: primero el descuento, después los dos rechazos, después el saldo.
- **Los tres datos** (solo con el pago rechazado): `descuento_previo` (`si` | `no`: si ya le mandamos un mensaje con el descuento antes de este rechazo), `rechazos` (`uno` | `varios`: los rechazos de este pago, también los que llegaron seguidos) y `tipo_de_rechazo` (`sin_saldo` | `banco`: sin saldo o sin límite, o cualquier otro motivo del banco).
- **Cada versión es una sola plantilla**, sin esperas adentro: la espera va antes de la cadena y es la misma para todas.
- **El botón del checkout** (`boton: {link: checkout, cupon: <código>}`): nuestro link al pago, con el cupón fijo de la instancia sumado (en Maribel, `PROTOCOLO10`). La plantilla tiene que traer el botón de link al pago primero y, si lo tiene, «No más mensajes» después. El cupón por persona (`personal`) todavía no va en un flujo nativo.
- Así queda el de Maribel:

```yaml
pasos:
  - esperar: 15m
  - condicion: {dato: descuento_previo, es: [si]}
    entonces: [{plantilla: maribel_pago_rechazado_cupon_01, variables: [nombre, producto], boton: {link: checkout, cupon: PROTOCOLO10}}]
    si_no:
      - condicion: {dato: rechazos, es: [varios]}
        entonces: [{plantilla: maribel_pago_rechazado_dos_veces_01, variables: [nombre, producto], boton: {link: checkout, cupon: PROTOCOLO10}}]
        si_no:
          - condicion: {dato: tipo_de_rechazo, es: [sin_saldo]}
            entonces: [{plantilla: maribel_pago_rechazado_saldo_01, variables: [nombre, producto], boton: {link: checkout, cupon: PROTOCOLO10}}]
            si_no: [{plantilla: maribel_pago_rechazado_banco_01, variables: [nombre, producto], boton: {link: checkout, cupon: PROTOCOLO10}}]
```

- **Las ventas** de cada versión vienen en `sales.by_branch`, por plantilla: `sends` (los mensajes que salieron) y `purchases` (las compras después).

**Lo que no sabe el bridge.** Por qué Hotmart rechazó un pago más allá del motivo que manda, y lo que pasó en el panel de Hotmart que no llegó por webhook.
