# Runbook — onboarding asistido del primer infoproductor

- **Estado:** Procedimiento preparado; no ejecutado con un infoproductor real
- **Fecha:** 2026-08-12
- **Alcance:** convertir fuentes y decisiones de una oferta en una Conversation Release borrador, sanitizada y revisable
- **Diseño:** [Onboarding asistido V1](../design/first-infoproducer-onboarding-v1.md)
- **Registro:** [Template de fuentes y decisiones](../design/first-infoproducer-source-register-template.md)
- **No autoriza:** deploy, templates Meta, mensajes, cambios en Chatwoot/Hotmart/EasyPanel/Supabase, edición de `SOUL.md` ni activación

## 1. Resultado operativo

Al terminar una corrida válida deben existir, sin fuentes crudas en Git:

1. scope de una oferta;
2. owners confirmados o blockers explícitos;
3. registro de fuentes, items, conflicts y decisiones;
4. borradores de conocimiento, casos y Brand Voice;
5. Conversation Release inactiva;
6. snapshot sanitizado con gates y próximos responsables;
7. evidencia operativa de la corrida separada de los diseños.

## 2. Roles mínimos

| Rol | Responsabilidad | Puede aprobar |
|---|---|---|
| Facilitador técnico/producto | conducir intake, clasificar y compilar | nada por el negocio |
| Owner comercial | facts, oferta, límites y recorrido | conocimiento y reglas comerciales |
| Owner operativo | vigencia, procedimientos y refs canónicas | facts operativos dentro de su autoridad |
| Owner de voz | autoría, reglas de estilo y ejemplos | Brand Voice |
| Aprobador final | contenido del paquete completo | decisión de negocio registrada; la release conserva su lifecycle técnico separado |
| Owner de handoff | atención y horario | handoff comercial; puede permanecer pendiente |

No avanzar por “presencia” de una persona si no tiene autoridad sobre el dato.

## 3. Preparación privada

### 3.1 Crear espacio de trabajo

La opción recomendada es un storage privado aprobado fuera del checkout. `data/`
sólo puede usarse si el operador demuestra antes que el filesystem y sus backups
cumplen el mismo control. Estar en `.gitignore` **no es un control de acceso**.

Antes de ingerir una fuente, registrar y verificar:

- ubicación aislada por cliente/onboarding, sin acceso de otros agentes o procesos;
- owner/custodian y lista mínima de personas autorizadas;
- permisos restrictivos equivalentes a directorios `0700` y archivos `0600`;
- cifrado en tránsito y en reposo del storage elegido;
- comportamiento de backups/snapshots y ubicación de sus copias;
- plazo o evento de retención, procedimiento de borrado y responsable;
- prohibición de sincronización automática a servicios no aprobados.

Si cualquiera queda desconocido, `source_custody_safe=false` y no se copia la
fuente. Después de verificar la custodia, separar:

```text
raw/          fuentes originales, acceso restringido
working/      extracción temporal y redacción
sanitized/    ejemplos y outputs revisables
release/      artefactos derivados sin PII
```

Si se autoriza `data/`, verificar además su exclusión de Git, permisos efectivos y
ausencia en backups no aprobados antes de copiar. No copiar materiales a chat,
logs, issues o documentación versionada.

### 3.2 Inicializar registro

Copiar el template a la ubicación privada de la corrida y completar sólo:

- `onboarding_id` opaco;
- customer/offer refs opacas;
- custodian;
- owners conocidos;
- referencias privadas de fuentes.

No usar nombres de leads en paths o IDs.

### 3.3 Freeze inicial

Registrar:

- commit base del repositorio;
- versión de templates usados;
- fecha UTC;
- alcance de una oferta;
- capacidades explícitamente fuera de alcance.

Esto fija el proceso, no el comportamiento productivo.

## 4. Fase A — reunión de corte

Duración objetivo: 15–30 minutos.

Preguntar únicamente:

1. oferta y público;
2. trigger y resultado observable de éxito;
3. owner comercial, operativo, voz y aprobador;
4. materiales vigentes disponibles;
5. principal error que no podemos cometer.

### Go

- una oferta identificada;
- al menos owner comercial y aprobador identificados;
- custodia privada acordada;
- fuentes iniciales enumeradas.

### No-go

- “todas las ofertas” como scope;
- owner desconocido para datos comerciales;
- fuentes que sólo pueden copiarse a Git o chat;
- pedido de activar mientras se recopila información.

Salida: brief mínimo y gap list de máximo diez items.

## 5. Fase B — ingestión y sanitización

Por cada fuente:

1. registrar versión, owner, vigencia y alcance;
2. verificar autoría cuando corresponda;
3. ejecutar el procedimiento de scan aprobado para esa corrida, que debe cubrir
   archivos de texto, documentos convertidos a texto, metadata/nombres, URLs,
   emails, teléfonos/IDs largos, datos financieros, patrones de secretos y
   adjuntos/OCR cuando correspondan;
4. separar contenido necesario de información sensible;
5. si aparece un posible secreto, detener la fuente, alertar por categoría sin
   repetir el valor y solicitar rotación por un canal seguro;
6. producir un manifiesto sanitizado con herramienta y versión, timestamp, tipos y
   cantidad de archivos, cobertura por formato, archivos omitidos/ilegibles,
   categorías detectadas y decisión del revisor, sin retener valores;
7. un revisor humano comprueba una muestra y todos los findings de alto riesgo;
8. marcar sanitización completa sólo si la cobertura es total para los formatos
   admitidos, no quedan archivos omitidos y todos los findings fueron resueltos;
9. conservar raw sólo según política privada acordada.

No se fija todavía una herramienta universal: la corrida debe declarar la elegida
y demostrar su cobertura. Un `grep`, una declaración verbal o ausencia de matches
no bastan. Si un formato no puede inspeccionarse, queda `unsupported` y se excluye
o se convierte mediante un procedimiento revisado; nunca se aprueba por omisión.

### Evidencia permitida

```text
source_count=7
source_types=[faq,sales_page,conversation_export]
sanitization=complete
sensitive_categories_detected=[email,url]
values_retained=false
scanner_ref=approved-tool-and-version
files_scanned=7
files_unsupported=0
human_review=complete
```

### Evidencia prohibida

- nombres, emails, teléfonos o JIDs;
- URLs privadas;
- extractos identificables;
- tokens, keys o IDs externos;
- documentos completos.

## 6. Fase C — extracción y clasificación

Extraer unidades atómicas. Para cada una:

1. redactar propuesta sanitizada;
2. enlazar source refs;
3. asignar clase y destino;
4. registrar owner requerido y vigencia;
5. detectar conflict con items existentes;
6. conservar `pending` hasta decisión válida.

### Controles de calidad

- un precio no se infiere de una conversación vieja si existe fuente comercial;
- una frase recurrente no se convierte en Brand Voice si contiene una promesa;
- una resolución humana aislada es ejemplo/candidato, no caso aprobado;
- IDs Hotmart, estado de compra y autorización son `runtime_fact`;
- opt-out e identidad transparente son kernel/runtime, no preferencias.

## 7. Fase D — entrevista por gaps

Agrupar preguntas por owner y reunión. Cada pregunta debe incluir:

```text
Pregunta
→ por qué se necesita
→ artefacto afectado
→ capacidad bloqueada
→ decisión o fuente esperada
```

Orden recomendado:

1. facts y prohibiciones de mayor riesgo;
2. condition of success y recorrido;
3. casos prioritarios;
4. Brand Voice;
5. primer contacto y follow-ups;
6. handoff sólo cuando exista operación humana definida.

Registrar una respuesta verbal como `pending` hasta que owner, fecha y alcance estén
confirmados. Un “lo vemos después” se convierte en `unknown`, no en default
inventado.

## 8. Fase E — completar artefactos existentes

No crear estructuras paralelas. Derivar:

| Información | Artefacto destino |
|---|---|
| resumen de objetivo | `conversation-design-brief.md` |
| facts, FAQ y prohibiciones | `lancemos-offer-knowledge-template.md` |
| tipos de caso | `lancemos-case-catalog-template.md` |
| reglas y tests de voz | `lancemos-brand-voice-review-protocol.md` |
| manifest/versiones | `lancemos-conversation-release-v1.md` |
| escenarios | `lancemos-conversation-acceptance-matrix.md` |

Los artefactos completos de un cliente deben vivir en storage/configuración privada
hasta acordar su formato canónico. Git conserva templates y evidencia sanitizada,
no la base comercial privada por defecto.

## 9. Fase F — Brand Voice

1. seleccionar sólo mensajes del owner objetivo;
2. excluir leads, otros vendedores, bots y conversaciones irrelevantes;
3. proponer reglas observables con confianza y evidencia parafraseada;
4. resolver cada regla como `confirmed`, `modified` o `discarded`;
5. revisar prohibiciones explícitas;
6. generar escenarios con facts ficticios;
7. evaluar apertura y continuidad por separado;
8. registrar aprobación independiente de reglas y tests.

No incorporar nada a `SOUL.md` ni reiniciar profiles durante onboarding.

## 10. Fase G — contenido de canal y políticas

El onboarding puede dejar `ready_for_business_review`:

- copy de primer contacto;
- CTA;
- variables necesarias;
- idioma deseado;
- intención y copy de follow-ups;
- tiempos propuestos y stops.

El workstream WABA valida categoría, placeholders, aprobación Meta, portfolio,
método de pago y envío. El motor durable valida tiempos y autoridad. Ninguno de
esos gates se saltea por aprobación comercial del documento.

## 11. Fase H — compilación y evaluación

### Preflight de compilación

- [ ] scope de una oferta;
- [ ] facts con fuente/vigencia;
- [ ] prohibiciones revisadas;
- [ ] conflicts visibles;
- [ ] casos mínimos completos o explícitamente diferidos;
- [ ] Brand Voice revisada;
- [ ] owners y approvals registrados;
- [ ] cero raw/PII/secrets en artefactos derivados;
- [ ] output contract compatible identificado o gate bloqueado.

Compilar una release con `release_status=draft`. Su campo separado de completitud
puede ser `draft_incomplete` o `ready_for_conversation_evaluation`; esos valores no
son estados del lifecycle. Nunca pasarla a `active` desde este runbook.

Ejecutar la matriz conversacional sólo con:

- release y kernel pinned;
- validator compatible pinned;
- modelo/runtime identificado;
- inputs y outputs sanitizados;
- repeticiones requeridas;
- reviewer y decisiones.

Si el output contract sigue incompatible, registrar
`blocked_incompatible_output_contract`; no adaptar outputs a mano para aprobar.

## 12. Fase I — revisión final

Presentar al negocio en lenguaje simple:

1. qué sabe el agente;
2. qué no sabe;
3. qué está prohibido;
4. qué casos cubre;
5. ejemplos de cómo respondería;
6. qué mensajes/templates se proponen;
7. qué sigue bloqueado por owners o tecnología;
8. qué se activaría más adelante y cómo se revierte.

Registrar decisión por artefacto y una decisión final sobre el paquete. El estado máximo del **onboarding** en este runbook es:

```text
onboarding_status=completed_for_release_review
activation_authorized=false
```

La decisión comercial puede recomendar aprobación, pero la Conversation Release
permanece `release_status=draft` hasta que el mecanismo técnico valide y ejecute su
transición canónica a `validated`/`approved`. Onboarding y release no comparten un
campo `status`.

## 13. Snapshot y reason codes

Publicar sólo conteos, booleans y códigos sanitizados:

```text
missing_offer_scope
missing_commercial_owner
missing_final_approver
source_sanitization_pending
facts_need_review
open_source_conflict
cases_need_review
brand_voice_owner_missing
brand_voice_review_pending
template_copy_needs_business_review
followup_policy_undefined
human_handoff_owner_undefined
incompatible_output_contract
conversation_evaluation_failed
integral_readiness_external
```

No usar `ready` sin calificar el nivel. Para `onboarding_status`, usar
`ready_for_business_review`, `ready_for_conversation_evaluation` o
`completed_for_release_review`; informar `release_status` y `completeness` por
separado.

## 14. Evidencia operacional de una corrida real

Crear un documento fechado en `docs/operations/` con:

- alcance sanitizado;
- versión del proceso;
- tipos y conteos de fuentes;
- categorías sensibles detectadas, sin valores;
- conteos por clase y conflicts;
- gates y reason codes;
- decisiones y owners como referencias opacas;
- resultados de evaluación;
- estado final;
- qué no fue probado ni activado.

No usar la evidencia como fuente de facts comerciales.

## 15. Pausa, reanudación y descarte

### Pausar

- congelar snapshot y lista de gaps;
- preservar fuentes según custodia acordada;
- no marcar pendientes como rechazados;
- registrar próximo owner y acción.

### Reanudar

- revalidar vigencia de fuentes;
- confirmar que oferta y owners no cambiaron;
- aplicar nuevas respuestas como items/decisions, no sobrescribir historia;
- recompilar una versión nueva si cambió un artefacto ya aprobado.

### Descartar

- conservar sólo evidencia sanitizada requerida;
- eliminar working/raw según política acordada;
- retirar accesos temporales;
- no reutilizar contenido en otro cliente u oferta.

## 16. Cierre de la corrida

- [ ] registro completo y sanitizado;
- [ ] artefactos derivados versionados;
- [ ] decisions y blockers trazables;
- [ ] snapshot emitido sin PII;
- [ ] evidencia operacional creada;
- [ ] fuentes privadas retenidas o eliminadas según política;
- [ ] ningún cambio productivo ejecutado;
- [ ] siguiente hito asignado: negocio, WABA, runtime o evaluación.
