# Diseño propuesto — agregado comercial genérico para inbound y recovery

- **Estado:** Diseño aceptado por [ADR 0013](../decisions/0013-commercial-case-root.md); Corte A implementado localmente
- **Fecha:** 2026-08-16
- **Alcance:** crear un caso durable inbound sin fabricar un abandono Hotmart y preparar handoff stop-first reutilizable
- **No implementa todavía:** admisión inbound, RPC, wiring, handoff V2, outbound ni cambios remotos

## 1. Problema

El handoff V1 sólo acepta `recovery_cases.id`. Esa tabla exige físicamente:

- `abandonment_event_id` Hotmart/simulator;
- producto;
- `grace_expires_at`;
- estados y semántica de recuperación.

Un mensaje inbound no demuestra abandono, no necesita un timer de recovery y puede no estar correlacionado con una intención pre-checkout. Insertarlo como `recovery_case` obligaría a crear evidencia ficticia o debilitar constraints que hoy protegen el motor durable.

A la vez, duplicar toda la maquinaria de handoff para una tabla `inbound_cases` produciría dos autoridades de pausa, dos contratos de proyección y dos caminos de reconciliación para el mismo efecto Chatwoot.

## 2. Recomendación

Introducir un agregado padre mínimo `commercial_cases`, orientado a autoridad de conversación y automatización. `recovery_cases` pasa a ser un subtipo especializado; inbound usa el padre sin inventar un recovery.

```text
commercial_cases
├── kind = inbound_sales
│   └── conversación Chatwoot canónica
└── kind = cart_recovery
    └── recovery_cases (workflow Hotmart existente)
```

El padre no reemplaza `contacts`, `channel_identities`, `conversations` ni el motor de follow-up. Sólo concentra la identidad del caso, su conversación, su estado de automatización y la referencia comercial mínima.

## 3. Modelo lógico propuesto

### `commercial_cases`

- `id uuid`;
- `case_kind`: `inbound_sales | cart_recovery | payment_failure`;
- `contact_id`;
- `selected_channel_identity_id`;
- `conversation_id`;
- `tenant_ref`, `product_ref`, `offer_ref` server-owned;
- `status`: `active | paused_human | completed | cancelled | error`;
- `automation_status`: `draft_only | enabled | paused | disabled | restricted`;
- `identity_resolution_status`: contrato V1;
- `conversation_release_id/version` nullable mientras no haya release aprobada;
- timestamps y versión de optimistic fencing.

Invariantes:

- conversación, identidad y contacto pertenecen al mismo agregado;
- inbound exige conversación canónica desde admisión;
- un caso con identidad incierta puede existir, pero no consume contexto cruzado;
- `draft_only` es el default para inbound;
- ningún insert crea secuencia, acción ni permiso outbound;
- una conversación sólo tiene un caso inbound vivo por scope/producto/oferta;
- el caso conserva la conversación como autoridad según ADR-0008.

### Vínculo con recovery

Agregar `recovery_cases.commercial_case_id` como FK única. La migración backfillea un padre `cart_recovery` por cada recovery existente usando su contacto, identidad y conversación actuales.

Durante una transición acotada, las columnas duplicadas de `recovery_cases` continúan siendo las que usa el motor actual. Un trigger/constraint de consistencia deberá impedir divergencia antes de que el handoff lea el padre. No se cambiará de autoridad en el mismo corte que crea y backfillea la tabla.

### Correlación pre-checkout

Una tabla de vínculo separada relaciona:

- `commercial_case_id`;
- `purchase_intent_id`;
- `resolution_status`;
- `resolution_attempt/evidence`.

Sólo `resolved` permite incorporar datos del intent al contexto. Candidate/ambiguous/conflict/unmatched permanecen visibles pero no cambian identidad ni autorización.

## 4. Admisión inbound propuesta

```text
webhook Chatwoot autenticado y scoped
→ identidad canónica account + inbox + external user + conversation
→ normalización de observaciones
→ RPC admit_inbound_commercial_case
→ contact/channel_identity/conversation/case idempotentes
→ estado draft_only, cero mensajes
→ correlación precheckout separada y opcional
```

La command key usa identidad canónica de Chatwoot y scope, no nombre, email ni teléfono fuzzy. Replay exacto reutiliza el caso. Semántica distinta bajo la misma key produce conflicto durable.

Si el contacto interno todavía no existe, la identidad canónica de Chatwoot puede crear el contacto mínimo. Eso no confirma que sea la persona de una submission anterior.

## 5. Evolución del handoff

El handoff V2 debería admitir `commercial_case_id` y detener primero:

1. `commercial_cases.automation_status=paused`;
2. conversación canónica;
3. cuando existe subtipo recovery, secuencia/acciones/intentos pre-request;
4. proyección assignment + private note existente.

Para un inbound sin recovery no hay secuencia que cancelar. Se reutilizan policy, snapshots, leases, idempotency marker y worker de proyección; no se duplica el side effect Chatwoot.

El cutover debe mantener la RPC V1 como wrapper temporal sólo para `cart_recovery`, resolviendo su `commercial_case_id`. Tras verificación completa, la implementación interna deja de aceptar un recovery sin padre.

## 6. Secuencia de implementación segura

### Corte A — raíz sin autoridad nueva

- crear `commercial_cases`;
- backfill de recoveries;
- FK única y checks de consistencia;
- sin cambiar workers, handoff ni runtime;
- pruebas clean-stack y PostgreSQL real.

### Corte B — inbound draft-only

- RPC idempotente de admisión Chatwoot;
- contacto, identidad, conversación y caso canónicos;
- vínculo de correlación en estados V1;
- cero Hermes, handoff y outbound;
- TCP/lifespan/restart con stateful PostgreSQL.

### Corte C — handoff V2

- generalizar stop-first al padre;
- V1 wrapper para recovery;
- assignment + private note existentes;
- replay/restart y fallos Chatwoot;
- outbound público siempre cero.

### Corte D — agent wiring

- release/case pin;
- output comercial V1;
- `suggest_handoff` únicamente sobre caso durable;
- drafts permanecen sin envío hasta aprobaciones y gates.

## 7. Alternativas

### A. Reutilizar `recovery_cases` para inbound — descartada

Es el cambio más pequeño superficialmente, pero obliga a mentir sobre `abandonment_event_id`, producto, grace period y estados, o a debilitar invariantes probadas del motor.

### B. Crear `inbound_cases` y otro handoff — descartada

Evita tocar recovery, pero duplica autoridad, proyección, idempotencia y reconciliación de Chatwoot.

### C. Usar solamente `conversations` como caso — descartada

Una conversación es autoridad del canal, no del objetivo comercial. No ofrece tipo de caso, release, scope, estado de automatización ni concurrencia de casos por contacto.

### D. Padre genérico `commercial_cases` — recomendada

Tiene mayor costo inicial, pero preserva la semántica de recovery y crea una única raíz para inbound, handoff y futuros payment failures sin convertir el workflow en un CRM universal.

## 8. Riesgos y mitigaciones

- **Duplicación temporal de columnas:** backfill + constraints/triggers y cutover por fases.
- **Refactor de handoff amplio:** no cambiar autoridad hasta que todos los recoveries tengan padre y V1 wrapper pase la misma suite.
- **Overgeneralización:** el padre contiene sólo autoridad común; timers, políticas y acciones siguen en subtipos.
- **Doble caso inbound:** índice único parcial por conversación + scope para estados vivos.
- **Falsa correlación:** vínculo separado; status incierto no muta contacto ni contexto.
- **Migración remota:** queda fuera hasta clean-stack, PostgreSQL real, ACL y postflight.

## 9. Decisión

La introducción de `commercial_cases` como raíz común fue aceptada el 2026-08-16.
El Corte A quedó implementado localmente en modo `shadow`, sin runtime ni efectos.
Scope server-side, admisión inbound y cambio de autoridad continúan pendientes para
los cortes posteriores.
