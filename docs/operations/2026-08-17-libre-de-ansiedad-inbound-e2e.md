# E2E inbound allowlisted — Libre de Ansiedad

- **Fecha:** 2026-08-17
- **Estado:** evidencia operativa aprobada para el único JID de prueba
- **No autoriza:** tráfico general, carrito abandonado, compra fallida,
  follow-ups, scheduler, dispatcher ni outbound proactivo

## Artefacto desplegado

- PR: `#41`
- commit revisado: `534e971d6fe44da7610c8cbff93b567274893d1d`
- imagen efectiva:
  `easypanel/infra/appointment-bridge:534e971d6fe44da7610c8cbff93b567274893d1d`
- estado del rollout Swarm: `completed`
- `/health`: HTTP `200`
- `/ready`: HTTP `200`, runtime operativo y automatización comercial
  proactiva pausada
- volumen durable `/app/data`: presente y escribible

El profile `agente-comercial` recibió el `SOUL.md` versionado de Libre de
Ansiedad. Hash efectivo:

```text
72201e6d0ef4b601ddaecbfb87c092e4a06a1f6def7868f567f372ca2d624c1a
```

El gateway fue reiniciado bajo s6 y respondió una prueba sintética por la ruta
real bridge → Hermes con propuesta estructuralmente válida.

## Configuración efectiva

Activado exclusivamente para el inbound allowlisted:

```text
CHATWOOT_CUT_B_ADMISSION_ENABLED=true
CHATWOOT_CUT_B_SCOPE_KEY=libre-de-ansiedad-inbound
CHATWOOT_CUT_B_SCOPE_VERSION=1
CHATWOOT_CUT_B_AGENT_ENABLED=true
HERMES_SHADOW_ENABLED=true
CHATWOOT_AUTOMATED_REPLIES_ENABLED=true
```

Permanecieron apagados:

```text
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
```

El scope publicado en Supabase Cloud coincidió con:

- producto `F106691755G`;
- oferta `bxjge6zq`;
- account e inbox WABA configurados server-side.

## E2E real

La persona allowlisted inició un mensaje desde WhatsApp. La verificación
sanitizada confirmó:

1. un inbound canónico en Chatwoot;
2. una admisión durable en Corte B;
3. exactamente un `commercial_case` inbound;
4. exactamente un contacto, una identidad WhatsApp y una conversación ligados
   a esa admisión;
5. identidad durable en estado `active`;
6. contexto canónico con anchor exacto de la conversación Chatwoot;
7. invocación del profile `agente-comercial`;
8. exactamente una respuesta pública posterior al inbound;
9. autor de la respuesta: AgentBot configurado;
10. estado de transporte Chatwoot: `delivered`.

No se preservan en este documento JID, teléfono, contenido, IDs de conversación,
tokens ni payloads.

## Gates y revisión

- focal Chatwoot/Corte B/deployment: PASS;
- suite completa `pytest`: PASS;
- compilación, `git diff --check` y workspace preflight: PASS;
- CI remoto `verify`: PASS;
- revisión focal independiente del gate: PASS, sin blockers;
- cero intentos `request_started` durante el postflight;
- cero trabajos inbound activos después de completar el E2E.

## Límites vigentes

- El caso durable permanece `automation_status=draft_only` y
  `authority_mode=shadow`; la autorización ejecutable del reply controlado es el
  gate runtime + JID allowlisted. Esto es una etapa de piloto controlado, no una
  autorización general.
- El E2E demuestra inbound regular hasta entrega WhatsApp. No demuestra carrito
  abandonado, compra fallida, templates proactivos ni follow-ups.
- La actualización del servicio se aplicó directamente al servicio Swarm desde
  el snapshot Git inmutable y quedó respaldada de forma privada en el host. Un
  deploy futuro desde el control plane de EasyPanel debe preservar explícitamente
  los cuatro valores `CHATWOOT_CUT_B_*`; de lo contrario el runtime debe volver a
  estado default-off o fallar cerrado.
