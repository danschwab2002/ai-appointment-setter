# Template — registro de fuentes y decisiones del primer infoproductor

- **Estado:** Plantilla vacía para onboarding asistido
- **Alcance:** procedencia, clasificación, conflicts y aprobaciones de una oferta
- **Custodia:** sólo referencias opacas y resúmenes sanitizados en Git
- **Autoridad:** registro canónico reutilizable de procedencia, clasificación y decisiones; los paquetes de Conversation Release lo referencian y no duplican sus tablas
- **No es:** repositorio documental, CRM, base de leads, payload de runtime ni Conversation Release activa

## 1. Control

```yaml
onboarding_id: PENDIENTE
register_version: 1
onboarding_status: not_started
customer_ref: PENDIENTE
conversation_release_ref: PENDIENTE
created_at: null
updated_at: null
custodian: PENDIENTE
raw_sources_in_git: false
pii_in_register: false
secrets_in_register: false
```

`onboarding_status` pertenece al registro de onboarding, no al lifecycle de la
Conversation Release. En snapshots combinados debe mantener `release_status` por
separado.

## 1.1 Custodia y sanitización fail-closed

```yaml
custody:
  storage_ref: PENDIENTE
  isolated_per_customer: false
  access_reviewed: false
  restrictive_permissions_verified: false
  encryption_in_transit_verified: false
  encryption_at_rest_verified: false
  backup_scope_reviewed: false
  retention_and_deletion_owner_defined: false
  source_custody_safe: false
sanitization_manifest:
  scanner_ref: PENDIENTE
  scanner_version: PENDIENTE
  scanned_at: null
  files_total: 0
  files_scanned: 0
  files_unsupported: 0
  formats_covered: []
  finding_categories: []
  finding_values_retained: false
  high_risk_findings_resolved: false
  human_review_complete: false
  sanitization_complete: false
```

`source_custody_safe` sólo puede ser `true` cuando todos los controles de custodia
anteriores fueron verificados. `sanitization_complete` sólo puede ser `true` si
`files_total == files_scanned`, `files_unsupported == 0`, la cobertura declarada
incluye todos los formatos, los findings de alto riesgo fueron resueltos y la
revisión humana terminó. Ausencia de matches no equivale a scan completo.

Una referencia puede ser una etiqueta opaca como `private-drive:offer-page-v3`. No
registrar URL firmada, ruta con nombre de lead, token, email, teléfono, JID, WABA ID,
Hotmart ID sensible ni fragmento reidentificable.

## 2. Owners

| Role | Owner ref | Alcance de autoridad | Confirmado | Fecha |
|---|---|---|---|---|
| Comercial | `PENDIENTE` | facts, límites, oferta y recorrido | no | null |
| Operativo | `PENDIENTE` | vigencia, procesos y referencias canónicas | no | null |
| Brand Voice | `PENDIENTE` | autoría y reglas de estilo | no | null |
| Template | `PENDIENTE` | copy antes de aprobación Meta | no | null |
| Handoff | `PENDIENTE` | persona/Team, horario y respuesta | no | null |
| Aprobador final | `PENDIENTE` | paquete completo, no activación técnica | no | null |

No crear un owner ficticio para completar la tabla. `PENDIENTE` conserva el gate
correspondiente en falso.

## 3. Fuentes

Una fila por fuente o versión de fuente.

| Source ID | Tipo | Ref privada opaca | Owner | Fecha de fuente | Consultada | Vigencia | Alcance respaldado | Autoría verificable | Sanitización | Estado |
|---|---|---|---|---|---|---|---|---|---|---|
| source-001 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | null | null | `PENDIENTE` | `PENDIENTE` | unknown | pending | pending |

Tipos iniciales permitidos:

- `sales_page`;
- `offer_terms`;
- `faq`;
- `email_sequence`;
- `message_sequence`;
- `waba_template_copy`;
- `conversation_export`;
- `owner_interview`;
- `operational_procedure`;
- `hotmart_configuration_reference`;
- `other` con explicación.

### Gate previo al uso

- [ ] fuente y versión identificables;
- [ ] owner y alcance respaldado registrados;
- [ ] vigencia conocida o marcada `unknown`;
- [ ] scan de PII, URLs privadas, credenciales, datos financieros y adjuntos;
- [ ] autoría separada cuando se usará para Brand Voice;
- [ ] contenido crudo permanece fuera de Git;
- [ ] un posible secreto activa alerta/rotación sin copiar el valor.

## 4. Unidades extraídas

Registrar una idea por fila. No mezclar precio, vigencia y garantía en una sola
unidad.

| Item ID | Clase | Propuesta sanitizada | Source refs | Owner requerido | Vigencia | Destino | Estado | Conflict ref |
|---|---|---|---|---|---|---|---|---|
| item-001 | `unknown` | `PENDIENTE` | `[]` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | pending | null |

Clases:

- `confirmed_fact`;
- `approved_rule`;
- `example`;
- `unknown`;
- `prohibited`;
- `runtime_fact`;
- `kernel_rule`.

Destinos:

- `offer_knowledge`;
- `case_catalog`;
- `conversation_policy`;
- `followup_policy`;
- `brand_voice`;
- `conversation_examples`;
- `waba_template`;
- `handoff_business_policy`;
- `runtime_context`;
- `kernel_reference`;
- `evaluation_only`.

Estados:

```text
pending
confirmed
modified
rejected
conflict
superseded
```

Un `confirmed_fact` exige al menos una fuente, owner y vigencia. Un
`approved_rule` exige decisión registrada. Un `example` nunca convierte su
contenido en hecho o permiso.

## 5. Conflicts y unknowns

| Conflict ID | Item/source refs | Descripción sanitizada | Bloquea | Resolver | Resolución | Estado |
|---|---|---|---|---|---|---|
| conflict-001 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | null | open |

Reglas:

- no elegir silenciosamente una versión;
- no convertir una respuesta aproximada en fact;
- no ocultar el conflict eliminando una fuente;
- una resolución crea una decisión y puede superseder items anteriores;
- mientras esté abierto, el agente no afirma el dato afectado.

## 6. Decisiones

Una decisión por artefacto/campo o regla revisada.

| Decision ID | Target + versión | Acción | Owner/reviewer | Fecha | Source/item refs | Nota sanitizada | Estado |
|---|---|---|---|---|---|---|---|
| decision-001 | `PENDIENTE` | `approve/reject/request_changes` | `PENDIENTE` | null | `[]` | `PENDIENTE` | pending |

La aprobación del paquete conversacional no autoriza:

- activar runtime;
- publicar scope/cohorte;
- enviar templates;
- aplicar migraciones;
- cambiar Meta, Chatwoot, Hotmart o EasyPanel.

## 7. Mapeo a artefactos existentes

| Destino | Template canónico | Items requeridos | Versión borrador | Estado |
|---|---|---|---|---|
| Brief | `conversation-design-brief.md` | oferta, público, trigger, objetivo, éxito, límites | null | blocked |
| Conocimiento | `lancemos-offer-knowledge-template.md` | facts, FAQ, prohibiciones, vigencia | null | blocked |
| Casos | `lancemos-case-catalog-template.md` | señales, facts, pasos, resolución, ejemplos | null | blocked |
| Brand Voice | `lancemos-brand-voice-review-protocol.md` | reglas y tests resueltos | null | blocked |
| Evaluación | `lancemos-conversation-acceptance-matrix.md` | release, escenarios y validator pin | null | blocked |
| Release | `lancemos-conversation-release-v1.md` | versiones exactas y aprobaciones | null | blocked |

No copiar el mismo fact manualmente en varios artefactos sin conservar `item_id` y
`source_refs`. Si cambia, se crea una nueva versión y se vuelve a evaluar.

## 8. Snapshot sanitizado de completitud

```yaml
onboarding_id: PENDIENTE
onboarding_status: not_started
release:
  release_status: draft
  completeness: draft_incomplete
counts:
  sources_registered: 0
  sources_sanitized: 0
  items_total: 0
  confirmed_facts: 0
  approved_rules: 0
  examples: 0
  unknowns: 0
  conflicts_open: 0
  prohibitions: 0
  decisions_pending: 0
gates:
  owners_identified: false
  offer_scope_complete: false
  source_custody_safe: false
  knowledge_ready_for_review: false
  cases_ready_for_review: false
  brand_voice_ready_for_review: false
  template_copy_ready_for_review: false
  followup_policy_ready_for_review: false
  handoff_business_owner_defined: false
  conversation_release_approved: false
activation_authorized: false
blockers:
  - code: missing_offer_scope
    owner_role: commercial
```

Reason codes deben ser estables, sanitizados y accionables. No incluir el valor
faltante, nombre de persona, teléfono, ID externo ni contenido fuente.

## 9. Gate antes de entregar a revisión

- [ ] todos los items tienen clasificación y destino;
- [ ] facts tienen fuente, owner y vigencia;
- [ ] rules tienen decision ref;
- [ ] examples están sanitizados y no legislan;
- [ ] conflicts siguen visibles hasta resolverse;
- [ ] runtime facts no fueron convertidos en contenido estático;
- [ ] kernel rules no fueron presentados como editables;
- [ ] cero PII, secretos o contenido crudo en el registro;
- [ ] cada artefacto derivado puede rastrearse a items y decisiones;
- [ ] el snapshot informa blockers sin declarar activación.
