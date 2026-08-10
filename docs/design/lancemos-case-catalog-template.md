# Template — catálogo inicial de casos de Lancemos

- **Estado:** Plantilla vacía para onboarding manual
- **Alcance:** pocos tipos de caso de una oferta, revisados por el negocio
- **No es:** una fila de `recovery_cases`, una skill activa ni el contrato técnico de handoff

## 1. Catálogo

```yaml
catalog_id: lancemos-cart-recovery-cases
catalog_version: 1
status: draft_incomplete
offer_ref: PENDIENTE_NEGOCIO
business_owner: PENDIENTE_NEGOCIO
cases: []
```

Cada entrada describe un **tipo de caso operativo reutilizable**. No representa una conversación o caso de ejecución real.

## 2. Template por tipo de caso

### Identidad y objetivo

```yaml
case_type_id: PENDIENTE
name: PENDIENTE
status: draft
objective: PENDIENTE
business_owner: PENDIENTE
source_refs: []
```

### Reconocimiento

- señales suficientes para considerarlo candidato: `PENDIENTE`;
- señales que lo contradicen: `PENDIENTE`;
- datos canónicos requeridos: `PENDIENTE`;
- preguntas permitidas para aclarar: `PENDIENTE`;
- condiciones de ambigüedad: `PENDIENTE`.

Si faltan señales suficientes, el agente no fuerza la tipificación.

### Procedimiento conversacional permitido

| Paso | Objetivo observable | Hechos requeridos | Respuesta/acción que puede proponer | Prohibiciones | Próximo estado |
|---|---|---|---|---|---|
| 1 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` |

El agente sólo propone respuestas o acciones compatibles con su contrato. El bridge conserva autoridad para validar y ejecutar cualquier efecto.

### Condición de resolución

```text
Evidencia necesaria:
PENDIENTE_NEGOCIO

Resultado observable:
PENDIENTE_NEGOCIO

Lo que no alcanza para declarar resolución:
PENDIENTE_NEGOCIO
```

### Condición comercial de handoff

```text
Señales o pedido explícito:
PENDIENTE_NEGOCIO

Información mínima que debe acompañar la propuesta:
PENDIENTE_NEGOCIO

Responsable o equipo esperado:
PENDIENTE_NEGOCIO

Horario/SLA operativo:
PENDIENTE_NEGOCIO
```

Este bloque sólo define **cuándo el negocio desea escalar y qué contexto resumir**. La pausa durable, asignación, nota privada, reason codes técnicos y carreras pertenecen al Workstream D y deben referenciar su contrato aceptado cuando exista. Hasta entonces, la compatibilidad queda bloqueada.

### Stops de autoridad superior

El caso no puede alterar ni reinterpretar:

- opt-out, `denied` o `restricted`;
- compra autoritativa y cierre del caso;
- takeover o pausa humana vigente;
- perímetro, allowlist, cohorte, presupuesto o kill switch;
- identidad, conversación y oferta canónicas;
- leases, idempotencia o autorización pre-efecto;
- reglas del canal y templates WABA.

### Ejemplos de evaluación

| ID | Contexto sanitizado | Mensaje del lead | Respuesta/decisión deseada | Respuesta/decisión prohibida | Regla demostrada | Estado |
|---|---|---|---|---|---|---|
| example-001 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |

No usar conversaciones reales completas. Cada ejemplo debe eliminar PII, hechos transitorios y datos no necesarios.

## 3. Tipos iniciales a descubrir, no asumir

El onboarding debe pedir al negocio que priorice los pocos casos más frecuentes o costosos del abandono. Posibles categorías sólo para facilitar la entrevista —no son casos aprobados—:

- pregunta factual sobre la oferta;
- problema o duda de pago;
- pedido explícito de hablar con una persona;
- pregunta no cubierta o información insuficiente;
- situación ajena a la oferta elegida.

La taxonomía final surge de evidencia de Lancemos, no de esta lista.

## 4. Gate por caso

- [ ] objetivo inequívoco;
- [ ] señales positivas y contradictorias;
- [ ] información requerida y unknowns explícitos;
- [ ] pasos compatibles con capacidades reales;
- [ ] hechos referenciados al conocimiento de oferta;
- [ ] límites y promesas prohibidas;
- [ ] condición de resolución verificable;
- [ ] criterio comercial de escalamiento;
- [ ] compatibilidad con el contrato aceptado de D;
- [ ] ejemplo positivo y contraejemplo;
- [ ] fuente y responsable identificados;
- [ ] aprobación humana registrada.

## 5. Gate del catálogo

- [ ] sólo incluye casos necesarios para la primera oferta;
- [ ] superposiciones y ambigüedades tienen salida segura;
- [ ] un caso desconocido escala y no improvisa un procedimiento;
- [ ] ningún caso obtiene autoridad determinística;
- [ ] todos los casos pertenecen a la misma versión aprobable;
- [ ] no contiene PII, secretos ni transcripciones completas.
