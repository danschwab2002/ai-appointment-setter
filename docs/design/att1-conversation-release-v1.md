# ATT1 — Conversation Release V1

- **Estado:** Borrador incompleto con alcance comercial inicial recibido
- **Fecha:** 2026-09-01
- **Oferta:** `Alimenta Tu Tiroides`
- **Fuente de facts recién recibidos:** [registro ATT1 V1](att1-source-register-v1.md)
- **Autoridad de vertical y canal:** [dirección aprobada del piloto](lancemos-pilot-product-direction.md)
- **No implica:** contenido aprobado, cambio de mecánica, profile activo, deploy, migración Cloud ni autorización para contactar leads

## 1. Scope de la release candidata

```yaml
release_id: att1-cart-recovery-v1
release_version: 1
release_status: draft
completeness: draft_incomplete
scope:
  customer_ref: att1
  offer_public_name: Alimenta Tu Tiroides
  base_price_received: "47"
  currency_received: USD
  primary_objective: purchase_recovery
  success_outcome: purchase_observed
  channel: whatsapp_official
  pilot_language: null
  pilot_countries: null
handoff:
  recipient_ref: mariana-marin
  recipient_role_reported: chat_responder
  chatwoot_target_verified: false
  schedule_and_sla_verified: false
approval:
  business_owner_ref: null
  operational_owner_ref: null
  conversation_release_approved: false
  activation_authorized: false
```

`purchase_recovery` y `whatsapp_official` no provienen de la entrevista de
Mariana. Se heredan de la dirección de producto aprobada para la vertical mínima
de Lancemos: abandono Hotmart, primer contacto por WhatsApp oficial, conversación
en Chatwoot y cierre por compra u otra causa permitida. La entrevista sólo fija
para ATT1 la oferta candidata y el outcome de compra.

El precio y la moneda fueron recibidos en entrevista, pero aún no deben presentarse como facts aprobados al prospecto hasta contrastarlos con la oferta vigente y registrar owner/vigencia.

## 2. Audiencia recibida

La audiencia principal reportada son mujeres de 35 a 55 años, trabajadoras o emprendedoras, con ingresos propios y diagnosticadas con hipotiroidismo y/o Hashimoto. La distribución declarada fue México 60%, USA 15%, Colombia 10%, Canadá 3% y España 3%.

Esto desbloquea el borrador de audience fit, no una regla de elegibilidad ni contacto. La suma declarada es 91%; no se completa el resto por inferencia. Tampoco se interpreta la distribución como selección automática de países del piloto.

## 3. Objetivo y éxito

El objetivo comercial inicial es recuperar una compra. El outcome observable propuesto es `purchase_observed`, sustentado únicamente por una señal autoritativa y correlacionada de compra. Una respuesta, clic, interés, handoff o aceptación del proveedor de mensajería no equivale a conversión.

La mecánica ya existente permanece fuera de esta release: no se cambia cantidad de mensajes, posiciones, demoras, condiciones de envío, stops, prioridades, budgets, workers ni delivery attempts.

## 4. Handoff humano

Mariana fue identificada como la persona que responde estos chats. Esto permite fijarla como owner comercial candidato del handoff, pero no permite proyectar una asignación todavía.

Para hacer ejecutable el handoff faltan:

- Team o assignee canónico de Chatwoot;
- account e inbox correctos;
- horario, zona horaria, SLA y comportamiento fuera de horario;
- contexto mínimo que debe acompañar la derivación;
- confirmación de que Mariana acepta ese rol para el piloto;
- prueba controlada de pausa durable, asignación y nota privada.

La base durable debe pausar antes de la proyección CRM. Si la proyección falla, la automatización no se reanuda.

## 5. Conocimiento disponible y todavía no incorporado

Se reportó que existen materiales sobre:

- historia y autoridad de la aliada;
- transformación, temario, precio y order bumps;
- audiencia;
- copy, promesa, pilares y mensajes;
- páginas y ads;
- upsell y oferta posterior.

Hasta recibirlos en custodia privada, sanitizarlos, registrar vigencia/owner y resolver contradicciones, esos materiales no forman parte del conocimiento del agente ni de esta release.

## 6. Política sanitaria provisional

Dado que la oferta y audiencia pertenecen al dominio de salud, la release falla cerrada ante consejo clínico. El agente no puede:

- diagnosticar o interpretar síntomas;
- recomendar tratamientos, suplementos, dosis o cambios de medicación;
- prometer cura, eficacia o resultados clínicos;
- pedir historia clínica o información médica innecesaria;
- presentar el producto como sustituto de atención profesional.

Los límites definitivos requieren revisión y aprobación del negocio y, cuando corresponda, revisión sanitaria/legal.

## 7. Estado técnico desbloqueado

La información recibida ya está preservada en el artefacto machine-readable `config/commercial-allies/att1/intake-v1.json`, con gates explícitos `false`. Puede alimentar la preparación posterior del manifiesto y del profile sin copiar estado de otra aliada.

Aún no puede generarse el `CommercialAllyConfig` operativo porque faltan identificadores canónicos de landing, Hotmart y Chatwoot. No se usan los identificadores de Johanna como defaults.

## 8. Gate de publicación

Permanece bloqueada hasta completar y aprobar:

- [ ] owner comercial y owner operativo;
- [ ] precio/moneda/facts con fuente y vigencia;
- [ ] país(es) e idioma del piloto;
- [ ] referencias canónicas de producto, oferta, landing y checkout;
- [ ] consentimiento comercial para WhatsApp;
- [ ] claims permitidos/prohibidos y límites sanitarios;
- [ ] FAQs, casos, Brand Voice y ejemplos sanitizados;
- [ ] política económica publicada, si habrá descuento;
- [ ] templates WABA aprobados;
- [ ] destino operativo de handoff y SLA;
- [ ] matriz conversacional y output contract compatible;
- [ ] aprobación explícita de la release.

La aprobación de esta release seguirá siendo independiente de la autorización para activar workers, outbound o contacto real.
