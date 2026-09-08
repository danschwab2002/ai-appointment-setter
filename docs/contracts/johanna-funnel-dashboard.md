# Contrato sanitario de observabilidad Johanna V2

- **Estado:** implementado localmente; despliegue y activación pendientes
- **Versión de evento:** `1.0.0`
- **Alcance:** observación append-only del navegador y proyección read-only del funnel
- **Fuera de alcance:** mensajes, PII, identificadores publicitarios, payloads libres, mutaciones comerciales y efectos externos

## Frontera de autoridad

- Supabase conserva los hechos durables del funnel.
- El bridge autentica y valida el contrato antes de invocar un único RPC atómico.
- El navegador nunca recibe credenciales Supabase ni escribe tablas directamente.
- Chatwoot sigue siendo la superficie operativa conversacional; el dashboard no recopila mensajes.

## Ingreso HTTP firmado

```text
POST /webhooks/johanna-funnel-events
Content-Type: application/json; charset=utf-8
X-Lancemos-Signature: sha256=<hex HMAC-SHA256 del body exacto>
```

El endpoint está default-off mientras `LEAD_PRECHECKOUT_SECRET` no exista. La
presencia de ese secreto existente activa el ingreso con independencia de
`LEAD_PRECHECKOUT_ENABLED`. El cuerpo máximo es 8 KiB; la firma se verifica
sobre los bytes recibidos antes de decodificar JSON.

Body cerrado:

```json
{
  "version": "1.0.0",
  "event_id": "01K4N9YQ2T7W3H5J8M6P0R1SVC",
  "event_type": "page_view",
  "occurred_at": "2026-09-08T08:00:00Z",
  "anonymous_session_id": "01K4N9YQ2T7W3H5J8M6P0R1SVD",
  "landing_ref": "ads-a",
  "offer_ref": "bxjge6zq",
  "utm": {
    "source": "meta",
    "medium": "paid_social",
    "campaign": "anxiety_vsl",
    "content": "creative_a",
    "term": null
  }
}
```

Reglas:

- `event_id` y `anonymous_session_id`: ULID Crockford uppercase de 26 caracteres;
- `event_type`: `page_view`, `preform_opened`, `preform_submitted` o `checkout_redirected`;
- `occurred_at`: timestamp ISO-8601 con zona, antigüedad máxima de 31 días y
  tolerancia futura máxima de 5 minutos respecto de la hora server-side,
  validada tanto por el bridge como por el RPC;
- `(landing_ref, offer_ref)`: uno de los seis pares publicados en `johanna_precheckout_landing_offers`;
- cada valor UTM: string nullable de hasta 128 caracteres;
- cualquier campo adicional falla cerrado, incluidos `email`, `phone`, `name`, `fbclid` y `payload`.

Respuestas:

- `202 {"status":"received",...}`: inserción nueva;
- `200 {"status":"duplicate",...}`: replay byte-semántico exacto;
- `409 {"status":"conflict",...}`: mismo `event_id` con cualquier campo durable distinto;
- `400/401/413`: contrato, firma o tamaño inválidos, sin llamada de persistencia;
- `503`: feature/configuración/persistencia no disponible.

## Persistencia

La migración canónica es
`20260907000200_johanna_funnel_observability_v1.sql`.

`public.johanna_funnel_events` contiene exclusivamente los campos cerrados del
evento y `admitted_at`. Tiene constraints físicos de versión, enums, ULID,
longitudes y FK al par landing/oferta. Un trigger de owner impide `UPDATE` y
`DELETE`.

El bridge sólo invoca:

```text
public.admit_johanna_funnel_event_v1(
  text, text, text, timestamptz, text, text, text,
  text, text, text, text, text
)
```

El RPC devuelve exactamente `inserted`, `duplicate` o `semantic_conflict`. En
replay compara todos los campos durables. `service_role` tiene `EXECUTE` sobre el
RPC pero no `SELECT`, `INSERT`, `UPDATE` ni `DELETE` sobre la tabla. `PUBLIC`,
`anon` y `authenticated` no tienen acceso.

## Dashboard V2

Firma PostgreSQL:

```text
public.read_johanna_funnel_dashboard_v2(
  p_window_days integer = 7
) returns table (...)
```

PostgREST:

```text
POST /rest/v1/rpc/read_johanna_funnel_dashboard_v2
```

El scope es fijo y no lo elige el caller:

- tenant `lancemos`;
- funnel `psicologajohanna`;
- producto `F106691755G` (comparación lowercase `f106691755g`);
- inbound scope `libre-de-ansiedad-inbound` versión 2, cuenta 1 e inbox 9;
- comandos, handoffs y opt-outs limitados a la cuenta 1 e inbox 9;
- sólo pares landing/oferta Johanna publicados.

`p_window_days` debe estar entre 1 y 31. El caller no puede elegir una fecha
histórica. La función fija `snapshot_at = statement_timestamp()` y define la
ventana actual `[snapshot_at - p_window_days, snapshot_at)`. Los estados
mutables representan exclusivamente su valor actual al ejecutar la consulta;
V2 no promete reconstrucción histórica.

La primera fila tiene `row_kind = meta`, `snapshot_at` y `window_start`, incluso
cuando no hay casos. Las demás filas tienen `row_kind = case` y repiten esos dos
timestamps, permitiendo que el consumidor rechace una respuesta inconsistente.

La correlación de observabilidad es exclusivamente:

```text
precheckout_submissions.external_submission_id
  = johanna_funnel_events.anonymous_session_id
```

V2 conserva las doce columnas sanitarias de caso de V1 y agrega:

| Campo | Tipo | Regla |
|---|---|---|
| `page_view_count` | bigint | conteo en `[window_start, snapshot_at)` |
| `preform_opened_count` | bigint | conteo en `[window_start, snapshot_at)` |
| `preform_submitted_count` | bigint | conteo en `[window_start, snapshot_at)` |
| `checkout_redirected_count` | bigint | conteo en `[window_start, snapshot_at)` |
| `last_funnel_event_at` | timestamptz nullable | última actividad correlacionada |
| `last_funnel_event_type` | text nullable | tipo del último evento; desempate por `event_id` |

V1 permanece instalado por compatibilidad de catálogo, pero la migración revoca
su `EXECUTE` a todos los roles API. Sólo `service_role` recibe `EXECUTE` sobre V2;
la función es `STABLE SECURITY DEFINER`, fija `search_path` y no contiene DML.

## Generador

```text
uv run python scripts/generate_johanna_funnel_dashboard.py \
  --live \
  --window-days 7 \
  --precheckout-outbound-enabled true|false \
  --output /opt/data/cache/johanna-funnel-dashboard-<UTC>.html
```

Variables requeridas: `SUPABASE_BASE_URL` y `SUPABASE_SERVICE_ROLE_KEY`.
`CHATWOOT_BASE_URL` y `CHATWOOT_ACCOUNT_ID` son opcionales pero deben aparecer
juntas; la cuenta debe ser exactamente la canónica `1`. La CLI invoca únicamente
V2 y sólo envía `p_window_days`; toma
`snapshot_at`/`window_start` de la fila `meta`, valida que todas las filas
pertenezcan al mismo snapshot, agrega tarjetas de los cuatro eventos y muestra la
última actividad sin exponer el ULID de sesión.

El HTML es autónomo, temporal, sin acciones de escritura ni contenido
conversacional. Los IDs UUID completos, credenciales, firmas, payloads, nombres,
email, teléfono, JID y transaction refs no se renderizan.
