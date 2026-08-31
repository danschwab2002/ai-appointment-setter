# Contrato V1 de readiness para first-touch pre-checkout

- **Estado:** Implementado; activación selectiva preparada y pendiente de deploy
- **Versión:** 1.1.0
- **Alcance:** promoción, diagnóstico sanitario y gate final del first-touch diferido
- **No acredita:** aprobación Meta, envío WABA, entrega física ni activación productiva

## 1. Frontera durable preparada

La migración `20260829000500_precheckout_production_readiness.sql` sólo prepara
autoridad y observabilidad:

- publica `johanna-precheckout-delayed-first-touch / 1` para tenant `lancemos`,
  Chatwoot `account=1/inbox=9`, provider `waba`, evento
  `PRECHECKOUT_FORM_SUBMITTED`, producto `F106691755G` y oferta `bxjge6zq`;
- fija presupuesto de scope `max_cohort_contacts=1`,
  `max_outbound_request_starts_total=1` y
  `max_outbound_request_starts_per_day=1`;
- crea runtime `inactive / generation=0`;
- conserva o crea un binding de timer de 60 minutos con
  `precheckout_first_touch_enabled=false`;
- si encuentra el binding productivo histórico exacto de 5 minutos, lo migra a
  la policy dedicada de 60 minutos, incrementa su generación y lo mantiene
  apagado;
- no cambia flags de proceso, no arma runtime, no crea timers/comandos y no hace
  llamadas externas.

Si ya existe un binding productivo, sólo se acepta la forma histórica exacta y
apagada. Un binding divergente, un scope previo o backlog pre-checkout hacen fallar
la transacción completa.

## 2. RPC sanitaria

### Firma

```text
get_precheckout_delayed_first_touch_readiness()
```

La función es `SECURITY DEFINER`, usa
`search_path = pg_catalog, public, pg_temp` y sólo concede `EXECUTE` a
`service_role`. `PUBLIC`, `anon` y `authenticated` permanecen revocados.

### Salida exacta

Devuelve una fila con:

| Campo | Tipo | Semántica |
|---|---|---|
| `migration_tracking_complete` | boolean | Existen en el ledger `00200`–`00500`. |
| `scope_configured` | boolean | El scope publicado coincide campo por campo. |
| `runtime_state` | text/null | Estado durable observado. |
| `runtime_generation` | bigint/null | Generación durable observada. |
| `timer_binding_enabled` | boolean | El timer base está habilitado. |
| `timer_binding_generation` | bigint/null | Generación del binding. |
| `first_touch_binding_enabled` | boolean | La admisión puede programar first-touch. |
| `due_count` | bigint | Timers pre-checkout vencidos o reservados recuperables. |
| `reserved_count` | bigint | Comandos pre-checkout en `reserved`. |
| `request_started_count` | bigint | Comandos en frontera de efecto. |
| `delivery_unknown_count` | bigint | Efectos ambiguos que no admiten retry ciego. |
| `reason_code` | text | Diagnóstico sanitario prioritario. |

Los conteos son observabilidad agregada; no conceden autorización.

### Prioridad de `reason_code`

1. `migration_tracking_incomplete`;
2. `precheckout_scope_not_configured`;
3. `precheckout_runtime_not_inactive`;
4. `timer_binding_disabled`;
5. `timer_binding_policy_mismatch`;
6. `first_touch_binding_disabled`;
7. `precheckout_first_touch_ready`.

Sólo el último reason acredita que la capa durable coincide con la configuración
esperada, incluida la policy publicada y su delay exacto de 60 minutos. No
acredita aprobación del template ni entrega WABA.

## 3. `GET /ready`

Cuando `PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED=false`, la respuesta saludable
incluye:

```json
{"precheckout_delayed_first_touch":"disabled"}
```

No se consulta la RPC en esa rama y ningún estado durable se interpreta como
permiso de ejecución.

Cuando el flag está en `true`, el bridge consulta la RPC antes de responder ready.
La respuesta `200` agrega:

```json
{
  "precheckout_delayed_first_touch": "enabled",
  "precheckout_delayed_database": "precheckout_first_touch_ready",
  "precheckout_delayed_due": "0",
  "precheckout_delayed_reserved": "0",
  "precheckout_delayed_request_started": "0",
  "precheckout_delayed_delivery_unknown": "0"
}
```

Los conteos se serializan como strings para conservar el contrato existente de
`dict[str, str]`.

El endpoint responde `503` cuando:

- la RPC no está disponible: `precheckout_delayed_readiness_unavailable`;
- el `reason_code` durable no es ready: devuelve ese reason sanitario;
- el reason declara ready pero cualquier booleano/runtime/generación contradice la
  forma exacta: `precheckout_delayed_state_mismatch`.

Ningún error devuelve SQL, nombres de host, tokens, PII ni bodies.

## 4. Gate final de salida

`PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED=true` habilita al worker para listar,
reevaluar y reservar el caso pre-checkout. No autoriza por sí solo el efecto HTTP.

El template productivo `johanna_interes_precheckout_01` usa exactamente dos
parámetros de body, en este orden: `{{1}} = buyer_name` y
`{{2}} = product_name`. El sender debe construir ambos; una aceptación sincrónica
de Chatwoot no reemplaza la observación posterior del estado WABA.

`PRECHECKOUT_DELAYED_OUTBOUND_ENABLED=false` es el default y se evalúa después de
obtener `command_reserved`, pero antes de invocar la RPC que cambia el comando a
`request_started`. En ese estado:

- admisión, timer de 60 minutos, reevaluación y reserva siguen operativos;
- el comando permanece `reserved` y puede reanudarse al habilitar el gate;
- no se obtiene PII para el sender, no se construye el request y no existe POST;
- reinicios y polls repetidos no consumen el único intento.

Un valor distinto de `true` o `false` impide el arranque. Cuando el flag pasa a
`true`, las autoridades durables se releen en la RPC de request-start antes de
obtener permiso de envío. Un comando bloqueado por compra, opt-out, takeover,
identidad o scope no se envía.

La coordenada `runtime_state=inactive / generation=0` se conserva en V1 porque
las funciones SQL publicadas la exigen como parte de la versión de policy. En
este flujo el interruptor de admisión operativa es el binding
`precheckout_first_touch_enabled`; cambiar el runtime a `armed` requiere una
nueva versión de policy y no forma parte de esta activación.

## 5. Compatibilidad y rollout

Orden compatible:

1. aplicar y registrar migraciones `00200`–`00500`;
2. desplegar el bridge con `PRECHECKOUT_DELAYED_OUTBOUND_ENABLED=false`;
3. habilitar worker y binding first-touch para que el pipeline llegue hasta
   `reserved`;
4. comprobar `/ready` y que `request_started` no aumente mientras el gate final
   permanezca apagado;
5. después de la aprobación Meta, activar sólo el gate final mediante una
   operación autorizada y acotada.

La migración preparatoria no constituye la operación del paso 3. Un `502`, timeout
o resultado ambiguo nunca autoriza reintento del POST externo.
