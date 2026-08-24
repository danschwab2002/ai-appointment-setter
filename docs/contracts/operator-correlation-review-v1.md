# Consulta de correlaciones pendientes para operadores — V1

- **Estado:** Implementado localmente; migración y runtime todavía no desplegados
- **Versión:** `1.0.0`
- **Ámbito:** lectura bajo demanda de `unmatched`, `ambiguous` y `conflict`
- **Efectos externos:** ninguno

## 1. Propósito

El Client Copilot puede listar y explicar correlaciones determinísticas que quedaron bloqueadas. No elige candidatos, no modifica el ledger y no invoca IA para decidir identidad.

```text
operador pregunta
  -> tool Hermes read-only
  -> endpoint interno autenticado del bridge
  -> RPC SECURITY DEFINER acotada por tenant + funnel
  -> proyección ya enmascarada
  -> explicación al operador
```

## 2. Fuente de verdad y elegibilidad

La fuente autoritativa continúa siendo:

- `hotmart_purchase_intent_correlations`;
- `hotmart_purchase_intent_correlation_candidates`;
- `hotmart_purchase_intent_event_identities`;
- `hotmart_purchase_intent_scopes`;
- `purchase_intents` sólo para evidencia de candidatos.

Una fila se muestra únicamente si cumple todo lo siguiente:

```text
manual_handoff_required = true
purchase_intent_id is null
outcome in (unmatched, ambiguous, conflict)
scope.tenant_ref = tenant configurado
scope.funnel_ref = funnel configurado
```

Una correlación sin `scope_id` durable no puede atribuirse con seguridad a un tenant y queda excluida fail-closed de V1. Esto incluye `scope_not_configured` sin ownership durable. No se corrige mediante payload, heurística ni suposición single-tenant.

Cada candidato proyectado debe coincidir además con `tenant_ref`, `funnel_ref`,
`product_ref` y `offer_ref` del scope de la correlación. Una asociación durable cruzada
se excluye y nunca proyecta identidad de otro tenant.

## 3. RPC autorizadas

```text
list_operator_unresolved_correlations(
  p_tenant_ref text,
  p_funnel_ref text,
  p_limit integer default 20,
  p_webhook_event_id uuid default null
) -> table(case_data jsonb)

get_operator_unresolved_correlation(
  p_tenant_ref text,
  p_funnel_ref text,
  p_webhook_event_id uuid
) -> table(case_data jsonb)
```

Ambas funciones son `STABLE SECURITY DEFINER`, fijan `search_path=pg_catalog, public, pg_temp` y conceden `EXECUTE` sólo a `service_role`. `anon`, `authenticated` y `public` no pueden ejecutarlas. `service_role` conserva revocado el `SELECT` directo sobre las tablas protegidas.

El orden de lista es `observed_at desc, webhook_event_id asc`. `p_limit` admite `1..50`.

## 4. Endpoint del bridge

El bridge registra los endpoints sólo cuando `OPERATOR_CORRELATION_READ_ENABLED=true`:

```text
GET /internal/operator/correlations/unresolved?limit=20
GET /internal/operator/correlations/unresolved/{case_id}
Authorization: Bearer <OPERATOR_CORRELATION_READ_TOKEN>
```

Configuración obligatoria al habilitar:

```text
OPERATOR_CORRELATION_READ_TOKEN     mínimo 32 caracteres
OPERATOR_CORRELATION_TENANT_REF     no vacío
OPERATOR_CORRELATION_FUNNEL_REF     no vacío
SUPABASE_BASE_URL
SUPABASE_SERVICE_ROLE_KEY
```

Sin habilitación, las rutas no existen (`404`). Bearer ausente o distinto devuelve `401`. ID inválido devuelve `422`; caso inexistente o fuera del scope devuelve `404`; fallo SQL o evidencia inválida devuelve `503` sin incluir cuerpo, token ni PII en logs.

### Lista

```json
{
  "count": 1,
  "cases": [
    {
      "case_id": "uuid",
      "event_type": "PURCHASE_APPROVED",
      "outcome": "ambiguous",
      "reason_code": "multiple_candidates",
      "reason": "Las señales coinciden con más de una intención de compra elegible.",
      "candidate_count": 2,
      "observed_at": "timestamp",
      "automation_blocked": true,
      "scope": {
        "tenant_ref": "lancemos",
        "funnel_ref": "psicologajohanna",
        "product_ref": "f106691755g",
        "offer_ref": "bxjge6zq"
      },
      "identity": {
        "email_present": true,
        "phone_present": true,
        "masked_email": "b***r@example.com",
        "masked_phone": "********9999"
      }
    }
  ]
}
```

La lista no incluye candidatos individuales. El detalle agrega `candidates` con `purchase_intent_id`, `matched_by`, `submitted_at`, `lifecycle_state` e identidad enmascarada.

## 5. Razones explicadas

| `reason_code` | Explicación determinística |
|---|---|
| `scope_not_configured` | Producto/oferta sin alcance activo; V1 no lo muestra si no puede atribuir tenant |
| `identity_not_found` | Email y teléfono sin intención elegible |
| `multiple_candidates` | Más de una intención elegible |
| `email_phone_conflict` | Email y teléfono no convergen en una intención única |

Un reason code desconocido hace fallar la lectura cerrada; el Copilot no improvisa una explicación.

## 6. Minimización de PII

Email y teléfono se enmascaran dentro de PostgreSQL. El bridge recibe únicamente `masked_email`, `masked_phone` y flags de presencia. El validador Python rechaza cualquier respuesta que contenga `normalized_email` o `normalized_phone`.

Los local-parts de email de uno o dos caracteres se reemplazan completamente por
`***`; no se conserva el único carácter real ni ambos caracteres completos.

No se incluyen nombre, payload Hotmart, JID, teléfono completo, email completo, dirección, tokens ni secretos.

## 7. Plugin del Client Copilot

El toolset `operator_correlation_review` expone sólo:

- `list_unresolved_correlations(limit?)`;
- `get_unresolved_correlation(case_id)`.

El perfil mantiene deshabilitados terminal, archivos, código y web general. El plugin usa `OPERATOR_CORRELATION_API_URL` y `OPERATOR_CORRELATION_API_TOKEN` desde el `.env` privado del perfil. No recibe la key `service_role`.

## 8. Exclusiones V1

Este contrato no:

- resuelve ni promueve correlaciones;
- selecciona candidatos;
- corrige email o teléfono;
- crea notas, Team, etiquetas, tareas o notificaciones;
- activa `RESOLUTION_WORKER_ENABLED`;
- crea recovery cases, follow-ups, acciones ni outbound;
- define todavía el comando durable de decisión humana.
