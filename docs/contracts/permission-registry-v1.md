# Contrato: el registro de permiso de WhatsApp (permission-registry-v1)

- **Estado:** M1 de E-624 (E-627, 2026-11-16): las tablas, las RPC y la consulta del núcleo, en la migración `20261116000200_permission_registry_v1.sql`. M2 (E-628, 2026-11-16): los conectores `hotmart_checkout` y `landing` del bridge, `[[permisos.avisos]]` en el manifiesto y el flag `PERMISSION_NOTICE_CONNECTOR_ENABLED`, apagado por defecto (§8). No dice que nada esté desplegado ni activado.
- **Contrato:** `permission-registry-v1`, versión `0.2.0` (0.1.0: M1; 0.2.0: suma §8).
- **Diseño:** E-624 «Permiso de WhatsApp sin formulario: registro de permiso y conectores» (vault del OS, `coordinacion/etapa-1/reportes/E-624.md`, §4, opción A), aprobado en su enfoque por Dan el 2026-11-16: el núcleo solo pregunta si hay un permiso registrado; de dónde salió lo resuelven conectores enchufables (ADR-0021 punto 8).
- **Código:** `supabase/migrations/20261116000200_permission_registry_v1.sql`; validador `tests/sql/followup_engine/validate_permission_registry.mjs`. Conectores: `src/bridge/permission_connectors.py`; pruebas `tests/test_permission_connectors.py`.

## 1. Qué resuelve

El permiso del carrito y del pago rechazado nativos era, hasta esta versión, una propiedad de la intención del formulario (`purchase_intents` con `whatsapp_contact_authorized`) que la correlación ató a **ese** evento de Hotmart. Sin formulario no hay intención y el resultado es `no_entra:permiso`. Un aviso en el checkout de Hotmart o en la landing no deja intención: lo convierte en permiso un conector, que escribe una fila en `permission_grants`. El núcleo consulta esa fila, sin saber de qué fuente salió.

## 2. Las dos tablas

Viven en la base de cada instancia. Las dos tienen RLS y ningún rol de la API las lee ni las escribe directo: todo pasa por las RPC de §3. **Solo se agrega:** nada se borra, un aviso solo se cierra (`valid_until`, de nulo a un valor) y un permiso solo se revoca (`revoked_at` y `revoked_by`) o gana su contacto (`contact_id`, de nulo a un valor). Cualquier otro cambio aborta con `55000 permission_registry_append_only`.

### 2.1 `permission_notices`: el aviso declarado

Lo que una fuente afirma mostrar, medido en la página publicada. Se carga desde el manifiesto de la instancia (`[[permisos.avisos]]`, M2/M4).

| Columna | Qué es |
|---|---|
| `tenant_ref`, `notice_ref` | la instancia y el id estable del aviso (`maribel-forever-young-checkout-v1`); únicos juntos |
| `connector` | quién lo usa: `hotmart_checkout`, `landing`, `ghl_form`, `lead_precheckout`. Texto libre para el núcleo |
| `product_ref` | el producto de la instancia (`flow_controls.product_ref`, el `funnel_ref` del binding) |
| `offer_refs` | las ofertas que cubre; vacío = todo el producto |
| `uses` | `{recuperacion}` en v1; `difusion` solo con una decisión aparte |
| `surface_url` | la URL `https://` donde está el aviso |
| `copy_version`, `notice_text`, `notice_text_sha256` | el texto exacto, su versión y el sha256 del texto en UTF-8 (la base lo verifica). Un texto nuevo es una versión nueva y un `notice_ref` nuevo |
| `checkbox` | `ninguna` · `opcional_sin_premarcar` · `obligatoria` |
| `measured_at`, `measured_by` | la medición de la página publicada que habilita declararlo |
| `valid_from`, `valid_until` | la vigencia. `valid_from` nunca es anterior a `measured_at`. `valid_until` lo pone el monitor o una persona |
| `requires_src` | solo landing: el `origin.src` que tiene que traer un pago rechazado. Lo mira el conector, no el núcleo |

### 2.2 `permission_grants`: el permiso de una persona

La única tabla que lee el núcleo.

| Columna | Qué es |
|---|---|
| `tenant_ref` | la instancia |
| `normalized_phone` | el teléfono canónico (`_whatsapp_phone_canonical`: México sin el 1, Argentina sin el 9) |
| `contact_id` | el contacto, si el conector lo sabe; nulo vale para cualquier contacto con ese móvil |
| `normalized_email` | opcional, en minúsculas |
| `channel` | `whatsapp` |
| `product_ref`, `offer_ref` | el alcance; `offer_ref` nulo = todo el producto (solo si el aviso es de todo el producto) |
| `uses` | los usos, dentro de los del aviso |
| `connector`, `connector_version` | quién lo escribió |
| `evidence_kind` | `envio_persona` (un formulario) o `aviso_en_el_paso` (el evento del proveedor más el aviso declarado de esa oferta) |
| `evidence_ref` | el `precheckout_submission_id` o el `webhook_event_id` del acto. `unique (tenant_ref, connector, evidence_ref)`: Hotmart reenvía y el permiso no se duplica |
| `notice_ref`, `copy_version` | el aviso que lo respalda (llave foránea al aviso) |
| `granted_at` | el momento del acto (el `submitted_at` del formulario o el `creation_date` del evento) |
| `expires_at` | el fin de la ventana para **entrar** (hoy, `granted_at` más el `max_lookback` del scope de la oferta) |
| `revoked_at`, `revoked_by` | la revocación explícita. **La baja no se copia acá:** el núcleo la sigue mirando en `contact_opt_out_events` y en el contacto |

## 3. Las RPC (solo `service_role`, `security definer`)

| RPC | Devuelve | Qué hace |
|---|---|---|
| `register_permission_notice_v1(p_notice jsonb)` | `(notice_id, outcome)` | `inserted`; el mismo aviso otra vez, `unchanged`; otro contenido con el mismo `notice_ref`, `55000 permission_notice_conflict`. Claves desconocidas, hash que no es del texto, vigencia anterior a la medición o un campo inválido: `22023 permission_notice_invalid_arguments` |
| `close_permission_notice_v1(p_tenant_ref, p_notice_ref, p_valid_until)` | `(notice_id, outcome)` | `closed`, `already_closed` o `not_found`. Desde `valid_until` no nacen permisos; los otorgados siguen valiendo |
| `record_permission_grant_v1(p_grant jsonb)` | `(grant_id, outcome, reason)` | Claves: `tenant_ref`, `phone`, `contact_id`, `normalized_email`, `product_ref`, `offer_ref`, `uses`, `connector`, `connector_version`, `evidence_kind`, `evidence_ref`, `notice_ref`, `copy_version`, `granted_at`, `expires_at`. `inserted`; el mismo acto otra vez, `duplicate` con el id que ya estaba; lo que el aviso no cubre, `rejected` sin escribir y con el motivo: `aviso_desconocido`, `aviso_de_otro_conector`, `aviso_de_otro_producto`, `aviso_de_otra_version`, `aviso_no_vigente` (`granted_at` fuera de `[valid_from, valid_until)`), `oferta_fuera_del_aviso`, `uso_fuera_del_aviso`. Un argumento inválido (teléfono sin número, `expires_at <= granted_at`, una clave de más): `22023 permission_grant_invalid_arguments` |
| `revoke_permission_grant_v1(p_grant_id, p_revoked_by)` | `(grant_id, outcome)` | `revoked`, `already_revoked` o `not_found` |

## 4. La pregunta del núcleo

`_registered_permission_reason(p_tenant_ref, p_contact_id, p_destination, p_product_ref, p_offer, p_at, p_use)` → `(reason, permission_grant_id)`. Privada (ningún rol de la API la ejecuta).

`reason` es nulo, con el id del permiso, si:

1. hay un permiso de la instancia, del teléfono canónico del destino, del producto, con `offer_ref` nulo o igual a la oferta, con `p_use` en `uses`, `granted_at <= p_at < expires_at`, sin revocar, y con `contact_id` nulo o igual al contacto (el más nuevo, si hay varios);
2. el contacto tiene ese móvil en `contact_points` y su `contacts.phone` canónico es ese teléfono (el mismo criterio que el formulario: el envío sale a `contacts.phone`);
3. hay un binding activo de la instancia y el producto;
4. no hay una baja de ese móvil en `contact_opt_out_events` de la cuenta de Chatwoot del binding, en cualquiera de sus formas (anterior o posterior al permiso, como con el formulario). Si la hay: `baja`.

Si no: `no_entra:permiso`. Son los motivos de hoy: la página, el Automation Expert y `NATIVE_CART_UNCLAIMED_REASONS` no cambian.

## 5. Dónde la consulta el motor nativo

`_flow_engine_native_registered_audience` envuelve la pregunta para el carrito (`PURCHASE_OUT_OF_SHOPPING_CART`) y el pago rechazado (`PURCHASE_CANCELED`): el evento tiene que ser de Hotmart, de la instancia, del producto del flujo y de esa oferta (la procedencia de `commercial_ally_hotmart_event_bindings`); un cancelado que no es un rechazo da `no_es_un_rechazo`, como en las otras ramas. El uso es `recuperacion` y el momento es el del evento.

En `_flow_engine_admit_native_flow`:

- **Carrito:** primero `_flow_engine_native_cart_audience` (el formulario), intacta. Solo si termina en `no_entra:permiso`, el registro.
- **Pago rechazado:** primero el formulario, después el teléfono de la campaña (`20261112210400`, si el bridge manda la lista y la oferta está en ella). Solo si sigue en `no_entra:permiso` y no entró por el teléfono, el registro.
- La ficha, la intención, la compra y la audiencia no consultan el registro.

Con un permiso registrado, la fila de `flow_engine_native_runs` lleva `audience_permission_grant_id` y la intención y el envío del formulario van nulos (la restricción `flow_engine_native_runs_audience_by_trigger` lo exige así, solo para el carrito y el pago rechazado). La autorización de `contact_authorizations` queda con la evidencia `{"reason": "registered_permission", "permission_grant_id", "webhook_event_id", "offer", "recovery_case_id"}`.

**La puerta antes de cada envío** (`_flow_engine_authorize_native_tail`) mira el permiso otra vez con `_registered_permission_recheck` (con el permiso `FOR SHARE`): revocado, `ya_no_entra`; la baja del contacto o la de Chatwoot de ese móvil, `baja`; el destino que no es el teléfono del permiso, `no_entra:permiso`. El rechazo no consume cupo. `expires_at` no se mira en la puerta: es la ventana para entrar, no para los toques que siguen.

## 6. Lo que no cambia

- Sin filas en `permission_grants`, la admisión es exactamente la de antes.
- Un formulario que da permiso nunca llega a la rama nueva: la intención, su autorización y su recorrido salen como antes, aunque haya un permiso registrado del mismo teléfono.
- `permiso: formulario` sigue siendo el nombre en la definición: la definición del carrito con `permiso: formulario` entra con un permiso registrado sin tocarla. `registrado` como sinónimo es D4 del destino.

## 7. Lo que espera a otros encargos

- **M3:** la skill del Automation Expert y la línea de la página de aprobación («le llega a quien dio permiso para WhatsApp: formulario, aviso en el checkout de <oferta> desde <fecha>»).
- **M4:** el PR de cada instancia que declara su aviso (`[[permisos.avisos]]`, con la medición de la página publicada).
- **Destino (D1-D5 de E-624):** el formulario como conector más, el relleno del histórico, las audiencias que leen solo el registro, `permiso: registrado` y el monitor diario del aviso.

## 8. Los conectores del aviso: `hotmart_checkout` y `landing` (0.2.0, E-628)

Código: `src/bridge/permission_connectors.py`. Solo con `PERMISSION_NOTICE_CONNECTOR_ENABLED=true` (apagado por defecto: no se carga ningún aviso, no se escribe ningún permiso y `/ready` no cambia).

**El aviso.** Sale de `[[permisos.avisos]]` del manifiesto de la instancia (`docs/referencia-manifiesto.md`). Al arrancar, antes del primer evento, el bridge carga cada aviso con `register_permission_notice_v1` (el sha256 del texto en UTF-8) y cierra con `close_permission_notice_v1` el que el manifiesto cierra (`vigente_hasta`). Si la base no lo acepta (no responde, o `permission_notice_conflict`: otro texto con el mismo `ref`), lo reintenta antes de cada evento con aviso y, mientras tanto, no escribe ningún permiso. `ventana_horas` es la ventana para entrar de cada permiso: el `max_lookback` del scope de la oferta, declarado en el manifiesto porque el bridge no lee `hotmart_purchase_intent_scopes` (el rol del bridge no tiene acceso).

**Cuándo corre.** En la resolución del evento, solo cuando el carrito (`PURCHASE_OUT_OF_SHOPPING_CART`) o el pago rechazado (`PURCHASE_CANCELED`) van a la admisión nativa (un flujo nativo de ese producto prendido o en cohorte, la identidad portable de la instancia), **inmediatamente antes** de `admit_flow_engine_event_v1`, para que `_registered_permission_reason` lo encuentre. La ficha, la compra y la intención no pasan por acá.

**Qué escribe.** A lo sumo un permiso por evento, con `record_permission_grant_v1`:

| Clave | Valor |
|---|---|
| `tenant_ref` | el de la instancia |
| `phone` | el teléfono del evento (`buyer.phone` o `buyer.checkout_phone`); la base lo pasa a canónico |
| `contact_id` | el contacto que la resolución acaba de resolver o crear |
| `normalized_email` | el mail del evento, si viene |
| `product_ref` | el producto de la admisión nativa (el `funnel_ref` del binding del producto) |
| `offer_ref` | la oferta del evento |
| `uses` | `["recuperacion"]` |
| `connector`, `connector_version` | el del aviso elegido, `v1` |
| `evidence_kind`, `evidence_ref` | `aviso_en_el_paso`, el `webhook_event_id` |
| `notice_ref`, `copy_version` | los del aviso elegido |
| `granted_at` | el `creation_date` del evento |
| `expires_at` | `granted_at` + `ventana_horas` |

**El aviso elegido.** Del producto, con la oferta en `ofertas` (o sin ofertas, en un aviso de checkout), vigente al `creation_date` y con `recuperacion`. Si hay de los dos conectores, el de `hotmart_checkout`; dentro de uno, el de `vigente_desde` más nuevo. Un aviso de `landing` con `requiere_src` no respalda un pago rechazado cuyo `data.purchase.origin` no traiga ese valor en `src` ni en `sck`; el carrito no trae `origin` y no se le exige. Que la oferta sea **exclusiva** de la landing no lo puede ver el bridge: lo afirma el manifiesto (`oferta_exclusiva = true`, obligatoria en un aviso de landing), y una oferta en avisos de dos landings distintas no carga.

**Lo que no escribe.** Un evento sin teléfono (sin teléfono no hay a quién escribirle), de una oferta sin aviso, anterior a `vigente_desde` o posterior a `vigente_hasta`. Lo anota en el log (`permission_grant_skipped` con el motivo: `sin_telefono`, `sin_aviso`, `src_distinto`).

**Lo que contesta la base.** `inserted` y `duplicate` (Hotmart reenvía) quedan en el log (`permission_grant_recorded`); `rejected` con su motivo, como `warning` (`permission_grant_rejected`). Ni un `rejected` ni una falla de la base frenan el evento: sigue a la admisión, que sin permiso termina en `no_entra:permiso`, como antes. Ninguna línea lleva teléfono, mail ni nombre.

**`/ready`.** Con el flag, `permission_notices`: `<n>-activos` (los avisos del manifiesto vigentes ahora) o `sin_cargar` si la base todavía no los aceptó. Nunca responde 503 por esto.
