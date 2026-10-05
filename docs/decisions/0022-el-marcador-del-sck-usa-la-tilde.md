# ADR-0022: el marcador del recuperador en el `sck` se separa con `~`

- Estado: aceptada
- Fecha: 2026-10-05
- Decide: Dan
- Reemplaza: la nota de la migración `20260925000100` («El marcador del recuperador conserva la |. No se unifica sin una decision nueva»)
- Implementación: migración `20261005000100` y bridge `1.3.2`

## Contexto

Desde el 2026-09-25 el estándar de tracking de Lancemos compone el `sck` del anuncio con `~`, en cinco o seis campos posicionales: `utm_source~utm_term~utm_content~utm_medium~utm_campaign[~utm_id]` (E10, E13 y E02 del estándar; `lancemos/core#51` y `#54`). El recuperador agrega su marcador al final del `sck` del anuncio (E46, migración `20260922000200`). La `|` del marcador se dejó a propósito, para distinguir los dos tramos: `<anuncio>|hermes|v1|<ULID>`.

El 2026-10-05 Dan vio el link del E2E del entrante de ATT1 (`sck=hermes%7Cv1%7C<ULID>`) y decidió que el setter use el mismo separador que el estándar, porque el tracking de Lancemos lee el `sck`. Una `|` que el resto del sistema ya no escribe es una forma más que todo lector tiene que conocer.

## Decisión

1. **El marcador se escribe `hermes~v1~<ULID>`.** Con el `sck` del anuncio delante, queda `<sck del anuncio>~hermes~v1~<ULID>`. Solo cambia el separador: los campos, su orden, `src=hermes` y `sck_format_version = 'v1'` quedan igual.
2. **Los lectores aceptan las dos formas, para siempre.** Son el bridge, los dos `CHECK` de `checkout_link_issuances`, el correlador y la admisión de la compra. Las dos formas no se mezclan: `hermes|v1~<ULID>` se rechaza. Los links que ya salieron con `|` siguen llegando, y una reserva anterior que se reusa sale como se escribió.
3. **La `|` del `sck` de un anuncio de un linaje viejo no se toca.** Se preserva, y en la URL se sigue encodeando a `%7C`.

## Consecuencias

- Quien parte el `sck` por `~` lee los campos del anuncio sin que el último arrastre el marcador. Con `|`, el último campo del anuncio (la campaña) quedaba pegado a `|hermes|v1|<ULID>`.
- Partido por `~`, el marcador agrega tres campos al final. Un lector que exija una cantidad exacta de campos rompe con las ventas del recuperador. El estándar ya lo dice (E46): el `sck` se lee por contenido, nunca por posición ni por cantidad. Sin `sck` de anuncio, el primer campo es `hermes`.
- Hotmart devuelve la `~` literal en `data.purchase.origin.sck`. Lo muestran 10 de 10 compras de Johanna con el `sck` del core, entre el 2026-09-26 y el 2026-10-01 (`tests/fixtures/hotmart_purchase_sck_shapes_20261005.json`).
- Los consumidores fuera de este repo no se re-verificaron. El principal es el tablero de tracking de Lancemos. Dan lo dio por cubierto porque el estándar ya usa `~`.
- **Orden de despliegue: el bridge va primero.** El bridge `1.3.1` rechaza la fila que devuelve la reserva con `~`, y el link no sale.
- **Después de la migración no hay vuelta atrás del bridge por debajo de `1.3.2`.** Las filas reservadas con `~` son inmutables. Volver atrás exige antes la migración inversa, que sirve solo para las dos reservas, porque los lectores ya aceptan las dos formas.
- **La regex del bridge es la misma que la de la base.** Si el bridge reconociera un `sck` que la base rechaza, la compra iría a la admisión, la base daría `22023` y Hotmart recibiría 503 en cada reintento.
