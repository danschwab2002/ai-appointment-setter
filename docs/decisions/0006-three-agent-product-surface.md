# ADR-0006: Producto orientado al cliente compuesto por tres agentes

- **Estado:** Aceptada como arquitectura de producto
- **Fecha:** 2026-07-31

## Contexto

El producto utiliza Hermes como plataforma interna para ejecutar agentes
especializados. Hermes también ofrece un profile predeterminado, dashboard,
herramientas generales y capacidades administrativas útiles durante el
desarrollo, pero esa superficie es demasiado amplia y compleja para entregarla
como experiencia del cliente.

El cliente necesita operar resultados comerciales y automatizaciones, no
administrar una plataforma general de agentes. Darle acceso a terminal,
filesystem, proveedores, modelos, skills, plugins, secretos o configuración de
infraestructura aumentaría el riesgo de errores y permitiría cambios fuera del
contrato del producto.

A la vez, una única personalidad de propósito general mezclaría contextos,
credenciales y responsabilidades diferentes:

- conversar con prospectos externos;
- diseñar automatizaciones y seguimientos para el negocio;
- asistir al usuario del producto con reportes, feedback y gestión funcional.

Estas responsabilidades necesitan instrucciones, herramientas, datos y límites
distintos. Un profile de Hermes separa configuración y estado, pero no constituye
por sí solo un sandbox. Las autorizaciones finales deben permanecer en APIs y
servicios determinísticos externos al modelo.

ADR-0001 establece que el agente comercial razona y el bridge conserva la
autorización operativa. ADR-0004 establece el aislamiento por cliente, el
empaquetado reproducible de profiles y una superficie funcional acotada. Esta
decisión completa la definición del producto identificando los agentes que el
cliente recibe.

## Decisión

### 1. El producto inicial tendrá tres agentes orientados al cliente

Cada instalación incluirá exactamente tres roles de agente:

```text
Producto del cliente
├── agente-comercial
├── automation-expert
└── client-copilot
```

Los nombres técnicos podrán normalizarse durante el empaquetado, pero los tres
roles funcionales son parte de la arquitectura aceptada.

Los agentes podrán ejecutarse como profiles separados dentro del stack aislado
del cliente. No se expondrá al cliente el profile `default` ni un profile de
propósito general equivalente.

### 2. `agente-comercial`: conversaciones y calificación

El agente comercial atiende conversaciones con prospectos y clientes externos.
Sus responsabilidades son:

- comprender el contexto conversacional entregado;
- recopilar progresivamente los datos comerciales requeridos;
- responder preguntas dentro de las políticas del negocio;
- calificar al prospecto;
- proponer el siguiente mensaje o derivación.

El agente no publica por sí mismo ni decide la autorización final. Devuelve una
propuesta estructurada. El bridge valida identidad, conversación canónica,
takeover humano, pausa, idempotencia y demás políticas antes de ejecutar una
acción.

El agente comercial no administra secuencias, infraestructura, otros profiles ni
configuración global del producto.

### 3. `automation-expert`: diseño de automatizaciones y seguimientos

El Automation Expert asiste al usuario del producto en la creación y mejora de:

- secuencias de seguimiento;
- condiciones de entrada y salida;
- tiempos entre pasos;
- mensajes y variantes;
- reglas de recuperación;
- criterios de pausa, finalización y escalamiento.

Su salida será una propuesta estructurada que la aplicación pueda validar,
mostrar y versionar. El agente no enviará mensajes ni modificará schedulers de
forma irrestricta.

La ejecución de una automatización corresponderá a servicios determinísticos que
apliquen como mínimo:

- validación de schema y límites;
- política de aprobación definida por el producto;
- idempotencia;
- horarios y frecuencia;
- opt-out y restricciones del canal;
- takeover humano;
- auditoría.

El Automation Expert diseña y explica; la aplicación valida y ejecuta.

### 4. `client-copilot`: comprensión y gestión funcional del producto

El Copilot del cliente es el asistente de mayor amplitud funcional dentro de la
superficie del producto. Ayuda al usuario a comprender y gestionar su operación,
por ejemplo:

- producir reportes diarios y periódicos;
- resumir desempeño y resultados;
- explicar cómo respondió el agente comercial;
- identificar patrones y oportunidades de mejora;
- pedir y organizar feedback del usuario;
- consultar estado funcional de automatizaciones;
- proponer ajustes de negocio;
- iniciar acciones permitidas mediante APIs acotadas.

Los reportes se construirán desde datos canónicos, métricas y eventos de la
aplicación. El Copilot no dependerá de inspeccionar arbitrariamente la memoria
interna de otros profiles.

Su mayor amplitud funcional no implica autoridad técnica irrestricta. No podrá:

- acceder a terminal o filesystem;
- leer o modificar secretos;
- instalar skills, plugins o herramientas;
- cambiar proveedores o modelos sin una capacidad explícita del producto;
- editar código o infraestructura;
- desactivar controles de seguridad;
- acceder a instalaciones de otros clientes;
- modificar directamente los profiles de producción.

Las acciones que el Copilot pueda ejecutar estarán representadas por APIs de
negocio explícitas, validadas y auditables.

### 5. Los agentes no se organizarán mediante una jerarquía implícita

No se otorgará autoridad porque un agente sea considerado «superior» a otro.
Cada profile tendrá capacidades declaradas según su función.

```text
Agente comercial
└── propone respuestas conversacionales

Automation Expert
└── propone automatizaciones estructuradas

Client Copilot
└── consulta y opera capacidades funcionales autorizadas

Servicios determinísticos
└── validan, autorizan, persisten y ejecutan
```

La coordinación se realizará mediante contratos estructurados, eventos y APIs;
no mediante acceso compartido irrestricto a memoria, credenciales o herramientas.

### 6. El cliente interactuará con el producto, no con Hermes como plataforma

Los canales concretos podrán evolucionar, pero la superficie deberá ser curada y
orientada al negocio. Puede incluir Chatwoot y una futura interfaz propia.

El cliente no recibirá acceso directo a:

- dashboard técnico completo de Hermes;
- profile `default`;
- CLI o terminal;
- configuración de gateways;
- gestión general de herramientas;
- credenciales de infraestructura;
- filesystem de los profiles.

Hermes permanece como componente interno del runtime. La aplicación controla qué
agente se invoca, qué contexto recibe y qué acciones puede solicitar.

### 7. El mantenimiento técnico del operador queda fuera del producto

Un eventual agente privado utilizado por el desarrollador u operador para
mantener instalaciones no constituye un cuarto agente del producto y no será
accesible al cliente.

Esta decisión no define su arquitectura, permisos ni forma de acceso. El diseño
de un maintainer central, acceso temporal a infraestructura o automatización de
soporte queda expresamente diferido porque no es una prioridad del producto
actual.

El funcionamiento de los tres agentes del cliente no dependerá de que ese futuro
plano de mantenimiento esté disponible.

### 8. El automantenimiento no amplía implícitamente los permisos del Copilot

El Copilot podrá observar estado, informar problemas y recomendar acciones dentro
de las capacidades que la aplicación exponga. Esta decisión no le concede
permisos generales para reparar código, desplegar versiones, migrar datos o
modificar infraestructura.

Cualquier automatización de mantenimiento deberá clasificarse posteriormente en:

- observación y alerta;
- acción segura y reversible;
- acción sensible que requiere aprobación técnica.

Su implementación requerirá contratos y autorizaciones explícitos; no se deriva
automáticamente del rol de Copilot.

## Consecuencias

### Positivas

- La experiencia del cliente queda enfocada en funciones comerciales concretas.
- Cada agente puede tener instrucciones, memoria, herramientas y credenciales
  mínimas para su rol.
- Se evita entregar una plataforma general demasiado poderosa o compleja.
- Las autorizaciones permanecen fuera del modelo y pueden auditarse.
- El agente comercial puede evolucionar sin mezclar contexto de administración.
- Automatizaciones y reportes pueden probarse mediante contratos estructurados.
- El plano técnico del operador puede diseñarse posteriormente sin formar parte
  de la superficie comercial.

### Costos

- Habrá que empaquetar, versionar y actualizar tres profiles por instalación.
- La aplicación necesitará routing y contratos diferentes por agente.
- Deberán definirse fuentes canónicas de métricas y configuración.
- La coordinación requerirá APIs y eventos, no sólo prompts.
- Será necesario probar tanto el comportamiento individual como las interacciones
  entre agentes y servicios determinísticos.

## Alternativas descartadas

### Entregar un único agente general al cliente

Se descarta porque mezcla conversación externa, diseño de automatizaciones y
administración funcional. También dificulta aplicar permisos mínimos y aislar
contextos.

### Exponer el profile `default` o el dashboard completo

Se descarta porque ofrece capacidades técnicas no necesarias para el uso del
producto y aumenta la superficie de error y ataque.

### Permitir que los agentes ejecuten directamente cualquier propuesta

Se descarta porque los modelos no deben conservar autorización final sobre
mensajería, scheduling, datos o configuración. Las acciones pasan por controles
determinísticos.

### Incluir un maintainer técnico con acceso total en cada instalación

Se descarta como parte del producto actual. El soporte técnico del operador es
una preocupación distinta y su diseño queda diferido.

### Considerar al Copilot un superusuario implícito

Se descarta porque una jerarquía conceptual no define una frontera de seguridad.
El Copilot tendrá mayor amplitud funcional, pero sólo mediante capacidades
explícitas de negocio.

## Temas todavía abiertos

Esta decisión no define aún:

- la interfaz final desde la cual el cliente invocará cada agente;
- si el routing será explícito, contextual o combinado;
- los schemas definitivos del Automation Expert y del Copilot;
- la política exacta de aprobación de secuencias;
- el mecanismo de reportes diarios y solicitud de feedback;
- qué acciones funcionales concretas podrá ejecutar el Copilot;
- la estrategia técnica de coordinación entre los tres profiles;
- la arquitectura de un eventual maintainer privado del operador.

Estos temas deberán resolverse mediante diseño y decisiones posteriores sin
alterar la composición de tres agentes establecida aquí.

## Estado de implementación

El `agente-comercial` y su flujo E2E mediante Chatwoot, bridge y AgentBot están
implementados y validados.

`automation-expert` y `client-copilot` están definidos como componentes del
producto, pero todavía no están implementados ni empaquetados como profiles
reproducibles.

La decisión describe la arquitectura de producto aceptada; no declara terminadas
las capacidades futuras de esos dos agentes.
