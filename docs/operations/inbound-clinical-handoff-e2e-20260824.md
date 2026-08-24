# Evidencia: handoff clínico inbound en producción

- **Tipo:** Evidencia operativa sanitizada
- **Fecha:** 2026-08-24
- **Runtime verificado:** `1a2826bc80ef8a926c3aa1cd60086981c12d3de7`
- **Scope:** `libre-de-ansiedad-inbound / v2 / account 1 / inbox 9`
- **Policy:** `lancemos-inbound-handoff / v1 / Team 1`

## Escenario

Un mensaje real del JID allowlisted pidió indicaciones para dejar medicación
psiquiátrica y definir dosis. La primera ejecución produjo una respuesta segura,
pero no `decision=handoff`. El E2E se consideró fallido y no se atribuyó una
intervención humana.

Se agregó un guard determinístico estrecho para solicitudes directas de dejar,
suspender, cambiar, reducir, aumentar o tomar medicación, o definir su dosis. El
guard conserva el texto seguro del agente y fuerza `decision=handoff` antes de la
RPC durable. PR `#65`, merge `1a2826bc80ef8a926c3aa1cd60086981c12d3de7`.

## Resultado durable observado

```text
commercial_case.kind=inbound_sales
commercial_case.scope_version=2
commercial_case.status=paused
commercial_case.automation_status=disabled
commercial_case.version=2
human_handoff_request.status=projected
human_handoff_request.expected_team_id=1
assignment effect=applied / attempts=1
private_note effect=applied / attempts=1
pending effects=0
retryable effects=0
conflicts=0
dead letters=0
```

La nota privada quedó creada con su marcador idempotente. El replay del webhook
real no agregó una segunda respuesta visible.

## Reconciliaciones operativas

- Runtime de Chatwoot corregido de inbox `6` a inbox `9`.
- Scope publicado `v2` para inbox `9`; policy activa para Team `1`.
- Identidad canónica migrada del inbox histórico `7` al `9` y de la conversación
  histórica `37` a la `38`.
- Caso inbound histórico `v1` detenido como `paused/disabled`, versión `2`.
- Auto-assignment del inbox WABA desactivado.
- Conversación de prueba sin assignee individual y asignada al Team `1`.

## Distinción importante

En la primera proyección, Chatwoot ya había autoasignado la conversación a Dan.
El bridge preservó correctamente al humano existente y no reemplazó ownership;
por eso el efecto de assignment se finalizó como satisfecho sin Team. Después de
apagar auto-assignment se retiró el assignee de la conversación controlada y se
asignó Team `1` por el endpoint oficial de Chatwoot. Esto confirma el estado final,
pero no constituye una segunda proyección limpia del efecto assignment sobre una
conversación nueva sin ownership.

## Postflight

```text
/health=200 / ok
/ready=200 / ready
image=1a2826bc80ef8a926c3aa1cd60086981c12d3de7
CHATWOOT_INBOX_ID=9
CHATWOOT_CUT_B_ADMISSION_ENABLED=true
CHATWOOT_CUT_B_AGENT_ENABLED=true
HUMAN_HANDOFF_ADMISSION_ENABLED=true
HUMAN_HANDOFF_PROJECTION_ENABLED=true
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
```

El replay sintético usado para validar el hotfix terminó con
`ChatwootProtocolError` después de que request, pausa y nota ya habían quedado
durables. Su payload se eliminó; se conservó sólo el tombstone sanitizado
`failed/8` para auditoría.

## Pendiente operativo conocido

Los gates anteriores están activos en la spec efectiva de Docker Swarm. EasyPanel
conserva defaults propios y puede restaurarlos en un redeploy iniciado desde su
panel. Antes del próximo redeploy deben copiarse los mismos valores al bloque de
environment de EasyPanel.
