# Dirección de producto para el piloto de Lancemos

- **Estado:** Base de diseño aprobada
- **Fecha de aceptación:** 2026-08-07
- **Alcance:** Principios, alcance y condiciones de la primera implementación real
- **Implementación:** Pendiente
- **Fuente:** Reunión con Juan Martitegui del 7 de agosto de 2026 ([grabación de Fathom](https://fathom.video/share/yj78Kt41tfdyWwPwTsqk-SUcDC3x9JSi))

## 1. Propósito

La siguiente etapa del producto debe aprender de una operación real y acotada, no de completar anticipadamente una plataforma general. Lancemos será el design partner y el primer piloto propuesto.

Esta base orienta prioridades, pero no declara implementadas las capacidades mencionadas. El código, los contratos y `docs/architecture.md` continúan describiendo el estado ejecutable vigente.

## 2. Principios que orientan el trabajo

1. **Realidad antes que abstracción.** Las decisiones de producto y conversación se contrastan contra ofertas, casos y conversaciones reales.
2. **Piloto antes que plataforma.** Se implementa la vertical mínima segura antes de automatizar onboarding, configuración o administración general.
3. **Una oferta y alcance acotado.** El primer piloto comienza con un website u oferta de Lancemos, no con todas sus unidades de negocio.
4. **Onboarding manual antes que automatización.** Los primeros onboardings serán asistidos y con contacto humano cercano.
5. **Automatización progresiva.** Se automatizan patrones observados y repetidos; no se automatiza prematuramente un proceso todavía desconocido.
6. **Casos concretos antes que conocimiento genérico.** El conocimiento operativo se organiza alrededor de situaciones que el agente debe resolver de punta a punta.
7. **Configuración como dirección de un empleado inteligente.** El infoproductor expresa objetivos e instrucciones en lenguaje natural; el sistema interpreta, explica, hace pushback y propone una configuración comprobable.
8. **Aprendizaje supervisado.** Una conversación o escalamiento puede originar una propuesta de mejora, pero nunca modificar silenciosamente el comportamiento activo.
9. **Seguridad determinística.** Autorización, opt-out, límites de canal, idempotencia, takeover humano, frecuencia y permisos peligrosos permanecen fuera del juicio final del modelo.
10. **Supervisión intensiva inicial.** La primera implementación se observa de cerca y opera sin capacidades de alto riesgo.

## 3. Vertical mínima del piloto

El objetivo no es ofrecer todavía un producto autoservicio. La primera vertical debe cubrir:

```text
abandono de una oferta real en Hotmart
→ webhook autoritativo
→ creación o reutilización segura del caso de recuperación
→ primer contacto mediante WhatsApp oficial
→ conversación en Chatwoot
→ respuestas y seguimientos contextuales
→ takeover humano cuando corresponda
→ cierre por compra, inactividad u otra causa permitida
```

El piloto parte de:

- un cliente: Lancemos;
- un número de WhatsApp del cliente;
- un website u oferta inicial;
- una cuenta e inbox de Chatwoot;
- templates aprobados para los mensajes que los requieran;
- una biblioteca inicial pequeña de casos;
- operación altamente supervisada.

La relación definitiva entre cliente, número, websites, productos y ofertas permanece abierta. Para el piloto se elige la configuración más pequeña que permita aprender sin mezclar operaciones.

## 4. Insumos necesarios del negocio

Antes de activar el piloto se necesita obtener de Marcela o del responsable operativo:

- número y cuenta que se conectarán a WhatsApp oficial;
- acceso o coordinación para configurar la API y el inbox;
- webhook de Hotmart para la oferta elegida;
- FAQs existentes;
- flujos y procedimientos utilizados actualmente;
- mensajes y secuencias de seguimiento actuales;
- templates existentes o copy para solicitar su aprobación;
- website, producto y oferta iniciales;
- límites comerciales, promesas prohibidas y criterios de escalamiento;
- responsables humanos para takeover y resolución de casos desconocidos.

Estos materiales no se cargan sin estructura directamente al runtime. Primero se interpretan y revisan como conocimiento, casos y configuración propuesta.

## 5. Condiciones mínimas antes de salir

La activación requiere, como mínimo:

- una prueba adicional del flujo end to end en el canal configurado;
- primer contacto y seguimientos compatibles con las reglas vigentes de WhatsApp;
- webhook de abandono autenticado e idempotente;
- mecanismo autoritativo para detectar una compra y cerrar la recuperación;
- takeover humano probado antes del request saliente;
- allowlist y alcance limitados a los participantes del piloto;
- casos iniciales revisados por el negocio;
- prohibición de acciones de alto riesgo no necesarias, incluidos refunds o cambios directos en Hotmart;
- observabilidad suficiente para detectar envíos, bloqueos y estados inciertos;
- procedimiento manual para pausar o detener el piloto.

Los detalles exactos deben convertirse en contratos y runbooks cuando se implemente la integración real. Esta sección no reemplaza esos artefactos.

## 6. Fuera del MVP inicial

No son prerrequisitos del primer piloto:

- onboarding autoservicio;
- cobertura de todas las ofertas de Lancemos;
- biblioteca exhaustiva de casos;
- panel completo de configuración;
- diales comerciales;
- Automation Expert y Client Copilot implementados de punta a punta;
- autoaprobación de cambios o skills;
- acceso del agente a refunds, credenciales o administración de Hotmart;
- producto multiempresa generalizado;
- publicación inmediata de contenido comercial sobre el proceso.

Los tres agentes permanecen como arquitectura de producto aceptada por ADR-0006, aunque sólo el agente comercial sea necesario para la primera vertical.

## 7. Estrategia de onboarding

Los primeros onboardings serán un servicio asistido:

1. seleccionar una oferta;
2. recopilar materiales y entrevistar a las personas responsables;
3. identificar los casos más frecuentes;
4. documentar procedimientos y límites;
5. preparar conocimiento, skills y configuración;
6. revisar el resultado con el negocio;
7. activar de forma controlada;
8. observar conversaciones y escalaciones;
9. registrar patrones repetidos;
10. automatizar sólo las partes ya comprendidas.

La meta no es eliminar el trabajo humano desde el primer cliente. Es utilizarlo para descubrir un onboarding reproducible que reduzca progresivamente el tiempo hasta valor y la dependencia del operador.

## 8. Experiencia de configuración futura

El infoproductor debería poder comunicarse con el sistema como con un empleado competente:

```text
instrucción en lenguaje natural
→ interpretación y preguntas faltantes
→ contraste con evidencia y buenas prácticas
→ pushback cuando corresponda
→ resumen de la configuración propuesta
→ validación determinística
→ aprobación humana
→ publicación versionada
```

El usuario puede decidir aspectos comerciales permitidos, como tiempos u objetivos. No puede utilizar esta flexibilidad para evadir límites del canal, consentimiento, opt-out, seguridad, frecuencia máxima o permisos definidos por el producto.

## 9. Uso de evidencia del piloto

El piloto debe responder, con conversaciones reales:

- qué tipos de caso aparecen;
- qué complejidad conversacional tienen;
- qué conocimiento falta con mayor frecuencia;
- qué escalaciones se repiten;
- qué partes del onboarding consumen más tiempo;
- qué controles necesita realmente el infoproductor;
- qué decisiones puede proponer un agente y cuáles requieren una interfaz o regla explícita;
- cuándo una resolución humana puede convertirse en una skill reutilizable.

La evidencia operativa se registrará en `docs/operations/` después de cada verificación o evento relevante. Este documento no funciona como log del piloto.

## 10. Temas abiertos

- proveedor o modalidad exacta para simplificar el alta de WABA;
- contrato de templates y reglas de ventana que se usarán en el piloto;
- relación definitiva entre números, websites, productos y ofertas;
- oferta inicial de Lancemos;
- primeros tipos de caso;
- responsables y tiempos de takeover;
- métricas de éxito y duración del piloto;
- criterio para ampliar a una segunda oferta o implementación;
- alcance operativo inicial de Automation Expert y Client Copilot.

## 11. Documentos relacionados

- [ADR-0004: capa de mensajería abstraída](../decisions/0004-messaging-layer-abstraction.md)
- [ADR-0006: producto compuesto por tres agentes](../decisions/0006-three-agent-product-surface.md)
- [ADR-0007: motor durable de próxima acción](../decisions/0007-durable-next-action-engine.md)
- [ADR-0008: autoridad de conversación por caso](../decisions/0008-per-case-conversation-anchor.md)
- [Diseño de biblioteca de casos](case-library-and-supervised-skills.md)
