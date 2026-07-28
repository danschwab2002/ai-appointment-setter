# ADR-0002: Detección y señalización de intervención humana

- **Estado:** Aceptada
- **Fecha:** 2026-07-28

## Contexto

Chatwoot es la fuente canónica de conversaciones, mensajes, asignaciones,
etiquetas e intervención humana según ADR-0001. El bridge necesita detener
automatizaciones cuando una persona responde, sin depender de estado duplicado
ni invocar Hermes para tomar esa decisión.

Una prueba real sobre Chatwoot 4.13.0 mostró que un mensaje manual:

- aparece en la API con `message_type = 1`;
- tiene actor `user`;
- no asigna necesariamente la conversación;
- no cambia necesariamente las etiquetas;
- puede dejar la conversación `open` y `can_reply = true`;
- no activa mute ni snooze.

Por lo tanto, `assignee`, estado, etiquetas preexistentes, `unread_count` y timestamps de lectura no permiten detectar de forma confiable una respuesta humana por sí solos.

Chatwoot también ofrece AgentBot como identidad nativa para automatizaciones.
Una prueba real demostró que un AgentBot de tipo `webhook`, aun sin estar
vinculado al inbox, puede enviar mediante la API a una conversación existente.
El mensaje llegó al WhatsApp autorizado y quedó registrado con:

- `message_type = 1`;
- `sender.type = "agent_bot"`;
- el ID estable del AgentBot configurado.

Mantener el AgentBot desvinculado evita que Chatwoot cambie conversaciones
nuevas a `pending` y evita introducir un segundo webhook para los mismos eventos.

## Decisión

La automatización enviará mensajes mediante un **AgentBot nativo de Chatwoot**,
exclusivo del appointment setter. El bridge usará el access token del AgentBot y
validará explícitamente su ID; no inferirá su identidad por nombre ni orden de
creación.

El AgentBot permanecerá desvinculado del inbox mientras el webhook general de
cuenta sea la vía canónica de entrada. No se configurará un webhook adicional de
AgentBot salvo una decisión posterior que incluya deduplicación entre ambas
fuentes y una migración deliberada del flujo.

El bridge clasificará como intervención humana todo mensaje que cumpla simultáneamente estas condiciones:

1. es público;
2. es saliente;
3. el actor es un usuario de Chatwoot;

El bridge clasificará como mensaje propio de automatización únicamente un
mensaje público saliente cuyo actor sea `agent_bot` y cuyo `sender.id` coincida
con el ID configurado. Cualquier actor saliente desconocido se tratará de forma
cerrada: no habilitará ni continuará automatizaciones hasta ser reconciliado.

Al detectar una intervención humana, el bridge realizará acciones determinísticas, sin invocar Hermes:

1. marcará la automatización o caso correspondiente como pausado en Supabase;
2. cancelará o invalidará las acciones programadas pendientes de esa conversación;
3. añadirá la etiqueta `automation_paused` a la conversación en Chatwoot.

La etiqueta será la señal canónica visible para operadores. El estado de pausa en Supabase será su proyección operativa para impedir ejecuciones y auditar la causa.

Antes de ejecutar cualquier acción programada, el bridge deberá consultar Chatwoot y cancelar la ejecución si ocurre cualquiera de estas condiciones:

- la conversación tiene la etiqueta `automation_paused`;
- apareció desde el punto de control un mensaje humano según la regla anterior;
- apareció un mensaje entrante posterior al mensaje esperado;
- la conversación ya no permite responder;
- Chatwoot no puede consultarse o la respuesta no puede validarse.

Los mensajes enviados por el AgentBot autorizado no activarán la pausa. Los
eventos salientes se procesarán sólo para control determinístico, idempotencia y
auditoría; no se enviarán a Hermes como instrucciones.

Quitar `automation_paused` no resucitará acciones canceladas ni reanudará automáticamente una secuencia anterior. Una futura reanudación deberá ser una operación explícita que vuelva a validar el estado actual de Chatwoot y cree nuevas acciones.

## Consecuencias

- La detección no depende de que un agente se asigne la conversación antes de responder.
- Las respuestas automáticas y humanas se distinguen por tipos de actor nativos
  (`agent_bot` frente a `user`) y por un ID técnico estable.
- Los operadores ven la pausa directamente en Chatwoot.
- Supabase puede detener workers sin duplicar el historial conversacional.
- El sistema falla de forma cerrada cuando no puede verificar el estado canónico.
- El token del AgentBot debe permanecer sólo en el entorno secreto del bridge.
- No se consume una identidad humana ni se atribuyen mensajes automáticos a un
  operador.
- Mantener el bot desvinculado preserva el estado `open` actual y evita webhooks
  duplicados.
- El bridge deberá aceptar y clasificar ciertos eventos salientes aunque continúe ignorándolos para razonamiento comercial.
- Habrá que implementar idempotencia para añadir la etiqueta y cancelar acciones ante reintentos del mismo webhook.

## Alternativas descartadas

### Usar únicamente `assignee`

Se descarta porque la prueba real mostró una respuesta humana con assignee nulo.

### Exigir una etiqueta manual antes de responder

Se descarta como única protección porque depende de que cada operador recuerde aplicarla y deja una ventana para respuestas automáticas concurrentes.

### Detectar cualquier mensaje saliente como humano

Se descarta porque los mensajes del AgentBot también son salientes. El tipo de
actor y el ID del AgentBot permiten diferenciarlos.

### Crear un usuario humano exclusivo para la automatización

Se reemplaza por AgentBot porque Chatwoot ofrece una identidad nativa que queda
registrada como `agent_bot`, puede enviar con su propio token y no confunde la
auditoría con agentes humanos.

### Vincular el AgentBot al inbox desde el inicio

Se descarta en la arquitectura actual porque introduce otro origen de webhooks y
puede cambiar conversaciones nuevas a `pending`. La prueba demostró que no es
necesario vincularlo para enviar a una conversación existente.
