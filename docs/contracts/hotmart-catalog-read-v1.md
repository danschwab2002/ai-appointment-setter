# Contrato: la lectura de Hotmart, productos y ofertas (hotmart-catalog-read-v1)

- **Estado:** propuesta, escrita con el código (MP-16 del plan de multiproducto, E-574, 2026-11-15). Describe la interfaz que van a usar MP-17 (sincronización diaria) y MP-18 (`read_hotmart_product` del AE); no dice que nada esté desplegado ni activado, y ningún camino vivo la llama todavía.
- **Contrato:** `hotmart-catalog-read-v1`, versión `0.1.0` (propuesta).
- **Diseño:** «Multiproducto: el plan de implementación» (vault del OS, `coordinacion/etapa-1/multiproducto-plan-de-implementacion.md` §3 MP-16 y §4 F6).
- **Depende de:** `product-card-v1` §2 (el mínimo al que se vuelca lo leído). Reusa `src/bridge/hotmart_api.py` (token y errores) sin cambiarlo.
- **Código:** `src/bridge/hotmart_catalog.py`. Flag `HOTMART_CATALOG_READ_ENABLED`, apagado por defecto.

## 1. Qué hace

Lee de la API de productos de Hotmart lo que el mínimo de `product-card-v1` necesita y el usuario no tiene que tipear: el ID, el nombre, el hotlink, y cada oferta con su código, precio, moneda y cuál es la principal. **Solo lectura:** dos `GET`; no crea, cambia ni borra nada en Hotmart.

## 2. Los endpoints

Documentación pública de Hotmart, leída el 2026-11-15 **[sin verificar contra una respuesta real: §6]**:

| Endpoint | Qué trae | Doc |
|---|---|---|
| `GET /products/api/v1/products` (filtros `id`, `status`, `format`; `max_results`, `page_token`) | `items[]`: `id`, `name`, `ucode` (UUID), `status`, `format`, `is_subscription`, `warranty_period` | https://developers.hotmart.com/docs/en/v1/product/product-list/ |
| `GET /products/api/v1/products/:ucode/offers` (`max_results`, `page_token`) | `items[]`: `code`, `name`, `description`, `price.value` (double), `price.currency_code`, `payment_mode`, `is_currency_conversion_enabled`, `is_smart_recovery_enabled`, `is_main_offer`, `direct_offer_link_for_creator` | https://developers.hotmart.com/docs/en/v1/product/product-offers/ |

La lista de productos **no trae el hotlink**: sale del `direct_offer_link_for_creator` de cada oferta (`https://pay.hotmart.com/<hotlink>?off=<codigo>`), solo si el host es `pay.hotmart.com`, la ruta es un único segmento con la forma `[A-Za-z0-9]{4,32}` y el `off=` es el código de esa misma oferta.

## 3. La interfaz

- `build_catalog_reader(env, credentials_path=…)`: con el flag apagado devuelve `None` **sin abrir el archivo de credenciales**. Prendido, lee el mismo archivo `KEY=VALUE` que el cupón (`HOTMART_CLIENT_ID`, `HOTMART_CLIENT_SECRET`, `HOTMART_BASIC`).
- `HotmartCatalogReader.read_product(product_id)`: el producto con todas sus ofertas (`HotmartProductReading`), o `None` si la cuenta no lo tiene.
- `find_product(product_id)`, `list_products(status=…)`, `list_offers(ucode)`.
- `minimo_from_reading(reading)`: los campos del mínimo (`nombre`, `hotmart.producto_id`, `hotmart.hotlink`, `ofertas[].codigo`/`precio`/`moneda`/`por_defecto`) y las advertencias. Falta lo que es del usuario: `clave`, `no_se_puede_prometer`, `texto_libre`, `otros_montos`, `relaciones`.

## 4. Reglas

1. **El precio** se vuelca como texto decimal (`"10"`, `"1397.5"`), por `repr` del double, con **hasta dos decimales**. Un texto se acepta solo con punto decimal y hasta dos decimales (`"1.397"` se rechaza: puede ser 1397 con punto de miles). Una oferta sin precio finito mayor que 0 o sin moneda de tres letras (se pasa a mayúsculas; no se chequea contra la lista ISO) **se descarta con advertencia** (`offer_skipped:<codigo>:price_invalid`), no se adivina. Un `payment_mode` distinto de `PAY_IN_FULL` o `UNIQUE_PAYMENT` advierte `payment_mode:<codigo>:<modo>`: en una suscripción o en cuotas el precio puede ser por período.
2. **La oferta por defecto** es la que Hotmart marca `is_main_offer` (un valor que no es booleano se lee como `false` y advierte `offer:<codigo>:is_main_offer_invalid`). Si no hay exactamente una, ninguna queda marcada y se advierte `main_offer_count:<n>`: lo elige el usuario (el mínimo exige exactamente una).
3. **El hotlink**: el `off=` del link tiene que ser el código de la oferta, byte a byte (con otra capitalización no se lee, a propósito). Si las ofertas dan más de uno, va `null` con `hotlink_ambiguous`; si ninguna lo da, `null` con `hotlink_missing`.
4. **Un producto que no está `ACTIVE`** se lee igual y se advierte `product_status:<estado>`. Un nombre de más de 80 caracteres advierte `name_too_long` (el mínimo lo rechazaría).
5. **Paginado entero o error:** se siguen los `next_page_token` hasta el final. Un token repetido es `…_pagination_loop` y pasar el tope de páginas (20 de 50) es `…_too_many_pages`: nunca se devuelve una lista cortada, porque la sincronización (MP-17) leería una oferta faltante como «desapareció».
6. **Errores**, con los tipos de `hotmart_api.py` y códigos cortos sin URL ni cuerpo: 401 (invalida el token), 429 y 5xx son transitorios; el resto de 4xx, permanentes. Un 404 en la **primera página** de la búsqueda por ID es «no está» (la doc dice que un valor que no existe responde 404); en una página siguiente es un error; un 404 en las ofertas de un producto ya encontrado es un error. Un cuerpo que no es JSON o no tiene la forma es transitorio.
7. **El ID se exige igual** en la respuesta: un filtro `id` que Hotmart ignore no devuelve otro producto. El `ucode` se valida como UUID antes de armar la ruta.

## 5. El flag

`HOTMART_CATALOG_READ_ENABLED`, apagado por defecto. En este contrato lo lee el módulo de un mapping que le pasa quien lo use; el que lo cablee en la configuración del bridge (MP-17) lo suma a `Settings` y a `.env.example`. Apagado no cambia nada: ningún módulo del bridge importa `hotmart_catalog` en `0.1.0` (lo prueba `tests/test_hotmart_catalog.py`).

## 6. Lo que falta medir

Los fixtures (`tests/fixtures/hotmart_api_products_list_doc_20261114.json`, `…_product_offers_doc_20261114.json`) son **los ejemplos de la documentación, no capturas**. Antes de que MP-17 prenda el flag hay que capturar F6: las dos respuestas reales, en solo lectura, con las credenciales de la instancia adentro de su contenedor, enmascaradas, y correr el parser contra ellas. Sin verificar: si `id` filtra de verdad, qué responde la búsqueda de un ID que no existe (200 vacío o 404), el máximo real de `max_results`, si `price.value` viene como número o como texto y si `direct_offer_link_for_creator` llega en producción. **Riesgo a cerrar con F6:** un 404 de una ruta o un permiso mal armado también se leería como «no está», y MP-17 no lo puede distinguir de un producto borrado hasta ver el `error` real del cuerpo; y si `id` no filtra, una cuenta con más de 1.000 productos termina en `too_many_pages`. **Plan B** (el `__NUXT_DATA__` del checkout público, sin credenciales) y **plan C** (a mano) no están en `0.1.0`: el plan B necesita una página capturada cruda, que hoy no está en `tests/fixtures/` (los `hotmart_checkout_*` guardan solo un resumen).
