# ADR-0009: Conocimiento operativo organizado mediante tipos de caso y skills supervisadas

- **Estado:** Aceptada; implementación pendiente
- **Fecha:** 2026-08-07
- **Fuente de decisión:** Reunión con Juan Martitegui del 7 de agosto de 2026 ([grabación de Fathom](https://fathom.video/share/yj78Kt41tfdyWwPwTsqk-SUcDC3x9JSi))

## Contexto

El agente comercial necesita conocimiento suficiente para resolver situaciones concretas de prospectos y compradores. Volcar documentos heterogéneos dentro de un prompt general no define:

- cómo reconocer una situación;
- qué información falta;
- qué procedimiento seguir;
- qué límites aplicar;
- cuándo el problema está resuelto;
- cuándo escalar;
- cómo incorporar una resolución humana reutilizable.

Diseñar por anticipado un system prompt universal también obliga a especular sobre conversaciones que todavía no observamos. El primer piloto necesita un mecanismo pequeño, revisable y extensible que pueda comenzar con el conocimiento real de una oferta de Lancemos.

El motor vigente ya utiliza `recovery_cases` para representar instancias temporales de recuperación y ADR-0008 asigna autoridad conversacional a cada instancia. Esta decisión introduce un concepto diferente: el **tipo de caso operativo**, que describe un procedimiento reutilizable. Ambos deben mantenerse separados.

ADR-0003 establece que Hermes razona sobre hechos canónicos y que el bridge autoriza y ejecuta. ADR-0006 define agentes especializados y ADR-0005 exige artefactos reproducibles. Esta decisión extiende esas fronteras al conocimiento operativo.

## Decisión

### 1. El conocimiento operativo se organizará alrededor de tipos de caso

Una parte central del conocimiento del agente comercial se representará como una biblioteca versionada de tipos de caso. Cada tipo describe una situación que el sistema puede intentar resolver de punta a punta.

Un tipo de caso debe poder expresar conceptualmente:

- identidad y objetivo;
- señales de reconocimiento;
- información requerida;
- procedimiento o estrategia;
- respuestas y acciones permitidas;
- límites y afirmaciones prohibidas;
- condición de resolución;
- condición de escalamiento;
- ejemplos y contraejemplos;
- fuentes y responsable del negocio.

La representación técnica exacta se definirá durante el diseño y la implementación. La decisión no obliga a que todos los campos vivan en un único archivo o schema.

### 2. Un tipo de caso no es una instancia de ejecución

Se utilizarán términos distintos:

```text
tipo de caso operativo
└── definición reutilizable y versionada

caso de ejecución
└── ocurrencia temporal asociada a contacto, producto, oferta y conversación
```

`recovery_cases` continúa siendo una entidad de ejecución. La biblioteca no reemplaza su estado, autoridad ni ciclo de vida.

### 3. Los tipos de caso se materializarán como playbooks o skills acotadas

El runtime podrá tipificar la situación y cargar el playbook o skill pertinente, en lugar de depender de un prompt monolítico con todos los procedimientos.

Conceptualmente:

```text
hechos y conversación canónicos
→ tipificación
→ selección de skill
→ propuesta estructurada
→ validación determinística
→ ejecución autorizada
```

La tipificación y la skill ayudan a razonar. No adquieren autoridad para publicar mensajes, modificar schedulers, ignorar takeover humano ni ejecutar acciones no permitidas.

### 4. La fuente inicial puede ser manual y de baja complejidad

Para el primer piloto, el negocio puede entregar un Google Doc o documento equivalente organizado mediante un template. Nosotros podemos convertirlo manualmente en tipos de caso y skills.

El formato de onboarding inicial es una decisión táctica y reemplazable. No se construirá una plataforma de edición como prerrequisito del piloto.

### 5. Toda incorporación o modificación es supervisada

El material aportado por el negocio produce borradores. Antes de activarse, cada tipo de caso o skill debe:

1. ser revisado;
2. declarar fuentes, límites e información faltante;
3. pasar validaciones estructurales y de seguridad;
4. evaluarse contra escenarios disponibles;
5. recibir aprobación humana;
6. incorporarse a una versión publicable del comportamiento conversacional.

Una conversación, feedback o resolución humana nunca modifica directamente el comportamiento activo.

### 6. Un caso desconocido puede originar una propuesta de skill

Si una situación se escala y luego una persona la resuelve, el sistema puede proponer:

- crear un tipo de caso nuevo;
- ampliar uno existente;
- corregir una skill;
- agregar ejemplos o contraejemplos.

La propuesta permanece como borrador hasta completar revisión, evaluación, aprobación y publicación. «Autopaquetizar» una resolución significa preparar un artefacto revisable, no aprender online ni desplegar automáticamente.

### 7. La biblioteca forma parte del paquete conversacional versionado

Las versiones activas de tipos de caso y skills deben poder vincularse con la Conversation Release utilizada por una conversación. Un cambio crea un nuevo borrador o release; no reescribe silenciosamente conversaciones históricas.

La forma definitiva de almacenamiento y manifiesto permanece abierta, pero deben preservarse trazabilidad, rollback e inmutabilidad de versiones aprobadas.

## Consecuencias

### Positivas

- El onboarding puede comenzar con casos reales y pocos artefactos.
- El conocimiento queda organizado alrededor de resultados operativos comprobables.
- Las conversaciones desconocidas tienen una salida segura mediante escalamiento.
- Las resoluciones humanas pueden transformarse en mejoras reutilizables y supervisadas.
- El prompt base puede permanecer pequeño y estable.
- Las skills pueden evaluarse, versionarse y desplegarse independientemente de la infraestructura.
- El sistema aprende de evidencia real sin permitir mutación automática de producción.

### Costos y riesgos

- Se necesita diseñar taxonomía, schemas, tipificación y versionado.
- Una clasificación incorrecta puede seleccionar un procedimiento inaplicable.
- Casos superpuestos o ambiguos requieren una política explícita.
- Mantener demasiadas skills pequeñas puede fragmentar el conocimiento.
- El Google Doc inicial exige trabajo manual y revisión cercana.
- La evaluación y aprobación agregan fricción, pero son necesarias para operar con seguridad.

## Alternativas consideradas

### Prompt monolítico con todo el conocimiento

Se descarta como arquitectura principal porque mezcla voz, datos, procedimientos y casos; aumenta el contexto; dificulta evaluar una resolución y vuelve poco trazables los cambios.

### Búsqueda libre sobre todos los documentos o tickets

Se difiere. Puede complementar la biblioteca, pero recuperar texto similar no garantiza seleccionar un procedimiento aprobado ni respetar sus límites.

### Árbol rígido programado para cada situación

Se descarta como solución general porque convierte cada variación comercial en código y reduce innecesariamente la capacidad de razonamiento. Los servicios determinísticos continúan aplicando invariantes, no escribiendo cada conversación.

### Aprendizaje automático desde conversaciones resueltas

Se descarta para producción. Una conversación puede contener errores, excepciones o PII y no constituye por sí sola una política aprobada.

## Estado de implementación

No implementada. El piloto comenzará con documentación manual y una biblioteca pequeña. Todavía deben definirse:

- template inicial;
- schema de tipo de caso;
- contrato del tipificador;
- representación de skills;
- integración con Conversation Releases;
- evaluación y permisos de aprobación;
- persistencia del vínculo entre tipo, instancia, skill y release.

`docs/architecture.md` no debe presentar esta decisión como componente vigente hasta que exista una implementación verificada.

## Documentos relacionados

- [Diseño de biblioteca de casos](../design/case-library-and-supervised-skills.md)
- [Dirección del piloto de Lancemos](../design/lancemos-pilot-product-direction.md)
- [Conversation Release MVP](../design/conversation-release-mvp.md)
- [ADR-0003: frontera determinística y de razonamiento](0003-deterministic-reasoning-boundary.md)
- [ADR-0005: despliegues reproducibles por cliente](0005-reproducible-client-deployments.md)
- [ADR-0006: producto compuesto por tres agentes](0006-three-agent-product-surface.md)
- [ADR-0008: autoridad de conversación por caso](0008-per-case-conversation-anchor.md)
