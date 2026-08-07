# Biblioteca de casos y skills supervisadas

- **Estado:** Base de diseño aprobada; detalles técnicos en revisión
- **Fecha de aceptación:** 2026-08-07
- **Alcance:** Captura, organización, ejecución y evolución del conocimiento operativo del agente comercial
- **Implementación:** No iniciada
- **Fuente:** Reunión con Juan Martitegui del 7 de agosto de 2026 ([grabación de Fathom](https://fathom.video/share/yj78Kt41tfdyWwPwTsqk-SUcDC3x9JSi))

## 1. Propósito

El agente comercial no debe depender de un único prompt general ni de una carga indiscriminada de documentos. Una parte central del conocimiento operativo se organizará como una biblioteca de casos concretos que el agente debe poder reconocer y resolver.

Un caso representa una situación recurrente del negocio, por ejemplo:

- recuperación de una compra fallida en Hotmart;
- duda sobre un medio de pago;
- comprador que no encuentra su acceso;
- solicitud que requiere intervención humana.

El objetivo es comenzar con pocos casos bien comprendidos, probarlos en conversaciones reales y ampliar la biblioteca de forma supervisada.

## 2. Dos significados de «caso»

El diseño distingue conceptos que no deben confundirse:

1. **Tipo de caso operativo:** definición reutilizable de una situación y su procedimiento. Ejemplo: `hotmart_payment_failure`.
2. **Caso de ejecución:** instancia temporal perteneciente a un contacto, producto u oferta. En el motor actual corresponde a una fila como `recovery_cases`.

```text
tipo de caso operativo
└── playbook/skill reutilizable

caso de ejecución
└── una ocurrencia real con contacto, conversación, estado y evidencia
```

Un tipo puede guiar muchas instancias. Cada instancia conserva su propia autoridad, conversación y ciclo de vida de acuerdo con ADR-0008.

## 3. Fuente inicial: documento asistido

Para las primeras implementaciones no se construirá un editor complejo. El negocio aportará un Google Doc o documento equivalente preparado a partir de un template y acompañado por nosotros durante el onboarding.

La estructura conceptual mínima por caso es:

```text
Nombre del caso
Objetivo
Cómo reconocerlo
Datos que se necesitan
Pasos de resolución
Respuestas o alternativas permitidas
Límites y afirmaciones prohibidas
Condición de resolución
Condición de escalamiento
Ejemplos correctos
Contraejemplos
Responsable humano
Fuentes del negocio
```

El documento puede estar incompleto o utilizar lenguaje natural. No se convierte automáticamente en comportamiento activo: primero debe interpretarse, revisarse y validarse.

## 4. Artefactos derivados

El sistema o el operador podrá derivar del material inicial:

- un catálogo de tipos de caso;
- criterios o señales para clasificarlos;
- campos requeridos y faltantes;
- una skill o playbook por tipo;
- ejemplos y contraejemplos;
- reglas de resolución y escalamiento;
- pruebas representativas;
- referencias a conocimiento comercial y Brand Voice.

El conocimiento de producto, precios, condiciones y voz puede ser compartido entre casos. La biblioteca no reemplaza esos artefactos: define cómo utilizarlos para resolver una situación concreta.

## 5. Flujo de incorporación

```text
material del negocio
→ extracción de casos propuesta
→ revisión con el responsable
→ borradores de skills/playbooks
→ validación estructural y de seguridad
→ evaluación con ejemplos
→ aprobación humana
→ incorporación a una Conversation Release
→ activación versionada
```

Reglas obligatorias:

- el material fuente no es comportamiento activo;
- la extracción de casos es una propuesta revisable;
- una skill nueva no se autoaprueba;
- toda activación queda versionada y auditable;
- una release activa nunca se modifica silenciosamente;
- los límites del kernel prevalecen sobre cualquier instrucción del caso.

## 6. Flujo durante una conversación

Conceptualmente, el runtime opera así:

```text
SituationReport y conversación canónica
→ tipificación del caso
→ selección de playbook o skill aplicable
→ razonamiento dentro del caso y el conocimiento permitido
→ propuesta estructurada de respuesta, acción o escalamiento
→ validación determinística del bridge
→ ejecución autorizada
```

La tipificación puede requerir preguntas adicionales. Si no existe evidencia suficiente, el agente no debe forzar una clasificación ni inventar el procedimiento.

El diseño detallado deberá resolver si una intervención puede tener un caso primario y casos secundarios, cómo se representan ambigüedades y cómo se evita cargar skills irrelevantes.

## 7. Caso desconocido y autopropuesta de una skill

Cuando el agente no puede resolver una situación conocida de forma segura:

1. escala a una persona;
2. la automatización queda pausada según las reglas del caso de ejecución;
3. la persona resuelve el problema;
4. el sistema reúne la evidencia necesaria sin copiar PII innecesaria;
5. propone una definición de tipo de caso o una mejora de una skill existente;
6. un responsable revisa, corrige y aprueba o rechaza la propuesta;
7. si se acepta, se crea un nuevo borrador de Conversation Release;
8. sólo después de validación y activación puede utilizarse en conversaciones nuevas.

«Autopaquetizar» significa producir un borrador supervisado. No significa aprendizaje online ni escritura directa sobre producción.

## 8. MVP para Lancemos

La primera implementación será deliberadamente manual:

- un documento único con pocos casos;
- extracción y edición asistidas por nosotros;
- skills mantenidas como artefactos versionados;
- tipificación acotada a los casos seleccionados;
- escalamiento por defecto ante casos desconocidos;
- revisión cercana de las primeras conversaciones;
- incorporación manual de mejoras.

No se necesita inicialmente:

- un portal de edición;
- generación autónoma de skills;
- búsqueda semántica sobre todos los tickets históricos;
- aprobación automática;
- cobertura de todas las áreas de soporte;
- autoactivación de una nueva release.

## 9. Validación mínima de un caso

Antes de aprobar un tipo de caso o skill se debe comprobar:

- nombre y objetivo inequívocos;
- señales de entrada suficientes;
- información faltante declarada;
- pasos compatibles con las capacidades reales;
- ausencia de promesas prohibidas;
- límites de autorización explícitos;
- condición de resolución verificable;
- escalamiento seguro;
- ejemplos y contraejemplos;
- compatibilidad con el contrato de salida;
- evaluación contra escenarios representativos;
- responsable y fuente identificables.

## 10. Relación con otros componentes

### Brand Voice

Define cómo se comunica el agente. No decide cómo resolver un caso.

### Conocimiento comercial

Define productos, ofertas, precios, condiciones y restricciones. Un caso referencia ese conocimiento cuando lo necesita.

### Política de seguimiento

Define próximas acciones, demoras y condiciones. Un caso puede seleccionar o proponer una política, pero el motor durable la ejecuta y vuelve a autorizar cada efecto.

### Conversation Release

Publica una combinación exacta e inmutable del catálogo de casos, skills, Brand Voice, conocimiento, políticas y ejemplos aprobados.

### Automation Expert

Ayuda a crear y modificar casos, procedimientos y automatizaciones mediante lenguaje natural. Propone; la aplicación valida y un humano aprueba.

## 11. Temas abiertos

- schema definitivo del tipo de caso;
- ubicación y formato de las skills;
- identificadores y versionado;
- taxonomía inicial de Lancemos;
- contrato de salida del tipificador;
- soporte de casos múltiples o ambiguos;
- selección entre creación de caso nuevo y mejora de uno existente;
- datos mínimos para aprender de una resolución humana;
- evaluación y umbrales de aprobación;
- permisos para crear, revisar y activar;
- persistencia del vínculo entre instancia, tipo, skill y release.

## 12. Documentos relacionados

- [ADR-0009: conocimiento operativo mediante casos](../decisions/0009-case-oriented-operational-knowledge.md)
- [Conversation Release MVP](conversation-release-mvp.md)
- [Dirección del piloto de Lancemos](lancemos-pilot-product-direction.md)
- [ADR-0003: frontera determinística y de razonamiento](../decisions/0003-deterministic-reasoning-boundary.md)
- [ADR-0006: tres agentes](../decisions/0006-three-agent-product-surface.md)
- [ADR-0008: autoridad por caso de ejecución](../decisions/0008-per-case-conversation-anchor.md)
