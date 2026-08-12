# Runbook — re-onboarding de portfolio WABA

- **Estado:** Preparado; no ejecutado sobre el portfolio definitivo
- **Objetivo:** portar configuración y demostrar readiness por niveles sin activar efectos
- **Prohibido en este runbook:** crear copy/templates, enviar mensajes, aplicar migraciones, borrar recursos o activar cohorte

## 1. Insumos manuales irreducibles

Sólo se escalan accesos/MFA y decisiones del negocio: portfolio autorizado; WABA/número definitivos; aprobación de templates existentes o copy provisto por el negocio; método de pago; oferta/política; owner de kill switch y, si se habilita, owner de handoff. No pedir que el usuario copie secretos al chat.

## 2. Baseline read-only

1. Fijar commit/image digest desplegado.
2. Inventariar por presencia portfolio, WABA, número, Phone Number ID, account/inbox, webhook, templates y secret bindings.
3. Conservar referencias opacas para `new_official_inbox` y `previous_official_or_legacy_inbox`.
4. Verificar que el inbox histórico sigue presente y que Evolution está fuera del scope; no borrar ninguno.
5. Contar trabajo elegible y efectos inciertos antes de cualquier worker.

Si una lectura no es autorizada o resulta ambigua, registrar `blocked`; no reparar durante la observación.

## 3. Preparar configuración default-off

Cargar secretos sólo en EasyPanel. El candidato debe fijar el account/inbox nuevo y provider `waba`, manteniendo exactamente apagados replies, splitter, shadow, resolución, compra, pausa humana, opt-out y su proyección, handoff y su proyección, dispatcher, outbound y perímetro piloto. La presencia de configuración capaz de autoconstruir workers cuenta como enabled y bloquea.

Desplegar/reiniciar es una mutación productiva separada y requiere autorización. Esta rama no la ejecuta.

## 4. Snapshot sanitizado y gate

El snapshot temporal puede contener los IDs necesarios para comparación, pero no se persiste en Git ni evidencia. La salida sólo conserva level/status/reasons y presencia de commit/config digest, timestamp y cero efectos.

```text
uv run python scripts/verify_waba_staged_readiness.py \
  --expected-account-id "$ACCOUNT_ID" \
  --expected-inbox-id "$NEW_INBOX_ID" \
  --previous-inbox-id "$PREVIOUS_INBOX_ID" < snapshot.json
```

`ready_for_observational_inbound.ready=true` permite solicitar autorización para una corrida inbound sin respuesta. No requiere Team, template, pago, migraciones de handoff ni schema piloto.

## 5. Prueba negativa obligatoria de portabilidad

Antes del inbound real, inyectar sólo en test/HTTP controlado un webhook firmado con el mismo JID pero el inbox anterior. Debe producir cero captura, lectura de historia, Hermes, RPC, pausa y mensaje. Luego ejecutar un control positivo con el inbox nuevo y efectos apagados para probar que el receptor no está muerto. Un nombre de inbox o número coincidente no reemplaza IDs canónicos.

## 6. Nivel template controlado

Obtener por lectura autorizada una única selección de first touch y follow-up. Atestar por separado aprobación Meta y negocio, idioma, categoría y `{{1}}`. La categoría debe ser `MARKETING` o `UTILITY`; otra categoría bloquea con `template_category_runtime_unsupported`. Si los templates difieren en idioma/categoría/schema, no adaptar manualmente: `template_pair_runtime_mismatch` y cambio de contrato previo.

Requiere pago operativo, recipient allowlisted, presupuesto exactamente uno, backlog elegible cero repetido antes de armar y rollback listo. Este gate no habilita cohorte. Enviar el template es otra autorización productiva y no pertenece al re-onboarding.

## 7. Nivel piloto supervisado

Requiere además schema remoto verificado, scope publicado pero `inactive`, stops de compra/opt-out, política y Conversation Release aprobadas, monitoreo, cohorte/presupuesto acotados y kill switch con owner. `handoff_enabled` debe ser `true` o `false`: con `false`, Team/owner no bloquea; con `true`, el owner real es obligatorio; cualquier otro valor bloquea.

Publicar scope, migrar schema y cambiar a `armed` son mutaciones separadas. Ninguna se ejecuta por obtener readiness.

## 8. Rollback

1. Cerrar ingress nuevo.
2. Si algún request cruzó `request_started`, pausar autoridad durable y reconciliarlo sin retry ciego.
3. Mantener workers necesarios sólo para reconciliación ya iniciada.
4. Deshabilitar outbound/dispatcher/consumidores después del drain.
5. Restaurar el binding anterior sólo mediante despliegue autorizado y manteniendo efectos apagados.
6. Conservar inboxes, conversaciones y evidencia; no borrar para hacer pasar contadores.

## 9. Evidencia permitida

Registrar: commit/image digest, configuration digest, timestamp, nivel, status, reason codes, conteos agregados de backlog/efectos y resultado del rollback. Nunca registrar IDs externos, nombres/copy de templates, teléfonos, tokens, payloads o paths de backups.
