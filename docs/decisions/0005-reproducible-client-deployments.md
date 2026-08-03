# ADR-0005: Empaquetado reproducible y aislamiento por cliente

- **Estado:** Aceptada como arquitectura objetivo
- **Fecha:** 2026-07-31

## Contexto

El producto utiliza Hermes tanto como plataforma de agentes como motor de
razonamiento de agentes comerciales especializados. Durante el desarrollo, el
profile predeterminado de Hermes funciona como herramienta técnica para diseñar,
programar, probar y operar el sistema. El producto entregado a un cliente no debe
exponer esa superficie general ni sus capacidades administrativas.

La solución combina componentes propios y de terceros:

- `appointment-bridge`, que aplica autenticación, autorización, idempotencia,
  takeover humano y publicación por AgentBot;
- profiles especializados de Hermes, que generan propuestas estructuradas;
- Chatwoot, como fuente canónica de conversaciones y estado operativo;
- Evolution API, como transporte hacia y desde WhatsApp;
- bases de datos, almacenamiento persistente y futuras integraciones.

El profile comercial actualmente operativo contiene configuración útil para el
producto, pero también estado runtime bajo `/opt/data`: secretos, sesiones,
memoria, logs y archivos de supervisión. Copiar ese directorio completo no sería
un mecanismo seguro ni reproducible para instalar nuevos clientes.

También es necesario distinguir entre un profile y un sandbox. Un profile separa
configuración y estado de Hermes, pero no limita por sí solo el acceso al
filesystem, red, herramientas o credenciales. Las capacidades del agente deben
restringirse mediante configuración, credenciales mínimas, aislamiento de
contenedor y controles determinísticos externos.

## Decisión

### 1. Separar Hermes de desarrollo de Hermes de producto

El entorno usado para desarrollar y administrar la solución no será una
dependencia del runtime de los clientes. Cada cliente recibirá una instalación
aislada que contenga sólo los profiles y servicios necesarios para su caso de
uso.

El profile `default`, las herramientas generales de desarrollo y el dashboard
técnico completo no formarán parte de la superficie entregada al cliente. Si se
mantiene un dashboard para operación interna, permanecerá autenticado, no
expuesto públicamente y reservado al operador técnico.

### 2. Adoptar un modelo single-tenant administrado

Para las primeras implementaciones, cada cliente tendrá su propio stack y su
propia frontera de confianza:

```text
Cliente
├── appointment-bridge
├── Hermes
│   └── profiles especializados del cliente
├── Chatwoot
├── Evolution API
├── datos y volúmenes persistentes
└── secretos y observabilidad
```

No se compartirán entre empresas diferentes:

- `/opt/data` de Hermes;
- sesiones o memoria;
- tokens y API keys;
- AgentBots e inboxes;
- bases de datos operativas;
- contenedores gateway.

Un despliegue multi-tenant sólo podrá introducirse mediante una decisión
posterior que defina explícitamente aislamiento, autorización, cuotas,
observabilidad y migración.

### 3. Tratar los agentes especializados como código

La definición reproducible de cada agente se versionará como una plantilla
sanitizada. Podrá incluir:

- `SOUL.md`;
- configuración no sensible;
- skills, plugins y hooks propios;
- schemas de entrada y salida;
- reglas de comportamiento;
- casos de prueba y evaluaciones.

No se versionará una copia completa del profile runtime. Quedarán excluidos:

- `.env` y credenciales;
- sesiones y memoria operativa;
- logs, capturas y payloads;
- `state.db`, estado del gateway y locks;
- directorios home de herramientas;
- PII y datos conversacionales.

Los recuerdos que representen reglas estables del producto deberán convertirse
en configuración, documentación o skills versionadas, en lugar de depender de
memoria runtime no reproducible.

### 4. Mantener inicialmente un monorepo del producto

Mientras el sistema conserve su escala actual, código, agentes, infraestructura,
provisioning, pruebas y documentación podrán convivir en el mismo repositorio.
Una estructura objetivo posible es:

```text
apps/
  appointment-bridge/
agents/
  commercial/
infrastructure/
provision/
docs/
tests/
```

La adopción de esta estructura será gradual; esta decisión no obliga a una
reorganización inmediata del repositorio.

Mantener el bridge y las plantillas de agentes en una misma versión permite
probar sus contratos conjuntamente y saber qué definición del agente corresponde
a cada release del producto.

### 5. Componer varias imágenes en lugar de crear una imagen monolítica

El producto se desplegará como un conjunto de servicios coordinados, no mediante
un único Dockerfile:

- el bridge tendrá su propia imagen;
- Hermes usará una versión fijada de la imagen oficial o una imagen derivada
  mínima cuando existan plugins o dependencias propias;
- Chatwoot y Evolution usarán imágenes de proveedor con versiones fijadas;
- una definición de infraestructura conectará redes, volúmenes, puertos,
  políticas de reinicio y health checks.

Las imágenes serán inmutables. Los secretos y el estado mutable se inyectarán o
montarán durante el despliegue; no se incorporarán a capas Docker.

Se evitará depender de tags móviles como `latest` en instalaciones de clientes.
Cada release deberá fijar versiones o digests conocidos para permitir rollback y
reproducción.

### 6. Separar configuración base, configuración del cliente, secretos y estado

Cada instalación utilizará cuatro capas:

1. **Producto versionado:** código, agentes base, schemas, tests e
   infraestructura.
2. **Configuración del cliente:** criterios comerciales, branding, precios,
   horarios y reglas no sensibles.
3. **Secretos:** tokens, API keys, webhook secrets, JID autorizado y
   credenciales de servicios.
4. **Estado runtime:** conversaciones, sesiones, memoria, logs, bases de datos y
   backups.

Los secretos permanecerán en EasyPanel, Docker Secrets o un gestor equivalente.
La configuración específica podrá administrarse en un repositorio privado o en
un sistema de configuración, según su sensibilidad.

### 7. Versionar el provisioning de recursos externos

Los webhooks, AgentBots, etiquetas, inboxes y callbacks viven en sistemas
externos y no pueden materializarse únicamente copiando archivos. Su creación y
validación se implementarán mediante procesos idempotentes y versionados.

El provisioning deberá:

- consultar el estado existente antes de crear recursos;
- evitar duplicados;
- persistir identificadores y secretos fuera de Git;
- verificar permisos y conectividad;
- producir evidencia sanitizada;
- soportar reejecución y rollback cuando corresponda.

### 8. Exponer al cliente sólo una superficie funcional acotada

El cliente operará principalmente desde Chatwoot y, si se desarrolla, desde un
panel de negocio con opciones curadas. No recibirá acceso directo a:

- terminal o filesystem de Hermes;
- instalación de skills o plugins;
- selección irrestricta de herramientas y modelos;
- credenciales internas;
- configuración de red o proveedores;
- profile técnico predeterminado.

Cada agente tendrá herramientas mínimas y credenciales limitadas a su función.
Hermes generará propuestas; el bridge continuará tomando las decisiones
operativas determinísticas y publicando mediante las integraciones autorizadas.

## Consecuencias

### Positivas

- Una instalación puede reconstruirse sin copiar estado vivo de otra.
- Los cambios de comportamiento quedan revisables, testeables y auditables.
- Los clientes quedan aislados entre sí y del entorno de desarrollo.
- Las credenciales y la PII no contaminan Git ni imágenes.
- Bridge y agentes pueden evolucionar como una única versión del producto.
- Las versiones fijadas permiten reproducir despliegues y ejecutar rollback.
- El cliente recibe una interfaz adecuada para su negocio, no una plataforma de
  agentes de propósito general.

### Costos

- Será necesario crear un mecanismo de bootstrap y actualización de profiles.
- El provisioning deberá automatizar recursos que hoy se configuran manualmente.
- Cada stack requerirá backups, monitoreo, upgrades y gestión de secretos.
- Las personalizaciones deberán clasificarse entre producto base, configuración
  por cliente y código específico.
- Mantener instalaciones single-tenant consume más infraestructura que compartir
  un runtime multi-tenant.

## Alternativas descartadas

### Entregar el profile predeterminado y el dashboard completo

Se descarta porque expone capacidades técnicas y administrativas que no son
necesarias para operar el producto y aumenta el riesgo de cambios inseguros.

### Ejecutar clientes distintos como profiles dentro del mismo Hermes

Aunque Hermes soporta múltiples profiles, se descarta inicialmente entre
empresas diferentes porque un profile no constituye por sí solo una frontera de
sandboxing y todos compartirían infraestructura y radio de impacto.

### Copiar `/opt/data` para crear cada instalación

Se descarta porque mezcla definición del agente con secretos, sesiones, memoria,
logs y estado no reproducible.

### Empaquetar todo en un único contenedor

Se descarta porque acopla ciclos de vida, upgrades, persistencia y permisos de
componentes que deben poder evolucionar y recuperarse de forma independiente.

### Configurar cada cliente exclusivamente a mano

Se descarta como estado objetivo porque no produce instalaciones repetibles,
dificulta auditoría y aumenta la probabilidad de diferencias y errores entre
clientes.

## Estado de implementación

La decisión establece la arquitectura objetivo; no declara completada la
productización.

Actualmente están versionados el bridge, sus pruebas y la documentación
arquitectónica. El profile comercial validado continúa viviendo principalmente
como estado operativo en `/opt/data/profiles/agente-comercial`.

El siguiente hito asociado será extraer una plantilla sanitizada del agente y
definir un bootstrap verificable que pueda instalarla en una nueva instancia de
Hermes sin copiar secretos, sesiones, memoria ni PII.
