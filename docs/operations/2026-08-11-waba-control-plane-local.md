# Evidencia local/read-only — control plane WABA Lancemos

- **Fecha:** 2026-08-11
- **Estado:** `blocked`
- **Alcance:** inventario sanitizado de Chatwoot, WABA, Evolution, bridge y estado durable; sin mensajes ni mutaciones
- **No prueba:** webhook nativo WABA emitido, entrega física, respuesta dentro de ventana, template outbound, handoff real, despliegue nuevo ni rollback productivo

## Límites de la observación

Se usó el acceso SSH operativo autorizado para ejecutar consultas read-only dentro
de los contenedores desplegados. No se imprimieron tokens, teléfonos, nombres de
inbox, payloads, contenido de mensajes ni valores de `provider_config`. La consulta
Rails leyó únicamente tipos, presencia de configuración, conteos e identidad de
provider. Supabase se consultó mediante SQL read-only y sólo se conservaron
agregados.

No se cambió configuración de EasyPanel, Chatwoot, Evolution, Meta ni Supabase. No
se reinició ningún servicio y no se envió ningún mensaje.

## Chatwoot y canal oficial

| Comprobación | Resultado sanitizado |
|---|---|
| Account accesible con credencial de control | `pass` |
| Inbox oficial único | `pass` |
| Tipo del inbox oficial | `Channel::Whatsapp` |
| Provider | `whatsapp_cloud` |
| Número configurado | `true` |
| Phone Number ID configurado | `true` |
| WABA ID configurado | `true` |
| Token de provider configurado | `true` |
| Miembros del inbox oficial | `2` |
| Inbox legacy separado | `Channel::Api` |
| Teams disponibles para handoff | `0` — blocker |
| AgentBot de la cuenta | existe; no está ligado directamente a ninguno de los dos inboxes |

La credencial de control obtuvo HTTP 200 al listar inboxes, Teams y AgentBots. El
endpoint de webhooks devolvió 401 para esa credencial, por lo que se verificó el
webhook mediante una consulta Rails read-only dentro de Chatwoot; no se amplió el
token.

Chatwoot tiene 12 templates sincronizados y marcados `APPROVED`: 11 `UTILITY` y
uno `MARKETING`; 11 usan `es` y uno `es_MX`. No se leyeron nombres ni contenido,
por lo que esta evidencia no identifica todavía cuáles corresponden a apertura y
follow-up de Lancemos ni valida su esquema de variables.

## Webhook compartido

Se observó exactamente un webhook de cuenta:

- HTTPS: `true`;
- destino: host del bridge, path `/webhooks/chatwoot`;
- suscripciones: sólo `message_created`.

Debe conservarse: sirve a la cuenta completa. El aislamiento correcto se aplica en
el bridge por account + inbox + JID, no eliminando indiscriminadamente este
webhook.

## Evolution legacy

| Comprobación | Resultado |
|---|---|
| Estado de conexión del transporte | `close` |
| Integración Evolution → Chatwoot | `enabled` — blocker |
| Credencial Chatwoot almacenada en Evolution | `present=true`; valor no leído |

El transporte ya está desconectado, pero la integración Chatwoot sigue habilitada.
El retiro seguro requiere deshabilitar esa integración conservando el inbox e
historial. No se ejecutó esa mutación en esta observación.

## Bridge desplegado

| Comprobación | Resultado |
|---|---|
| `/health` | HTTP 200 |
| `/ready` | HTTP 404 |
| Artefacto | anterior al runtime WABA/handoff integrado |
| Provider configurado | `evolution` — blocker |
| Inbox configurado | inbox legacy — blocker |
| Automated replies | `true` — blocker |
| Reply splitter | `true` — blocker |
| Hermes shadow | `true` — blocker |
| ResolutionWorker | `true` — blocker |
| Durable dispatcher | `true` — blocker |
| Durable outbound | `true` — blocker |
| Perímetro Lancemos | `false` |

El deployment actual no es apto para un inbound WABA controlado: apuntar el JID de
prueba al nuevo canal mientras replies siguen activos podría producir un efecto
externo. No se pidió ningún mensaje al usuario.

El volumen del bridge contiene agregados históricos: 3 capturas, 3 resultados
shadow y 6 replies. No se inspeccionaron nombres ni contenidos.

## Estado durable agregado

La lectura Supabase encontró:

- 8 acciones `accepted_by_chatwoot`;
- 8 intentos `completed`;
- 1 intento `reserved`;
- 2 secuencias `completed`;
- 2 casos `sequence_exhausted`;
- cero acciones pendientes, diferidas, retryable, delivery-unknown o request-start
  visibles en el agregado.

El intento `reserved` debe reconciliarse por identidad y alcance antes de una
corrida outbound; no se borró ni modificó para forzar backlog cero.

Una segunda lectura confirmó que ese intento tiene 114 horas, modo `freeform`,
ningún `request_started` y referencia una acción ya `accepted_by_chatwoot`, una
secuencia `completed` y un caso `sequence_exhausted`. No está vivo para despacho,
pero queda como inconsistencia histórica a explicar antes de declarar backlog
operativo limpio.

El catálogo remoto todavía no contiene `contact_opt_out_events`,
`pilot_scope_versions`, `pilot_runtime_controls`, `human_handoff_requests` ni
`human_handoff_projection_effects`. Por ausencia de las tablas no existe backlog
remoto de proyección opt-out/handoff, pero tampoco están disponibles esas
capacidades. El perímetro WABA y el handoff integrados en Git aún no tienen
autoridad durable desplegada. No se aplicaron migraciones remotas.

## Verificador sanitizado

Se implementó `scripts/verify_chatwoot_waba_readiness.py`. Recibe un snapshot por
stdin y los IDs esperados por argumentos, pero devuelve únicamente status,
blockers y el booleano `safe_for_controlled_inbound`; no refleja IDs ni campos
desconocidos del input.

Resultado del snapshot real sanitizado:

```json
{
  "blockers": [
    "bridge_automated_replies_not_off",
    "bridge_dispatcher_not_off",
    "bridge_outbound_not_off",
    "bridge_provider_mismatch",
    "bridge_reply_splitter_not_off",
    "bridge_resolution_worker_not_off",
    "bridge_scope_mismatch",
    "bridge_shadow_not_off",
    "evolution_chatwoot_integration_enabled",
    "human_handoff_team_missing"
  ],
  "safe_for_controlled_inbound": false,
  "status": "blocked"
}
```

El exit code esperado y observado fue `1`.

## Verificación local

```text
uv run pytest
628 passed, 1 warning
```

La advertencia es la deprecación preexistente de `starlette.testclient` respecto de
`httpx`; no corresponde al cambio. Los tests incluyen:

- account/inbox correctos admitidos;
- account incorrecto rechazado;
- inbox incorrecto rechazado aun con el mismo JID;
- desacuerdo entre `inbox.id` y `conversation.inbox_id` rechazado;
- IDs booleanos rechazados aunque Python compare `true == 1`;
- scope productivo incompleto rechazado;
- HTTP firmado de inbox incorrecto sin captura;
- evento humano del inbox incorrecto sin pausa, historial, Hermes ni reply;
- salida y errores CLI del verificador sin IDs ni valores desconocidos del input.
- cada uno de los 12 switches de efectos bloquea de forma independiente tanto
  cuando está activo como cuando falta;
- cada barrera de backlog de proyección bloquea cuando no es cero o está ausente.

## Implementación local de aislamiento de ingreso

El clasificador local ahora valida antes de persistir:

- `account.id` exacto;
- `inbox.id` exacto;
- `conversation.inbox_id` exacto y coherente;
- JID allowlisted.

Un mismo JID proveniente del inbox legacy recibe `200 ignored` con
`inbox_not_allowed`; no se captura, no pausa, no invoca Hermes y no produce efectos.
La implementación está localmente verificada, pero todavía no está publicada ni
desplegada.

## Go/no-go

**No-go** para pedir el mensaje inbound y para cualquier outbound. Para llegar al
siguiente gate se requiere, como operaciones separadas y verificables:

1. publicar y desplegar una revisión que incluya el filtro de account/inbox;
2. configurar el bridge con el inbox WABA y todos los efectos en `false`;
3. deshabilitar la integración Chatwoot de Evolution sin borrar historial;
4. crear o seleccionar un Team humano con miembros;
5. para handoff/outbound, aplicar y verificar por separado las migraciones remotas
   de perímetro y handoff;
6. reconciliar el intento durable `reserved` antes de outbound;
7. repetir el verificador hasta obtener `status=ready`;
8. recién entonces solicitar un único mensaje inbound del teléfono allowlisted.
