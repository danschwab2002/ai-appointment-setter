# Conversation Release MVP

- **Estado:** Propuesta para revisión
- **Alcance:** Versionado y publicación del paquete conversacional del agente comercial
- **Fuera de alcance:** Contenido comercial concreto, interfaz de edición, almacenamiento, permisos detallados, aprendizaje por feedback y experimentación A/B

## 1. Propósito

Una `Conversation Release` es un snapshot completo, identificable e inmutable del comportamiento conversacional que puede utilizar el agente comercial.

Su objetivo es permitir que una configuración sea:

- revisada antes de publicarse;
- activada como una unidad;
- asociada a las conversaciones que la utilizan;
- reproducida posteriormente;
- reemplazada sin modificar el historial;
- revertida de manera segura.

Una release no representa código de infraestructura ni estado de una conversación. Representa la versión publicada del paquete comercial y conversacional.

## 2. Capas relacionadas

La release se encuentra entre dos capas que no controla:

```text
Kernel de plataforma
└── invariantes, seguridad y contratos no editables por el cliente

Conversation Release
└── comportamiento comercial versionado y aprobado

Contexto de ejecución
└── conversación, hechos canónicos y autorizaciones del turno actual
```

Una release nunca puede reducir las restricciones del kernel ni modificar el contexto canónico de ejecución.

## 3. Contenido mínimo

Cada release referencia versiones exactas de los artefactos que forman el paquete:

1. **Política conversacional:** árbol, guion o playbook que establece cómo debe avanzar la conversación.
2. **Brand voice:** voz, tono, expresiones y comportamientos comunicacionales.
3. **Conocimiento comercial:** productos, ofertas, condiciones, información permitida y afirmaciones prohibidas.
4. **Política de calificación:** criterios e información comercial requerida, cuando corresponda.
5. **Ejemplos conversacionales:** ejemplos y contraejemplos aprobados.
6. **Contrato de salida:** estructura que debe producir el agente para que la aplicación pueda validarla.

La política conversacional puede implementar cualquiera de las variantes bajo evaluación:

- árbol con respuestas casi guionadas;
- árbol estricto con libertad local de redacción;
- playbook orientado por objetivos.

La variante utilizada debe quedar declarada en la release.

## 4. Manifiesto mínimo

El manifiesto identifica la release y fija las versiones exactas de sus artefactos.

```yaml
release_id: conversation-release-0001
release_version: 1
status: draft
scope_id: commercial-agent
conversation_policy_mode: strict_tree_local_wording

artifacts:
  conversation_policy_version: 1
  brand_voice_version: 1
  commercial_knowledge_version: 1
  qualification_policy_version: 1
  conversation_examples_version: 1
  output_contract_version: 1

compatibility:
  platform_kernel_version: 1
  output_schema: commercial-response-v1

change:
  reason: "Primera versión para evaluación"
  created_by: null
  approved_by: null
  created_at: null
  approved_at: null
  activated_at: null
```

Los identificadores, campos definitivos y forma de persistencia se decidirán durante la implementación. El manifiesto expresa el contrato conceptual del MVP.

## 5. Ciclo de vida

```text
draft
  ↓
validated
  ↓
approved
  ↓
active
  ↓
retired
```

También puede terminar en `rejected` antes de su activación.

### `draft`

- Puede modificarse.
- No puede utilizarse en producción.
- Cada modificación invalida cualquier validación anterior.

### `validated`

- Pasó los controles requeridos.
- Puede volver a `draft` si necesita cambios.
- Todavía no está autorizada para producción.

### `approved`

- Fue aprobada por el responsable definido.
- Su contenido queda inmutable.
- Está lista para activarse.

### `active`

- Puede asignarse a conversaciones nuevas dentro de su alcance.
- Continúa siendo inmutable.

### `retired`

- Ya no se asigna a conversaciones nuevas.
- Se conserva para auditoría, reproducción y conversaciones que sigan vinculadas a ella.

### `rejected`

- No puede activarse.
- Para continuar debe crearse o revisarse un borrador.

## 6. Reglas obligatorias

### 6.1. Inmutabilidad

Una release aprobada, activa o retirada nunca se edita. Cualquier cambio produce una versión nueva.

### 6.2. Activación atómica

La release se activa como una unidad completa. No se activa un artefacto individual mientras los demás permanecen en una combinación no aprobada.

### 6.3. Una versión activa por alcance

Dentro de un mismo alcance sólo puede existir una release activa. El significado definitivo de alcance —agente, producto, oferta o campaña— queda abierto hasta contar con más información del producto.

### 6.4. Asociación con conversaciones

Cada conversación debe registrar qué release utiliza. Por defecto:

- las conversaciones nuevas reciben la release activa;
- una conversación existente conserva la release con la que comenzó;
- cualquier migración a otra release debe ser explícita y auditable.

Las correcciones obligatorias del kernel de plataforma no dependen de esta asociación y pueden prevalecer inmediatamente.

### 6.5. Rollback completo

Un rollback reactiva una release anterior completa. No reconstruye manualmente combinaciones de artefactos.

### 6.6. Conservación

Las releases publicadas no se eliminan. Se conservan su manifiesto, sus artefactos y la evidencia mínima de validación y aprobación.

### 6.7. Sin mutación por feedback

El feedback nunca modifica una release activa. Puede originar un nuevo borrador, que deberá atravesar nuevamente el ciclo de validación y aprobación.

## 7. Validación mínima antes de aprobar

El MVP debe comprobar, como mínimo:

### Controles estructurales

- el manifiesto está completo;
- todos los artefactos requeridos existen;
- las referencias apuntan a versiones exactas;
- el contrato de salida es compatible con el kernel declarado;
- no existen identificadores duplicados;
- la release no mezcla artefactos incompatibles.

### Controles de contenido

- el conocimiento comercial no contiene contradicciones evidentes;
- el Brand Voice fue revisado y aprobado expresamente por el infoproductor;
- para la primera release, el onboarding obligatorio de Brand Voice está completo;
- los ejemplos no contradicen reglas de mayor autoridad;
- no existen promesas o afirmaciones expresamente prohibidas;
- la política conversacional respeta los límites del kernel;
- la información faltante está declarada y no se completa mediante suposiciones.

### Evaluación

- la release se ejecutó contra el conjunto mínimo de escenarios disponible;
- los errores críticos bloquean la aprobación;
- los resultados y observaciones quedan asociados a la release evaluada.

El número de escenarios, los umbrales y la automatización de estos controles quedan fuera del MVP conceptual.

## 8. Auditoría mínima

Deben poder reconstruirse los siguientes hechos:

```text
release.created
release.validation_passed
release.validation_failed
release.approved
release.rejected
release.activated
release.retired
release.rolled_back
```

Cada hecho debe identificar como mínimo:

- release afectada;
- momento;
- actor;
- transición de estado;
- motivo cuando corresponda.

La auditoría registra hechos del ciclo de vida. No reemplaza los documentos de diseño ni los ADR.

## 9. Versionado

Para el MVP se utilizará un número entero creciente dentro de cada alcance:

```text
release 1
release 2
release 3
```

No se utilizará versionado semántico inicialmente. Las versiones de los artefactos también serán referencias exactas e inmutables.

Una nueva versión puede originarse por:

- cambio de oferta;
- cambio de producto;
- modificación de voz;
- ajuste del comportamiento conversacional;
- incorporación de ejemplos;
- corrección derivada de feedback;
- cambio del contrato de salida;
- incompatibilidad con una nueva versión del kernel.

## 10. Temas abiertos

Este diseño no decide todavía:

- si el alcance corresponde al agente completo, producto, oferta o campaña;
- qué artefactos serán Markdown y cuáles tendrán una representación estructurada;
- dónde se almacenarán las releases;
- cómo será la interfaz de edición;
- quién puede crear, validar, aprobar o activar;
- si una misma conversación puede migrarse a otra release;
- cómo se compila la release al prompt efectivo de Hermes;
- cuáles serán las pruebas y umbrales definitivos;
- cómo se implementarán experimentos entre variantes;
- cómo el aprendizaje supervisado propondrá nuevos borradores.

Estas decisiones deben resolverse cuando exista información suficiente. No son requisitos del Conversation Release MVP conceptual.

## 11. Criterio de aceptación de este diseño

El diseño se considera aceptado cuando permita afirmar que:

- el comportamiento conversacional publicado puede identificarse exactamente;
- una modificación nunca altera silenciosamente una versión activa;
- toda conversación puede vincularse a la versión que utilizó;
- la publicación y el rollback operan sobre paquetes completos;
- una release no puede evitar las restricciones del kernel;
- la primera activación requiere un Brand Voice aprobado durante el onboarding;
- el feedback sólo puede producir una nueva versión revisable;
- las decisiones todavía desconocidas permanecen explícitamente abiertas.
