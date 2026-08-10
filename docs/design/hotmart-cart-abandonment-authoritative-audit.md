# Auditoría del ingreso autoritativo de abandono de carrito Hotmart

- **Estado:** Auditoría read-only completada; brechas remediadas y verificadas localmente
- **Fecha:** 2026-08-10
- **Base auditada:** `origin/main` en `0fd2a26edac4dddc0913ff0014b9455517396c35`
- **Evento:** `PURCHASE_OUT_OF_SHOPPING_CART` v2.0.0
- **Alcance:** implementación, contratos, tests, despliegue y evidencia E2E
- **No declara:** despliegue vigente, migraciones nuevas aplicadas ni autorización para contactar leads reales

## 1. Veredicto

**request_changes** para considerar autoritativa la vertical actual.

El repositorio ya contiene una vertical funcional y no corresponde reconstruirla: autentica el webhook, limita tamaño y antigüedad, persiste por `source + external_event_id`, resuelve contacto, crea o reutiliza un caso y una secuencia durable, deriva autorización en la planificación, y reevalúa compra y opt-out antes del efecto. También existe evidencia productiva histórica del flujo abandono → primer WhatsApp → respuesta.

Sin embargo, esa evidencia E2E precede el motor durable y el grant de autorización actuales. Además, la admisión del abandono acepta y persiste un payload antes de validarlo estrictamente, trata como duplicado cualquier replay con el mismo ID aunque cambie la tupla de negocio, y la RPC de planificación confía en producto, oferta, contacto y timestamp suministrados por el bridge sin compararlos con el evento durable. Por eso no está demostrado que un evento válido produzca exactamente un plan autorizado mientras un conflicto semántico o binding ambiguo falle cerrado con evidencia durable.

## 2. Matriz de estado

| Capacidad | Implementación | Contrato | Tests | Desplegado | E2E | Estado |
|---|---|---|---|---|---|---|
| Hottok antes de leer body | Comparación constante antes de `request.stream()` | Sólo referido indirectamente desde compra aprobada | Cubierto | Evidencia histórica | HTTP 401 histórico | **Existente** |
| Body limitado, JSON, versión y anti-replay temporal | 1 MiB, JSON, v2.0.0 y ventana absoluta | Sin contrato propio de abandono | Cubierto | Evidencia histórica parcial | HTTP 202/401 histórico | **Existente/parcial** |
| Validación estricta del payload oficial | El endpoint sólo clasifica ID/evento/versión; el worker parsea después. Buyer, identidad, producto y oferta pueden faltar o ser inconsistentes al admitir | Ausente | Sólo casos parciales de teléfono y payload totalmente inválido | No demostrado para versión actual | No | **Faltante** |
| Identidad semántica e idempotencia | `unique(source, external_event_id)`; replay con igual ID y payload distinto se reporta `duplicate` | Ausente | Happy path y duplicate exacto; sin conflicto semántico de abandono | No demostrado | No | **Faltante** |
| Binding evento ↔ producto/oferta/timestamp | El bridge pasa valores parseados, pero `plan_cart_recovery` sólo verifica source/event type y confía en parámetros | Follow-up general, no contrato exacto de ingreso | Sin prueba adversarial de mismatch | Migración base aplicada históricamente; estado vigente no demostrado | No | **Parcial** |
| Binding exacto de contacto | Lookup por email y luego teléfono; toma la primera fila. Email y teléfono que apuntan a contactos distintos no se consideran ambiguos | Ausente | Sin caso cross-identity/ambigüedad | No demostrado | No | **Faltante** |
| Caso/secuencia/acción durable e idempotente | RPC transaccional crea o agrega por contacto + producto + oferta; replay del mismo evento reutiliza | Follow-up Engine V1 | PGlite cubre planificación y replay | Evidencia SQL histórica | E2E automatizado con mocks | **Existente** |
| Autorización derivada del abandono | `plan_cart_recovery_with_identity` inserta `allowed/hotmart` en la misma transacción y no pisa autorización activa | Decisión aceptada reflejada en migración, sin contrato de abandono propio | Python estructural + PGlite conductual | No hay evidencia explícita de migración aplicada y runtime vigente | No | **Implementado, no probado operativamente** |
| Compra previa o concurrente | Guardas SQL cierran/cancelan por correlación exacta; conflictos de compra bloquean request start | Contrato `hotmart-purchase-approved-v1` | PGlite/Postgres/probe | Base SQL verificada; bridge/E2E final pendientes | No para orden combinado actual | **Existente/parcial** |
| Opt-out previo o concurrente | Denial gana en reevaluación y request-start; planificación no lo sobreescribe | Diseño/contrato de opt-out | PGlite/Postgres local | No desplegado según evidencia vigente | No | **Implementado localmente** |
| Takeover previo o concurrente | Reevalúa autoridad Chatwoot antes del efecto; no hay handoff ejecutable completo | Fuera del contrato de abandono | Cobertura parcial del dispatcher | Flujo histórico verificó pausa | No para motor actual | **Parcial/dependencia posterior** |
| Despliegue actual | Compose declara flags y dependencias; efectos apagados por default | Operación documentada | Config cubierta | No hay evidencia de que `origin/main` actual y todas las migraciones estén desplegados | — | **No demostrado** |
| E2E real actual | Existe E2E del 2026-08-02 sobre la ruta inmediata antigua | Evidencia operativa histórica | Test `tests/test_e2e.py` usa transportes mock y termina en planificación, sin outbound | Histórico | No cubre admisión/plan/autorización durable actuales ni WABA | **Histórico, insuficiente para V1 actual** |

## 3. Hallazgos bloqueantes

### B-01 — El endpoint persiste abandonos no procesables

`classify_hotmart_event` valida únicamente ID no vacío, tipo soportado y versión. Para abandono, `POST /webhooks/hotmart` persiste inmediatamente; `parse_hotmart_payload` recién corre en el worker y no exige que el evento sea efectivamente abandono v2.0.0, que exista una identidad utilizable, ni que producto y oferta estén completos.

**Fallo concreto:** un payload autenticado con buyer vacío o producto/oferta ausentes recibe `202 received` y crea un evento durable que luego falla o produce contexto parcial. Esto contradice “evento válido produce exactamente un caso/plan” y no distingue rechazo de admisión de fallo asíncrono.

### B-02 — El replay conflictivo se oculta como duplicate

La persistencia de abandono usa `resolution=ignore-duplicates` sobre `unique(source, external_event_id)`. No compara una tupla normalizada del negocio ni conserva el payload conflictivo.

**Fallo concreto:** el mismo `id` puede reaparecer con otro buyer, teléfono, producto, oferta o timestamp y el bridge responde `200 duplicate`. No queda incidente durable ni bloqueo fail-closed equivalente al ya implementado para `PURCHASE_APPROVED`.

### B-03 — La planificación no prueba el binding contra el evento durable

`plan_cart_recovery` bloquea y verifica que el evento sea Hotmart/simulator y del tipo correcto, pero utiliza `p_contact_id`, `p_external_product_id`, `p_product_name`, `p_offer_code` y `p_abandoned_at` sin compararlos con `webhook_events.payload`.

**Fallo concreto:** un bug o caller con permiso de ejecución puede planificar el producto/oferta/contacto equivocados usando un evento auténtico distinto. La transacción será consistente internamente pero no autoritativa respecto de Hotmart.

### B-04 — La resolución puede elegir una identidad ambigua

Los lookups de `contact_points` aceptan la primera fila. El flujo prioriza email y no verifica que una coincidencia por teléfono, si existe, pertenezca al mismo contacto.

**Fallo concreto:** email y teléfono del mismo evento pueden mapear a dos contactos y el bridge elige el email, agrega el teléfono al contacto elegido de forma best-effort y continúa. No hay outcome durable de ambigüedad.

### B-05 — La evidencia operativa no corresponde a la implementación actual

El E2E real del 2026-08-02 validó la ruta inmediata hacia Hermes/Chatwoot/Evolution. Desde entonces se incorporaron planificación durable, autorización derivada, dispatcher, compra aprobada y opt-out. El test automatizado actual corta correctamente después de la planificación y usa mocks; no demuestra migración, worker, autorización, request-start ni efecto real actuales.

## 4. Controles existentes que deben conservarse

- autenticación antes de leer el body;
- límite incremental de 1 MiB y rechazo de JSON inválido;
- ventana anti-replay configurable;
- normalización conservadora de teléfono;
- planificación durable separada del efecto externo;
- una secuencia activa y una acción viva por caso;
- grant `allowed/hotmart` dentro de la transacción de planificación con identidad;
- precedencia de denied/restricted y opt-out sobre allowed;
- cierre por compra y preservación de `delivery_unknown`;
- allowlist de un único JID antes de resolver identidad de canal;
- flags fail-closed y ausencia de PII en logs.

## 5. Corrección mínima propuesta

1. Crear contrato `hotmart-cart-abandonment-v1` con schema procesable, tupla semántica, outcomes y reason codes.
2. Agregar parser estricto de abandono y rechazarlo antes de persistencia cuando falten identidad, producto u oferta requeridos.
3. Reemplazar el insert directo por una RPC de admisión semántica que devuelva `inserted`, `duplicate` o `semantic_conflict` y preserve evidencia del conflicto.
4. Hacer que la frontera SQL de planificación valide producto, oferta, timestamp y contacto/identidad normalizados contra el evento durable antes de crear o reutilizar el plan.
5. Detectar múltiples contactos o desacuerdo email/teléfono como identidad ambigua; no crear ni mutar contacto en ese caso.
6. Convertir cada hallazgo en test RED antes de editar producción.
7. Ejecutar suite Python, parser/árbol SQL, PGlite/Postgres disponible y prueba HTTP real local.
8. Mantener despliegue y E2E productivo como pendientes explícitos hasta usar un commit integrado y migraciones verificadas.

## 6. Límites de coordinación

No se modifica el perímetro general del piloto, kill switch, cohorte, presupuesto, tenant, WABA, Conversation Release ni handoff. Esos recursos pertenecen a otros workstreams. Si la corrección necesita una guarda general de scope, debe quedar como dependencia para integrar después del workstream A, no implementarse aquí.

## 7. Evidencia de auditoría

- preflight multiagente exitoso en el worktree reclamado;
- checkout limpio durante toda la inspección;
- `45 passed` en pruebas enfocadas de webhook, resolución, E2E mock y autorización de planificación;
- revisión de implementación FastAPI, parser, worker, cliente Supabase y migraciones;
- revisión de contratos, compose, `.env.example` y registros operativos;
- ninguna llamada a producción, despliegue, credencial ni payload real durante la auditoría.
