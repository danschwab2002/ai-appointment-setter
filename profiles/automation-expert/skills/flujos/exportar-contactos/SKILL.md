---
name: exportar-contactos
description: "Una lista de contactos en CSV, para bajar"
---

# Pedir una lista de contactos

**Cargala antes de tu primera respuesta si alguien quiere una lista de personas de la base**: para mandarles un mail desde otra herramienta, para un sorteo, para la planilla del asesor. Por ejemplo: «pasame las que compraron para el mail», «necesito la lista de las que dejaron el formulario la semana pasada y no compraron», «exportame los contactos con la etiqueta black-2026». Si lo que quiere es mandarles un mensaje por WhatsApp, no es esta guía: es `programar-un-envio` o `envio-masivo`.

## Lo que hacés vos y lo que hace una persona

- **Vos armás el pedido** con `export_contacts`: los filtros y, si hace falta, las columnas. La respuesta trae `matched_count` (cuántas personas cumplen) y `page_url`, el link de la página.
- **El archivo lo baja un aprobador en esa página**, con «Entrar con Slack». Se baja **una sola vez** y el link **vence a los 30 minutos**: si se pasa, se pide de nuevo.
- **Nunca pegues ni pidas teléfonos, mails o nombres en este chat**, ni siquiera de una persona. El archivo es lo único que los trae. Si alguien te pide «decime los teléfonos», contestá que van en el archivo y mandá el link.
- Exportar no le manda nada a nadie.

## Los filtros (todos opcionales)

- **Etiquetas de Chatwoot**: `etiquetas` (tiene al menos una) y `sin_etiquetas` (no tiene ninguna). Si no sabés cómo se llama la etiqueta, mirala con `list_chatwoot_labels`. Por defecto se miran las conversaciones de los últimos 90 días (`etiquetas_dias`, hasta 365).
- **Compró o no**: `compro: true` o `compro: false`. Con `producto`, solo ese producto.
- **Dejó el formulario en unas fechas**: `formulario_desde` y `formulario_hasta` (AAAA-MM-DD, en la zona de la instancia, las dos incluidas). Con `producto`, solo el formulario de ese producto.
- **País del teléfono**: `paises` con el código de dos letras (MX, CO, AR).
- **Las bajas no salen** (quien tocó «No más mensajes»): `opted_out_excluded` dice cuántas quedaron afuera. Incluilas (`incluir_bajas: true`) solo si la persona lo pide con esas palabras, y avisale que esas personas pidieron no recibir más mensajes.

## Las columnas

Por defecto: nombre, teléfono (con el código de país), mail, etiquetas, si compró y la fecha del último formulario. Se pueden sumar `pais`, `baja` y `productos` (los que compró). Si es para un mail, proponé las de por defecto.

## El resumen

Antes de pedirla, en una sola ronda: para qué es, quiénes entran (los filtros en palabras), las columnas y que la baja un aprobador en la página en los próximos 30 minutos. Con el OK, llamá a `export_contacts` y mandá el número y `page_url` tal cual con lo que dice `note`.

## Después

- Para saber si ya se bajó, `get_contact_export` con el `export_id`: el estado (pendiente, bajado o vencido) y cuántas filas salieron.
- Con `contact_export_not_enabled`, esta instancia no tiene la exportación prendida: decilo así y no la armes por otro lado.
- Con `contact_export_labels_unavailable`, Chatwoot no dio las etiquetas enteras: probá de nuevo en un rato o pedila sin filtro ni columna de etiquetas. Con `contact_export_rate_limited`, se pidieron demasiadas en la última hora.
