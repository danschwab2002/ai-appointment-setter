# ADR-0001: Profile comercial como motor de razonamiento aislado

- **Estado:** Aceptada
- **Fecha:** 2026-07-27

## Contexto

El despliegue actual es single-tenant y contiene WhatsApp, Evolution API 2.3.7, Chatwoot 4.13.0 y un bridge FastAPI desplegado en Easypanel. Ya se verificó el flujo:

```text
WhatsApp -> Evolution API -> Chatwoot -> bridge seguro
```

El bridge autentica, deduplica y filtra los eventos antes de cualquier futura invocación a Hermes. El siguiente objetivo es incorporar razonamiento comercial sin mezclar mensajes externos con el copilot del propietario.

Ya existe un modelo de datos MVP en Supabase Cloud. Su responsabilidad principal es soportar las automatizaciones y secuencias que se incorporarán al sistema, especialmente los seguimientos comerciales. El detalle físico de ese esquema —tablas, columnas y relaciones— todavía no está documentado en este repositorio.

## Decisión

Cada despliegue tendrá un **profile comercial de Hermes** compartido por múltiples conversaciones y aislado del futuro profile copilot. Cada invocación estará limitada a una única conversación, identificada mediante referencias opacas, y recibirá un contexto acotado construido desde fuentes externas. La memoria global y las sesiones de Hermes no serán necesarias para reconstruir el estado comercial de un prospecto.

El profile comercial será únicamente el motor de razonamiento. El bridge conservará las responsabilidades determinísticas: autorización, identidad de la conversación, orden, idempotencia, intervención humana y ejecución de respuestas mediante Chatwoot.

Chatwoot será la fuente de verdad para los mensajes, los contactos, las conversaciones, las asignaciones, las etiquetas y la intervención de agentes humanos.

Supabase será la fuente de verdad para el estado operativo de las automatizaciones y secuencias, principalmente las secuencias de seguimiento. Su modelo existente complementará a Chatwoot sin duplicar la propiedad canónica del historial conversacional ni convertir la memoria de Hermes en un CRM.

El profile comercial tendrá credenciales mínimas y herramientas restringidas. No se creará un profile por prospecto ni un profile separado de seguimientos hasta que exista una necesidad real de razonamiento no determinístico.

## Consecuencias

- Se aíslan personalidad, sesiones, memoria y permisos comerciales.
- Los mensajes de prospectos no comparten contexto ni herramientas con el copilot.
- Compartir el profile comercial no implica compartir contexto mutable entre prospectos.
- El bridge debe construir para cada invocación el contexto necesario de una sola conversación desde Chatwoot y, cuando corresponda, desde Supabase.
- El bridge debe definir un contrato estructurado de entrada y salida para Hermes.
- La continuidad por conversación y los seguimientos deben persistirse fuera de la memoria global del profile.
- La integración con Supabase requerirá documentar o referenciar su esquema físico antes de implementar consultas y escrituras.
- Un profile no reemplaza un sandbox: seguirá siendo necesario restringir herramientas y permisos del proceso.
