# Paquete de preparación — Conversation Release V1 de Lancemos

- **Estado:** Borrador estructural; pendiente de insumos y aprobación del negocio
- **Fecha:** 2026-08-10
- **Alcance:** paquete manual, sanitizado y revisable para una oferta del piloto de Lancemos
- **No implica:** contenido comercial aprobado, prompt activo, cambio de runtime, despliegue ni autorización para contactar leads
- **Fuentes:** [dirección del piloto](lancemos-pilot-product-direction.md), [Conversation Release MVP](conversation-release-mvp.md), [brief conversacional](conversation-design-brief.md) y [biblioteca de casos](case-library-and-supervised-skills.md)

## 1. Propósito

Este documento organiza los artefactos mínimos que deben completarse para publicar, más adelante, una primera `Conversation Release` de Lancemos. El paquete parte vacío cuando no existe evidencia del negocio: ningún placeholder constituye un hecho, una regla aprobada ni una autorización.

La release controla comportamiento conversacional. No puede cambiar autorización, opt-out, compra, takeover, frecuencia, perímetro, identidad canónica ni ejecución de efectos externos; esas fronteras permanecen en el kernel y el bridge.

### Mapa del paquete

- [conocimiento de oferta, FAQs, límites y promesas](lancemos-offer-knowledge-template.md);
- [catálogo inicial de casos y criterios comerciales de handoff](lancemos-case-catalog-template.md);
- [protocolo de revisión de Brand Voice y ejemplos](lancemos-brand-voice-review-protocol.md);
- [matriz de aceptación conversacional](lancemos-conversation-acceptance-matrix.md).

Todos son borradores de preparación. Se aprueban como una combinación completa y
versionada; completar uno no habilita su uso aislado en producción.

## 2. Alcance de la primera release

| Dimensión | Valor actual | Estado |
|---|---|---|
| Cliente | Lancemos | dirección aceptada |
| Canal objetivo | WhatsApp oficial mediante Chatwoot | dirección aceptada; acceso y E2E pendientes |
| Trigger | abandono autoritativo de una oferta en Hotmart | implementado técnicamente; binding real pendiente |
| Objetivo principal | recuperación de compra abandonada | dirección aceptada |
| Oferta | `PENDIENTE_NEGOCIO` | bloqueante de contenido |
| Público | `PENDIENTE_NEGOCIO` | bloqueante de contenido |
| Condición de éxito conversacional | `PENDIENTE_NEGOCIO` | bloqueante de aprobación |
| Modo de política | `PENDIENTE_REVISION` | no asumir árbol o playbook |
| Responsable de aprobación | `PENDIENTE_NEGOCIO` | bloqueante de aprobación |
| Identidad transparente del agente | obligación fijada por el kernel | no editable; wording pendiente de compatibilidad |

No se incorpora agendamiento como objetivo mientras Juan no lo agregue explícitamente al alcance.

## 3. Manifiesto de preparación

```yaml
release_id: lancemos-cart-recovery-v1
release_version: 1
release_status: draft
completeness: draft_incomplete
scope:
  customer: lancemos
  offer_ref: PENDIENTE_NEGOCIO
  channel: whatsapp_official
  conversation_trigger: hotmart_cart_abandonment
  primary_objective: cart_recovery

artifacts:
  conversation_policy:
    artifact_id: lancemos-conversation-policy
    version: PENDIENTE_NEGOCIO
    mode: PENDIENTE_REVISION
  brand_voice:
    artifact_id: lancemos-brand-voice
    version: PENDIENTE_REVISION
  offer_knowledge:
    artifact_id: lancemos-offer-knowledge
    version: 1
  qualification_policy:
    artifact_id: lancemos-qualification-policy
    version: PENDIENTE_APLICABILIDAD
  conversation_examples:
    artifact_id: lancemos-conversation-examples
    version: PENDIENTE_REVISION
  operational_case_catalog:
    artifact_id: lancemos-cart-recovery-cases
    version: 1
  output_contract:
    artifact_id: bridge-commercial-output
    version: PENDIENTE_COMPATIBILIDAD_RUNTIME
  acceptance_matrix:
    artifact_id: lancemos-conversation-acceptance-matrix
    version: 1

compatibility:
  platform_kernel: PENDIENTE_PIN
  bridge_output_contract: PENDIENTE_PIN
  handoff_contract: PENDIENTE_WORKSTREAM_D
  messaging_template: PENDIENTE_WABA

approval:
  business_owner: PENDIENTE_NEGOCIO
  operational_owner: PENDIENTE_NEGOCIO
  created_by: PENDIENTE
  created_at: null
  change_reason: "Preparar la primera release conversacional de una oferta de Lancemos"
  validated_by: null
  validated_at: null
  approved_by: null
  approved_at: null
  activated_at: null
  evidence_refs: []
  all_facts_sourced: false
  all_unknowns_resolved_or_blocked: false
  brand_voice_approved: false
  cases_approved: false
  acceptance_matrix_passed: false
  owner_approved: false
  active: false
```

Los identificadores son nombres de trabajo para este paquete manual. No definen todavía schema, persistencia ni API.

## 4. Insumos concretos a solicitar

### A Juan o responsable de producto

- aprobación de la oferta única propuesta para el piloto;
- público objetivo y situación típica del abandono;
- objetivo principal y condición observable de éxito;
- recorrido comercial actual después de un abandono;
- información que el agente debe descubrir, explicar o confirmar;
- límites, promesas prohibidas y errores comercialmente costosos;
- temas que siempre requieren intervención humana;
- wording permitido para presentar al asistente, siempre subordinado a la obligación de identidad transparente del kernel;
- responsable final de aprobar el paquete.

### A Marcela o responsable operativo

- número y cuenta que se conectarán a WhatsApp oficial;
- acceso o coordinación para configurar API, inbox y AgentBot de Chatwoot;
- webhook Hotmart de la oferta elegida y coordinación para verificarlo;
- website, producto, oferta e identificadores canónicos propuestos para aprobación de Juan;
- FAQs vigentes y fuente/fecha de cada respuesta;
- precios, moneda, financiación y condiciones vigentes, con fuente autoritativa;
- mensajes y secuencias que se utilizan hoy;
- tres ejemplos positivos y tres negativos, si existen;
- pocos tipos de caso prioritarios y su resolución real;
- responsable/equipo y horario para escalamiento;
- conversaciones escritas por la persona cuya voz se quiere modelar;
- template WABA existente o copy que deba solicitar aprobación.

Si Marcela no es la responsable de alguno de estos insumos, debe identificar al
owner operativo que lo suministra. Juan actúa como fallback de decisión de
producto; el operador técnico actúa como fallback para referencias canónicas y
accesos, sin copiar secretos al paquete.

### Al equipo técnico u operador

- referencias canónicas del scope real, sin copiar secretos al repositorio;
- versión efectiva del contrato de salida del bridge;
- compatibilidad con el resultado aceptado del Workstream D;
- nombre, idioma, variables y aprobación del template WABA;
- mecanismo para fijar release y evidencias de evaluación antes de activarla.

## 5. Registro canónico de fuentes y decisiones

El registro canónico reutilizable para una corrida de onboarding es
[`first-infoproducer-source-register-template.md`](first-infoproducer-source-register-template.md).
Ese artefacto define una sola vez:

- clasificación de cada unidad como `confirmed_fact`, `approved_rule`, `example`,
  `unknown`, `prohibited`, `runtime_fact` o `kernel_rule`;
- fuentes, owners, vigencia y sanitización;
- conflicts y unknowns;
- decisiones y trazabilidad hacia artefactos derivados.

Este manifiesto referencia los `item_id`, `source_refs` y `decision_refs` de ese
registro; no mantiene una segunda tabla ni reclasifica los datos. Una contradicción
abierta conserva el campo afectado como `unknown/conflict` y bloquea su uso.

## 6. Gate de preparación y aprobación

La release permanece incompleta mientras falte cualquiera de estos puntos:

- [ ] una única oferta y público identificados;
- [ ] hechos comerciales con fuente y vigencia;
- [ ] límites y promesas prohibidas aprobados;
- [ ] FAQs sin contradicciones abiertas;
- [ ] objetivo, recorrido y condición de éxito definidos;
- [ ] casos iniciales completos y revisados;
- [ ] criterios comerciales de escalamiento alineados con el contrato aceptado de D;
- [ ] Brand Voice revisado regla por regla;
- [ ] ejemplos positivos y negativos sanitizados;
- [ ] compatibilidad con el output real del bridge verificada;
- [ ] matriz conversacional ejecutada y aprobada;
- [ ] template WABA aprobado y separado del copy libre dentro de ventana;
- [ ] aprobación explícita del responsable del negocio;
- [ ] pin de versiones y procedimiento de rollback definidos.

El contrato actual `src/bridge/hermes.py::_is_valid_proposal` valida un output de
calificación (`ask_question | qualified | disqualified | handoff`) y campos de
calificación. No es todavía el contrato adecuado para esta release de recuperación
ni para el resultado futuro de D. Por lo tanto, el gate de output permanece
**bloqueado** hasta definir un contrato compatible, identificar su símbolo/schema,
fijar su commit o hash y ejecutarlo sobre cada output evaluado.

## 7. Resultado y límites de este workstream

Este workstream termina cuando existe un paquete vacío pero completo, capaz de recibir insumos sin inventarlos y de mostrar exactamente qué impide su aprobación. No modifica `SOUL.md`, perfiles productivos, schemas, runtime, `docs/architecture.md` ni contratos implementados. La activación requiere trabajo posterior, datos reales, evaluación y aprobación humana.
