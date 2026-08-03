# ADR-0004: Capa de mensajería abstraída para soportar migración Evolution → WABA

- **Estado:** Aceptada; parcialmente supersedida por ADR-0007
- **Fecha:** 2026-07-31

> **Nota de supersesión:** ADR-0007 conserva Chatwoot como frontera de canal,
> pero reemplaza el contrato único propuesto para WABA por dos modalidades:
> texto libre cuando la ventana lo permite y template aprobado cuando resulta
> obligatorio.

## Contexto

El sistema envía mensajes de WhatsApp a través de Evolution API 2.3.7, conectado
como inbox de Chatwoot. Evolution es un puente no oficial a WhatsApp Web que
permite enviar texto libre a cualquier número en cualquier momento, sin
ventana de 24 horas, sin templates pre-aprobados y sin costo por mensaje.

La operación planea migrar a la **API oficial de Meta (WABA — WhatsApp Business
Account)**. Esto introduce restricciones fundamentales:

1. **Template obligatorio para iniciar conversaciones.** No se puede enviar un
   mensaje libre a alguien que no escribió en las últimas 24 horas. El primer
   mensaje debe ser un template pre-aprobado por Meta, con placeholders
   variables.
2. **Categorización de templates.** Para recupero de carrito, el template sería
   de categoría **marketing**, con costo por mensaje entregado.
3. **Ventana de 24 horas.** Si el prospecto escribió en las últimas 24h, se
   puede responder con texto libre dentro de esa ventana. Fuera de ella, se
   requiere template.
4. **Aprobación previa.** Cada template debe ser creado y aprobado por Meta
   antes de poder usarse.

### Simplificación clave

WABA se conectará como **inbox nativo de Chatwoot**, igual que Evolution hoy.
El bridge no necesita hablar con la API de Meta directamente —sigue usando la
API de Chatwoot para crear contactos, conversaciones y enviar mensajes. La
diferencia está en **qué** mensaje se puede enviar, no en **cómo** se envía.

## Decisión

### Abstracción de la capa de mensajería

El bridge no enviará mensajes directamente. Toda operación de mensajería pasa
por una **interfaz de envío** que abstrae el canal subyacente:

```text
Bridge → MessageSender.send_recovery_message(
            phone, buyer_name, product_name, content
         )
    │
    ├── EvolutionMessageSender (hoy)
    │   1. Crea contacto en Chatwoot (phone_number en E.164)
    │   2. Crea conversación en el inbox de WhatsApp
    │   3. Envía texto libre via AgentBot
    │
    └── WABAMessageSender (futuro)
        1. Crea contacto en Chatwoot (igual)
        2. Crea conversación en el inbox de WABA (igual)
        3. Selecciona template aprobado, completa placeholders, envía
```

El bridge le dice "enviá este mensaje de recupero a este número con estos
datos" y la implementación se encarga de adaptarlo al canal.

### Contrato del agente según el canal

**Con Evolution (hoy):** el agente redacta el mensaje libremente. Su propuesta
incluye un `message` de hasta 500 caracteres con el texto exacto que se enviará.

**Con WABA (futuro):** el agente no redacta el mensaje libremente. Su propuesta
incluye los **datos para completar el template** (nombre del lead, producto,
oferta), y el `WABAMessageSender` los mapea al template aprobado. La tarea del
agente es mucho más acotada: seleccionar qué datos incluir, no redactar el
texto completo.

El bridge, según el canal configurado, le indica al agente qué contrato usar.
La skill `cart-abandonment-recovery` tendrá dos modos:

- **Modo libre (Evolution):** el agente produce el mensaje completo.
- **Modo template (WABA):** el agente produce los datos estructurados para el
  template, y el bridge los mapea.

### Creación de contacto y conversación

Independientemente del canal, el flujo de creación es el mismo y ya es
soportado por la API de Chatwoot:

1. `POST /api/v1/accounts/{account_id}/contacts` — crea el contacto con
   `phone_number` en formato E.164, `name`, `email`, asociado al `inbox_id`.
2. `POST /api/v1/accounts/{account_id}/conversations` — crea la conversación
   con `inbox_id` y `contact_id`.
3. `POST /api/v1/accounts/{account_id}/conversations/{id}/messages` — envía
   el mensaje saliente via AgentBot.

Con Evolution, el paso 3 envía texto libre. Con WABA, el paso 3 envía un
mensaje de tipo template (Chatwoot lo soporta nativamente cuando el inbox es
WABA).

### Configuración por canal

El canal se determina por configuración (`MESSAGING_CHANNEL=evolution` o
`MESSAGING_CHANNEL=waba`). El bridge construye el `MessageSender` correspondiente
al iniciar. No hay detección automática —es una decisión de despliegue.

## Consecuencias

- El bridge introduce una nueva abstracción (`MessageSender`) que hoy tiene una
  sola implementación (Evolution), pero que permite agregar WABA sin tocar el
  código que orquesta el recupero.
- La skill del agente necesita soportar dos modos de salida (libre y template).
  El modo se le comunica en el contexto que envía el bridge.
- La migración a WABA requiere: (a) crear y aprobar los templates en Meta,
  (b) configurar el inbox de WABA en Chatwoot, (c) implementar
  `WABAMessageSender`, (d) cambiar `MESSAGING_CHANNEL` a `waba`. No requiere
  cambiar el worker, la resolución de identidad, ni el `SituationReport`.
- Con WABA, el costo por mensaje entregado (marketing) pasa a ser una
  consideración operativa. El sistema debe registrar el intento de envío y su
  resultado para auditoría de costo.
- La ventana de 24h no afecta el primer contacto de recupero (el prospecto no
  escribió primero), pero sí afecta los **mensajes de seguimiento** si el
  prospecto responde y pasan 24h sin actividad —ahí se necesita otro template.
- El `ChatwootClient` existente necesita métodos nuevos:
  `create_contact()`, `create_conversation()`, y un `send_first_message()`
  que no requiera `trigger_message_id`.

## Alternativas descartadas

### Hablar con la API de Meta directamente

Se descarta porque Chatwoot ya soporta WABA como inbox nativo. Hablar con Meta
directamente duplica la gestión de contactos, conversaciones y webhooks que
Chatwoot ya resuelve, y rompe la centralización de Chatwoot como fuente de
verdad conversacional (ADR-0001).

### No abstraer y acoplar a Evolution

Se descarta porque la migración a WABA es planificada. Acoplar el código de
envío a Evolution significa que la migración requiere reescribir el código de
mensajería del bridge, con riesgo de romper el flujo de recupero existente.

### Que el agente genere el template completo

Se descarta porque los templates de Meta tienen un formato rígido (header,
body, buttons) que no se puede generar libremente —deben estar pre-aprobados.
El agente puede completar placeholders, pero no puede inventar la estructura
del template.

## Lo que se implementa ahora

Este ADR documenta la decisión de diseño. La implementación inmediata es:

1. Interfaz `MessageSender` con `EvolutionMessageSender` como implementación.
2. Métodos nuevos en `ChatwootClient`: `create_contact()`,
   `create_conversation()`, `send_first_message()`.
3. El bridge usa `EvolutionMessageSender` para enviar el primer mensaje cuando
   el agente dice `send_first_touch`.

`WABAMessageSender` se implementa cuando se active la migración a WABA.
