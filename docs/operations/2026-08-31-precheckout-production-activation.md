# Activación productiva del first-touch precheckout — 2026-08-31

- **Estado:** activado y entregado en producción
- **Caso:** interés autorizado sin inicio observable de checkout Hotmart
- **Template:** `johanna_interes_precheckout_01`, `es_EC`, Marketing
- **Alcance de evidencia:** bridge → Chatwoot → Meta/WABA → callback `delivered`
- **No contiene:** PII, texto conversacional, teléfonos, IDs externos ni credenciales

## Preflight

La plantilla apareció sincronizada en el inbox WABA productivo con estado
`APPROVED`, idioma `es_EC`, categoría Marketing, body y tres botones. La autoridad
durable reportó timer y first-touch activos, cero `request_started`, cero
`delivery_unknown` y un comando vencido/reservado.

## Hallazgo durante la primera activación

La primera apertura del gate produjo aceptación sincrónica de Chatwoot, pero el
mensaje pasó luego a `failed` por el error estable de Meta `#132000`: la plantilla
esperaba dos parámetros y el sender había construido uno. El gate outbound se
cerró inmediatamente; timer y reserva permanecieron activos. No se hizo retry
ciego.

## Corrección publicada

PR `#91`, commit revisado `187fde74426cce74370be80aafad1f4bfed880b0` y
merge commit `3ee2b696b61ddda009ecc06ac58b4a2b68e02019` cambian únicamente el
binding del template a:

```text
{{1}} = buyer_name
{{2}} = product_name
```

La suite exacta previa a publicación terminó con `1227 passed`. El despliegue
posterior mostró el binding de dos parámetros dentro del task efectivo, `/health`
y `/ready` HTTP 200, worker/timer activos y outbound todavía apagado.

## Activación y reparación controlada

Se persistieron y verificaron en EasyPanel:

```text
HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=true
PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED=true
PRECHECKOUT_DELAYED_OUTBOUND_ENABLED=true
```

La reparación reutilizó el mismo mensaje `failed`, exigió `source_id` ausente,
hash semántico presente, inbox/template/idioma/categoría exactos y error
`#132000`. Sólo agregó el segundo parámetro desde el formulario durable y ejecutó
el servicio WABA nativo de Chatwoot sobre ese mismo registro.

Resultado observado:

```text
same_message=true
parameter_count=2
source_id_present=true
external_error_present=false
chatwoot_status=delivered
```

Postflight del bridge:

```text
ready_http=200
precheckout_delayed_database=precheckout_first_touch_ready
due=0
reserved=0
request_started=0
delivery_unknown=0
```

Esto acredita la activación productiva del caso y la entrega física del caso que
estaba reservado. No acredita respuesta del destinatario ni conversión comercial.
