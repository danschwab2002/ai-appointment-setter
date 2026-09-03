# Contrato del dashboard on-demand Johanna V1

- **Estado:** Retirado temporalmente por contención
- **Versión:** 1
- **Alcance:** proyección sanitaria read-only de casos comerciales durables
- **Fuera de alcance:** mensajes, PII, reportes conversacionales, mutaciones y servidor permanente

## Frontera de autoridad

- Supabase Cloud es la autoridad durable del funnel comercial.
- Chatwoot continúa siendo la superficie operativa del universo conversacional.
- El artifact sólo enlaza conversaciones canónicas ya persistidas; no consulta ni
  copia mensajes.

## RPC sanitario

Firma PostgreSQL:

```text
public.read_johanna_funnel_dashboard_v1(
  p_cutoff timestamptz,
  p_window_days integer = 7
) returns table (...)
```

PostgREST:

```text
POST /rest/v1/rpc/read_johanna_funnel_dashboard_v1
```

Body cerrado:

```json
{"p_cutoff":"2026-08-31T13:00:00Z","p_window_days":7}
```

`p_window_days` debe estar entre 1 y 31. La cohorte es
`[p_cutoff - p_window_days, p_cutoff)` según el timestamp raíz de cada caso.
La función es `STABLE SECURITY DEFINER`, fija
`search_path = pg_catalog, public, pg_temp`, revoca `PUBLIC`, `anon` y
`authenticated`, y concede sólo `EXECUTE` a `service_role`. El rol conserva cero
`SELECT` directo sobre `purchase_intents`.

Aunque PostgREST invoque la función mediante HTTP `POST`, el contrato es
semánticamente read-only: la función no contiene DML ni efectos externos.

## Forma de cada fila

| Campo | Tipo | Regla |
|---|---|---|
| `case_id` | UUID | Identificador interno; el HTML muestra sólo ocho hexadecimales |
| `case_type` | texto | `inbound`, `precheckout_only`, `hotmart_only`, `both` o `payment_failure` |
| `provenance` | texto | `controlled_test`, `simulator` o `unknown`; V1 no etiqueta producción sin evidencia explícita |
| `stage` | token sanitario | Última etapa durable observable |
| `commercial_outcome` | texto | `purchased` sólo con compra durable; en otro caso `unknown` |
| `control_outcomes` | `text[]` | Hechos independientes: handoff, opt-out, bloqueo, falla o ambigüedad |
| `created_at` | timestamptz | Timestamp raíz que define la cohorte |
| `updated_at` | timestamptz | Último timestamp durable usado para antigüedad |
| `conversation_id` | UUID nullable | Referencia interna; nunca se renderiza completa |
| `chatwoot_conversation_id` | bigint nullable | Sólo se usa para construir el enlace canónico |
| `chatwoot_status` | texto nullable | Snapshot opcional; V1 implementada no consulta la API conversacional |
| `attention_reasons` | `text[]` | Reason codes sanitarios, sin texto libre |

El resultado separa compra, handoff y opt-out; no interpreta `resolved` de
Chatwoot como resultado comercial.

## CLI

```text
uv run python scripts/generate_johanna_funnel_dashboard.py \
  --live \
  --window-days 7 \
  --precheckout-outbound-enabled true|false \
  --output /opt/data/cache/johanna-funnel-dashboard-<UTC>.html
```

Variables obligatorias:

- `SUPABASE_BASE_URL`;
- `SUPABASE_SERVICE_ROLE_KEY`.

Variables opcionales, que deben aparecer juntas:

- `CHATWOOT_BASE_URL`;
- `CHATWOOT_ACCOUNT_ID`.

No se usa token Chatwoot. La URL sólo habilita enlaces con forma
`/app/accounts/{account}/conversations/{conversation}`.

El argumento `--precheckout-outbound-enabled` es obligatorio en modo live y debe
reflejar el gate desplegado exacto. Si es `false`, un caso precheckout `reserved`
recibe el hecho visual `outbound_blocked_by_configuration`; no se infiere envío.

En éxito, stdout contiene únicamente la ruta del HTML. Todo error devuelve exit
`2`, stderr `dashboard_generation_failed` y no escribe un artifact parcial. No
hay retries automáticos; un timeout, `502` o respuesta ambigua falla cerrado.

`--snapshot <archivo>` existe para fixtures o snapshots locales confiables. La
misma allowlist, validación de UUID, tokens y timestamps se aplica antes de
renderizar.

## Artifact HTML

- autónomo, temporal y sin dependencias externas;
- tarjetas y funnels calculados sobre la cohorte completa;
- detalle limitado a los primeros 100 casos, con truncación explícita;
- filtros client-side sólo sobre ese detalle sanitario;
- buckets no terminales `<1 h`, `1–24 h` y `>24 h`;
- estados de fuente `complete`, `partial` o `unavailable`;
- enlaces a Chatwoot, nunca contenido de conversaciones;
- sin acciones de escritura, backend ni proceso permanente.

Supabase se marca `complete` únicamente después de una respuesta RPC íntegra y
validada. Si falla, no se genera HTML. Chatwoot es `partial` cuando se configuraron
enlaces y `unavailable` en caso contrario; V1 nunca lo marca `complete` porque no
consulta metadata conversacional.

## Privacidad

La proyección y el renderer excluyen payloads, mensajes, notas, nombre, email,
teléfono, destination/JID, transaction refs, firmas, tokens y claves. Campos
semánticos dinámicos sólo aceptan tokens `a-z0-9_:-`; valores no conformes fallan
cerrado sin eco del dato rechazado.

## Compatibilidad y despliegue

La CLI live requiere que la migración
`20260831000100_johanna_funnel_dashboard_read.sql` esté aplicada. Antes de esa
migración, el RPC responde inexistente y la CLI falla cerrado. La implementación
no amplía grants directos de tablas ni requiere cambiar el bridge productivo.

La evidencia del despliegue inicial y del primer reporte live está en
[Johanna funnel dashboard release](../operations/2026-09-03-johanna-funnel-dashboard-release.md).

El acceso live quedó revocado después de que una revisión adversarial detectara
falta de scoping por tenant y semántica histórica incompleta respecto de
`p_cutoff`. La CLI debe fallar cerrado hasta que una migración forward-only
corregida sea aceptada y desplegada.
