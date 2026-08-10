# Matriz integral E2E y go/no-go — piloto Lancemos

- **Estado:** Preparación para revisión; no ejecutada contra el canal objetivo
- **Fecha:** 2026-08-10
- **Workstream:** F
- **Alcance:** una oferta, un inbox WABA, una cohorte mínima y todos los stops críticos
- **No implica:** migraciones desplegadas, WABA disponible, Conversation Release aprobada, handoff implementado ni autorización para contactar leads

## 1. Propósito

Esta matriz reúne en un único gate las capacidades que hoy están distribuidas
entre pruebas Python, probes SQL, evidencias históricas y dependencias externas.
No reemplaza esos artefactos: define qué debe observarse junto antes de decidir si
el piloto puede recibir tráfico real.

El corte evaluado es:

```text
Hotmart autenticado
→ admisión y scope exactos
→ primer contacto WABA aprobado
→ respuesta y conversación canónica
→ follow-up durable
→ compra / opt-out / takeover / handoff
→ restart, incertidumbre y rollback
```

## 2. Niveles de evidencia

| Nivel | Significado | Puede habilitar piloto real |
|---|---|---|
| `L0` | lectura estática o test unitario | no |
| `L1` | integración/HTTP local con dobles stateful | no |
| `L2` | motor SQL local ejecutable (incluido PGlite) o harness stateful multi-componente | no |
| `L3` | PostgreSQL real disposable o dependencia remota real sin efecto físico | no por sí solo |
| `L4` | entorno desplegado, cuentas objetivo y llegada/efecto físico controlado | sólo si todos los gates críticos pasan |

Una evidencia histórica de Evolution puede demostrar una propiedad del bridge,
pero no se promueve automáticamente a `L4` para WABA o para la versión actual.

Estados permitidos:

- `pass`: postcondiciones observadas en el nivel exigido;
- `fail`: el escenario se ejecutó y una postcondición fue incorrecta;
- `blocked`: falta dependencia, aprobación, despliegue o implementación;
- `not_run`: estaban dadas las condiciones, pero todavía no se ejecutó;
- `not_applicable`: justificación explícita aprobada.

Cuando `status=blocked`, `blocked_reason` debe ser exactamente uno de:
`external`, `deployment`, `implementation` o `business_input`. Los estados con
forma `blocked_<reason>` no son válidos.

## 3. Foto de cobertura existente

| ID | Escenario | Evidencia actual | Nivel actual | Nivel exigido | Estado hoy |
|---|---|---|---|---|---|
| F-01 | Abandono válido produce un primer contacto | Ingreso autoritativo local; E2E histórico físico por Evolution | `L2` actual + `L4` histórico no equivalente | `L4` WABA | blocked (`external`) |
| F-02 | Replay exacto y replay semántico no duplican | Python y SQL/PGlite; idempotencia durable local | `L2` | `L4` | blocked (`external`) |
| F-03 | Tenant, inbox, producto u oferta incorrectos se rechazan | Perímetro y request-start probados en PGlite/PostgreSQL disposable | `L3` parcial | `L3` y cero efecto remoto | blocked (`deployment`) |
| F-04 | WABA usa template aprobado y nunca freeform/Evolution | Factory y payloads locales; frontera SQL provider/modo | `L2` | `L4` | blocked (`external`) |
| F-05 | Varios inbound rápidos producen un turno coherente | E2E físico histórico con Evolution y debounce 30 s | `L4` histórico no equivalente | `L4` sobre inbox objetivo | blocked (`external`) |
| F-06 | Follow-up usa la conversación canónica, sin crear otra | Unit/integration local de sender y motor durable | `L1` parcial | `L4` | blocked (`external`) |
| F-07 | Compra antes del próximo request cancela o bloquea | SQL local/real y probe remoto histórico con rollback; correcciones forward no demostradas desplegadas | `L3` parcial | `L4` con webhook y cero send | blocked (`deployment`) |
| F-08 | Opt-out persiste, cancela y bloquea replay/restart | PostgreSQL real disposable + HTTP TCP local con dobles | `L3` parcial | `L4` | blocked (`deployment`) |
| F-09 | Takeover humano gana antes del send | Clasificación, reautorización y guardas locales | `L1` parcial | `L4` | blocked (`deployment`) |
| F-10 | Handoff pausa, asigna equipo y deja nota privada | Diseño aceptado; implementación concurrente fuera de este workstream | diseño | `L4` | blocked (`implementation`) |
| F-11 | Reinicio recupera trabajo sin duplicar | Tests de lifecycle/replay; opt-out HTTP reutiliza autoridad en memoria | `L1/L2` | `L4` con procesos y stores reales | blocked (`deployment`) |
| F-12 | Resultado remoto incierto queda retenido sin retry ciego | Worker y SQL locales para `delivery_unknown` | `L1/L2` parcial | `L4` con ambigüedad controlada | blocked (`deployment`) |
| F-13 | Kill switch y rollback detienen requests nuevos y drenan iniciados | PostgreSQL real disposable y runbook WABA preparado | `L3` parcial | `L3/L4` desplegado | blocked (`deployment`) |
| F-14 | Destino fuera de allowlist causa cero lookup/create/send | Unit tests y evidencia histórica del incidente/corrección | `L1` actual | `L3` con contadores remotos | blocked (`deployment`) |
| F-15 | Respuesta cumple oferta, voz, límites y handoff comercial | Matriz conversacional preparada, sin insumos ni aprobación | diseño | revisión humana + corrida | blocked (`business_input`) |

`parcial` significa que sólo algunas fronteras del escenario alcanzaron ese nivel;
no equivale a un `pass` del escenario completo.

## 4. Matriz ejecutable

Cada corrida usa IDs nuevos, una versión de política fija y evidencia sanitizada.
Los escenarios que podrían enviar deben ejecutarse por separado y con presupuesto
acotado.

| ID | Entrada / carrera | Postcondición durable | Efecto externo | Cero efectos obligatorio | Severidad |
|---|---|---|---|---|---:|
| F-01 | abandono autenticado del producto/oferta aprobados | evento admitido una vez; caso, secuencia, acción e intento bound al scope | una llegada física por template de apertura | cero freeform y cero Evolution | 5 |
| F-02 | mismo delivery y luego misma semántica con delivery distinto | un único caso/acción; replay auditable | ningún mensaje adicional | `additional_send_count=0` | 5 |
| F-03 | variar por separado tenant, inbox, provider/cuenta, producto y oferta | rechazo tipado sin trabajo parcial | ninguno | modelo, contact lookup, conversation create y send en cero | 5 |
| F-04 | WABA con config completa; luego omitir cada campo y cruzar modo/provider | intento autorizado sólo como `approved_template`; config inválida no arranca | payload exacto del template aprobado | cero fallback | 5 |
| F-05 | 3–4 inbound públicos rápidos en una conversación | admisiones completas como un grupo; origen canónico incluido | una respuesta lógica después de quiet window | cero respuestas extra | 4 |
| F-06 | vencer un follow-up de una conversación existente | mismo case/conversation anchor y attempt idempotente | un template follow-up en la conversación existente | cero contact/conversation create | 5 |
| F-07 | `PURCHASE_APPROVED` antes de claim, antes de request-start y después de request-start | pre-request cancela; post-request conserva aceptación/unknown sin sucesor | ninguno nuevo después del stop | cero successor y cero nuevo send | 5 |
| F-08 | baja clara antes de plan, antes de request-start y bajo replay/restart | stop durable; permiso denied; acciones canceladas; proyección separada | ninguno comercial; proyección operativa según contrato | Hermes y send en cero | 5 |
| F-09 | mensaje público humano/takeover entre autorización y request-start | caso/conversación pausados; request-start rechazado | ninguno | send en cero | 5 |
| F-10 | motivo allowlisted con conversación canónica existente | pausa antes de proyección; request idempotente; efectos tipados | equipo esperado + una nota privada; silencio al contacto | cero outbound comercial y cero labels reemplazadas | 5 |
| F-11 | reiniciar con evento, acción, lease y resultado terminal en puntos separados | lease recuperado; completion única; journals/backlog consistentes | efecto como máximo una vez | cero duplicados | 5 |
| F-12 | timeout/lost response después del POST | `delivery_unknown` hasta reconciliación; no se inventa aceptación/rechazo | como máximo un efecto físicamente observable | cero retry ciego | 5 |
| F-13 | pause/kill switch mientras compite request-start | ningún request nuevo tras pausa; iniciado se finaliza/reconcilia | sólo efecto ya iniciado, si existía | cero successor | 5 |
| F-14 | destino o JID distinto al único autorizado | rechazo antes del adapter | ninguno | todos los RPC mutantes de Chatwoot en cero | 5 |
| F-15 | escenarios CR-001..CR-016 con release pinneada | evaluation run auditable por scenario/release | sólo salidas aprobadas en entorno controlado | cero promesa/facto/template no aprobado | 5 |

## 5. Inventario de harnesses y evidencia

### Automatización vigente

- `uv run pytest -q`: suite Python completa.
- `npm --prefix tests/sql/followup_engine test`: motor durable, compra, opt-out,
  abandono y perímetro.
- `tests/sql/followup_engine/validate_pilot_boundary.mjs`: scope, cohort, budget,
  kill switch, replay y ACL.
- `tests/sql/followup_engine/validate_pilot_boundary_runtime.mjs`: planificación y
  request-start atómicos, readiness y rechazo WABA/freeform.
- `tests/sql/followup_engine/real_postgres_pilot_boundary.py`: concurrencia real
  del perímetro.
- `tests/sql/followup_engine/real_postgres_pilot_boundary_runtime.py`: wiring y
  request-start en PostgreSQL real.
- `tests/sql/followup_engine/real_postgres_opt_out.py`: orden inverso y carrera de
  opt-out.
- `tests/test_opt_out_http_e2e.py`: TCP/lifespan local con dobles stateful.
- `tests/test_messaging.py`: payloads WABA de apertura/follow-up y allowlist.
- `tests/test_webhook.py`: debounce, replay, journals y lifecycle.
- La implementación de handoff y sus harnesses permanecen bajo otro claim; no se
  aceptan como evidencia hasta integración y revisión.

### Evidencia operativa reutilizable, con límites

- [`2026-08-02-hotmart-recovery-e2e.md`](../operations/2026-08-02-hotmart-recovery-e2e.md):
  primer contacto y respuesta físicos por Evolution; histórico, no WABA actual.
- [`2026-08-07-chatwoot-inbound-batching-e2e.md`](../operations/2026-08-07-chatwoot-inbound-batching-e2e.md):
  batching físico sobre la conversación autorizada.
- [`2026-08-08-hotmart-purchase-cancellation-supabase.md`](../operations/2026-08-08-hotmart-purchase-cancellation-supabase.md):
  SQL remoto con rollback, pero declara correcciones forward y E2E pendientes.
- [`2026-08-09-inbound-opt-out-local.md`](../operations/2026-08-09-inbound-opt-out-local.md)
  y [`2026-08-10-inbound-opt-out-http-e2e.md`](../operations/2026-08-10-inbound-opt-out-http-e2e.md):
  PostgreSQL disposable y HTTP local, no servicios productivos.
- [`2026-08-10-lancemos-pilot-boundary-runtime-local.md`](../operations/2026-08-10-lancemos-pilot-boundary-runtime-local.md):
  runtime local y PostgreSQL real, no despliegue.
- [`lancemos-controlled-channel-e2e-runbook.md`](../operations/lancemos-controlled-channel-e2e-runbook.md):
  preparación WABA, todavía no evidencia.

## 6. Dependencias que impiden ejecutar la matriz completa

### Externas

- número/cuenta WABA, account e inbox Chatwoot;
- templates aprobados por Juan y Meta;
- producto y oferta Hotmart exactos;
- credenciales en EasyPanel y acceso automatizable al control plane;
- destinatario de prueba y operador de guardia.

### Implementación o despliegue

- handoff D integrado y verificado;
- historial de migraciones remoto reconciliado y stack actual aplicado;
- bridge desplegado desde un commit fijado;
- scope, cohorte, budget y kill switch provisionados default-off;
- Conversation Release y matriz conversacional aprobadas.

## 7. Regla go/no-go

### `NO-GO` obligatorio

- cualquier escenario de severidad 5 en `fail`, `blocked` o `not_run`;
- WABA/template/producto/oferta sin autoridad verificable;
- migraciones o commit desplegado desconocidos;
- backlog previo, ambigüedad o `delivery_unknown` sin owner;
- ausencia de compra, opt-out, takeover, handoff o kill switch ejecutables;
- evidencia que no puede correlacionar el efecto físico con el evento nuevo;
- Conversation Release no aprobada.

### E2E controlado permitido, piloto todavía `NO-GO`

Puede autorizarse una corrida individual cuando sus prerequisitos están completos,
el destinatario es el único allowlisted, el presupuesto es uno y todos los demás
efectos permanecen apagados. Un `pass` parcial no habilita cohorte.

### `GO` para cohorte mínima

Requiere todos los escenarios críticos `pass` al nivel exigido, rollback probado,
backlog cero, evidencia firmada por el responsable técnico y aprobación de negocio
de la release. La activación es una decisión posterior y separada.

## 8. Artefacto de salida

Cada ejecución produce un registro fechado en `docs/operations/` y actualiza esta
matriz sólo con un enlace a esa evidencia. No se copian payloads, textos, teléfonos,
emails, tokens ni IDs externos sin sanitizar.
