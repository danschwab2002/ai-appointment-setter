# Contrato de observabilidad del funnel Johanna V1

- **Estado:** implementado localmente; despliegue y activación pendientes
- **Versión:** `1.0.0`
- **Frontera:** navegador → bridge firmado → RPC Supabase atómico

## Transporte

```text
POST /webhooks/johanna-funnel-events
Content-Type: application/json; charset=utf-8
X-Lancemos-Signature: sha256=<HMAC-SHA256 hex del body exacto>
```

El endpoint reutiliza `LEAD_PRECHECKOUT_SECRET` para el HMAC. La presencia del
secreto habilita este endpoint aunque `LEAD_PRECHECKOUT_ENABLED` sea `false`;
sin el secreto, responde `503 johanna_funnel_not_enabled`. El límite del body es
8 KiB y la firma se valida antes del parseo JSON.

## Body cerrado

```json
{
  "version": "1.0.0",
  "event_id": "01K4N9YQ2T7W3H5J8M6P0R1SVC",
  "event_type": "preform_opened",
  "occurred_at": "2026-09-08T08:00:00Z",
  "anonymous_session_id": "01K4N9YQ2T7W3H5J8M6P0R1SVD",
  "landing_ref": "ads-a",
  "offer_ref": "bxjge6zq",
  "utm": {
    "source": "meta",
    "medium": "paid_social",
    "campaign": null,
    "content": null,
    "term": null
  }
}
```

- `event_id` y `anonymous_session_id`: ULID Crockford uppercase, 26 caracteres.
- `event_type`: `page_view`, `preform_opened`, `preform_submitted` o `checkout_redirected`.
- `occurred_at`: ISO-8601 con zona, no anterior a 31 días ni posterior en más de
  5 minutos respecto de la hora server-side de admisión; bridge y RPC aplican
  los mismos límites fail-closed.
- landing/oferta: uno de los seis pares publicados por Johanna.
- UTM: los cinco campos exactos, nullable, máximo 128 caracteres cada uno.
- Todo campo adicional se rechaza. No se aceptan nombre, email, teléfono,
  `fbclid`, payload libre ni PII de otra clase.

## Persistencia e idempotencia

El bridge invoca sólo
`public.admit_johanna_funnel_event_v1(text,text,text,timestamptz,text,text,text,text,text,text,text,text)`.
El RPC devuelve una fila cerrada:

- `inserted`: nuevo `event_id`;
- `duplicate`: replay con todos los campos durables idénticos;
- `semantic_conflict`: mismo `event_id` y al menos un campo durable distinto.

La tabla `public.johanna_funnel_events` es append-only por trigger. Tiene RLS,
constraints físicos y FK al par landing/oferta. `service_role` sólo puede ejecutar
el RPC; no tiene DML ni lectura directa. `PUBLIC`, `anon` y `authenticated` no
pueden ejecutar el RPC ni acceder a la tabla.

## Respuestas HTTP

| Estado | Significado |
|---|---|
| 202 | insertado |
| 200 | replay exacto |
| 409 | conflicto semántico |
| 400 | transporte, JSON o contrato inválido |
| 401 | firma inválida |
| 413 | body mayor de 8 KiB |
| 503 | endpoint deshabilitado, configuración incompleta o persistencia no disponible |

La proyección y el generador derivados están definidos en
[Contrato sanitario de observabilidad Johanna V2](johanna-funnel-dashboard.md).
