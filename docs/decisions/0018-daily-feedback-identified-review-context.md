# ADR-0018: La revision diaria muestra la identidad del lead y el contexto completo de la conversacion

- **Estado:** Aceptada
- **Fecha:** 2026-09-26
- **Decide:** Dan Schwab (operador del producto), en sesion con Andy
- **Reemplaza:** la minimizacion de `daily-feedback-review-package-v1` para la ruta productiva (`docs/contracts/daily-feedback-review-package-v1.md`, seccion 4)

## Contexto

El primer informe diario real (26/09/2026, 21 conversaciones) llego a los cuatro
revisores anonimizado: sin nombre ni telefono del lead, sin link a Chatwoot, con
el enlace de pago reemplazado por `[ENLACE]`, sin los mensajes del equipo, sin la
nota de derivacion del bridge y con la plantilla de reactivacion indistinguible
de una respuesta normal del agente. Esa minimizacion era deliberada: el paquete
V1 nacio como superficie local de cuarentena y se llevo a produccion con la misma
sanitizacion.

El revisor no puede juzgar una conversacion sin saber quien es el lead, cuando
escribio, que le mando el sistema por su cuenta (reactivacion, primer toque,
seguimiento), que hizo el equipo humano y por que el agente decidio derivar.

## Decision

1. La ruta productiva usa un paquete identificado (`daily-feedback-review-package-v2`):
   nombre, telefono y mail del lead, link directo a la conversacion en Chatwoot,
   fecha del primer contacto, estado y etiquetas de la conversacion.
2. El hilo muestra todo lo que paso en la ventana: mensajes del lead, respuestas
   del agente, mensajes del equipo, notas privadas de derivacion, actividades
   (pausa, reanudacion, asignacion, resolucion) y envios fallidos, cada uno con
   hora local, estado de entrega y tipo (respuesta, plantilla de reactivacion,
   primer toque, seguimiento, link de pago).
3. El bridge estampa la decision y el reason code del agente en el mensaje que
   publica (`content_attributes.appointment_setter_decision` y
   `appointment_setter_reason_code`), para que la revision diga por que el
   agente contesto lo que contesto.
4. La revision suma lo que Supabase sabe de la conversacion: derivaciones,
   reactivaciones, reanudaciones, opt-outs, links de pago con su atribucion y
   si hubo compra, y decisiones de revision anteriores sobre la misma
   conversacion.
5. Entran tambien las conversaciones donde el agente no contesto: son las que
   mas hay que ver.
6. El texto de los mensajes sale tal cual, salvo secretos (tokens, bearer),
   esquemas peligrosos (`javascript:`) y caracteres de control.

## Por que es aceptable

- La pagina esta detras de Slack OpenID Connect y solo la abren los cuatro
  revisores del lote, que ya tienen acceso a Chatwoot con todos esos datos.
- Slack sigue recibiendo solo el aviso `REV-001` con el link; ningun dato del
  lead viaja por Slack.
- La retencion (72 h), la purga fisica y los tombstones no cambian.
- El HTML local de cuarentena (`build_daily_feedback_review.py`) conserva V1
  minimizado; no forma parte de la ruta productiva.

## Consecuencias

- Migracion `20260927000100_daily_feedback_review_context_v2.sql`: mensajes v2,
  columna `context`, RPC `get_daily_feedback_conversation_context_v1`.
- El scheduler del servicio `daily-feedback` consulta esa RPC antes de confirmar
  el lote; si falla, la recoleccion falla y se reintenta (sin informe a medias).
- Orden de despliegue propuesto: migracion primero, despues el servicio
  `daily-feedback`, despues el bridge (para el estampado de decision). El commit
  RPC se escribio para aceptar items v1 y v2, de modo que el orden no importe;
  la evidencia del primer lote real queda para `docs/operations/` tras el E2E.
- Contrato: `docs/contracts/daily-feedback-review-package-v2.md`.
