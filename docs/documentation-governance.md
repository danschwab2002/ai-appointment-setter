# Política de documentación del proyecto

- **Estado:** Aceptada
- **Propósito:** Mantener documentación suficiente, vigente y navegable para que personas y agentes de IA comprendan la arquitectura, las decisiones, los contratos y la operación de la aplicación.

## 1. Principio general

La documentación se crea desde el inicio del diseño, sin esperar a que toda la infraestructura esté implementada. Cada documento debe distinguir claramente entre:

- una propuesta;
- una decisión aceptada;
- un contrato implementado;
- el estado actual del sistema;
- evidencia operativa.

No se documentan hipótesis como si fueran realidad ni se utiliza un diario cronológico como fuente de verdad arquitectónica.

## 2. Tipos de documento

### `docs/design/`

Contiene propuestas, exploraciones y diseños detallados en evolución.

Usar cuando:

- todavía existen decisiones abiertas;
- el usuario debe revisar una propuesta;
- se necesita desarrollar el razonamiento antes de decidir;
- el diseño aún no está implementado.

Todo documento debe declarar su estado, alcance y temas abiertos.

### `docs/decisions/`

Contiene ADR para decisiones arquitectónicas importantes y aceptadas.

Un ADR debe registrar:

- contexto;
- decisión;
- consecuencias;
- alternativas relevantes;
- estado de implementación;
- supersesiones cuando correspondan.

No crear un ADR para cada conversación ni para una propuesta todavía no aceptada. Si una decisión cambia materialmente, crear una nueva decisión que superseda total o parcialmente la anterior en lugar de reescribir la historia.

### `docs/architecture.md`

Describe la composición y el funcionamiento vigentes del sistema.

Debe incluir o enlazar:

- componentes existentes;
- flujos principales;
- fuentes de verdad;
- fronteras de responsabilidad;
- agentes y servicios;
- ADR vigentes;
- contratos relevantes.

No debe presentar trabajo futuro como implementado.

### `docs/contracts/`

Define interfaces exactas y versionadas:

- entradas y salidas;
- schemas;
- estados;
- invariantes;
- reason codes;
- errores;
- reglas de compatibilidad.

Los contratos se crean o actualizan cuando existe una representación técnica suficientemente definida.

### `docs/operations/`

Contiene procedimientos y evidencia operativa:

- despliegues;
- migraciones;
- rollback;
- verificaciones HTTP;
- pruebas E2E;
- incidentes;
- reconciliaciones;
- runbooks.

La evidencia operativa demuestra qué ocurrió; no reemplaza la arquitectura ni los contratos.

### `AGENTS.md`

Contiene instrucciones breves y obligatorias para trabajar en el repositorio:

- convenciones;
- límites de seguridad;
- comandos de verificación;
- política documental;
- ubicación de las fuentes autoritativas.

No debe duplicar el contenido detallado de los documentos enlazados.

## 3. Flujo documental diario

```text
Idea o necesidad
        ↓
Diseño propuesto en docs/design/
        ↓
Revisión y aceptación
        ↓
ADR cuando la decisión es arquitectónica y durable
        ↓
Implementación
        ↓
Contrato técnico y actualización de architecture.md
        ↓
Verificación y evidencia en docs/operations/
```

No todas las tareas requieren todos los artefactos. El agente debe crear o actualizar sólo los documentos que correspondan al alcance real del cambio.

## 4. Estados documentales

Usar estados explícitos y breves, por ejemplo:

- `Propuesta para revisión`;
- `En revisión`;
- `Aceptada`;
- `Base de diseño aprobada`;
- `Parcialmente implementada`;
- `Implementada`;
- `Supersedida parcialmente`;
- `Supersedida`.

Cuando un documento no describe el estado implementado, debe decirlo expresamente.

## 5. Comportamiento proactivo del agente

Durante cada tarea, el agente debe evaluar si el trabajo:

- introduce una propuesta que necesita un documento de diseño;
- confirma una decisión arquitectónica que necesita un ADR;
- cambia la realidad del sistema y requiere actualizar `docs/architecture.md`;
- cambia una interfaz y requiere actualizar un contrato;
- produce evidencia operativa que debe registrarse;
- supersede documentación existente.

Si corresponde, debe actualizar la documentación dentro de la misma tarea, sin esperar una solicitud adicional, siempre que el alcance ya haya sido aceptado y la edición no interfiera con trabajo concurrente.

El agente no debe:

- convertir una conversación exploratoria en una decisión aceptada;
- crear ADR antes de que la decisión esté acordada;
- declarar implementado algo que sólo fue diseñado;
- crear logs narrativos para reemplazar documentos autoritativos;
- duplicar la misma regla en varios lugares;
- modificar un documento concurrentemente editado sin revisar el estado del repositorio.

## 6. Jerarquía documental

Ante contradicciones, utilizar esta interpretación:

1. código, migraciones y configuración desplegada describen el comportamiento ejecutable real;
2. contratos vigentes definen las interfaces prometidas;
3. `docs/architecture.md` resume el estado arquitectónico actual;
4. ADR explican por qué se tomaron decisiones;
5. documentos de diseño contienen detalle, propuestas y temas abiertos;
6. documentos operativos aportan evidencia de ejecuciones concretas;
7. sesiones, handoffs, issues y Git aportan historia, pero no sustituyen las fuentes anteriores.

Una contradicción entre implementación y documentación debe corregirse o registrarse explícitamente; no debe ocultarse el desacuerdo eligiendo silenciosamente una fuente.

## 7. Navegación mínima para agentes

Un agente que necesita comprender el proyecto debe comenzar por:

```text
AGENTS.md
   ↓
docs/architecture.md
   ├── docs/decisions/
   ├── docs/contracts/
   ├── docs/design/
   └── docs/operations/
```

Debe leer sólo los documentos relevantes para su tarea y seguir enlaces hacia el detalle necesario.

## 8. Criterio de finalización

Antes de cerrar una tarea que cambie diseño, arquitectura, contratos u operación, verificar:

- [ ] Cada cambio documental está clasificado en el tipo correcto.
- [ ] El estado de cada documento coincide con la realidad.
- [ ] Las decisiones aceptadas y las hipótesis están diferenciadas.
- [ ] Los contratos afectados fueron actualizados.
- [ ] `docs/architecture.md` sigue describiendo el sistema vigente.
- [ ] Las supersesiones están declaradas.
- [ ] La evidencia operativa relevante quedó registrada.
- [ ] No se crearon diarios o duplicaciones innecesarias.
- [ ] No se tocaron archivos de trabajo concurrente fuera del alcance.
