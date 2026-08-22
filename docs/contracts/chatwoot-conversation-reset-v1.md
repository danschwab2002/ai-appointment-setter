# Contrato de reset conversacional Chatwoot V1

- **Estado:** Implementado localmente; pendiente de publicación, despliegue y E2E real
- **Versión:** 1
- **Ámbito:** conversación inbound del único JID autorizado del piloto

## Propósito

Permitir que el operador inicie una prueba conversacional nueva dentro de la
misma conversación de Chatwoot sin borrar el registro operativo de Chatwoot ni
mutar conocimiento o memoria global del profile Hermes.

El bridge reconstruye cada turno de Hermes desde el historial canónico de
Chatwoot. Por eso el reset crea un límite de contexto en ese historial; no
elimina físicamente mensajes.

## Comando

El comando es el contenido público entrante exacto y case-sensitive:

```text
/nuevo
```

No se normalizan mayúsculas ni espacios para reconocerlo. Por ejemplo,
`/Nuevo`, ` /nuevo` y `/nuevo ` son mensajes ordinarios y no resetean contexto.

El comando sólo es admisible después de las guardas existentes de autenticidad,
anti-replay, dirección, privacidad, inbox y JID autorizado.

## Ejecución

```text
Chatwoot admite /nuevo durablemente
→ responde HTTP 202
→ worker lo procesa sin esperar el debounce conversacional
→ no invoca Hermes
→ reautoriza el envío mediante el adapter AgentBot existente
→ responde exactamente: Memoria eliminada.
```

La confirmación respeta el kill switch `CHATWOOT_AUTOMATED_REPLIES_ENABLED` y
las guardas finales existentes del adapter. Si el envío queda bloqueado, el
bridge no elude el bloqueo. Una respuesta incierta permanece retryable y reutiliza
la idempotencia existente de conversación, trigger y delivery.

## Límite de historial

Para todo turno posterior, el bridge:

1. obtiene y valida el historial canónico hasta el trigger actual;
2. comprueba primero que contiene todos los IDs del batch actual;
3. conserva la ventana conversacional acotada vigente;
4. encuentra el último mensaje de prospecto cuyo contenido raw fue exactamente
   `/nuevo`;
5. excluye ese mensaje y todo lo anterior;
6. excluye también la confirmación AgentBot exacta e inmediatamente posterior;
7. entrega a Hermes sólo los mensajes nuevos restantes.

El marcador es durable porque permanece en Chatwoot. No depende de memoria de
proceso ni de una sesión persistente del modelo.

## Invariantes

- `/nuevo` nunca se entrega a Hermes.
- La confirmación nunca es redactada por un modelo.
- Un reset no agrupa mensajes dentro de la ventana de debounce.
- El batch posterior mantiene sus propias validaciones canónicas.
- Variantes no literales no crean un límite de contexto.
- Replays no deben producir más de una confirmación visible.
- El reset no habilita una conversación pausada ni evade opt-out, ownership,
  allowlist o autorizaciones de salida.

## Fuera de alcance

El reset V1 no elimina ni modifica:

- mensajes almacenados en Chatwoot;
- memoria durable o facts globales del profile Hermes;
- contactos, conversaciones, etiquetas, asignaciones o notas;
- opt-outs, handoffs, casos comerciales, secuencias o acciones programadas;
- capturas y evidencia durable ya admitida por el bridge.

`Memoria eliminada.` significa únicamente que los mensajes anteriores no se
volverán a incluir en el contexto conversacional de Hermes.

## Configuración de la prueba rápida

El debounce ordinario ya es configurable mediante:

```text
CHATWOOT_INBOUND_DEBOUNCE_SECONDS=1
```

Ese valor se aplicará sólo al despliegue controlado de prueba. El valor por
defecto del producto permanece en 30 segundos hasta una decisión distinta. La
activación requiere mutar la configuración administrada, reiniciar el bridge y
verificar `/health`, `/ready` y un E2E por el JID autorizado.
