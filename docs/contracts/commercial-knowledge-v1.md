# Contrato: conocimiento comercial por instancia v1

- **Fecha:** 2026-09-28
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

No se acepta ninguna clave fuera de esta tabla.

## Reglas de carga

- **El bridge solo carga un conocimiento `aprobado`**, y uno aprobado lleva `aprobado_por` y `aprobado_el`. `setter validate` carga también los borradores, para revisarlos.
- **El `ally_ref` del conocimiento es el del manifiesto.** Un conocimiento de otra aliada es un error.
- **El bloque renderizado pesa como máximo 16 KB.** Hermes trunca los archivos de contexto largos y no documenta si trunca el `system` de cada pedido, así que el tope lo pone el producto.
- **Sin secretos:** el cargador rechaza valores con forma de credencial (claves `sk-`, tokens de Slack, GitHub o Meta, JWT, hex largos). Es la última red, no la primera.
- **Los comentarios no llegan al agente.** Ahí van las fuentes de cada dato, para quien revisa.

## El render

Orden fijo: título con la marca, identidad, voz, oferta, contenido, no confirmado, promesas prohibidas, límites sensibles y preguntas frecuentes. El mismo archivo produce siempre el mismo texto y el mismo `sha256`. Ese hash identifica con qué conocimiento respondió el agente.

## Versiones

Un cambio en el conocimiento es un archivo nuevo (`knowledge-v2.toml`) con `knowledge_version = 2`, y el manifiesto pasa a apuntarlo en `agente.conocimiento`. El archivo anterior queda en el repo de la instancia como historia.
