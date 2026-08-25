# ADR-0016: Resolución manual supervisada de correlaciones no inequívocas

- **Estado:** Aceptada
- **Fecha:** 2026-08-24
- **Complementa:** ADR-0003 y ADR-0007
- **Estado de implementación:** Implementada localmente; no mergeada ni desplegada

## Contexto

La correlación Hotmart es determinística. Los resultados `unmatched`, `ambiguous` y `conflict` conservan `purchase_intent_id = null`, requieren revisión humana y bloquean la automatización. El Client Copilot ya puede listar y explicar esos casos mediante una proyección enmascarada, pero no puede aplicar una decisión del operador.

Un LLM no obtiene evidencia autoritativa adicional por razonar sobre dos candidatos. Permitir que el modelo seleccione identidad o compra por probabilidad convertiría una conjetura en autoridad de negocio. Al mismo tiempo, obligar al operador a modificar PostgreSQL directamente impediría una operación segura, acotada y auditable.

## Decisión

Se incorpora una operación supervisada en dos pasos:

```text
prepare_correlation_resolution
→ crea un comando durable, inmutable y con vencimiento
→ liga una idempotency key UUID a un fingerprint semántico
→ fija scope, actor, acción, evidencia determinística y snapshot de candidatos
→ no resuelve ni produce efectos

confirm_correlation_resolution
→ Hermes exige aprobación humana nativa antes de ejecutar la tool
→ PostgreSQL revalida comando, caso, scope, candidato y snapshot
→ inserta una resolución terminal e inmutable
```

Las acciones iniciales son:

- `resolve_with_candidate`: requiere un candidato visible y scoped; registra `linked_candidate` y un `effective_purchase_intent_id`.
- `close_without_match`: no acepta candidato; registra `closed_without_match`.
- mantener pendiente no es una transición: no se crea comando ni resolución.

El bridge fija tenant, funnel y actor desde configuración autenticada. El modelo no puede elegir esos valores. El motivo se expresa mediante un código cerrado compatible con la acción; no se persiste texto libre ni PII.

Cada prepare exige una key UUID nueva. Un replay exacto reutiliza esa key y recupera
el mismo comando; reutilizarla con otra semántica falla cerrado.

La tool de confirmación registra un hook `pre_tool_call` que devuelve `action = approve`. El perfil usa `approvals.mode = manual`. El `rule_key` incluye el UUID del comando, por lo que una aprobación persistente sólo alcanza al replay idempotente del mismo comando y no autoriza otro caso.

PostgreSQL conserva sin cambios:

- `hotmart_purchase_intent_correlations.outcome`;
- `reason_code`;
- `candidate_count`;
- `purchase_intent_id`;
- `manual_handoff_required`;
- las filas de candidatos determinísticos.

La resolución efectiva se registra en tablas separadas. La proyección de pendientes omite los casos que ya tienen una resolución aplicada.

## Frontera de efectos de V1

Resolver la correlación no equivale a autorizar contacto, activación o outbound. V1 no:

- actualiza `purchase_intents`;
- modifica `activation_authorized` ni `whatsapp_contact_authorized`;
- agenda o cancela reevaluaciones;
- crea acciones, deliveries, conversaciones o mensajes;
- habilita workers.

Una futura continuidad operacional deberá consumir el binding manual mediante un contrato separado y volver a evaluar sus autoridades determinísticas.

## Invariantes

1. Una correlación admite como máximo una resolución aplicada.
2. El resultado determinístico original es inmutable y distinguible de la resolución manual.
3. Sólo `unmatched`, `ambiguous` y `conflict` pendientes y scoped pueden prepararse.
4. `resolve_with_candidate` sólo acepta un candidato del snapshot scoped actual.
5. La proyección scoped debe contener exactamente todos los candidatos durables antes de permitir escritura; una proyección parcial falla cerrada.
6. El comando expira y su payload es inmutable.
7. Confirmar revalida el mismo actor, scope, acción, candidato, outcome, reason y snapshot.
8. Un replay exacto del mismo comando devuelve la resolución existente; otro comando sobre el mismo caso se rechaza.
9. Los roles API no reciben DML directo; `service_role` sólo ejecuta los RPC públicos estrechos.
10. Sin aprobación humana interactiva, Hermes bloquea la tool de confirmación.

## Consecuencias

- El Copilot puede aplicar una elección humana sin adquirir autoridad para decidir identidad.
- La auditoría distingue algoritmo, comando preparado y resolución aplicada.
- Un cambio concurrente de candidato o intención obliga a revisar nuevamente.
- La primera prueba puede resolver la fixture sintética y demostrar cero efectos externos.
- La activación posterior queda deliberadamente fuera de este corte.

## Alternativas descartadas

- **Un único RPC mutante:** descartado porque no deja una vista previa durable ni una frontera de confirmación humana.
- **Confirmación sólo por texto conversacional:** descartada porque el modelo podría emitir la tool sin una aprobación de UI confiable.
- **Reescribir `outcome = resolved`:** descartado porque falsea la evidencia del algoritmo.
- **Actualizar inmediatamente `purchase_intents`:** diferido para no mezclar resolución de identidad con continuidad operacional.
- **Service-role dentro del perfil:** descartado; el bridge conserva la autoridad de base de datos.
