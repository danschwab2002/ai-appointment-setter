# ADR 0013 — Raíz durable común para casos comerciales

- **Estado:** Aceptada
- **Fecha:** 2026-08-16
- **Implementación:** Corte A implementado localmente; no desplegado
- **Complementa:** ADR-0008, ADR-0010 y ADR-0012

## Contexto

El piloto necesita crear un caso durable al recibir una conversación inbound de Chatwoot, incluso cuando su vínculo con una intención pre-checkout sea incierto.

`recovery_cases` no es un agregado comercial genérico. Exige un evento de abandono Hotmart, producto, grace period y estados del motor de recuperación. Reutilizarlo para inbound obligaría a fabricar un abandono o a debilitar invariantes verificadas.

Crear una tabla inbound totalmente separada y duplicar el handoff tampoco es seguro: produciría dos autoridades de pausa, idempotencia y proyección para el mismo efecto Chatwoot.

## Decisión

Introducir `commercial_cases` como raíz durable mínima para la autoridad común a casos comerciales:

```text
commercial_cases
├── inbound_sales
└── cart_recovery → recovery_cases
```

Reglas:

1. `commercial_cases` conserva contacto, identidad de canal, conversación canónica, scope comercial mínimo, estado y versión.
2. La conversación pertenece al caso, conforme ADR-0008; no se deriva de un puntero global de la identidad.
3. `recovery_cases` continúa siendo el workflow autoritativo de recuperación y se vincula uno-a-uno con una raíz `cart_recovery`.
4. Inbound podrá existir sin recovery y sin correlación confirmada con pre-checkout.
5. La correlación con una intención será un vínculo separado bajo ADR-0012; nunca será autorización de efectos.
6. Handoff evolucionará para detener una raíz comercial y sólo operará secuencias/actions cuando exista el subtipo recovery.
7. No se habilita un tipo de caso nuevo sin una RPC idempotente, ACL cerrada y pruebas ejecutables.

## Implementación por cortes

### Corte A — sombra de recovery

Aceptado e implementado localmente en `20260816000100_commercial_case_root.sql`:

- crea `commercial_cases` con RLS y sin acceso para API roles;
- usa primary key compartida con `recovery_cases`;
- backfillea exactamente una raíz por recovery existente;
- crea una raíz automáticamente para recoveries futuros;
- mantiene recovery como autoridad y sincroniza la sombra;
- fija el UUID compartido antes del write de recovery y crea/sincroniza la sombra después;
- protege mutaciones directas de la sombra y valida el estado final desde recovery;
- soporta constraints inmediatos y transacciones update-delete o insert-delete;
- preserva los `ON DELETE SET NULL` históricos de conversación/identidad a través del
  recovery autoritativo, sin agregar una FK duplicada con semántica más restrictiva;
- ejecuta sólo la sincronización y validación internas como funciones trigger
  `SECURITY DEFINER`, con `search_path` endurecido y `EXECUTE` revocado; esto preserva
  writes históricos de recovery sin conceder DML directo sobre la sombra;
- rechaza físicamente `inbound_sales` y `payment_failure`;
- no agrega RPCs, scheduling, handoff ni efectos.

### Corte B — inbound draft-only

Pendiente y requiere diseño/contrato ejecutable:

- scope server-side;
- identidad y conversación canónicas de Chatwoot;
- admisión idempotente;
- correlación de intención separada;
- estado `draft_only`;
- cero agent calls y outbound.

### Corte C — handoff generalizado

Pendiente. Debe preservar stop-first, snapshots, idempotencia y reconciliación existentes, con wrapper temporal para recovery.

## Invariantes del Corte A

- Todo recovery tiene exactamente una raíz con el mismo UUID.
- La raíz `cart_recovery` coincide con contacto, identidad, conversación, producto, oferta, estado y versión del recovery.
- `created_at` y `updated_at` también coinciden con recovery; no son metadata mutable de la sombra.
- Una mutación directa divergente de la sombra falla antes de escribirse.
- Un delete directo o anidado de la sombra falla mientras el recovery autoritativo exista;
  sólo el borrado del recovery puede activar el cascade correspondiente.
- Recovery es la fuente de sincronización durante el modo `shadow`: un trigger `BEFORE`
  fija el vínculo y un trigger `AFTER` sincroniza la raíz ya con el recovery visible.
- La validación diferible relee el recovery final; si recovery y raíz fueron eliminados en
  la misma transacción, no interpreta el evento histórico del trigger como divergencia.
- Ninguna fila inbound puede existir todavía.
- Ningún consumidor runtime lee `commercial_cases` como autoridad.

## Consecuencias

### Positivas

- inbound no necesita fingir un abandono Hotmart;
- recovery conserva su semántica y pruebas;
- handoff puede converger después en una sola autoridad común;
- la transición se puede verificar sin cambiar comportamiento productivo.

### Costos

- existe duplicación controlada mientras la raíz permanezca en modo `shadow`;
- el trigger agrega una escritura interna por transición de recovery;
- el cambio de autoridad requiere un corte posterior y una nueva verificación integral;
- scope/tenant no se backfillea todavía porque no todos los recoveries históricos tienen una binding de piloto autoritativa.

## Alternativas descartadas

- **Usar `recovery_cases` para inbound:** evidencia ficticia e invariantes debilitadas.
- **Duplicar caso y handoff inbound:** dos autoridades y dos proyecciones reconciliables.
- **Usar sólo `conversations`:** confunde autoridad de canal con objetivo comercial.

## Evidencia exigida antes de desplegar Corte A

- migración sobre stack limpio;
- backfill de recovery previo;
- creación de recovery posterior;
- sincronización de estado/version;
- rechazo de divergencia directa;
- rechazo de inbound directo;
- creación con `SET CONSTRAINTS ALL IMMEDIATE`;
- update-delete e insert-delete dentro de una transacción;
- rechazo de delete de sombra directo y desde otro trigger;
- preservación de `conversation_id ON DELETE SET NULL`;
- rechazo de divergencia de timestamps y anti-join global sin huérfanos;
- progreso positivo de un write de recovery bajo `service_role`, manteniendo denegado
  el DML directo del mismo rol sobre `commercial_cases`;
- suite Python y SQL completas;
- ACL/postflight en PostgreSQL real y Supabase Cloud antes de cualquier activación.

El estado local o un resultado PGlite verde no demuestra despliegue ni reemplaza el postflight remoto.

La evidencia local reproducible y su límite operativo están registrados en
[`2026-08-16-commercial-case-cut-a-local-verification`](../operations/2026-08-16-commercial-case-cut-a-local-verification.md).
