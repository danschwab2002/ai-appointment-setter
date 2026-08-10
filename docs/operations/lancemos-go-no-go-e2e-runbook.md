# Runbook — ejecución integral de go/no-go del piloto Lancemos

- **Estado:** Preparado; no ejecutado
- **Fecha:** 2026-08-10
- **Fuente:** `docs/design/lancemos-go-no-go-e2e-matrix.md`
- **Alcance:** ejecución coordinada y sanitizada de F-01..F-15
- **No es evidencia:** este documento prescribe el procedimiento; cada corrida genera un registro fechado separado

## 1. Principios

- Un escenario por corrida y por evento nuevo.
- Un único destinatario allowlisted hasta terminar el gate.
- Ningún `2xx` remoto prueba por sí solo entrega, asignación o nota.
- Los stops determinísticos se prueban antes de ampliar conversación o volumen.
- Una corrida `blocked` no se “desbloquea” inventando fixtures productivos ni
  sustituyendo WABA por Evolution.
- No se corrige código o configuración durante una corrida de observación; se
  preserva el fallo, se hace rollback y la repetición usa un nuevo run ID.
- Secrets en EasyPanel; PII y payloads completos fuera de Git y logs.

## 2. Freeze y preflight

Registrar antes de cualquier efecto:

```yaml
run_id: opaque
release_commit: sha
container_image_digest: opaque
migration_stack_hash: opaque
conversation_release_version: opaque
pilot_scope_key_version: opaque
followup_policy_key_version: opaque
waba_templates_version: opaque
source_mode: hotmart_real | manual_official_v2
```

Gates:

- [ ] commit desplegado coincide con el commit revisado;
- [ ] historial remoto reconciliado y todas las migraciones esperadas aplicadas;
- [ ] `/health` y `/ready` responden con estados sanitizados;
- [ ] runtime `inactive` o `paused`, outbound apagado;
- [ ] backlog elegible y presupuesto consumido en cero;
- [ ] ingress Hotmart quiescente;
- [ ] account/inbox/canal y templates verificados read-only;
- [ ] Juan y Meta aprobaron ambos templates;
- [ ] producto/oferta y único contacto de prueba coinciden con el scope;
- [ ] handoff integrado para ejecutar F-10;
- [ ] operator owner y rollback disponibles.

Si un gate falla, registrar `blocked` y no armar runtime.

## 3. Baseline sin efectos

Ejecutar sobre el commit fijado:

```text
uv run pytest -q
npm ci --prefix tests/sql/followup_engine
npm --prefix tests/sql/followup_engine test
node tests/sql/followup_engine/validate_pilot_boundary.mjs
node tests/sql/followup_engine/validate_pilot_boundary_runtime.mjs
```

Los probes PostgreSQL reales no crean ni destruyen bases. Provisionar tres bases
vacías, aisladas y desechables cuyos nombres comiencen respectivamente con
`pilot_boundary_concurrency`, `pilot_boundary_runtime` y `optout_concurrency`.
`PSQL` debe apuntar al binario aprobado y cada URL debe pertenecer sólo a esa
base. Los scripts verifican el prefijo y se niegan a usar una base no vacía.

```text
DATABASE_URL="$PILOT_BOUNDARY_DATABASE_URL" PSQL="$PSQL" \
  ALLOW_DISPOSABLE_DATABASE=pilot-boundary-concurrency \
  uv run python tests/sql/followup_engine/real_postgres_pilot_boundary.py

DATABASE_URL="$PILOT_RUNTIME_DATABASE_URL" PSQL="$PSQL" \
  ALLOW_DISPOSABLE_DATABASE=pilot-boundary-runtime \
  uv run python tests/sql/followup_engine/real_postgres_pilot_boundary_runtime.py

DATABASE_URL="$OPT_OUT_DATABASE_URL" PSQL="$PSQL" \
  ALLOW_DISPOSABLE_DATABASE=optout-concurrency \
  uv run python tests/sql/followup_engine/real_postgres_opt_out.py
```

Al terminar, destruir las tres bases mediante el mismo mecanismo aislado que las
provisionó y verificar que ya no existen. No apuntar nunca estos probes a
Supabase, a una base compartida o a una base con datos. Agregar los probes de
handoff sólo después de su integración. Guardar comandos, exit codes y marcadores
sanitizados; no pegar logs con fixtures completos.

Luego seguir la barrera de backlog cero y fase de observación definida en
[`lancemos-controlled-channel-e2e-runbook.md`](lancemos-controlled-channel-e2e-runbook.md).

## 4. Orden de ejecución

El orden minimiza efectos y evita que un escenario deje trabajo que contamine al
siguiente.

### Etapa A — canal y perímetro sin efectos

1. **F-03 scope incorrecto:** ejecutar cada dimensión incorrecta con outbound
   apagado y demostrar cero trabajo/llamadas mutantes.
2. **F-04 config y modo WABA:** omitir cada campo en un arranque separado; luego
   probar el cruce WABA/freeform en frontera durable.
3. **F-14 destino no permitido:** observar cero lookup/create/send.
4. **F-13 kill switch:** competir pausa/request-start sin emitir mensajes.

Estos probes usan harnesses locales/stateful y validación read-only. Sus contadores
cero no se promueven todavía como evidencia remota. Cualquier fallo detiene el
run integral.

### Etapa B — primer contacto e idempotencia

1. Ejecutar **F-01** conforme al runbook del canal controlado.
2. Confirmar llegada física y correlación durable antes de continuar.
3. Ejecutar **F-02** con replay exacto y semántico.
4. Cerrar ingress y volver a backlog cero.

F-01 es el control positivo físico. Sólo después de observarlo pueden los
contadores cero de F-03/F-13/F-14 ejecutados sobre el mismo commit, cuenta e inbox
contar como evidencia remota no vacua; cada escenario se repite entonces en un
run aislado y enlaza `positive_control_ref` a F-01.

Si `source_mode=manual_official_v2`, el resultado no acredita entrega real de
Hotmart aunque F-01/F-02 pasen para el bridge.

### Etapa C — inbound y follow-up

1. **F-05:** enviar 3–4 mensajes rápidos desde el contacto autorizado; confirmar
   una evaluación y una respuesta lógica.
2. Volver a estado terminal y backlog cero.
3. **F-06:** usar una política de prueba versionada con tiempo comprimido;
   comprobar un follow-up en la misma conversación sin provisioning nuevo.

No mezclar batching y follow-up en una misma observación.

### Etapa D — stops

La matriz no se ejecuta dentro de un único `scope_key`. Cada escenario que pueda
producir un efecto usa un scope de prueba aislado, con la misma tupla comercial y
caps diario y total iguales a uno, mínimo admitido por el contrato durable. En un
escenario de cero efecto, el resultado exige que ambos consumos permanezcan en
cero; el cap uno es sólo un límite conservador de daño ante una regresión, no
demuestra por sí mismo que el camino falle cerrado. F-01 conserva
el presupuesto uno exigido por el runbook del canal. Cada scope se cierra al
terminar y nunca se reutiliza. El presupuesto es acumulativo dentro de cada
`scope_key`: no se reinicia creando otra versión y nunca se edita o borra el
ledger.

1. **F-07 compra:** ejecutar por separado antes de claim, antes de request-start y
   después de request-start.
2. **F-08 opt-out:** ejecutar por separado antes de plan, antes de request-start y
   bajo replay/restart.
3. **F-09 takeover:** insertar la acción humana entre autorización y
   request-start.
4. **F-10 handoff:** sólo con implementación integrada; confirmar pausa durable
   primero, luego equipo y nota privada, con silencio externo.

Después de cada stop exigir cero successor y cero request nuevo.

### Etapa E — recuperación e incertidumbre

1. **F-11 restart:** reiniciar en puntos separados: evento admitido, acción con
   lease, intento reservado y completion terminal.
2. **F-12 ambigüedad:** inyectar lost response/timeout después del POST y observar
   `delivery_unknown` sin segundo POST ciego.
3. **F-13 rollback:** cerrar ingress, pausar durablemente, drenar sólo lo iniciado,
   apagar outbound y dejar `/ready` coherente.

### Etapa F — aceptación conversacional

Ejecutar **F-15** con la release pinneada y la matriz
[`lancemos-conversation-acceptance-matrix.md`](../design/lancemos-conversation-acceptance-matrix.md).
No usar mensajes generados en etapas técnicas como aprobación implícita de voz,
oferta o límites.

## 5. Evidencia mínima por escenario

```yaml
scenario_id: F-XX
run_id: opaque
status: pass | fail | blocked | not_run | not_applicable
blocked_reason: external | deployment | implementation | business_input | null
release_commit: sha
positive_control_ref: run_id | null
preconditions_verified: true | false
ingress_state_before: closed | quiescent | open
outbound_enabled_before: true | false
runtime_state_before: inactive | armed | paused | closed
runtime_generation_before: integer
backlog_before:
  webhook_events: integer
  cases: integer
  sequences: integer
  actions: integer
  reserved_attempts: integer
  request_started_attempts: integer
  outbound_authorizations: integer
  delivery_unknown: integer
budget_before:
  total_used: integer
  daily_used: integer
  total_cap: integer
  daily_cap: integer
durable_terminal_state: enum
model_call_count: integer
chatwoot_mutating_call_counts:
  contact_create: integer
  conversation_create: integer
  public_message_create: integer
  assignment: integer
  private_note: integer
physical_effect_confirmed: true | false
additional_effect_count: integer
backlog_after:
  webhook_events: integer
  cases: integer
  sequences: integer
  actions: integer
  reserved_attempts: integer
  request_started_attempts: integer
  outbound_authorizations: integer
  delivery_unknown: integer
budget_after:
  total_used: integer
  daily_used: integer
runtime_state_after: inactive | armed | paused | closed
runtime_generation_after: integer
ingress_state_after: closed | quiescent | open
outbound_enabled_after: true | false
uncertainty_owner: role | null
uncertainty_deadline: ISO-8601 | null
rollback_state: enum
limitations: []
evidence_refs: []
```

Los contadores deben provenir de autoridad durable, control plane o un harness
stateful observado. Un contador cero sin progreso positivo en el camino válido es
prueba vacua.

## 6. Severidad y detención

- Severidad 5: detener inmediatamente, cerrar ingress y ejecutar rollback.
- Severidad 4: detener el bloque actual; no avanzar a cohorte.
- Un `blocked` crítico conserva `NO-GO`; no se convierte en `pass` por evidencia
  histórica o de un provider diferente.
- Un fallo de harness/configuración se clasifica aparte de un fallo de producto,
  pero la frontera funcional sigue `not_run`.

## 7. Rollback común

1. cerrar Hotmart/manual ingress;
2. llevar el runtime a `paused` con generación esperada;
3. impedir nuevos request-start;
4. reconciliar únicamente efectos que ya comenzaron;
5. conservar `delivery_unknown` si no existe evidencia concluyente;
6. apagar outbound y workers según su orden válido;
7. verificar backlog y `/ready`;
8. no borrar ledger ni evidencia para limpiar el resultado.

## 8. Veredicto final

Generar un registro fechado con:

- tabla F-01..F-15 y enlace a cada evidencia;
- lista de bloqueantes sin datos sensibles;
- commit, imagen, migraciones y versiones exactas;
- confirmación del operador técnico;
- aprobación o rechazo de negocio.

El veredicto sólo puede ser:

- `NO-GO`;
- `GO_CONTROLLED_E2E_ONLY`;
- `GO_MINIMAL_SUPERVISED_COHORT`.

Ningún agente activa la cohorte como efecto colateral de redactar el veredicto.
