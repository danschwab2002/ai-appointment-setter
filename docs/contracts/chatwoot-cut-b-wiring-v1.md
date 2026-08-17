# Contrato — wiring Chatwoot a Corte B V1

- **Estado:** implementado default-off; no activado en runtime productivo.
- **Alcance:** admisión durable inbound; no respuesta, agente, handoff ni outbound.

## Configuración

```text
CHATWOOT_CUT_B_ADMISSION_ENABLED=false
CHATWOOT_CUT_B_SCOPE_KEY=<scope publicado>
CHATWOOT_CUT_B_SCOPE_VERSION=<versión positiva>
```

Al habilitar el flag, también deben existir `CHATWOOT_ACCOUNT_ID`,
`CHATWOOT_INBOX_ID`, `SUPABASE_BASE_URL` y `SUPABASE_SERVICE_ROLE_KEY`.
Configuración incompleta impide iniciar el proceso.

## Flujo

Después de validar firma, antigüedad, dirección, visibilidad, JID, account e inbox,
el worker deriva únicamente:

- conversación: `conversation.id` positivo;
- usuario externo: dígitos del JID canónico ya allowlisted;
- scope y versión: configuración server-owned.

Invoca `admit_inbound_commercial_case`. Los outcomes `created`, `already_exists` y
`evidence_conflict` terminan el envelope sin invocar Hermes ni enviar respuestas.
Errores operativos de Supabase quedan retryables. El RPC debe devolver
`automation_status=draft_only`; cualquier otro valor es error de protocolo.

## Frontera de activación

Activar este flag sólo crea o relee el agregado canónico. No habilita
`HERMES_SHADOW_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED`, handoff,
dispatcher, follow-ups ni outbound. Esos efectos conservan gates separados.
