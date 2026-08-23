# E2E controlado del reset conversacional de Johanna

- **Fecha:** 2026-08-23
- **Estado:** PASS dentro del perímetro controlado
- **Ámbito:** único WhatsApp/JID allowlisted, inbox configurado del piloto y bridge desplegado en EasyPanel
- **Contrato:** [`../contracts/chatwoot-conversation-reset-v1.md`](../contracts/chatwoot-conversation-reset-v1.md)

## Objetivo

Verificar que el comando exacto `/nuevo` crea un límite de contexto conversacional sin borrar el historial operativo de Chatwoot ni habilitar efectos comerciales generales.

## Evidencia observada

### Canal real

El operador envió exactamente `/nuevo` desde el WhatsApp allowlisted y recibió:

```text
Memoria eliminada.
```

El operador confirmó además que ya verificó el turno posterior de aislamiento de contexto. La respuesta completa de ese segundo turno no se conserva en este registro; por lo tanto, se registra como confirmación del operador y no como transcripción independiente.

### Runtime

La comprobación read-only posterior mostró:

- bridge `1/1`, imagen `easypanel/infra/appointment-bridge:d5cdd6274d92c8ac28fb02a0ab6de859d03529b0`;
- Hermes `1/1`;
- `/health`: HTTP `200`, estado `ok`;
- `/ready`: HTTP `200`, estado `ready`, perímetro del piloto deshabilitado y automatización general `default_off`;
- debounce controlado: `CHATWOOT_INBOUND_DEBOUNCE_SECONDS=1`;
- replies del único perímetro de prueba: habilitadas;
- resolution worker, dispatcher, outbound durable y purchase worker: deshabilitados;
- archivo runtime `SOUL.md`: modo `0600`;
- SHA-256 del `SOUL.md` runtime: `5ea21ff0d87bf742d8624c4bff4aa2701bd2802eca5c104e284d4f99a807e34a`;
- el hash coincide con `profiles/agente-comercial/SOUL.md` en `origin/main`.

## Proveniencia Git

- reset conversacional: `44e06d9a7e3220ccb9fcbbba0ede6ad236e7adae`, integrado mediante PR #58;
- hardening de transparencia del prompt: `d9a6463a9880a9f7dac46e339c676eb676214eb1`, integrado mediante PR #59;
- ambos commits son ancestros de `origin/main`;
- `origin/main` verificado: `1e8b82000f30941486e0e44dc6ef63cf25a096f7`.

## Resultado

**PASS** para el slice controlado:

1. el comando exacto fue reconocido por el bridge;
2. la confirmación determinística llegó por el canal real;
3. el operador confirmó que el contexto previo no reapareció en la prueba posterior;
4. el runtime usó el prompt versionado esperado;
5. las automatizaciones comerciales generales permanecieron apagadas.

## Límites de la evidencia

Esta prueba no demuestra que se hayan eliminado mensajes, memoria global, datos personales ni registros durables; el contrato excluye expresamente esas operaciones. Tampoco valida tráfico abierto del piloto, campañas, secuencias proactivas, compra Hotmart, dispatcher/outbound durable, handoff humano efectivo ni un rollout WABA general. No se preservan contenido del prospecto, identificadores personales ni payloads del canal.
