# Perímetro acotado del piloto de Lancemos

- **Estado:** Fase 1 implementada y verificada localmente; wiring de fase 2 pendiente
- **Fecha:** 2026-08-10
- **Alcance:** MVP V1, un tenant, un inbox, un número/cuenta de canal, un producto, una oferta, cohorte explícita, presupuesto de requests y kill switch
- **No implica:** activación de outbound, carga de valores reales, despliegue de DDL, configuración WABA ni contacto con leads reales
- **Fuente de producto:** [Dirección del piloto](./lancemos-pilot-product-direction.md)
- **Contrato técnico:** [Perímetro Lancemos V1](../contracts/lancemos-pilot-boundary-v1.md)

## 1. Problema

La allowlist de un único JID protege pruebas controladas, pero no constituye el perímetro de una cohorte real. Tampoco es seguro pasar al piloto eliminándola. El reemplazo debe ser una conjunción autoritativa y durable:

```text
tenant Lancemos
AND scope publicado
AND runtime armado
AND cuenta + inbox canónicos
AND cuenta/número de canal esperado
AND evento Hotmart de abandono permitido
AND producto + oferta exactos
AND autorización del contacto vigente
AND contacto inscripto en la cohorte
AND presupuesto total y diario disponible
AND stops negativos ausentes
→ recién entonces puede comenzar un request outbound
```

Las restricciones negativas existentes —opt-out, compra, takeover, delivery incierto y conflicto de correlación— conservan precedencia. El perímetro no reemplaza esos gates: agrega otro requisito obligatorio.

## 2. Decisiones de fase 1

### 2.1 Scope versionado e inmutable

Cada configuración se identifica por `scope_key + version`. Una versión publicada fija:

- tenant `lancemos`;
- cuenta e inbox de Chatwoot;
- proveedor y referencia opaca de la cuenta/número del canal;
- source/evento `hotmart/PURCHASE_OUT_OF_SHOPPING_CART`;
- producto y oferta exactos;
- policy key/version de seguimiento;
- timezone;
- máximo de contactos activos;
- máximo total de request-starts;
- máximo diario de request-starts.

Una versión publicada es inmutable. Cambiar cualquier límite o identificador exige otra versión y una activación explícita. La activación sólo se admite desde `inactive|paused`, fuerza la nueva versión a `inactive`, incrementa la generación y no migra miembros de cohorte. El operador debe revisar la nueva versión, inscribir su cohorte y armarla en pasos separados.

### 2.2 Default apagado

Crear o publicar configuración nunca activa outbound. El control runtime comienza `inactive`. Sólo un RPC administrativo puede moverlo a `armed`, con actor, motivo y compare-and-swap por generación.

### 2.3 Kill switch fail-closed

El estado `paused` o `closed` bloquea nuevos request-starts. El RPC de cambio de estado y la reserva del presupuesto bloquean la misma fila de control. Por lo tanto, quedan serializados:

- si la pausa confirma primero, la reserva posterior se rechaza;
- si una reserva confirma primero, ese request ya cruzó honestamente la frontera durable y la pausa impide los siguientes.

No se promete cancelar una llamada externa que ya empezó.

### 2.4 Cohorte explícita

El abandono no incorpora automáticamente un contacto a la cohorte. Un operador autorizado debe inscribirlo mediante RPC. La inscripción:

- usa sólo `contact_id`, nunca PII;
- respeta el máximo de contactos activos;
- es idempotente;
- puede retirarse sin borrar auditoría;
- no equivale por sí sola a consentimiento ni permiso de envío.

### 2.5 Presupuesto conservador

El presupuesto cuenta autorizaciones durables de request-start, no mensajes confirmados. En fase 2 la autorización debe acoplarse con la transición durable `request_started` inmediatamente antes del efecto. Es deliberadamente conservador: una autorización consumida no devuelve cupo automáticamente, porque el efecto pudo ocurrir. Una reserva se identifica por `attempt_id`, por lo que el replay del mismo intento no consume dos veces.

Hay dos caps obligatorios:

- total de requests outbound del piloto;
- requests outbound por fecha local del scope.

Los consumos se cuentan por `scope_key` a través de todas sus versiones. Publicar o activar una versión nueva no reinicia presupuesto.

La timezone es invariante para todas las versiones de un mismo `scope_key`. Cambiarla durante el piloto podría partir artificialmente el día presupuestario; por eso requiere cerrar el scope y una decisión operativa explícita, no una activación V1→V2.

El cap total forma parte del cierre conservador de este diseño. La política comercial concreta del cap diario todavía requiere confirmación de Juan/operación; la implementación lo exige igualmente como guarda fail-closed y no permite armar un scope real hasta cargar un valor aprobado. Esto no declara que el valor o la política diaria ya hayan sido aceptados como requisito de producto.

### 2.6 Dos fronteras

1. **Evaluación:** permite rechazar temprano admisión/planificación fuera de scope, sin consumir presupuesto.
2. **Autorización de request-start:** revalida todo, exige cohorte y consume presupuesto atómicamente inmediatamente antes del efecto.

Sólo la segunda habilita un efecto. Una evaluación positiva anterior no es un permiso durable para enviar después.

## 3. Modelo propuesto

### `pilot_scope_versions`

Configuración publicada e inmutable del scope.

### `pilot_runtime_controls`

Estado mutable `inactive|armed|paused|closed`, versión activa y generación CAS. Una fila por `scope_key`.

### `pilot_cohort_memberships`

Membresía auditable por contacto y versión, con estado `active|removed`.

### `pilot_outbound_request_authorizations`

Ledger append-only de slots consumidos por `attempt_id`. Conserva action/contact, fecha local, versión y generación observada.

### `pilot_control_events`

Auditoría append-only de activación, pausa, cierre e inscripción/retiro.

## 4. Reason codes mínimos

Evaluación y autorización devuelven un resultado tipado. Entre otros:

- `pilot_scope_allowed`;
- `pilot_scope_not_published`;
- `pilot_runtime_not_armed`;
- `pilot_scope_version_mismatch`;
- `pilot_tenant_mismatch`;
- `pilot_chatwoot_account_mismatch`;
- `pilot_chatwoot_inbox_mismatch`;
- `pilot_channel_account_mismatch`;
- `pilot_source_event_mismatch`;
- `pilot_product_mismatch`;
- `pilot_offer_mismatch`;
- `pilot_contact_not_in_cohort`;
- `pilot_total_budget_exhausted`;
- `pilot_daily_budget_exhausted`;
- `pilot_attempt_mismatch`;
- `pilot_request_time_invalid`.

Valores nulos, vacíos o malformados fallan cerrados; no se interpretan como comodines.

## 5. Integración por fases

### Fase 1 — este workstream

- tablas, constraints, índices y ACL;
- control runtime CAS;
- cohort enrollment/removal;
- evaluación tipada;
- autorización idempotente de request-start;
- pruebas SQL adversariales;
- contrato y diseño.

No modifica todavía los entrypoints centrales que el workstream de abandono puede estar auditando.

### Fase 2 — después de integrar el workstream de abandono

- llamar la evaluación en admisión/planificación;
- propagar scope key/version al caso y la acción;
- llamar la autorización en la frontera durable `request_started`;
- sumar settings/deployment contract;
- prueba HTTP controlada; la concurrencia SQL ya fue comprobada localmente en PostgreSQL real durante fase 1 y debe revalidarse después del wiring.

## 6. Invariantes

1. No hay wildcard para tenant, inbox, producto, oferta ni cuenta de canal.
2. Publicar no arma el piloto.
3. Sólo una versión puede estar seleccionada por el control runtime.
4. Cambiar versión exige pausa/inactividad y siempre deja el runtime `inactive`.
5. `paused|closed|inactive` nunca autorizan un request nuevo.
6. Un contacto no inscripto nunca consume presupuesto ni envía.
7. El máximo de cohorte se aplica bajo el mismo lock del control.
8. Los caps total/diario se verifican y consumen en una transacción y no se reinician al cambiar versión.
9. El replay del mismo `attempt_id` devuelve el resultado original sin consumir otro slot.
10. Otro `attempt_id` no puede reutilizar la misma acción de forma incompatible.
11. Pausa y request-start quedan serializados; no se promete deshacer efectos ya iniciados.
12. Tablas autoritativas no permiten DML directo a roles API ni a `service_role`.
13. RPCs mutantes son `SECURITY DEFINER`, con `search_path` cerrado y sólo `service_role`.
14. No se almacenan teléfonos, JIDs, nombres, mensajes ni payloads en las tablas del perímetro.

## 7. Temas externos pendientes

Antes de crear una versión publicada real deben confirmarse:

- account e inbox definitivos de Lancemos;
- proveedor WABA y referencia opaca del número/cuenta;
- producto y offer code exactos;
- policy version aprobada;
- timezone operativa;
- tamaño máximo de cohorte;
- cap total y diario;
- operador habilitado para armar/pausar;
- duración/fecha de cierre del piloto.

Ninguno de esos valores se inventa en la migración.
