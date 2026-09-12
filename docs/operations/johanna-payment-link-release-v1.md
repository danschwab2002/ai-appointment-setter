# Runbook — release del enlace de pago de Johanna V1

- **Estado:** Preparado; aprobado para commit y revisión, ejecución productiva bloqueada
- **Alcance:** gates desde commit hasta activación gradual
- **No autoriza:** commit, merge, DDL remoto, despliegue, compra, cambios de perfil ni activación

## 1. Decisión de readiness actual

**Resultado:** `GO_TO_COMMIT_REVIEW`, no `GO_TO_PRODUCTION`.

Evidencia local disponible al preparar este candidato:

- suite Python completa: exit `0`;
- integración focalizada de payment link/Chatwoot/webhook/HTTP: exit `0`;
- validador SQL de atribución: `PAYMENT_LINK_ATTRIBUTION_SQL_OK` y
  `PRECHECKOUT_DELAYED_WORKER_SENDER_SQL_OK`;
- hardening ACL: exit `0`;
- preflight de workspace y `git diff --check`: exit `0`;
- revisiones adversariales previas cerraron assignee, metadata malformada,
  caracteres no imprimibles y URLs desnudas.

No existe aún evidencia de commit aprobado, migración Cloud, despliegue, compra
sintética o reporte Hotmart. Por ello el feature debe permanecer apagado.

## 2. Gate de commit y merge — requiere coordinación con el usuario

Antes de commit:

```bash
uv run python scripts/agent_workspace.py preflight
git diff --check
uv run pytest -q
```

Revisar explícitamente:

- diff completo y lista de archivos;
- ausencia de secretos, payloads o URLs con PII;
- que la única migración nueva efectiva sea
  `20260911000400_payment_link_attribution.sql`;
- inventarios ACL/schema coherentes con los tres RPCs;
- `PAYMENT_LINK_ENABLED=false` en configuración versionada;
- que `/opt/data/profiles/agente-comercial/SOUL.md` no forme parte del diff.

El commit, push y merge sólo se ejecutan después de autorización del usuario.
El integrador debe partir de un árbol limpio y procesar el cambio de forma
serial conforme al runbook multiagente.

## 3. Gate de Supabase Cloud — autorización productiva separada

Precondiciones:

- merge aprobado y commit exacto identificado;
- historial remoto y cola de migraciones reconciliados;
- backup/PITR y ventana operativa confirmados;
- workers/effects relevantes en estado seguro;
- plan forward-only aceptado.

Aplicar únicamente mediante el mecanismo de migraciones autorizado del
proyecto. No pegar SQL parcialmente, no usar `migration repair` para simular
aplicación y no reescribir una migración aplicada.

Postflight obligatorio y sanitizado:

- versión `20260911000400` registrada una sola vez;
- tablas `payment_link_bindings` y `payment_link_send_commands` presentes;
- RPCs `get_…candidate`, `prepare_…send` y `finalize_…send` con firma esperada;
- RLS activa, `PUBLIC`/`anon`/`authenticated` sin ejecución ni DML;
- `service_role` sólo con entrypoints previstos;
- triggers de inmutabilidad presentes;
- cero commands preexistentes creadas por la migración;
- inventarios schema/ACL y advisors revisados.

Ante cualquier diferencia: `NO_GO`, feature apagado y corrección mediante una
migración nueva forward-only.

## 4. Gate de despliegue default-off — autorización separada

Desplegar el commit exacto ya mergeado con:

```text
PAYMENT_LINK_ENABLED=false
PAYMENT_LINK_TRACKING_FIELDS=src,xcod
PAYMENT_LINK_TRACKING_PREFIX=hermes-
PAYMENT_LINK_MAX_AGE_SECONDS=604800
```

No modificar `SOUL.md` ni reiniciar/redeployar el profile
`agente-comercial` como efecto colateral. Verificar `/health` y `/ready`, logs
sanitizados y ausencia de incremento en payment-link commands. Un deploy sano
con el flag apagado no autoriza el E2E activo.

Rollback de esta etapa: restaurar la revisión anterior del bridge y mantener el
flag apagado. No eliminar tablas ni datos y no revertir tracking de migración.

## 5. Gate E2E sintético y compra real controlada — ejecución asistida

Usar primero un caso sintético nombrado y un destinatario de prueba autorizado.
Registrar sólo identificadores internos sanitizados.

Preflight:

1. snapshot de conteos payment-link y `delivery_unknown`;
2. confirmar delta cero y ninguna command ambigua;
3. confirmar checkout y precio directamente en Hotmart sin copiarlos a logs;
4. confirmar que compra, opt-out, takeover y pausa bloquean en controles
   negativos antes de habilitar el caso positivo;
5. habilitar únicamente el alcance sintético acordado.

Caso positivo:

1. crear un nuevo `lead.precheckout` sintético identificable;
2. confirmar nueva secuencia y ULID completo;
3. solicitar el enlace por Chatwoot;
4. comprobar un único mensaje y un único POST/command;
5. completar una compra real de prueba sin compartir tarjeta, CVV ni documento;
6. esperar el reporte autoritativo de Hotmart;
7. comprobar allí la igualdad exacta del marcador persistido en `src` o `xcod`;
8. confirmar compra correlacionada y ausencia de follow-up posterior.

Inspeccionar el URL o el mensaje no sustituye el paso 7. Si el reporte no
contiene el marcador exacto, hay duplicado, aparece PII en logs o queda
`delivery_unknown`, el resultado es `NO_GO`; apagar el alcance sintético,
preservar evidencia y reconciliar sin resend.

Después del caso, eliminar sólo datos sintéticos mediante el procedimiento
selectivo aprobado y demostrar delta cero respecto del baseline, conservando la
evidencia mínima requerida.

## 6. Gate de activación gradual — autorización final separada

Sólo después de un E2E aprobado y evidencia Hotmart:

- revisar nuevamente cero `delivery_unknown` sin resolver;
- confirmar scope, opt-out, handoff y observabilidad;
- acordar cohorte/canary, duración, responsable y umbrales de pausa;
- activar `PAYMENT_LINK_ENABLED=true` únicamente para el despliegue acordado;
- verificar `/ready` y observar el primer caso real con supervisión humana.

Pausar inmediatamente ante duplicado, atribución ausente/divergente,
checkout incorrecto, envío tras barrera o exposición de PII. El rollback
operativo es `PAYMENT_LINK_ENABLED=false`; las filas durables se preservan para
reconciliación y auditoría.

## 7. Registro mínimo de cierre

Registrar en evidencia operativa: commit desplegado, versión de migración,
estados de health/ready, conteos antes/después, ID sintético opaco, campo usado,
hash/igualdad del marcador, resultado Hotmart, limpieza y decisión Go/No-Go.
Nunca registrar el checkout completo, prellenado, teléfono, email, credenciales
o datos de pago.
