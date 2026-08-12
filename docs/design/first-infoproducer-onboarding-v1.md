# Onboarding asistido del primer infoproductor — V1

- **Estado:** Propuesta operable para revisión; no implementada ni activa
- **Fecha:** 2026-08-12
- **Alcance:** una oferta real, recuperación de carrito mediante WhatsApp oficial y una Conversation Release inicial
- **Modalidad:** concierge; nosotros conducimos el proceso y el negocio revisa hechos, reglas y ejemplos
- **No implica:** UI autoservicio, Automation Expert, configuración productiva, contenido aprobado, cambio de `SOUL.md` ni autorización para contactar leads
- **Fuentes:** [dirección del piloto](lancemos-pilot-product-direction.md), [paquete de Conversation Release](lancemos-conversation-release-v1.md), [biblioteca de casos](case-library-and-supervised-skills.md) y [protocolo de Brand Voice](lancemos-brand-voice-review-protocol.md)

## 1. Resultado buscado

El onboarding debe convertir materiales y decisiones del negocio en un paquete
revisable sin pedirle al infoproductor que diseñe prompts, schemas o árboles:

```text
fuentes privadas + entrevista guiada
→ registro de procedencia y vigencia
→ hechos, reglas, ejemplos, unknowns y prohibiciones
→ artefactos existentes de oferta, casos y Brand Voice
→ Conversation Release draft_incomplete
→ evaluación y aprobación posteriores
```

El primer resultado útil no es activar el agente. Es poder responder, sin
suposiciones:

- qué oferta y público están dentro del piloto;
- qué puede afirmar el agente y con qué fuente;
- qué no puede afirmar o prometer;
- cuáles son los pocos casos iniciales;
- cómo debe expresarse;
- qué decisiones faltan y quién las debe tomar;
- qué bloquea template, conversación, follow-up, handoff y activación.

## 2. Auditoría de reutilización

La base existente es suficiente para recibir contenido real. Esta propuesta no la
duplica: agrega la secuencia operativa y el registro común que hoy faltan.

| Artefacto existente | Estado útil actual | Reutilización | Falta para usarlo con el primer infoproductor |
|---|---|---|---|
| `conversation-design-brief.md` | plantilla mínima general | usar como resumen inicial, no como cuestionario completo | owner, fuentes, vigencia y enlace a artefactos derivados |
| `lancemos-conversation-release-v1.md` | manifiesto estructural completo | conservar como paquete agregador; referencia el registro canónico de fuentes | completar pins, owners y artefactos con datos reales; no activarlo |
| `lancemos-offer-knowledge-template.md` | plantilla robusta por oferta | completar una copia de trabajo privada/revisable | oferta real, facts, FAQs, límites, fuentes y aprobación |
| `lancemos-case-catalog-template.md` | template robusto por tipo de caso | usar sólo para pocos casos prioritarios | evidencia real, criterios de resolución y responsable cuando exista |
| `lancemos-brand-voice-review-protocol.md` | protocolo específico y seguro | aplicar sin reutilizar la voz privada de pruebas anteriores | owner de voz, fuentes propias, sanitización y revisión atómica |
| `brand-voice-from-conversations-mvp.md` | diseño conceptual amplio | referencia, no nuevo entregable | decisiones de storage/UI permanecen diferidas |
| `lancemos-conversation-acceptance-matrix.md` | matriz mínima completa | usar después de compilar el paquete | output contract compatible, escenarios reales sanitizados y corridas |
| `questions-for-juan.md` | registro histórico y temas abiertos | fuente de preguntas pendientes | no usarlo como checklist vivo ni duplicar su historia |
| `lancemos-waba-hotmart-readiness.md` | readiness técnico/externo | consumir para dependencias del canal | portfolio, número, templates e IDs definitivos pertenecen al frente WABA |
| evidencia `2026-08-06-brand-voice-onboarding-trial.md` | prueba favorable del proceso | reutilizar aprendizajes | no reutilizar conversaciones ni Brand Voice privados como si fueran del cliente |

### Gap que cierra esta propuesta

1. un único registro canónico de fuentes y decisiones, referenciado por la release;
2. intake progresivo ordenado por impacto;
3. reglas claras para convertir respuestas en artefactos existentes;
4. estados visibles de completitud por capacidad;
5. un runbook ejecutable de reuniones, trabajo offline y aprobación.

No se propone un schema, API, UI ni almacenamiento definitivo.

## 3. Fronteras de autoridad

### El negocio aporta o aprueba

- oferta, público, promesa comercial autorizada y condición de éxito;
- hechos, FAQs, límites y vigencia;
- ejemplos de ventas y estilo;
- casos prioritarios y resolución real;
- copy del template y políticas comerciales;
- responsable de aprobación;
- handoff sólo cuando exista una persona o equipo real.

### Nosotros conducimos

- preparación y custodia de fuentes;
- preguntas progresivas y detección de faltantes;
- separación entre hechos, voz, política, casos y runtime;
- propuestas estructuradas y pushback;
- sanitización, compilación y evaluación;
- trazabilidad entre fuente, decisión y artefacto.

### El sistema determinístico conserva

- identidad transparente;
- autorización, opt-out, compra y takeover;
- scope, allowlist, presupuesto y kill switch;
- frecuencia, reglas del canal y templates;
- idempotencia y ejecución de efectos.

Ninguna respuesta del onboarding puede debilitar esta última capa.

## 4. Modelo de intake

Se usan dos carriles y no más hasta contar con evidencia que justifique otro.

### Carril A — extracción asistida de materiales

Adecuado para página de venta, FAQ, emails, secuencias, documentos, templates y
conversaciones. Cada fuente se registra antes de extraer información. La extracción
produce propuestas clasificadas; nunca hechos aprobados automáticamente.

### Carril B — entrevista guiada por gaps

Se usa para confirmar, contradecir o completar lo que no está respaldado por los
materiales. Cada pregunta debe indicar qué artefacto y gate resuelve. No se pide
“contame todo sobre tu negocio”.

Los dos carriles convergen en el mismo registro. Una respuesta verbal sin owner,
fecha o alcance queda como propuesta pendiente, no como `confirmed_fact`.

## 5. Clasificación obligatoria

Cada unidad de información recibe una sola clasificación primaria:

| Clase | Significado | Puede llegar a una release aprobada |
|---|---|---|
| `confirmed_fact` | hecho con fuente, owner y vigencia | sí, como conocimiento |
| `approved_rule` | conducta revisada y aprobada | sí, como política/caso/voz según destino |
| `example` | muestra sanitizada que ilustra una conducta | sí, como ejemplo; no crea autoridad |
| `unknown` | faltante o conflicto no resuelto | no; conserva el gate bloqueado |
| `prohibited` | afirmación o conducta vedada | sí, como límite explícito |
| `runtime_fact` | dato que llega de fuente canónica por ejecución | no vive como hecho estático en la release |
| `kernel_rule` | restricción no editable por el negocio | referencia, no copia mutable |

Una contradicción se registra como `unknown/conflict`. No se elige silenciosamente
la fuente “más creíble”.

## 6. Secuencia mínima de onboarding

### Etapa 0 — owners y custodia

Definir antes de procesar materiales:

- owner comercial de la oferta;
- owner operativo/técnico para referencias canónicas;
- owner de Brand Voice;
- aprobador final de la Conversation Release;
- ubicación privada de fuentes y responsable de borrado/retención.

Un rol puede estar `pending`, pero debe verse como blocker. El Team de handoff puede
permanecer pendiente y no se reemplaza por un placeholder.

### Etapa 1 — corte de oferta

Elegir exactamente una oferta y establecer:

- nombre público, público y problema;
- trigger: abandono de carrito;
- objetivo primario y condición observable de éxito;
- website/product/offer refs como `runtime_fact` verificable por el operador;
- canales y acciones fuera de alcance.

Sin este corte no se profundiza en casos ni copy definitivo.

### Etapa 2 — fuentes y facts

Inventariar fuentes, escanear contenido sensible y extraer unidades pequeñas:

- hechos comunicables;
- FAQ;
- precios, moneda y financiación;
- modalidad, duración, acceso y soporte;
- garantías, fechas y restricciones;
- promesas prohibidas y errores costosos;
- conflictos y unknowns.

### Etapa 3 — recorrido y casos

Preguntar cómo se resuelven hoy los pocos abandonos más frecuentes o costosos.
Priorizar de uno a tres tipos de caso. Por cada uno capturar señales, facts
requeridos, pasos permitidos, condición de resolución, límites, ejemplo y
contraejemplo. Un caso desconocido conserva salida segura.

### Etapa 4 — Brand Voice

Identificar al autor objetivo y procesar sólo sus mensajes pertinentes:

```text
fuentes privadas
→ separación de autor
→ scan de PII/secretos
→ inferencias observables
→ confirmar / modificar / descartar cada regla
→ pruebas con hechos ficticios explícitos
→ aprobación del owner
```

Precio, promesas, política y casos nunca se aprenden como voz.

### Etapa 5 — templates y follow-ups

Preparar contenido comercial para revisión, pero delegar el contrato técnico y la
aprobación del canal al workstream WABA. Registrar por separado:

- motivo y texto del primer contacto;
- variables necesarias y su fuente;
- idioma y owner de aprobación;
- objetivo de cada follow-up;
- tiempos propuestos, máximos y stop conditions.

El copy no se considera enviable por estar completo en el onboarding.

### Etapa 6 — compilación y evaluación

Compilar una `Conversation Release` inactiva que fija versiones exactas. Ejecutar
la matriz sólo cuando exista un output contract compatible. Hallazgos vuelven al
artefacto responsable; no se corrige todo en Brand Voice ni en un prompt general.

### Etapa 7 — aprobación

Presentar un resumen humano de:

- hechos y reglas incluidos;
- unknowns y capacidades bloqueadas;
- prohibiciones;
- escenarios de aceptación;
- versiones y rollback;
- qué activaría el paquete y qué queda fuera.

La aprobación del contenido no activa canal, cohortes ni runtime.

## 7. Preguntas progresivas

### Bloque 1 — quince minutos, corte mínimo

1. ¿Cuál es la única oferta que vamos a probar?
2. ¿A quién está dirigida y qué problema concreto busca resolver?
3. ¿Qué evento inicia el contacto y qué resultado observable sería éxito?
4. ¿Quién confirma los datos comerciales y quién aprueba el paquete final?
5. ¿Qué materiales vigentes ya existen y quién es dueño de cada uno?

### Bloque 2 — facts y riesgo

1. ¿Qué debe poder responder desde el primer día?
2. ¿Cuál es la fuente vigente de precio, cuotas, garantía, acceso y fechas?
3. ¿Qué nunca debe prometer, afirmar o improvisar?
4. ¿Qué respuesta equivocada tendría mayor costo comercial o reputacional?
5. ¿Dónde hay contradicciones o información que cambia con frecuencia?

### Bloque 3 — casos y recorrido

1. ¿Por qué suelen abandonar o no completar?
2. ¿Cuáles son los tres casos más frecuentes o costosos?
3. ¿Cómo reconoce y resuelve hoy una persona cada caso?
4. ¿Qué evidencia demuestra resolución?
5. ¿Cuándo debe dejar de vender o pedir ayuda humana?

### Bloque 4 — voz y ejemplos

1. ¿La voz debe imitar a una persona, a la marca o a un equipo?
2. ¿Qué conversaciones fueron escritas por ese owner?
3. ¿Qué tres respuestas representan bien la forma deseada?
4. ¿Qué tres respuestas serían inaceptables y por qué?
5. ¿Qué expresiones, formatos o actitudes deben evitarse?

### Bloque 5 — contacto y seguimiento

1. ¿Cómo explicamos con transparencia quién escribe y por qué?
2. ¿Qué debería invitar a responder el primer contacto?
3. ¿Qué objetivo tiene cada seguimiento y cuándo deja de aportar valor?
4. ¿Qué eventos cancelan inmediatamente la secuencia?
5. ¿Quién revisa el copy antes de enviarlo a aprobación de Meta?

No se ejecutan todos los bloques en una llamada si el negocio no tiene los owners o
fuentes presentes. Se envía un gap list pequeño y se retoma con evidencia.

## 8. Matriz de habilitación por capacidad

| Capacidad | Insumo de negocio mínimo | Dependencia técnica externa | Estado si falta algo |
|---|---|---|---|
| redactar template para revisión | oferta, público, motivo, identidad, CTA, prohibiciones, owner | contrato de template WABA | `blocked_missing_business_input` |
| pedir aprobación del template | copy aprobado, idioma, categoría y variables | portfolio/WABA definitivo y método de pago | `blocked_channel_dependency` |
| evaluar conversación | facts, casos, política, voz y ejemplos | output contract compatible y modelo fijado | `blocked_missing_release_input` |
| definir follow-ups | objetivo por paso, tiempos, stops y aprobación | política durable y reglas WABA | `blocked_missing_policy_decision` |
| habilitar handoff | criterios y resumen requerido | persona/Team real, horario y runtime probado | `blocked_missing_human_owner` |
| recomendar piloto | release aprobada y matriz sin críticos | WABA, Hotmart, Supabase, stops y E2E | `blocked_integral_readiness` |

La falta de handoff no impide preparar oferta, voz, template o inbound
observacional. Sí impide declarar listo el piloto supervisado si el handoff forma
parte de su comportamiento habilitado.

## 9. Estado visible del paquete

Estados permitidos para el **proceso de onboarding**:

```text
not_started
collecting_sources
extracting
needs_business_input
ready_for_business_review
changes_requested
ready_for_conversation_evaluation
completed_for_release_review
```

Estos valores nunca se escriben como `release_status`. El lifecycle de la
**Conversation Release** permanece separado y canónico:

```text
draft → validated → approved → active → retired
```

`draft_incomplete` describe la completitud del manifiesto mientras continúa en
`release_status=draft`; no agrega un estado al lifecycle. El onboarding puede
terminar en `completed_for_release_review` mientras la release sigue `draft`, o
puede registrar que el owner aprobó contenido mientras la transición técnica a
`approved` permanece pendiente. `active` nunca pertenece al onboarding y requiere
publicación/readiness posteriores.

Cada snapshot debe informar como mínimo:

```yaml
onboarding_id: PENDIENTE
scope:
  customer_ref: PENDIENTE
  offer_ref: PENDIENTE
onboarding_status: not_started
release:
  release_status: draft
  completeness: draft_incomplete
owners:
  commercial: PENDIENTE
  operational: PENDIENTE
  voice: PENDIENTE
  final_approver: PENDIENTE
counts:
  sources_registered: 0
  confirmed_facts: 0
  approved_rules: 0
  examples: 0
  unknowns: 0
  conflicts: 0
  prohibitions: 0
gates:
  offer_scope_complete: false
  knowledge_ready_for_review: false
  cases_ready_for_review: false
  brand_voice_ready_for_review: false
  template_copy_ready_for_review: false
  followup_policy_ready_for_review: false
  handoff_business_owner_defined: false
  conversation_release_approved: false
activation_authorized: false
```

No incluir PII, contenido fuente, teléfonos, IDs externos ni secretos en este
snapshot.

## 10. Criterio de terminado de esta propuesta

El onboarding V1 está preparado cuando un operador puede:

- iniciar con una oferta y owners claros;
- registrar materiales sin copiarlos a Git;
- extraer y clasificar información con procedencia;
- formular sólo preguntas que cierran gaps concretos;
- completar las plantillas existentes sin duplicarlas;
- mostrar blockers por capacidad;
- compilar una release inactiva;
- obtener decisiones atómicas y auditables;
- entregar el paquete a evaluación y readiness posteriores.

La ejecución real con el primer infoproductor deberá producir evidencia separada en
`docs/operations/`; este diseño no cuenta como una corrida de onboarding.

## 11. Temas abiertos para aprender del primer caso

- duración y número óptimo de reuniones;
- volumen mínimo de materiales útil para oferta y voz;
- qué preguntas generan mayor fricción;
- qué extracción puede automatizarse sin perder procedencia;
- qué formato privado resulta más cómodo para el negocio;
- cuántos casos hacen falta para time-to-first-value;
- quién termina siendo owner operativo y de handoff;
- criterio observable de éxito comercial del piloto;
- qué campos merecen luego una UI o Automation Expert.
