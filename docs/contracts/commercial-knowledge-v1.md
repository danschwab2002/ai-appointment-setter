# Contrato: conocimiento comercial por instancia v1

- **Fecha:** 2026-09-28. Enmendado el 2026-10-16: la forma de varios productos y el tope de 32.000 bytes.
- **Diseño:** [setter-producto-instalable-v1.md §4.4](../design/setter-producto-instalable-v1.md), [ADR-0021](../decisions/0021-setter-producto-instalable.md) decisión 6
- **Código:** `src/bridge/commercial_knowledge.py`

## Qué es

El SOUL del agente comercial es uno solo para todas las instancias: reglas, contrato de entrada y salida, link de pago, derivación y barandas. Lo que cambia con cada negocio vive en un archivo de conocimiento por instancia, `conocimiento/knowledge-v<N>.toml`, en el repo de la instancia.

El bridge lo carga al arrancar, lo valida, lo renderiza a markdown en un orden fijo y lo manda como mensaje `system` en cada pedido a Hermes. Hermes lo apila sobre el prompt del profile (`api_server.py` de la imagen `v2026.8.31`). **Lo que no está en el bloque no está confirmado**, y el agente aplica la política de derivación.

## Estructura

| Tabla | Claves obligatorias | Opcionales |
|---|---|---|
| `[cabecera]` | `knowledge_version` (entero), `estado` (`borrador` o `aprobado`), `ally_ref` | `aprobado_por`, `aprobado_el` (fecha) |
| `[identidad]` | `habla_en_nombre_de`, `presentacion`, `marca`, `idioma`, `tratamiento` | `audiencia`, `asistente` |
| `[voz]` | `resumen`, `reglas` (lista, no vacía) | `evitar`, `ejemplos` |
| `[oferta]` | `nombre`, `descripcion`, `precio`, `moneda` | `garantia`, `notas` |
| `[contenido]` | `componentes` (lista cerrada; vacía significa que se deriva) | `se_puede_decir` |
| `[no_confirmado]` | `items` (no vacía) | |
| `[promesas_prohibidas]` | `items` (no vacía) | |
| `[limites_sensibles]` | `vertical`, `reglas` (no vacía) | |
| `[[faq]]` | `pregunta`, `respuesta`, `fuente`, `vigencia` | |
| `[pagos]` (opcional, desde la 1.6.0) | `pais_principal`, `medios` (lista, no vacía: exactamente los medios que las ofertas tienen prendidos) | `reglas` |
| `[[pagos.por_pais]]` | `pais` y al menos uno de `transferencia` y `efectivo` | |
| `[[pagos.pasos]]` | `medio`, `pasos` (lista, no vacía) | |
| `[[pagos.rechazos]]` | `motivo`, `que_hacer` | `como_llega` (los textos con que el medio de pago informa ese motivo) |

No se acepta ninguna clave fuera de esta tabla.

### La forma de varios productos (desde 2026-10-16)

Una aliada puede vender más de un producto por el mismo número, como ATT1 y ATT2. Decisión de Dan del 2026-10-16: el agente recibe todo el conocimiento de la aliada, con lo común una sola vez y una sección por producto, y deduce de qué producto se habla. La forma de arriba sigue valiendo para un solo producto. Un archivo que declara `[[productos]]` usa esta otra:

| Tabla | Claves obligatorias | Opcionales |
|---|---|---|
| `[cabecera]`, `[identidad]`, `[voz]`, `[limites_sensibles]`, `[pagos]` y `[[faq]]` | como arriba: son lo común | |
| `[no_confirmado]` y `[promesas_prohibidas]` | `items` (no vacía): lo que vale para todos los productos | |
| `[se_puede_decir]` (opcional) | `items` (no vacía): lo que se puede decir del negocio, como quién es la experta | |
| `[[productos]]` (al menos uno) | `clave` (slug en minúsculas, única), `oferta` y `contenido` (como arriba) | `no_confirmado`, `promesas_prohibidas` (`items`, no vacía) y `[[productos.faq]]`: lo que es solo de ese producto |

Con `[[productos]]`, `[oferta]` y `[contenido]` no van arriba. Dos productos no comparten la clave ni el nombre de la oferta, que titula su sección. La clave es la que nombra al producto en el resto de la instancia.

### La sección de pagos

Es lo que el agente usa ante una compra fallida (ruta 3 del SOUL común, que la consulta antes de derivar). La declara la instancia entera: el producto no nombra ningún medio local. La transferencia y el efectivo cambian de nombre con el país (SPEI y OXXO en México, PSE y Efecty en Colombia) y con lo que cada oferta tiene prendido en la plataforma de pago, así que van en `por_pais`. `como_llega` sirve para que el agente reconozca el motivo en `recent_payment_failure.refusal_reason`, que trae el texto tal como lo manda el medio de pago, a veces en portugués o en inglés.

## Reglas de carga

- **El bridge solo carga un conocimiento `aprobado`**, y uno aprobado lleva `aprobado_por` y `aprobado_el`. `setter validate` carga también los borradores, para revisarlos.
- **El `ally_ref` del conocimiento es el del manifiesto.** Un conocimiento de otra aliada es un error.
- **El bloque renderizado pesa como máximo 32.000 bytes** (16 KB hasta la 1.5.0 y 20.000 bytes hasta la 1.8.0). Hermes trunca los archivos de contexto largos, como el SOUL a los 20.000 caracteres, pero no el `system` de cada pedido. Se leyó el 2026-10-16 en la imagen de producción `v2026.8.31`, la misma de ATT1: `gateway/platforms/api_server.py` junta los mensajes `system` en `ephemeral_system_prompt`, y `agent/conversation_loop.py` lo suma al prompt sin pasar por `_truncate_content`, que solo usa `agent/prompt_builder.py` para los archivos de contexto. El tope lo pone el producto por costo, porque cada turno paga el bloque entero, y alcanza para dos productos como los de ATT1.
- **Sin secretos:** el cargador rechaza valores con forma de credencial (claves `sk-`, tokens de Slack, GitHub o Meta, JWT, hex largos). Es la última red, no la primera.
- **Los comentarios no llegan al agente.** Ahí van las fuentes de cada dato, para quien revisa.

## El render

Orden fijo: título con la marca, identidad, voz, oferta, pagos (si está), contenido, no confirmado, promesas prohibidas, límites sensibles y preguntas frecuentes. El mismo archivo produce siempre el mismo texto y el mismo `sha256`. Ese hash identifica con qué conocimiento respondió el agente.

En la forma de varios productos, primero va lo común: el título, una línea que nombra los productos, identidad, voz, pagos, lo que se puede decir, no confirmado, promesas prohibidas, límites sensibles y preguntas frecuentes. Después, un título `# Producto: <nombre de la oferta>` por producto, en el orden del archivo, con su oferta, su contenido y, si los tiene, su no confirmado, sus promesas prohibidas y sus preguntas frecuentes. Las secciones se llaman igual que en la otra forma, que son los nombres que lee el SOUL común. Un archivo de la forma de un solo producto se renderiza igual que antes de la enmienda, byte a byte: el v7 de ATT1 sigue dando `ac5cc64fbcad…`.

## Versiones

Un cambio en el conocimiento es un archivo nuevo (`knowledge-v2.toml`) con `knowledge_version = 2`, y el manifiesto pasa a apuntarlo en `agente.conocimiento`. El archivo anterior queda en el repo de la instancia como historia.
