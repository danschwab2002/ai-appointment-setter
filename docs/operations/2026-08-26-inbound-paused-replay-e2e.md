# Evidencia E2E — replay inbound después de handoff

- **Fecha:** 2026-08-26
- **Estado:** bypass reproducido en producción; corrección implementada y verificada sólo localmente
- **Alcance:** inbound Johanna desde una identidad distinta al JID histórico
- **Privacidad:** no se registran teléfono, JID, contacto, conversación, mensajes completos ni credenciales

## Evidencia física

El operador envió un único mensaje desde una identidad distinta al JID histórico.
La respuesta llegó por el canal oficial y se mostró en dos partes dentro de la
conversación canónica. Una captura aportada por el operador fue revisada localmente
y no se copió al repositorio.

Esto verifica que el inbound scoped dinámico acepta y responde a una identidad
canónica del account/inbox autorizado sin depender del JID fijo.

## Estado durable observado

El conteo de admisiones no aumentó. La consulta sanitizada explicó el replay:

```text
admisiones históricas para la identidad: 2
scope histórico: versión 1
scope vigente: versión 2
caso vigente: paused / disabled
conversación vigente: paused_human / paused
human_takeover: true
```

La captura de Chatwoot mostró además una interacción anterior que contenía un
disparador de derivación clínica. Por lo tanto, no era una identidad realmente
nueva para el sistema: Chatwoot reutilizó una conversación histórica que no era
visible inicialmente en la vista operativa.

## Hallazgo

El RPC original revalidaba identidad y conversación al encontrar una admisión,
pero no releía el estado actual del caso y devolvía `draft_only` de forma fija.
El Bridge trató `already_exists` como replyable e invocó Hermes aunque Supabase
conservaba un handoff durable.

Clasificación del escenario:

- inbound desde una identidad distinta al JID histórico: **PASS**;
- respeto de pausa/handoff en replay: **FAIL de seguridad**.

No se borró ni reactivó el contacto. La evidencia durable se preservó porque fue
la condición que permitió detectar el bypass.

## Corrección local

La migración local `20260826000200_inbound_paused_replay_guard.sql`:

- conserva la implementación previa como función base interna sin permisos API;
- agrega `admit_inbound_commercial_case_v2`;
- relee bajo lock caso y conversación;
- devuelve `blocked/disabled` ante pausa, deshabilitación o takeover;
- conserva el nombre legacy como wrapper que traduce `blocked` a
  `evidence_conflict` para réplicas viejas.

El Bridge local usa V2 y termina ante `blocked` antes de Hermes. Una revisión
independiente inicial reprodujo luego una carrera entre esa admisión y el envío:

```text
durable_paused=true
hermes_calls=0
splitter_calls=1
send_calls=1
veredicto=REQUEST_CHANGES
```

La remediación local vuelve a consultar V2 antes del splitter/manifiesto y dentro
de la frontera de envío antes de cada parte. Un stop concurrente produce cero
splitter y cero sender; el control activo conserva el multipart y reautoriza cada
parte.

Una segunda revisión independiente read-only terminó en `APPROVE`, sin bypasses
locales concretos ni hallazgos bloqueantes. Sus probes sanitizados comprobaron:

```text
pausa pre-split: auth=2 splitter=0 sender=0
pausa entre partes: auth=4 splitter=1 sender=1
error pre-split: splitter=0 sender=0 retryable=true
error antes de parte 1: splitter=1 sender=0 retryable=true
error antes de parte 2: primera enviada, segunda bloqueada, retryable=true
control activo persistido: partes=2 auth=4
veredicto=APPROVE
```

La revisión también confirmó que reset y handoff conservan su semántica, las
reautorizaciones mantienen el scope y la identidad canónicos, y Chatwoot conserva
dos lecturas finales antes del POST.

## Evidencia local

```text
pruebas focales Python: 22 passed
pruebas focales TOCTOU: 6 passed
módulo tests/test_webhook.py: 88 passed
INBOUND_PAUSED_REPLAY_SQL_OK
acl_hardening=OK
service_entrypoints=47
```

El probe SQL aplicó el stack completo, creó una admisión activa, ejecutó el RPC
real `request_inbound_human_handoff` y comprobó:

- replay activo: `already_exists`;
- replay posterior al handoff: `blocked` en V2;
- replay legacy posterior al handoff: `evidence_conflict`;
- V2 sólo ejecutable por `service_role`;
- función base no ejecutable por `service_role`.

## Límites y estado de rollout

- La corrección todavía no está publicada, migrada ni desplegada.
- La primera revisión independiente encontró el TOCTOU; la segunda aprobó su
  remediación focal.
- La migración nueva no está aplicada en Supabase Cloud.
- El Bridge corregido no está desplegado.
- No se enviaron mensajes adicionales para verificar la corrección.
- No se mutó Chatwoot, EasyPanel ni Supabase Cloud durante el diagnóstico o la implementación local.