# Contrato — wiring Chatwoot a Corte B V1

- **Estado:** implementado default-off; no activado en runtime productivo.
- **Alcance:** admisión durable inbound y, bajo un gate separado, respuesta
  conversacional sólo para el JID allowlisted.

## Configuración

```text
CHATWOOT_CUT_B_ADMISSION_ENABLED=false
CHATWOOT_CUT_B_SCOPE_KEY=<scope publicado>
CHATWOOT_CUT_B_SCOPE_VERSION=<versión positiva>
CHATWOOT_CUT_B_AGENT_ENABLED=false
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

Invoca `admit_inbound_commercial_case`. Con el gate de agente apagado, todos los
outcomes terminan sin invocar Hermes ni responder. Con
`CHATWOOT_CUT_B_AGENT_ENABLED=true`, sólo `created` y `already_exists` continúan
por historia canónica → Hermes → reply Chatwoot. `evidence_conflict` siempre
termina sin modelo ni respuesta. Errores operativos de Supabase quedan retryables.
El RPC debe devolver
`automation_status=draft_only`; cualquier otro valor es error de protocolo.

## Frontera de activación

El gate de agente exige admisión, `HERMES_SHADOW_ENABLED`,
`CHATWOOT_AUTOMATED_REPLIES_ENABLED`, AgentBot e historia canónica. No habilita
handoff, dispatcher, follow-ups, templates ni outbound proactivo; esos efectos
conservan gates separados. El allowlist de JID, account e inbox sigue siendo
determinístico y obligatorio antes del modelo y antes del reply.
