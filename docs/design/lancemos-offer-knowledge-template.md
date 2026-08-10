# Template — conocimiento de oferta de Lancemos

- **Estado:** Plantilla vacía para onboarding manual
- **Uso:** completar para una sola oferta con fuentes del negocio y revisión humana
- **No es:** conocimiento aprobado, prompt activo ni autorización para prometer o ejecutar acciones

## 1. Control del artefacto

```yaml
artifact_id: lancemos-offer-knowledge
artifact_version: 1
status: draft_incomplete
offer_ref: PENDIENTE_NEGOCIO
business_owner: PENDIENTE_NEGOCIO
reviewed_by: null
reviewed_at: null
source_checked_at: null
```

## 2. Identidad de la oferta

| Campo | Valor | Fuente | Vigencia | Estado |
|---|---|---|---|---|
| Nombre público | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |
| Website ID Hotmart | `PENDIENTE` | configuración canónica | `PENDIENTE` | unknown |
| Product ID Hotmart | `PENDIENTE` | configuración canónica | `PENDIENTE` | unknown |
| Offer code | `PENDIENTE` | configuración canónica | `PENDIENTE` | unknown |
| URL pública | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |
| Público objetivo | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |
| Problema que aborda | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |
| Resultado que ofrece | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |

Los identificadores operativos se validan contra configuración canónica; no se obtienen de texto del lead ni se copian como secretos.

## 3. Hechos comerciales comunicables

Agregar una fila por hecho. No combinar hechos diferentes en una misma afirmación.

| ID | Afirmación exacta permitida | Fuente autoritativa | Responsable | Vigente desde/hasta | Estado |
|---|---|---|---|---|---|
| fact-001 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |

Categorías esperadas cuando correspondan:

- contenido y modalidad;
- duración y acceso;
- precio, moneda y financiación;
- garantía o devolución;
- fechas y disponibilidad;
- requisitos y elegibilidad;
- soporte incluido;
- bonuses o condiciones especiales.

Si la fuente no permite afirmar algo con precisión, registrar el dato como desconocido y escalar la pregunta; no redactar una respuesta aproximada.

## 4. Promesas y afirmaciones prohibidas

| ID | Conducta o afirmación prohibida | Motivo | Respuesta segura o escalamiento | Aprobó |
|---|---|---|---|---|
| prohibition-001 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` |

Incluir expresamente, tras revisión del negocio:

- resultados garantizados;
- urgencia o escasez no verificadas;
- descuentos no vigentes;
- condiciones de devolución inventadas;
- asesoramiento legal, financiero o médico no autorizado;
- identidad engañosa;
- capacidad de ejecutar refunds, cobros o cambios en Hotmart.

## 5. FAQ revisada

| FAQ ID | Pregunta o intención | Respuesta factual aprobada | Fuente | No decir | Vigencia | Estado |
|---|---|---|---|---|---|---|
| faq-001 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | unknown |

Reglas:

- responder primero una pregunta directa cuando exista una respuesta aprobada;
- separar el hecho factual de la forma de expresarlo;
- una FAQ no define autorización, seguimiento ni handoff;
- una respuesta desactualizada se retira mediante una nueva versión del paquete;
- conflictos entre fuentes bloquean la respuesta automática.

## 6. Recorrido comercial

```text
Situación inicial:
PENDIENTE_NEGOCIO

Objetivo principal:
recuperar una compra abandonada (dirección aceptada; concretar condición de éxito)

Información que conviene obtener:
PENDIENTE_NEGOCIO

Información que conviene explicar:
PENDIENTE_NEGOCIO

Próximo objetivo permitido después de cada respuesta:
PENDIENTE_NEGOCIO

Condiciones comerciales de cierre:
PENDIENTE_NEGOCIO

Condiciones comerciales de escalamiento:
PENDIENTE_NEGOCIO
```

Los stops determinísticos —opt-out, compra, takeover, autorización, perímetro y kill switch— no se redefinen aquí.

## 7. Mensajes y templates del canal

| Uso | Modalidad | Copy/Template | Variables permitidas | Aprobación | Estado |
|---|---|---|---|---|---|
| Primer contacto | template WABA | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | blocked |
| Respuesta dentro de ventana | copy generado validado | reglas pendientes | contexto canónico | `PENDIENTE` | blocked |
| Follow-up | según reglas WABA vigentes | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | blocked |

No copiar tokens, teléfonos, JIDs ni credenciales en este artefacto.

## 8. Preguntas abiertas y conflictos

| ID | Pregunta o conflicto | Bloquea qué | Responsable | Resolución | Estado |
|---|---|---|---|---|---|
| open-001 | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | null | open |

## 9. Gate de aprobación

- [ ] cada hecho tiene fuente, responsable y vigencia;
- [ ] precios y condiciones coinciden con la oferta real;
- [ ] todas las FAQs están aprobadas o explícitamente bloqueadas;
- [ ] promesas prohibidas fueron revisadas;
- [ ] unknowns no aparecen como afirmaciones permitidas;
- [ ] variables de templates no permiten inyectar instrucciones;
- [ ] no hay PII, secretos ni payloads reales;
- [ ] el responsable del negocio aprobó el artefacto completo.
