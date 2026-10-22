# Changelog

Cada versión dice qué cambia y **qué tiene que hacer quien actualiza una instancia**: nada, correr una migración o sumar un campo al manifiesto. Las versiones siguen [SemVer](https://semver.org/lang/es/) con el criterio de [ADR-0021](docs/decisions/0021-setter-producto-instalable.md):

- **Mayor:** la instancia tiene que cambiar algo para actualizar (un campo obligatorio nuevo en el manifiesto, una migración con paso manual).
- **Menor:** funcionalidad nueva, apagada por defecto.
- **Parche:** arreglos.

Cada tag `vX.Y.Z` publica tres imágenes en GHCR con ese tag: `ghcr.io/danschwab2002/setter-bridge`, `setter-slack-connector` y `setter-daily-feedback`. La release de GitHub lleva el digest de cada una. Cómo se pasa un servicio a la imagen y cómo se vuelve atrás: [docs/operations/release-por-imagen-v1.md](docs/operations/release-por-imagen-v1.md).

## [Sin publicar]

Lo que ya está en `main` y todavía no tiene versión. El número se elige al publicar.

## [1.16.1] - 2026-10-22

Un parche sobre la 1.16.0, con tres arreglos que salieron al usarla en ATT1: el comando del operador confirma con una terminal real (#300), la historia del motor entra entera en la simulación (#301) y `/flows` del Automation Expert deja de pasarse del tiempo (#302). Lleva dos migraciones, `20261021000300` y `20261021000400`, que no cambian nada de lo que se manda a los leads.

### Arreglado

- **El comando del operador no podía pedir el código de confirmación** (#300, `scripts/flow_change_operator.py`). Abría `/dev/tty` en modo `r+`, que una terminal no admite, y lo leía como «sin terminal»: `publicar`, `prender` y el resto frenaban con `sin terminal no se confirma` aunque se corrieran con `ssh -t`. Ahora la terminal se abre dos veces, una para escribir y otra para leer. La prueba corre la confirmación en un proceso hijo con una terminal de verdad (`pty`).
- **La simulación de la vista previa salía siempre `failed`** (#301, migración `20261021000300`). `read_flow_engine_history_v1` cortaba en 1.000.000 bytes, y la semana de ATT1 pesa más: con la ventana de 8 días devolvía `flow_engine_history_too_large`. El tope pasa a 4.000.000 bytes, debajo de los 4 MiB con los que el bridge corta la respuesta. Medido en ATT1 el 22/10: la semana pesa 1.804.557 bytes y se lee en 1,4 s.
- **`/flows` del Automation Expert daba `statement timeout`** (#302, migración `20261021000400`). `read_automation_expert_flows_v1` tardaba más que los 8 s del bridge (SQLSTATE `57014`, HTTP 500 o 503), porque leía las reevaluaciones de la ventana una vez por acción y por flujo. Ahora las lee una sola vez. Medido en ATT1 desde el bridge: de ≈8 s a 0,3–0,4 s.

### Qué hace quien actualiza

- **Correr las dos migraciones, en orden y como `postgres`:** `20261021000300` y después `20261021000400`. Cambian solo dos funciones de lectura. En ATT1 ya están aplicadas (22/10, 00:42Z y 13:35Z).
- **Pasar el bridge y la revisión diaria a las imágenes 1.16.1** (`setter-bridge` y `setter-daily-feedback`). Sin flags, variables ni campos nuevos en el manifiesto.
- **El comando del operador ya no necesita `PRODUCTO=`** apuntando a un árbol con el arreglo de #300: el árbol de la 1.16.1 lo trae.

## [1.16.0] - 2026-10-21

Una versión menor sobre la 1.15.5. El Automation Expert puede proponer y validar un cambio de flujo (la T4: #288, #290, #296, #297 y #299), y dibuja los flujos (#295). La base suma las rutas, las lecturas y la guarda del motor de flujos (la T2.3a, #294), sobre el esquema de varios toques y la captura v3 (#287, #291 y #292). El instalador de Hermes instala un profile de Slack y se acota a uno (#289 y #293). Todo nace apagado. Lleva dos migraciones, `20261021000100` y `20261021000200`, que aplicadas no cambian nada de lo que corre.

### Agregado

- **El comando del operador del almacén de flujos** (#288, `scripts/flow_change_operator.py`). Corre en el VPS como `piloto.sh` y lee la base y el catálogo de plantillas de Chatwoot con `psql`. Con él se listan y se ven las propuestas, se arma una propuesta propia (`proponer`, `volver`, `prender` y `apagar`), se valida, y se aprueba y publica con un código leído de la terminal. También se pausa un flujo y se descarta una propuesta. El validador corre en un contenedor nuevo de la imagen del bridge, fijada por digest, sin red y de solo lectura. Publicar sin simulación exige `--sin-simulacion`.
- **El permiso de base del Automation Expert** (#290, migración `20261021000100`). El rol `automation_expert_change` (`NOLOGIN NOINHERIT`, lo asume PostgREST por un JWT) ejecuta solo cuatro funciones: leer el almacén, guardar un borrador, registrar una validación y releer una propuesta. No puede aprobar, publicar, pausar ni descartar. Topes: 10 propuestas abiertas, 100 revisiones y 100 validaciones por propuesta. Aprobar registra la simulación que quedó congelada en la vista previa (`built` con su `summary_sha256`, `failed` con el motivo, o `not_built`).
- **Las herramientas de cambio del Automation Expert** (#296). Las rutas del bridge bajo `/internal/automation-expert/v1/` están detrás de `AUTOMATION_EXPERT_CHANGE_ENABLED`, apagado por defecto:
  - `GET /flow-store`;
  - `POST /flow-changes`;
  - `POST /flow-changes/{id}/validate`;
  - `GET /flow-changes/{id}`.

  El plugin `automation-expert-change` suma cuatro herramientas en Slack: `get_flow_definitions`, `propose_flow_change`, `validate_flow_change` y `get_flow_change`. Quién pide el cambio lo pone el plugin, con el ID de Slack de la sesión. El agente propone y valida; nunca aprueba, publica ni pausa.
- **La simulación en la vista previa** (#297). Validar lee el motor (`read_flow_engine_v1`) y la historia de los últimos 8 días (`read_flow_engine_history_v1`), y simula la versión vigente contra la propuesta. El resultado queda congelado en `validation.preview.simulacion`. Si la simulación no se puede armar, la propuesta se valida igual y la vista previa dice por qué.
- **Registrar un flujo y crear su ruta desde el comando del operador** (#299). `registrar <producto>/<flujo>` lo deja en el almacén apagado y sin versión. `ruta <producto>/<flujo> --tipo carrito|pago_fallido|ficha --scope <scope del legado>` le crea la ruta en `legado`. Desde la ruta, pausar o apagar el flujo en el almacén frena lo que manda el legado, aunque una versión nueva no cambia ningún envío. Por eso `ruta` exige el flujo prendido y con versión: sobre un flujo apagado, el legado se frenaría al instante. `estado` muestra la ruta de cada flujo. La simulación de la vista previa necesita la ruta del carrito del producto, porque lee la historia por ella.
- **El motor de flujos en la base, apagado** (#294, migración `20261021000200`, contrato `docs/contracts/flow-engine-v1.md` de #287). Las tablas de rutas, corridas y autorizaciones; las lecturas `read_flow_engine_v1` y `read_flow_engine_history_v1`; las rutas `operator_create_flow_route_v1` y `operator_route_flow_v1` (a `motor` contestan `flow_route_kind_not_supported` hasta la T2.3b); la política compilada en SQL, gemela de `flow_policy.compile_policy`; y la guarda del almacén al principio de los envoltorios y los `mark_*` del carrito, el pago fallido y la ficha. Sin filas en `flow_engine_routes`, la guarda no frena nada y la bolsa suma cero.
- **El esquema de varios toques** (#291, `setter-flujo/v1` 1.1): hasta tres envíos por camino, `cupon: personal` y las variables `cuando`, `precio` y `vence`. La v7 del carrito de ATT1 compila igual a su fila real. **La captura v3 y la paridad del carrito de tres toques** (#292): sobre la captura real de ATT1, 83 envíos, ninguno antes de tiempo ni sin explicar.
- **El Automation Expert dibuja los flujos** (#295, `draw_flow` en el toolset `automation_expert_read`): una página HTML con el lienzo, armada desde la misma lectura que `list_flows`, adjunta en Slack. Necesita el permiso `files:write` en la app de Slack.
- **El instalador de Hermes instala un profile de Slack** (#289), con plugins y herramientas exactas, y **se acota a uno con `--solo PROFILE`** (#293) en `plan`, `aplicar` y `chequear`. No cuenta como diferencia lo que Hermes anota solo en el `config.yaml` (`onboarding.seen`, #295).

### Qué hace quien actualiza

- **Correr las dos migraciones, en orden y como `postgres`:** `20261021000100` (crea el rol `automation_expert_change` si no existe; si existe con otros atributos, falla cerrada) y después `20261021000200`. Aplicadas, no cambian nada de lo que corre.
- **Pasar el bridge a la imagen 1.16.0.** Sin campos nuevos en el manifiesto.
- **Para prender las herramientas de cambio del Automation Expert** (opcional), el bridge necesita estas variables:
  - `AUTOMATION_EXPERT_CHANGE_ENABLED=true`. Exige además `AUTOMATION_EXPERT_READ_ENABLED=true`, el manifiesto de la instancia y `SUPABASE_BASE_URL`.
  - `AUTOMATION_EXPERT_CHANGE_TOKEN`: 32 caracteres o más, distinto de todo otro valor del bridge.
  - `AUTOMATION_EXPERT_CHANGE_DB_KEY`: un JWT con `role: automation_expert_change`, firmado con el secreto JWT de la base, distinto de la clave de `service_role`.

  El profile del Automation Expert lleva `AUTOMATION_EXPERT_CHANGE_API_TOKEN`, con el mismo valor que `AUTOMATION_EXPERT_CHANGE_TOKEN`. Si falta alguna de estas condiciones, el bridge no arranca y dice cuál. Con el flag prendido, `/ready` muestra `"automation_expert_change": "enabled"`.
- **Sin el flag**, las rutas no existen y el plugin del Automation Expert contesta error.

## [1.15.5] - 2026-10-21

Un parche sobre la 1.15.4: los pasos siguientes (A2 y Z del carrito, B2 o Z del pago rechazado, C3 de la ficha) verifican Chatwoot contra el teléfono del caso (#286). Cambia el bridge y no lleva migración.

### Arreglado

- **Ningún paso siguiente salía en una instancia portable.** Antes de mandar un paso que no es el primero, el despachador lee la conversación del caso en Chatwoot y se la pasa como evidencia a `reevaluate_followup_action`. Esa lectura comparaba la conversación con el único JID que deja pasar un runtime viejo (`allowed_jid`), así que para cualquier comprador real fallaba con `conversation_identity_mismatch`, la evidencia quedaba vacía y la reevaluación escalaba con `chatwoot_authority_unavailable`. En ATT1, los 17 A2 que vencieron entre el 2026-10-20 a las 17:40Z y el 2026-10-21 a las 02:39Z cerraron así, sin mensaje. Ahora, con el destinatario portable prendido, se compara con el teléfono del contacto del caso, en sus dos formas equivalentes (`52` + 10 y `521` + 10 para México), la canónica primero. Sin teléfono, o en un runtime sin el destinatario portable (Johanna), la verificación queda como estaba y falla cerrada. Prueba con la conversación real capturada ese día: `tests/fixtures/chatwoot/conversation_a2_att1_2026-10-21.json`.

### Qué hace quien actualiza

- **Pasar el bridge a la imagen 1.15.5.** Sin migración ni cambios en el manifiesto. Los pasos que ya cerraron con `chatwoot_authority_unavailable` no vuelven: el arreglo vale para los que vencen desde la subida.

## [1.15.4] - 2026-10-20

Un parche sobre la 1.15.3: el despachador acepta la decisión `defer` de la reevaluación (#285). Cambia el bridge y no lleva migración.

### Arreglado

- **Una acción que vence con la ventana cerrada ya no corta la vuelta del despachador.** `reevaluate_followup_action` decide `defer` (`business_window_closed`) cuando una acción vence fuera de la ventana de su política, y la deja ella misma en `deferred` con `due_at` en la próxima apertura. El cliente aceptaba solo `execute`, `cancel`, `pause`, `expire` y `escalate`: cada `defer` tiraba `reevaluate_followup_action_invalid_row` y cortaba la vuelta entera de `dispatch_due`, así que las otras acciones reclamadas en esa vuelta esperaban a que venciera su lease. En ATT1 pasó unas 32 veces en 12 h desde el 2026-10-20 a las 03Z, siempre fuera de 09 a 21 de Ciudad de México. Ahora `defer` es una decisión válida y la vuelta sigue con la acción siguiente.

### Qué hace quien actualiza

- **Pasar el bridge a la imagen 1.15.4.** Sin migración ni cambios en el manifiesto.

## [1.15.3] - 2026-10-20

Un parche sobre la 1.15.2: el cupón personal vuelve a quedar `valid` (#284). Cambia el bridge y no lleva migración.

### Arreglado

- **Un cupón `valid` de Hotmart sin la clave `active` cuenta como válido.** El worker del cupón exigía `active: true` al verificarlo, pero la búsqueda por código de Hotmart (`GET /products/api/v1/coupon/product/{id}?code=…`) no manda esa clave: ningún cupón pasaba de `created` a `valid`, el worker los volvía a mirar cada minuto, y A1, Z del carrito y los mensajes con cupón del pago rechazado salían sin descuento o no salían. En ATT1, los 20 cupones del carrito del 19 al 20/10 quedaron en `created`, con hasta 576 verificaciones. Ahora solo un `active: false` explícito descarta un cupón `valid`. La respuesta real quedó capturada en `tests/fixtures/hotmart/coupon_find_att1_2026-10-20.json`, y el Hotmart simulado de las pruebas responde con esa forma.

### Qué hace quien actualiza

- **Pasar el bridge a la imagen 1.15.3.** Sin migración ni cambios en el manifiesto. Los cupones que quedaron en `created` y siguen vigentes pasan a `valid` en la próxima verificación.

## [1.15.2] - 2026-10-20

Un parche sobre la 1.15.1: los mensajes con el link de recuperación vuelven a poder salir (#282). Lleva una migración y no cambia el bridge.

### Arreglado

- **El link de recuperación se autoriza con el envío ya iniciado.** El bridge pide la autorización del link después de marcar el inicio del envío, a propósito, y `authorize_recovery_checkout_issuance_v1` solo la daba con el intento todavía en `reserved`: todo mensaje con link de recuperación (A1 con cupón, B1 con link, Z del carrito, B2 y Z del pago rechazado) terminaba en `delivery_unknown` con `recovery_checkout_blocked_attempt` y no salía. Lo mostró el primer B1 real de ATT1, el 2026-10-20 a las 02:07Z. Ahora la autorización acepta el intento en `reserved` o en `request_started`, con el mismo cerco de la lease.

### Qué hace quien actualiza

- **Correr la migración `20261020000100_recovery_checkout_authorize_started_attempt_v1.sql`.** Redefine una sola función, `authorize_recovery_checkout_issuance_v1`, desde su definición viva con un reemplazo contado (si el texto no coincide, aborta sin cambiar nada). Sin paso manual y sin cambios de permisos. Cambia desde que se aplica.
- **El bridge no cambia:** la imagen 1.15.0 anda sobre la base migrada.
- Los intentos que ya quedaron en `delivery_unknown` por este motivo no se reabren.

## [1.15.1] - 2026-10-19

Un parche sobre la 1.15.0: el recordatorio del vencimiento de la ficha ya no se cancela porque la persona conteste (#280). Lleva una migración y no cambia el bridge.

### Arreglado

- **El recordatorio de la ficha sobrevive a una respuesta.** Hasta acá, si la persona escribía después del toque anterior, el recordatorio del vencimiento (C2 o C3) se cancelaba con `prospect_replied`. Ahora sale igual el día que le toca, como pide la especificación: es utilidad sobre un pago que la persona generó, y el agente toma la respuesta igual. Lo siguen frenando la baja (botón o frase), la compra de cualquier oferta y una persona del equipo que escribe o toma la conversación. En cualquier otro flujo, una respuesta sigue cancelando el seguimiento. Contrato: [docs/contracts/portable-payment-slip-reminders-v1.md](docs/contracts/portable-payment-slip-reminders-v1.md).

### Qué hace quien actualiza

- **Correr la migración `20261019000800_portable_payment_slip_reminder_survives_reply_v1.sql`.** Redefine una sola función, `reevaluate_followup_action`, desde su definición viva con un reemplazo contado (si el texto no coincide, aborta sin cambiar nada). Sin paso manual y sin cambios de permisos. Con la ficha prendida, cambia desde que se aplica.
- **El bridge no cambia:** la imagen 1.15.0 anda sobre la base migrada.

## [1.15.0] - 2026-10-19

Una versión menor sobre la 1.14.0 con el seguimiento del pago rechazado (#278), apagado por defecto: B2 o Z, 24 h después de B1, y nada más. Lleva una migración.

### Agregado

- **El seguimiento del pago rechazado.** A quien no compra ni contesta después de B1 le llega un solo mensaje 24 h después: B2 (`WABA_PAYMENT_FAILURE_TEMPLATE_SECOND_TOUCH`) si B1 salió sin cupón, o Z (`WABA_PAYMENT_FAILURE_TEMPLATE_LAST_TOUCH`) si salió con cupón. Las dos llevan el cupón 2 del caso y el link de recuperación; sin el cupón o sin el link, el seguimiento cierra sin mensaje. Contrato: [docs/contracts/portable-payment-failure-followup-v1.md](docs/contracts/portable-payment-failure-followup-v1.md).
- **B1 registra qué variante salió** (`payment_failure_first_touch_records`), aunque el seguimiento esté apagado. Si el registro falla, B1 sale igual y queda en el log `payment_failure_first_touch_record_failed`.

### Arreglado

- **Un segundo rechazo con un seguimiento pendiente** ya no lo pausa con `authoritative_state_changed`: la versión esperada de los `no_reply_review` pendientes acompaña al caso, sin mover su `due_at`.
- **«Quiero mi 10 %» adelanta solo Z del carrito** (`cart_last_touch`), no cualquier paso con cupón de la conversación.

### Qué hace quien actualiza

- **Correr la migración `20261019000700_payment_failure_followup_v1.sql`.** Suma la tabla `payment_failure_first_touch_records` y dos funciones privadas (solo `service_role`), y redefine tres funciones desde su definición viva con reemplazos contados (si el texto no coincide, aborta sin cambiar nada). Sin paso manual. La ACL pasa de 145 a 147 entradas.
- **Para prender el seguimiento** (opcional, con B1 prendido): publicar una política del pago rechazado con el paso `payment_failure_followup` y apuntarle el scope, y después `PAYMENT_FAILURE_FOLLOWUP_ENABLED=true` con las dos plantillas; el bridge no arranca si falta alguna, o si falta B1 o el worker del cupón. Apagado, nada cambia salvo el registro de la variante de B1.

## [1.14.0] - 2026-10-19

Una versión menor sobre la 1.13.1 con A2 y Z del carrito (#276), apagados por defecto, y la base genérica de los pasos con plantilla y cupón, que reusa el pago rechazado. Lleva una migración.

### Agregado

- **A2 y Z del carrito.** Dos toques después de A1, con plantillas aprobadas: A2 (`cart_second_touch`) sin link, y Z (`cart_last_touch`) con el cupón 2 del caso, el link de recuperación y su vencimiento real. Sin el cupón o sin el link, Z cierra sin mensaje. Contrato: [docs/contracts/portable-cart-followups-v1.md](docs/contracts/portable-cart-followups-v1.md).
- **«Quiero mi 10 %» adelanta Z.** El webhook entrante reconoce la respuesta exacta del botón de A2 y pide la renovación (`request_recovery_renewal_v1`): Z sale en ese momento y el agente no corre. Sin Z pendiente, el mensaje sigue el camino de siempre.
- **Los pasos de una política pueden declarar dos marcas** (migración `20261019000600`):
  - `"approved_template": true`: la reevaluación no pausa el paso por la ventana de 24 h cerrada ni por una conversación resuelta, la adopción de una respuesta no lo cuenta como pendiente y un carrito nuevo no lo adelanta;
  - `"coupon_ordinal": 1 | 2`: se siembra el cupón de ese ordinal para el paso y se emite su link.
  Una política sin marcas se comporta como antes.

### Qué hace quien actualiza

- **Correr la migración `20261019000600_recovery_followup_steps_v1.sql`.** Suma tres funciones privadas (solo `service_role`) y la tabla `recovery_renewal_requests`, y redefine cinco funciones desde su definición viva con reemplazos contados (si el texto no coincide, aborta sin cambiar nada). Sin paso manual. La ACL pasa de 142 a 145 entradas.
- **Para prender A2 y Z** (opcional): `RECOVERY_CART_FOLLOWUPS_ENABLED=true` con al menos una de `RECOVERY_CART_SECOND_TOUCH_TEMPLATE_NAME` (A2) y `RECOVERY_CART_LAST_TOUCH_TEMPLATE_NAME` (Z), que exigen `RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED` (y Z, `RECOVERY_COUPON_WORKER_ENABLED`); el bridge no arranca si falta alguna. Además, publicar una política del carrito con los pasos y sus marcas. Apagado, el bridge cierra los dos pasos como hasta ahora.
- Si en la instancia el pago fallido todavía usa la política del carrito (`PAYMENT_FAILURE_VARIANTS_ENABLED` apagado), la política de varios toques del carrito se activa después de prender ese flag: si no, un pago fallido recibe A2.

## [1.13.1] - 2026-10-19

Un parche sobre la 1.13.0 (#274): B1-cupón, la variante del pago rechazado que nombra el cupón de otro caso, solo usa un cupón que le llegó a la persona. Lleva una migración y no cambia el bridge.

### Arreglado

- **B1 no le habla a la persona de un cupón que nunca recibió.** El worker del cupón (`RECOVERY_COUPON_WORKER_ENABLED`, que B1 exige) crea el cupón de cada carrito unos 15 minutos antes de A1, aunque A1 salga sin cupón (`RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED` apagado, o el cupón no estuvo listo a tiempo). B1 tomaba cualquier cupón `valid` de otro caso del producto como si fuera de la persona.
  - Desde esta versión, un cupón de carrito cuenta solo si su caso tiene el link de A1 (`recovery_first_touch`) aceptado por Chatwoot después de que el cupón quedó `valid`. Una entrega desconocida no cuenta.
  - La regla vive en `recovery_coupon_was_delivered(uuid)`, una función privada, y la usan `payment_failure_wants_first_coupon` (la siembra del cupón de B1-saldo) y `get_portable_payment_failure_send_context_v1` (la variante de B1).
  - Sin el cupón del carrito, B1 elige otra variante y B1-saldo pide su propio cupón, como si la persona no tuviera ninguno.

### Qué hace quien actualiza

- **Correr la migración `20261019000500_payment_failure_other_coupon_delivered_v1.sql`.** Suma una función y parcha dos en el lugar, con reemplazos contados (si el texto no coincide, aborta sin cambiar nada). Sin paso manual. Las imágenes no cambian de comportamiento: no hace falta redesplegar.
- Si la instancia va a prender B1 (`PAYMENT_FAILURE_VARIANTS_ENABLED`), esta migración va antes.

## [1.13.0] - 2026-10-19

Una versión menor sobre la 1.12.0 con C2 y C3 de la ficha de pago (#272), apagados por defecto, y el corte por la compra de cualquier oferta del producto (#268), que rige con la migración. Lleva dos migraciones.

### Agregado

- **C2 y C3, los recordatorios del vencimiento de la ficha** ([portable-payment-slip-reminders-v1.md](docs/contracts/portable-payment-slip-reminders-v1.md)). Después de C1, la ficha recibe `att1_ficha_vence_manana_01` el día anterior al vencimiento a las 10:00 de su país y `att1_ficha_vence_hoy_01` el día del vencimiento a las 09:00, en la conversación de C1 y con la ficha en el botón. Son pasos de la política del scope de la ficha (`payment_slip_due_eve` y `payment_slip_due_day`): el motor inserta cada uno al aceptarse el anterior, y el bridge lo hace esperar, con un solo diferimiento, hasta su momento; si ese día ya pasó, no sale (`payment_slip_step_late`). Cortan la compra de cualquier oferta, la baja, la derivación y, en esta versión, también que la persona conteste (`prospect_replied`).
  - Es el primer paso siguiente que manda el modo directo (`DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`): el flujo de un `no_reply_review` sale de su `step_key`, y `send_followup` acepta la plantilla armada, con su botón. El seguimiento sale como el AgentBot, como los otros seguimientos.

### Cambiado (con la migración, con o sin flags)

- **La compra de cualquier oferta del producto corta el toque** (#268). En un caso portable (carrito, pago rechazado, ficha), `reevaluate_followup_action` cancela el toque con `purchase_detected_any_offer` si la misma persona compró el producto por cualquier oferta, con cualquier resultado de correlación y sin ventana, cruzando el mail exacto o el teléfono en sus formas de WhatsApp. El caso queda `cancelled`, no `won`.
  - El helper nuevo `_portable_case_purchase_stop_reason(uuid)` es security definer y solo de service_role (ACL en 142): la reevaluación es security invoker y service_role no lee las tablas de la compra portable.
  - No cambia: el freno al llegar la compra (`apply_hotmart_purchase_approved` sigue ganando el caso de la misma oferta), el bridge, Johanna ni el precheckout.
  - El simulador (`flow_replay.py`) corta con la misma regla.

### Qué hace quien actualiza

- **Correr la migración `20261019000300_portable_purchase_stops_any_offer_v1.sql`.** Suma una función y parcha `reevaluate_followup_action` en el lugar. Sin paso manual.
- **Correr la migración `20261019000400_portable_payment_slip_reminders_v1.sql`.** Cuatro funciones reconocen el recordatorio de la ficha (un `no_reply_review` de una secuencia `payment_slip`): las dos RPC de la ficha lo aceptan, `reevaluate_followup_action` no lo pausa por la ventana de 24 h ni por una conversación resuelta (es una plantilla aprobada), y la adopción de la respuesta (`admit_portable_inbound_commercial_case_v1`) no lo cuenta como paso pendiente, así el agente toma a quien contesta C1. Ningún otro seguimiento cambia. Sin paso manual y sin efecto mientras la política tenga un solo paso.
- Para prenderlos: declarar `[plantillas].ficha_vence_manana` y `ficha_vence_hoy` en el manifiesto, cargar las cuatro `WABA_PAYMENT_SLIP_DUE_*` ([.env.example](.env.example)) y publicar una política de la ficha con los dos pasos (y un scope que la use). Sin la política nueva, nada cambia.

## [1.12.0] - 2026-10-19

Una versión menor sobre la 1.11.0 con B1 del pago rechazado (#269), apagado por defecto. La migración cambia dos cosas del pago fallido aunque el flag esté apagado (ver «Qué hace quien actualiza»).

### Agregado

- **B1, el primer toque del pago rechazado en cuatro variantes** ([portable-payment-failure-variants-v1.md](docs/contracts/portable-payment-failure-variants-v1.md)). Con `PAYMENT_FAILURE_VARIANTS_ENABLED`, un `PURCHASE_CANCELED` que es un rechazo planifica **un** mensaje a la gracia de su propia política (en ATT1, 15 minutos) contada desde el **último** rechazo, con su propio scope y su propia política. Sale la primera variante que corresponde: cupón vigente de otro caso, dos rechazos, saldo insuficiente (con su cupón de 24 h) o banco. El botón lleva el link de recuperación (`reserve_recovery_checkout_issuance_v1` acepta ahora `anchor_type = payment_failure`), con `&offDiscount=` cuando hay cupón. Si el cupón o el link fallan, sale la plantilla de hoy (`WABA_PAYMENT_FAILURE_TEMPLATE_NAME`): B1 nunca se pierde.
  - El tipo de rechazo lo decide la base (`payment_failure_refusal_kind`): sobre los 17 cancelados de ATT1 del 11 al 17/10, 9 de banco, 3 de saldo y 5 que no son un rechazo.
  - `/ready` publica `portable_payment_failure_own_scope` con el flag prendido.

### Cambiado (con la migración, con o sin el flag)

- **Un caso de pago fallido por persona y producto.** Un rechazo que llega hasta 72 h después de un caso que ya mandó su mensaje, compró o tomó una persona se suma a ese caso y no abre otro: no sale un segundo mensaje (en ATT1, la persona 2 recibió dos). Un segundo rechazo con el mensaje pendiente lo corre a su propia gracia en vez de adelantarlo.
- **Un cancelado que no es un rechazo no se planifica** (sin `refusal_reason` y con un medio que no es tarjeta ni PayPal: Mercado Pago, transferencia, Nequi). Queda `failed` con `payment_failure_not_a_refusal` y no recibe el mensaje de «rechazado».

### Qué hace quien actualiza

- **Correr la migración `20261019000200_portable_payment_failure_variants_v1.sql`.** Suma cuatro funciones y parcha en el lugar cuatro (`plan_payment_failure_recovery`, `plan_portable_payment_failure_recovery`, `claim_due_recovery_coupons_v1`, `reserve_recovery_checkout_issuance_v1`). Sin paso manual.
- Sin el flag, el bridge no lee nada nuevo. Para prenderlo, en este orden: publicar la política y el scope del pago rechazado en la base, armar el scope y recién ahí cargar `PAYMENT_FAILURE_VARIANTS_ENABLED`, `LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE_KEY`/`_VERSION`, `PAYMENT_FAILURE_FOLLOWUP_POLICY_KEY`/`_VERSION` y las cuatro `WABA_PAYMENT_FAILURE_TEMPLATE_*` ([.env.example](.env.example)). Exige `RECOVERY_COUPON_WORKER_ENABLED`, que también siembra el cupón de A1 de cada carrito. Con el flag y algo faltante, el bridge no arranca y dice qué falta.

## [1.11.0] - 2026-10-19

Una versión menor sobre la 1.10.2 con C1 de la ficha de pago en efectivo (#266), apagada por defecto.

### Agregado

- **El primer mensaje a quien generó una ficha de pago en efectivo** (C1 de la ficha de OXXO, [portable-payment-slip-first-contact-v1.md](docs/contracts/portable-payment-slip-first-contact-v1.md)). Hasta acá, `PURCHASE_BILLET_PRINTED` se guardaba y nadie le escribía: en ATT1, 13 fichas desde el 2026-10-12, todas `failed` con `unsupported_persisted_event_type`. Con `PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED`, la ficha se admite y se cruza con la intención del formulario (`admit_portable_hotmart_payment_slip_v2`), el worker la filtra por la tabla `[[ficha.medios]]` del manifiesto y la planifica con **su propio scope y su propia política** (no los del carrito ni los del pago fallido). El mensaje es la plantilla `[plantillas].ficha` con cuatro variables (nombre, producto, tienda y vencimiento, «jueves 22 de octubre») y el botón con el link de la ficha.
  - **El código es global y lo que se prende es una fila:** medio, país, tienda, zona horaria, de qué dígitos de la referencia sale el vencimiento (en ATT1, los 19 a 26 de los 34 de OXXO, medidos sobre las 5 fichas de México) y la base del botón. Lo que no matchea una fila habilitada queda `ignored` con su motivo, sin mensaje.
  - **Una ficha ya vencida al componer no se manda** (`payment_slip_expired`).
  - **El primer contacto declara el botón de URL que espera** (`parse_approved_template(url_button=…)`): el del carrito sigue en `https://pay.hotmart.com/{{1}}` y no cambia.
  - Las cuatro funciones del pago fallido no cambian: las de la ficha se derivan de su definición viva, y el validador comprueba el md5 de 13 funciones de producción antes y después.
  - Con un caso de ficha abierto, un carrito posterior de la misma persona termina `failed` con `pilot_case_binding_conflict`, sin mensaje: un pendiente abierto bloquea el carrito.

### Qué hace quien actualiza

- **Correr la migración `20261019000100_portable_payment_slip_first_contact_v1.sql`.** Amplía cuatro CHECK (toma `ACCESS EXCLUSIVE` un momento: aplicarla con poco tráfico) y suma la tabla `commercial_ally_payment_slip_details` y cinco funciones.
- Sin el flag no cambia nada: la ficha se sigue guardando y descartando como hoy. El manifiesto no necesita cambios; `flujos.ficha`, `ficha_impresa`, `[plantillas].ficha` y `[[ficha.medios]]` son opcionales ([referencia-manifiesto.md](docs/referencia-manifiesto.md)).
- Para prenderla: publicar la política y el scope de la ficha en la base, declarar la ficha en el manifiesto y cargar `PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED`, `LANCEMOS_PILOT_PAYMENT_SLIP_SCOPE_KEY`/`_VERSION`, `PAYMENT_SLIP_FOLLOWUP_POLICY_KEY`/`_VERSION` y `WABA_PAYMENT_SLIP_TEMPLATE_NAME`/`_CATEGORY` ([.env.example](.env.example)). Con el flag y algo faltante, el bridge no arranca y dice qué falta.

## [1.10.2] - 2026-10-19

Un parche sobre la 1.10.1 con un solo arreglo, LAN-132.

### Corregido

- **Quien se dio de baja y vuelve a escribir se deriva al equipo** (LAN-132). Hasta acá, con la baja durable el bridge no contestaba y la conversación quedaba sin etiqueta ni agente: en ATT1, la persona de la conversación 103 se dio de baja el 2026-10-12, el 17/10 escribió «Envíame el enlace» en la 257 y nadie la vio. Ahora, con `HUMAN_HANDOFF_ADMISSION_ENABLED` y el entrante admitido, el bridge pide una derivación (`commercial_exception`, detalle `opted_out_contact_wrote_again`), que asigna la conversación, deja una nota privada y suma `automation_paused`. La baja no cambia: el bridge no le contesta y no sale nada proactivo. Un nuevo pedido de baja no se deriva.

### Qué hace quien actualiza

Nada más que cambiar la imagen del bridge. Sin `HUMAN_HANDOFF_ADMISSION_ENABLED` no cambia nada.

## [1.10.1] - 2026-10-19

Un parche sobre la 1.10.0 con un solo arreglo, el del monitor de trabadas.

### Corregido

- **El monitor de trabadas lista por última actividad y corta en su ventana** (#262). Antes pedía todas las abiertas del inbox, sin orden, y el detalle y 20 mensajes de cada una, para después descartar las que tenían el último mensaje fuera de `CHATWOOT_STALLED_MAX_AGE_SECONDS`. Las abiertas crecen con cada plantilla sin respuesta (en ATT1, 234 el 2026-10-18, 175 de gente que nunca escribió), y pasado `CHATWOOT_STALLED_MAX_PAGES` el barrido fallaba cerrado con `conversation_scan_incomplete`: no rescataba ninguna trabada y `/ready` daba 503. Ahora pide `sort_by=last_activity_at_desc` y corta en la primera conversación más vieja que la ventana más una hora, igual que el barrido del seguimiento con cupón. El barrido depende de las conversaciones con actividad reciente, no del total.
  - **Dos motivos nuevos de falla**, que se registran por nombre: `invalid_conversation_activity` (un ítem sin `last_activity_at`) y `conversation_order_unexpected` (la lista no viene en el orden pedido).
  - **Un conteo que cambia entre páginas ya no corta el barrido** (antes, `conversation_count_changed`): con la lista ordenada, una conversación nueva entra arriba y solo repite una que ya se vio.

### Qué hace quien actualiza

Nada más que cambiar la imagen del bridge. No hay migraciones, variables, flags ni campos del manifiesto. `CHATWOOT_STALLED_MAX_PAGES` sigue valiendo como tope, pero con este cambio alcanza con pocas páginas.

## [1.10.0] - 2026-10-18

Sale de `main` y trae todo lo que entró después de la 1.9.0: A1 del carrito con el cupón personal y el link de recuperación, las frases nuevas de la baja, el almacén de flujos, varios productos por aliada (la 2a, la 2b y la 2c), los tenants del conector de Slack y la T1 y la T3a del motor de flujos. La 1.9.1 era un parche sobre la 1.9.0 y su arreglo (#255) también está acá. **Una instancia con un solo producto (sin `[[productos]]` en el manifiesto) y con las banderas nuevas apagadas se comporta igual que con la 1.9.1**, salvo por la baja, que reconoce más frases, y por el rótulo del contexto del agente, que pasa a `shadow-context-v4`.

### Agregado

- **El almacén de las definiciones de flujo**, la primera pieza de la 1b ([flow-change-cycle-v1](docs/contracts/flow-change-cycle-v1.md) 1.1.0, sección «Lo que fijó la implementación (1.1.0)», #243).
  - **Qué agrega:**
    - la migración `20261016000100`, con cuatro tablas (`flow_controls`, `flow_definitions`, `flow_change_proposals` y `flow_change_events`) y sus disparadores. Lo publicado no se puede cambiar y el registro solo suma;
    - siete funciones `operator_*_v1`, que ejecuta solo el dueño de la base;
    - el validador de las definiciones en Python (`src/bridge/flow_definition.py` y `src/bridge/flow_validator.py`), que no importa ningún módulo del bridge.
  - **No cambia nada de lo que ya corre:**
    - ningún rol de la API (`anon`, `authenticated`, `service_role`) ejecuta nada del almacén ni tiene permisos sobre sus tablas;
    - no crea roles ni toca objetos que ya existen;
    - nada lo lee para mandar un mensaje. El motor que va a ejecutar las definiciones es otra pieza.
- **Los flujos de ATT1 como definiciones, en las pruebas.** Salen de la configuración de `att1-db` leída en solo lectura el 2026-10-16 a las 13:15:51Z (`tests/fixtures/flow_store/`).
  - El validador da cero bloqueos y las tres advertencias que el esquema anticipa.
  - PGlite las publica con las funciones del dueño, incluida la reactivación con `horario: siempre`.
- **El conector de Slack acepta instancias nuevas por configuración** (#257). Hasta acá solo aceptaba `johanna` y `att1`, fijos en el código, y una tercera instancia hacía fallar el arranque con `invalid_tenant_channel_configuration`.
  - **Qué agrega:**
    - las instancias son las claves de `SLACK_TENANT_CHANNELS_JSON`, con el formato `^[a-z0-9][a-z0-9_-]{0,39}$`, el mismo que validan los endpoints de reconciliación y de backfill;
    - `SLACK_TENANT_LABELS_JSON`, opcional, con el nombre con que el conector muestra a cada instancia. Sin cargar, Johanna y ATT1 se ven como hasta ahora, y una instancia nueva aparece con su nombre en mayúsculas. Un nombre vacío, con espacios en los bordes, de más de 40 caracteres, con caracteres no imprimibles o de una instancia sin canal frena el arranque con `invalid_tenant_label_configuration`.
  - **No cambia nada de lo que ya corre:**
    - el canal reservado de Johanna y el chequeo de canales repetidos siguen igual;
    - una instancia sin ruta de token sigue recibiendo 401, y el backfill de una instancia sin canal, 409.

- **A1 del carrito de ATT1 con el cupón personal y el link de recuperación** (decisión de Dan del 18/10, «A1 espera al cupón, que dura 24 h, en la hora del país»). Apagado por defecto.
  - **Qué agrega:**
    - la migración `20261018000100`: la tabla `recovery_coupons` (dos cupones por caso como máximo) y las RPC del worker del cupón;
    - el worker del cupón (`RECOVERY_COUPON_WORKER_ENABLED`): a los 45 minutos del abandono crea el cupón en la API de Hotmart y lo vuelve a mirar hasta que figura válido. Las credenciales salen de `HOTMART_API_CREDENTIALS_FILE` y nunca van al log;
    - la migración `20261018000200`: el link de A1 en `checkout_link_issuances` sin conversación (`recovery_first_touch`, atado al caso y al intento de envío), y la compra con ese link deja el caso `won`, corta los toques que faltan y marca el cupón `redeemed`;
    - A1 (`RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED`, con `RECOVERY_DISCOUNT_TEMPLATE_NAME`, `RECOVERY_DISCOUNT_PRICE` y `RECOVERY_DISCOUNT_CURRENCY`, que por defecto es USD): con el cupón válido sale la plantilla con la hora del abandono, el precio y el vencimiento en la zona del país del teléfono, y el botón al checkout con el cupón ya puesto. Si el cupón no está listo, A1 se corre 5 minutos en los intentos 1 y 2, dentro de los 15 minutos de su hora; después sale la plantilla de hoy sin descuento;
    - las plantillas aprobadas pueden abrir con el botón al checkout y seguir con respuestas rápidas, en el bridge y en la regla 13 del validador de flujos;
    - la baja reconoce más formas de pedirla («STOP», «baja», «ya no me escriban», «denme de baja», «sáquenme de la lista») y la frase dentro de un mensaje de saludo o cortesía. Una razón, un momento o una pregunta la siguen frenando. Dejan de contar como baja «¿No más mensajes?» y una cita después de un saludo.
  - **Corrige:** `authorize_chatwoot_checkout_issuance_v2` comparaba la conversación con `<>` y dejaba pasar una fila sin conversación; ahora usa `is distinct from` y rechaza la del carrito.
  - **No cambia nada de lo que ya corre** con las dos banderas apagadas: no se lee el archivo de credenciales, no se crea ningún cupón y A1 sale como hoy.

- **Los productos de la aliada en el manifiesto y la etiqueta de producto en Chatwoot** ([referencia del manifiesto](docs/referencia-manifiesto.md), #245). `[[productos]]` es opcional: `clave`, igual a la sección del producto en el conocimiento, y, juntas o ninguna, `etiqueta` y `macro_etiqueta`. Con productos, el bridge no arranca si el conocimiento no separa los mismos. El bridge le suma la etiqueta a cada conversación cuando la persona escribe y cuando sale una plantilla de primer contacto, ejecutando un macro que solo hace `add_label`. Lo lee antes de usarlo y no lo ejecuta si hace algo más. Es de falla blanda, y `/ready` publica `product_label`.
- **Lo que hizo la persona y el producto del link, en el agente** ([commercial-ally-runtime-v1](docs/contracts/commercial-ally-runtime-v1.md), #246). Solo con `[[productos]]` en el manifiesto. Cada turno del agente lleva `products` (la clave y el nombre de cada producto) y `person_activity`: lo que hizo el móvil en el funnel en los últimos 30 días (formularios, carritos, pagos rechazados, fichas y compras), de `get_portable_person_activity_v1` (migración `20261016000200`, solo lectura, solo `service_role`). Si la lectura falla, el agente contesta igual. La salida del agente admite `product`, `null` o un producto declarado: con un solo producto, un link sin `product` es de ese; con dos o más, es inválido. El bridge deja `payment_link_product` en el log. El SOUL común suma esas tres cosas.
- **Un binding por producto en el manifiesto** ([referencia-manifiesto](docs/referencia-manifiesto.md), #248). Desde el segundo, cada producto de `[[productos]]` trae su `funnel_ref`, `binding_version` y `[productos.hotmart]`. El principal sigue en `[hotmart]` y `[instancia]`. El arranque y `/ready` verifican el binding de cada producto.
- **El ruteo por producto de Hotmart y de los formularios** ([commercial-ally-runtime-v1](docs/contracts/commercial-ally-runtime-v1.md), «El ruteo de Hotmart y de los formularios», #250). Con más de un producto, cada evento de Hotmart se admite con el binding de su producto y oferta, y cada formulario con el de su landing. Un evento sin producto en la instancia deja `hotmart_event_unrouted` en el log. Los eventos y formularios de un producto que no es el principal se admiten sin planificar nada.
- **El link del producto que nombra el agente** (#251). Con más de un producto, la reserva del link usa el funnel del producto que nombró el agente: `reserve_portable_checkout_issuance_for_funnel_v1` (migración `20261016000400`, derivada de la reserva portable con reemplazos contados; sin tablas ni índices). Con un solo producto la reserva es la de siempre.
- **La etiqueta y lo que hizo la persona de cada producto** (#252). Con más de un producto, `person_activity` se lee en el funnel de cada producto y el último pago rechazado suma su `product`; cada producto que declara etiqueta tiene su etiquetador. Con esto el bridge arranca con más de un producto. `CONTEXT_BUILDER_VERSION` pasa a `shadow-context-v4`.
- **El motor de flujos, tajadas 1 y 3a, fuera del runtime** (#249 y #254): la paridad contra el legado y la simulación sobre la captura de ATT1 (`scripts/flow_engine_simulate.py`). Nada de lo que corre importa sus módulos.

### Corregido

- **Lo que hizo la persona cuenta la compra hecha con el link del agente** (#247). `get_portable_person_activity_v1` sumaba solo las compras del correlador portable, y la compra con nuestro link pasa por el de la emisión. La migración `20261016000300` reemplaza solo el cuerpo de la función: la misma firma, los mismos permisos y ninguna función nueva.
- **Con manifiesto, la baja se registra aunque la conversación esté derivada o pausada** (#255, ya en la 1.9.1).

### Qué hace quien actualiza

1. **La base primero:** las seis migraciones nuevas, en orden: `20261016000100` (el almacén de flujos), `20261016000200` y `20261016000300` (lo que hizo la persona), `20261016000400` (el link por funnel), `20261018000100` (los cupones) y `20261018000200` (el link de A1). Todas suman objetos; la `…000200` del 18/10 además corrige `authorize_chatwoot_checkout_issuance_v2` y engancha el cierre del caso en `correlate_hotmart_checkout_issuance_v2`, las dos con reemplazos contados que frenan con `55000` si la definición viva no es la esperada. Con un bridge 1.9.1 todavía corriendo, nada de lo nuevo se llama.
2. **Después, las imágenes a la 1.10.0** y el redeploy. Sin `[[productos]]` y con las banderas apagadas no hace falta ninguna variable ni ningún campo nuevo del manifiesto.
3. **Varios productos:** nada, si no se usa `[[productos]]`. Para usarlo: crear en Chatwoot la etiqueta y un macro global con una sola acción, `add_label` de esa etiqueta, y declarar `[[productos]]` con el id del macro, **después** de subir la imagen (una versión anterior no carga un manifiesto con la sección). El conocimiento tiene que estar en dos capas, con las mismas claves, y el SOUL común de esta versión va en el profile del agente.
4. **A1 con cupón:** para prenderlo en una instancia, montar la credencial de la API de Hotmart, apuntar `HOTMART_API_CREDENTIALS_FILE` a ella y prender `RECOVERY_COUPON_WORKER_ENABLED` y `RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED` con la plantilla y el precio. Con el cupón prendido y A1 apagado se crean cupones que nadie recibe: van juntos.
5. **El conector de Slack** no pide nada para seguir igual. Para sumar una instancia: su canal en `SLACK_TENANT_CHANNELS_JSON`, su token en `SLACK_TENANT_TOKENS_JSON` y, si se quiere otro nombre, `SLACK_TENANT_LABELS_JSON` (`deploy/slack-connector.env.example`).

### Verificado antes de publicar

- **La imagen real de ATT1** (`supabase/postgres:17.6.1.167`), con toda la cadena y `aplicar-migraciones.sh` de la instancia:
  - `pg_dump -s` y la foto del clúster antes y después no muestran ningún objeto existente cambiado;
  - la huella pasa de `absent` a `present`, y las 275 funciones del inventario de permisos quedan en `ok`;
  - PostgREST 14.15 recarga su caché solo y rechaza la función del operador con `42501`, tanto con la clave de `service_role` como sin clave.
- **Johanna:** `supabase db push` con la CLI 2.113.0 la aplica sin pedir superusuario.
- **Las pruebas:** `validate_flow_store.mjs` (PGlite, 773 comprobaciones), siete carreras en Postgres 17 real (`real_postgres_flow_store.py`) y las pruebas del validador en pytest.
  - Cada guarda se vio fallar a propósito.
  - Un corredor de mutaciones de la revisión adversarial mata 34 de las 37 de la base (las otras tres son equivalentes o imposibles de poner en rojo) y las 29 de Python.
- **A1 con cupón y el link de recuperación** (#260): `uv run pytest -q` completo (6628 pasan y 5 se saltean, macOS, 18/10) y `npm test` de PGlite en verde, con `recovery_coupons=OK` y `recovery_checkout_issuance=OK`; la CI completa del #260 en verde, también `python-and-postgres`. Los dos reemplazos contados de `20261018000200` se buscaron en las definiciones vivas de `att1-db` el 18/10: cada fragmento aparece una sola vez. El detector de baja nuevo da el mismo veredicto que el viejo sobre los 267 mensajes entrantes cortos distintos de Chatwoot.
- **El conector de Slack:** `tests/test_slack_correlation_tenants.py`, 22 casos. Cubren una instancia nueva aceptada con su canal y su nombre, las claves y los nombres mal formados, el canal de Johanna, los canales repetidos y que la instancia nueva sobreviva a un respaldo y su restauración. Corre en la CI completa. **Sin desplegar:** el conector compartido sigue con el código anterior hasta la instalación de la próxima aliada.

## [1.9.1] - 2026-10-17

Un parche sobre la 1.9.0, que es lo que corre en ATT1. No trae nada de lo que entró a `main` después de la 1.9.0 (el almacén de flujos, varios productos por aliada, la T1 y la T3a del motor): sale de una rama sobre `956848b` con un solo arreglo.

### Corregido

- **Con manifiesto, la baja se registra aunque la conversación esté derivada o pausada** (#255). La admisión de esas conversaciones da `blocked` (o `evidence_conflict`) y el bridge salía antes de buscar la baja: un «No más mensajes» escrito ahí no quedaba en la base y la persona seguía abierta a los flujos siguientes. Pasó en ATT1 con la conversación 50, el 2026-10-12. Ahora, en esa salida, corre lo mismo que ya corría cuando la admisión falla: el stop y la baja, sin el agente. Sin manifiesto (Johanna) no cambia nada.

### Qué hace quien actualiza

Nada más que cambiar la imagen del bridge a `v1.9.1`. No hay migraciones, variables, flags ni campos del manifiesto.

## [1.9.0] - sin publicar

Trae tres cosas: la lectura del Automation Expert (la 1a de la Etapa 1), apagada; el conocimiento comercial en dos capas, lo común y una sección por producto (el paso 1 de varios productos por aliada); y el arreglo de los comentarios de más de un renglón en la revisión diaria. Con el flag de la 1a apagado y el conocimiento de un solo producto, una instancia que sube desde la 1.8.0 se comporta igual, salvo por ese arreglo.

### Agregado

- **Los lectores del Automation Expert en el bridge** ([automation-expert-read-v1](docs/contracts/automation-expert-read-v1.md), #236): tres rutas de solo lectura, `GET /internal/automation-expert/v1/flows[/{flow_key}]`, `POST /internal/automation-expert/v1/leads/lookup` y `GET /internal/automation-expert/v1/leads/{lead_ref}/history`. Están **apagadas por defecto** (`AUTOMATION_EXPERT_READ_ENABLED=false`): con el flag apagado, el bridge no crea las rutas ni llama a las funciones nuevas. Prendidas, exigen el manifiesto de la instancia y un `AUTOMATION_EXPERT_READ_TOKEN` propio, de 32 caracteres o más y distinto de todo otro secreto del bridge. La migración `20261015000200` suma las tres funciones que leen (`STABLE SECURITY DEFINER`, solo `service_role`), sin tablas nuevas ni permisos sobre tablas.
- **El profile `automation-expert` y su plugin de lectura** (`profiles/automation-expert/`, #237). Ningún Hermes lo instala todavía: `hermes/declaracion.toml` no lo lista.
- **Los contratos de la 1b**, solo como documentos: [flow-definition-v1](docs/contracts/flow-definition-v1.md) (con el producto de cada flujo y la regla 17) y [flow-change-cycle-v1](docs/contracts/flow-change-cycle-v1.md).

### Cambiado

- **El conocimiento comercial admite una segunda forma, en dos capas** ([commercial-knowledge-v1](docs/contracts/commercial-knowledge-v1.md), #241): arriba va lo común a la aliada, y cada producto va en `[[productos]]`, con `clave`, `oferta`, `contenido` y, si los tiene, su no confirmado, sus promesas y sus preguntas. El render pone lo común primero y después `# Producto: <nombre>` por producto. **La forma de un solo producto se renderiza igual, byte a byte:** el test fija el hash del v7 de ATT1 (`ac5cc64fbcad`).
- **El tope del conocimiento renderizado pasa de 20.000 a 32.000 bytes** (`MAX_RENDERED_BYTES`). El de 20.000 suponía que Hermes cortaba el `system` del pedido como corta el SOUL, y no lo corta (leído en `nousresearch/hermes-agent:v2026.8.31`).

### Arreglado

- **La revisión diaria rechazaba todo comentario de más de un renglón.** El navegador manda los saltos de línea de un textarea como `\r\n`, y la página contestaba «Decisión inválida» porque solo aceptaba `\n` y `\t` como caracteres de control. Ahora pasa `\r\n` y `\r` a `\n` antes de validar, y guarda el comentario con `\n`. El resto de los caracteres de control se sigue rechazando. Lo encontró Mariana en ATT1 el 2026-10-16, al escribir un link en otro renglón; el test usa el cuerpo que mandó Chrome 155 con un Enter (`tests/fixtures/daily_feedback_review_form_chrome_crlf_20261016.json`).

### Qué hace quien actualiza

1. Subir las imágenes a la 1.9.0 y redesplegar el bridge y el servicio de la revisión. No hace falta ninguna variable ni ningún campo nuevo del manifiesto: sin `AUTOMATION_EXPERT_READ_ENABLED`, la 1a queda apagada.
2. **La migración `20261015000200` hace falta recién para prender el Automation Expert.** Se puede correr antes, porque solo suma funciones que nadie llama con el flag apagado.
3. **El conocimiento no cambia:** un archivo de un solo producto da el mismo render y el mismo hash. La forma en dos capas se usa apuntando el manifiesto a un archivo nuevo, con su aprobación.

### Verificado antes de publicar

- La 1a: `validate_automation_expert_read.mjs` (PGlite, todas las migraciones, lectura en `begin transaction read only` como `service_role`, `anon` y `authenticated` sin permiso, canarios que no salen), `tests/test_automation_expert_read*.py` y `tests/test_automation_expert_profile_package.py`. Detalle en #236 y #237.
- El conocimiento: `tests/test_commercial_knowledge.py`, con los siete conocimientos de ATT1 dando el mismo hash que antes. Detalle en #241.
- El arreglo: el test manda el cuerpo capturado de Chrome; en rojo sin el arreglo y en verde con él. Detalle en #242.

## [1.8.0] - sin publicar

La página de la revisión diaria se puede filtrar por tipo de conversación, y quien revisa puede cerrar el lote sin revisarlo entero, con una aprobación escrita. Pedido de Dan del 2026-10-14, después de la reunión con Mariana: el lote de ATT1 del 2026-10-13 traía 28 conversaciones y en la mayoría no había nada que revisar ([contrato](docs/contracts/daily-feedback-review-package-v2.md), sección 11).

Trae una migración, que agrega una tabla y dos funciones y reemplaza otras dos. Ninguna variable ni campo del manifiesto. Le cambia algo a toda instancia con la revisión diaria, que hoy son Johanna y ATT1.

### Agregado

- **Las etiquetas de filtro, calculadas al armar el lote** (`context.summary.review_facets`): derivación, respuesta de la IA (el modelo escribió algo más que el link), link de pago de la IA, inbound (la abrió el lead sin responder a un mensaje nuestro) y opt-out. Para saber quién abrió la conversación, el colector pagina más allá de la ventana solo en las conversaciones que entran al lote (`context.conversation.opened_by`).
- **El desplegable de la página**, con el conteo de pendientes de cada etiqueta. Arranca con derivación, respuesta de la IA e inbound, y se puede ver todo.
- **El cierre del lote con aprobación escrita** (`close_daily_feedback_batch_v1`, tabla `daily_feedback_batch_closures`): una casilla obligatoria con la frase *«Apruebo que este reporte se cierre sin completar el feedback de todas las conversaciones.»*. El lote queda completo y las conversaciones sin revisar no reciben decisión, así que no figuran como revisadas al día siguiente ni en el feedback.
- **`get_daily_feedback_review_page_v2`**: la cola filtrada y sus conteos.

### Cambiado

- **`record_daily_feedback_decision_v1` acepta cualquier conversación pendiente del lote**, no solo la primera por índice: con un filtro, la que se ve no es la primera.
- **`get_daily_feedback_review_page_v1` da por completo un lote cerrado.**

### Arreglado

- **El seguimiento con cupón figuraba como un link de pago del agente.** La anotación de links le cambiaba el tipo porque el cupón emite su propio link. Ahora es `discount_followup_template` («Plantilla · seguimiento con cupón») y conserva los datos del link.

### Qué hace quien actualiza

1. Correr la migración `20261015000100` **antes** de subir la imagen de la revisión. Si la imagen llega primero, la página sigue andando como antes, sin filtros ni cierre, hasta que se corra.
2. Subir `setter-daily-feedback` a la 1.8.0 y redesplegar el servicio de la revisión. El bridge no cambia.

Los lotes armados antes de la 1.8.0 no traen etiquetas: la página los muestra enteros, con un aviso.

### Verificado antes de publicar

- La migración, sobre PGlite (`tests/sql/followup_engine/validate_daily_feedback_review_filters.mjs`, en `npm test`): el filtro, la decisión fuera de orden, el cierre con y sin la frase, un lote anterior a los filtros, los permisos y la purga. Con la regla vieja de la decisión, el validador falla con `review_item_not_current`.
- El colector nuevo, contra el Chatwoot de ATT1 en solo lectura, sobre la ventana del lote del 2026-10-13: las mismas 28 conversaciones; con el filtro por defecto se ven 14 (22 sumando el link de pago). La paginación extra sumó 3 pedidos a Chatwoot (260 contra 257).

## [1.7.0] - sin publicar

El producto declara cómo queda el Hermes de una instancia, y trae el instalador y el chequeo de deriva ([ADR-0025](docs/decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md)). No cambia el bridge.

### Agregado

- **`hermes/declaracion.toml`:** la imagen de Hermes fija (`v2026.8.31`, por digest) y los profiles que trae esta versión: el agente comercial.
- **`profiles/agente-comercial-comun/` es una distribución nativa de Hermes:** `distribution.yaml` y el molde de `config.yaml`, copiado del agente que atendía en ATT1 el 2026-10-14 (diez claves, sin herramientas). El `SOUL.md` no cambia.
- **`scripts/hermes_instancia.py`** con `plan`, `aplicar` y `chequear`, contra el `hermes.toml` de la instancia. Frena ante un contenedor de otro stack, otra imagen o un Hermes ajeno, y nunca imprime una key. Formato y uso en `hermes/README.md`.
- **El workflow `hermes`:** la prueba de punta a punta contra la imagen declarada, con un modelo de mentira, cuando cambia algo de Hermes.

### Quitado

- `profiles/att1/`, `scripts/install_att1_product_profiles.py` y su test: el paquete candidato inerte del 2026-09-07, que nunca se activó.

### Qué hace quien actualiza

Nada: el bridge no cambia, y el instalador no corre solo. Pasar una instancia a su propio Hermes es un paso aparte, con su `hermes.toml`.

## [1.6.1] - sin publicar

Arregla una propuesta del agente que el bridge descartaba entera por un detalle de formato.

### Arreglado

- **La propuesta del agente con saltos de línea literales en el `reply` se acepta** (`src/bridge/hermes.py`, `_parse_agent_proposal`). GLM 5.3 Flash a veces separa los párrafos de la respuesta con saltos de línea sin escapar dentro del string JSON. `json.loads` estricto rechazaba la propuesta entera aunque los seis campos fueran válidos: el bridge reintentaba una vez y, si volvía igual, guardaba `invalid_agent_output` y el lead se quedaba sin respuesta. Ahora se parsea con `strict=False`, que acepta caracteres de control dentro de los strings, y el `reply` llega con sus párrafos. El fixture es la respuesta real del 2026-10-12 (`tests/fixtures/hermes_glm_proposal_raw_newlines_20261012.json`): 1 de 222 en el arnés del conocimiento v3 de ATT1, con las respuestas de pagos, más largas que las de antes.

### Qué hace quien actualiza

Nada: ni migración, ni variables, ni campos del manifiesto. Se sube la imagen y se redespliega el bridge.

## [1.6.0] - sin publicar

El agente de una instancia con manifiesto intenta resolver un pago rechazado antes de derivarlo. Para eso suma tres piezas: una sección de pagos opcional en el conocimiento, el último rechazo de quien escribe en la entrada del agente y la ruta «Compra fallida» del SOUL común, que usa las dos. Además, el bridge guarda los eventos de la ficha de pago (OXXO y parecidos) para el seguimiento de la ficha sin pagar. Johanna no cambia.

Trae una migración, que solo agrega dos funciones. Ninguna variable ni campo obligatorio nuevo en el manifiesto. Para aprovecharla, la instancia publica un conocimiento nuevo con la sección `[pagos]` y suscribe `PURCHASE_BILLET_PRINTED` y `PURCHASE_EXPIRED` en el webhook de Hotmart.

### Agregado

- **La sección `[pagos]` del conocimiento** ([contrato](docs/contracts/commercial-knowledge-v1.md), opcional). La declara la instancia entera: país principal, medios activos (exactamente los que tienen prendidos sus ofertas), el nombre de la transferencia y del efectivo según el país, el paso a paso de cada medio y qué hacer ante cada motivo de rechazo, con los textos con que llega. El producto no nombra ningún medio local: SPEI, OXXO o PSE cambian con el país. Un conocimiento sin la sección se renderiza igual que en la 1.5.0.
- **`recent_payment_failure` en la entrada del agente**, con `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED` ([contrato](docs/contracts/commercial-ally-runtime-v1.md), *El último rechazo en la entrada del agente*). El último `PURCHASE_CANCELED` de ese móvil en la última semana, por sus dos formas (`52`/`521`), si no compró después: medio, cuotas, motivo tal como llegó y país. Si la lectura falla, el agente contesta igual, sin el dato. La forma del rechazo está medida con los cuatro de ATT1 del 11/10 (`tests/fixtures/hotmart_purchase_canceled_att1_20261012.json`): tres dicen «Transaction refused» y uno llega en portugués.
- **Los eventos de la ficha se guardan**, con el mismo flag: `PURCHASE_BILLET_PRINTED` y `PURCHASE_EXPIRED` van a `webhook_events` con su procedencia y no disparan nada. Hasta la 1.5.0 el bridge los contestaba con 200 sin guardarlos. Su forma todavía no está medida en ATT1: la admisión solo mira el sobre común y guarda el payload entero.

### Cambiado

- **La ruta «Compra fallida» del SOUL común** resuelve con la sección `[pagos]` antes de derivar: dice qué hacer según el motivo, ofrece otro medio activo con el nombre de su país y, si la persona quiere reintentar, pide el enlace. Deriva si le cobraron, si su caso no está en la guía, si ya probó lo que indica o si el conocimiento no trae la sección. Antes derivaba siempre que no hubiera «una resolución confirmada». El SOUL de Johanna no cambia, y `tests/test_common_soul.py` declara las diferencias texto por texto.
- **El bloque de conocimiento renderizado puede pesar hasta 20.000 bytes** (16 KB hasta la 1.5.0): en bytes queda debajo de los 20.000 caracteres que Hermes deja del SOUL.
- **`CONTEXT_BUILDER_VERSION` pasa a `shadow-context-v2`**: la entrada puede traer una clave nueva.

## [1.5.0] - sin publicar

Suma dos cosas, apagadas por defecto: la inferencia del primer nombre y el seguimiento con cupón del 10 % corren también en una instancia con manifiesto. Y ajusta la validación del nombre inferido para descartar las formas medidas que saludan peor que la regla, también en Johanna. Y que la revisión diaria corra en una instancia autohospedada como ATT1, con tres excepciones que se declaran por variable y que `/ready` muestra. Johanna no cambia.

Trae una migración, que solo agrega una función: la reserva del seguimiento con cupón para el runtime con manifiesto. Suma dos variables del seguimiento y seis del servicio de la revisión diaria, todas opcionales. Ningún campo obligatorio nuevo en el manifiesto.

### Agregado

- **`LEAD_FIRST_NAME_INFERENCE_ENABLED` con manifiesto** ([contrato](docs/contracts/lead-first-name-inference-v1.md)). Hasta la 1.4.0, una instancia con el flag no arrancaba (`ATT1 runtime capabilities are not portable`).
  - El modelo es el profile de `HERMES_MODEL_NAME` (o `LEAD_FIRST_NAME_MODEL_NAME`) por el API server de Hermes de la instancia, y las filas van a `lead_first_name_inferences` de su base.
  - Corre después de admitir un formulario nuevo de `/webhooks/lead` o del adaptador de GHL, y la lee el saludo del dispatcher directo.
  - Exige el saludo (`LEAD_FIRST_NAME_GREETING_ENABLED`, que con manifiesto exige el modo directo), Hermes, Supabase y, con manifiesto, una entrada de formularios (`LEAD_PRECHECKOUT_ENABLED` o `GHL_PRECHECKOUT_ADAPTER_ENABLED`): sin ninguna, nada la dispararía y el bridge no arranca, igual que el saludo sin el modo directo.
  - En una instancia el envío busca la inferencia por el nombre del contacto. Si el contacto nació con otro texto (el de Hotmart, un contacto anterior, o un formulario cuyo primer contacto no se planificó), el saludo cae a la primera palabra, como sin inferencia.
  - Cada inferencia carga el SOUL del profile. Medido con el profile de ATT1: USD 0,0006 por nombre y ninguna respuesta fuera del contrato ([evidencia](docs/operations/2026-10-10-nombre-de-pila-con-el-profile-de-att1.md)).
  - El nombre de todo formulario nuevo admitido sale al proveedor del modelo, tenga o no permiso de WhatsApp. En ATT1 el adaptador de GHL marca el permiso en todo formulario con un teléfono válido.
- **El cliente de la inferencia manda de a dos** (`MAX_CONCURRENT_INFERENCES`). El api_server de Hermes es el mismo de los turnos del agente y contesta 429 al pasar su tope de corridas en vuelo: una ráfaga de formularios espera en el bridge.
- **`CONVERSATION_FOLLOWUP_ENABLED` con manifiesto** ([contrato](docs/contracts/conversation-followup-discount-v1.md), sección *En un runtime con manifiesto*; [ADR-0023](docs/decisions/0023-el-cupon-con-manifiesto-reusa-descuento.md), propuesto). Hasta la 1.4.0, una instancia con el flag no arrancaba (`ATT1 runtime capabilities are not portable`). Sin manifiesto (Johanna) el seguimiento sigue igual: la misma RPC con los mismos argumentos, a cualquier hora y a todo el inbox. Con manifiesto:
  - el flag cuelga de `[flujos].descuento`, y la plantilla y el producto tienen que ser los de `[plantillas].descuento` y `hotmart.product_name`;
  - la identidad se resuelve como en el entrante (`52`/`521`) y es la que va a la reserva y a la autorización. Con el `wa_id` textual, un caso guardado en `52` daría `issuance_blocked_identity` y el cupón no saldría nunca. Si la lectura falla, no se reserva nada (`identity_lookup_failed`);
  - la reserva es `claim_portable_conversation_followup_v1`: el enlace sale de `reserve_portable_checkout_issuance_v2`, con la oferta, el `sck` y el `fbclid` del formulario, como el enlace del agente, y solo en una conversación adoptada, la respuesta a una plantilla nuestra. Si no, `blocked_not_template_reply`: quien nos escribió por su cuenta no recibe el cupón;
  - el horario es obligatorio. Fuera de él el barrido no lee el catálogo ni Chatwoot y publica `outside_send_hours`;
  - un nombre que no pasa el filtro de las plantillas (`template_greeting_name_is_safe`) no sale (`greeting_name_refused`). Se reintenta en cada barrido;
  - con el binding v1 en JSON no arranca: ahí no hay resolvedor ni reserva portable.
- **La plantilla del seguimiento puede llevar el cupón solo en el botón.** `parse_followup_template` acepta los marcadores `{{1}}` y `{{2}}` (la de ATT1, `att1_seguimiento_descuento_01`) además de `{{1}}`, `{{2}}` y `{{3}}` (la de Johanna). El cuerpo que se manda lleva un parámetro por marcador, porque Meta rechaza otra cantidad con `#132000` después de que Chatwoot aceptó el mensaje. Con la plantilla de Johanna no cambia nada.
- **`claim_portable_conversation_followup_v1`** (migración `20261010000100`). Se deriva de la definición vigente de `claim_conversation_followup_v1` (la de `20261001000500`) con tres reemplazos: el nombre, la reserva del enlace y la barrera de la conversación adoptada, que va después del límite de uno por conversación y antes del conteo de casos. Es el método de la reserva portable de `20261001000100`: cada texto tiene que estar exactamente una vez, o la migración falla con `55000` y no cambia nada. Solo `service_role` la ejecuta. En una base sin `reserve_portable_checkout_issuance_v2`, como la de Johanna, avisa y no crea nada, y el inventario de esquema da ahí `fingerprint_absent`. Es idempotente y no toca tablas, filas ni otras funciones: `claim_conversation_followup_v1` queda idéntica.
- **`CONVERSATION_FOLLOWUP_SEND_HOURS` y `CONVERSATION_FOLLOWUP_ONLY_PHONE`.**
  - `SEND_HOURS` es `HH-HH`, con 0 ≤ inicio < fin ≤ 24, en la zona del manifiesto (`instancia.zona_horaria`): `09-21`, o `00-24` para mandar a cualquier hora, escrito a propósito. Solo existe con manifiesto.
  - `ONLY_PHONE` es el modo de un solo teléfono, en E.164, para la prueba: solo ese teléfono recibe, comparado en forma canónica (`52…` y `521…` son el mismo). Las demás candidatas se cuentan como `held_only_phone`, que es una cota superior del último barrido: candidatas según Chatwoot, antes de la relectura, del filtro del nombre, del resolvedor y de las barreras de la base. No dice a cuántas personas les habría salido.
  - Una forma inválida de cualquiera de las dos, o un horario fuera de rango (`21-09`, `09-25`, `00-00`), no arranca aunque el flag esté apagado, con un mensaje que nombra la variable. El horario sin manifiesto tampoco.
  - Con manifiesto y el flag prendido, un `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS` menor a `86400` (24 h) no arranca sin `ONLY_PHONE`: menos de 24 h de silencio es solo para la prueba. Así, abrir sacando solo el teléfono no le manda el cupón a todo el inbox a los 15 minutos.
- **`/ready`, con manifiesto y el seguimiento prendido,** suma tres claves, leídas del barredor que corre y sin ningún número:
  - `conversation_followup_audience`: `only_phone` en la prueba; sin ella, `inbox` con los remitentes por scope (todo el inbox) o `allowed_jid` con `ALLOWED_WHATSAPP_JID` sin scope (solo ese número);
  - `conversation_followup_min_age_seconds` (`"86400"` abierto);
  - `conversation_followup_send_hours` (`09-21`).

  El payload de Johanna no cambia.
- **La revisión diaria corre en una instancia autohospedada** ([ADR-0024](docs/decisions/0024-la-revision-diaria-en-una-instancia-autohospedada.md), [contrato](docs/contracts/daily-feedback-production-v1.md)). Hasta la 1.4.0, `setter-daily-feedback` no arrancaba sin la base por https, sin la evidencia de cifrado y sin el AgentBot vinculado al inbox, y ATT1 no cumple ninguna de las tres. Ahora son tres excepciones explícitas, cada una con su variable y su control compensatorio. El 200 de `/ready` las lista en `"exceptions"`, solo si hay alguna y siempre en este orden:
  - `authority_internal_http` (`DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP=true`): la base por http, solo hacia un servicio del stack. `SUPABASE_BASE_URL` tiene que llevar un host de una sola etiqueta, con puerto, sin credenciales ni ruta; una IP, un host con punto o un host sin puerto dan `invalid_supabase_internal_origin`.
  - `storage_unencrypted_risk_accepted` (`DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED=false` más `DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF`, el permalink https de la aceptación escrita del riesgo): el disco sin cifrar. Nunca se declara cifrado lo que no lo está, y las dos referencias a la vez dan `ambiguous_storage_protection`.
  - `chatwoot_agent_bot_unlinked` (`DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING=unlinked`): el arranque exige que el inbox no tenga bot (si tiene, `chatwoot_agent_bot_unexpectedly_linked`) y que el bot configurado sea de la cuenta. Es un chequeo del arranque, no un control continuo.
- **Tres ajustes de la revisión, sin excepción:**
  - `DAILY_FEEDBACK_BRAND_NAME`: la marca de la página de revisión, de 1 a 60 caracteres imprimibles; por defecto «Johanna».
  - `DAILY_FEEDBACK_MAX_CONVERSATION_PAGES`: el tope de páginas del listado de Chatwoot, de 1 a 400; por defecto 20.
  - `DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS`: la lease de la recolección, de 30 a 900 segundos; por defecto 120. Es el límite real de un lote grande: el listado, los mensajes y el contexto tienen que entrar en ella, o el lote no se confirma (`stale_collection_lease`) y la recolección se reintenta.

### Arreglado

- **Las partículas de un nombre inferido van en minúscula, salvo al principio:** «María del Carmen», no «María Del Carmen». El modelo devolvía bien el compuesto y el arreglo de mayúsculas capitalizaba `de`, `del`, `la`, `las` y `los`. Vale para las inferencias nuevas; las filas guardadas no se recalculan.
- **Un nombre inferido de una forma que saluda peor que la regla cuenta como `uncertain`**:
  - uno que el filtro de plantilla rechaza: más de 60 caracteres, o un carácter numérico como «²». Antes se guardaba, y el modo directo no mandaba la plantilla;
  - uno de una sola letra (cae a la regla o, si la regla tampoco da nada, al nombre completo);
  - un compuesto cortado: termina con una partícula («Juan de»), o empieza con una sin que el nombre empiece así («de Dios»).

  Las partículas cuentan en el tope de tres palabras: «María de los Ángeles» queda `uncertain`, hasta medir esos compuestos con el modelo. La validación no garantiza el saludo correcto: un apellido suelto, por ejemplo, pasa si el modelo lo devuelve.
- **Dos actividades de Chatwoot salían como «Actividad» en la página de revisión:** «Conversation unassigned by X» y «Unassigned from <equipo> by X», las dos capturadas en el inbox de ATT1 el 2026-10-06. Ahora salen como «Sin asignar», con quién lo hizo. «Unassigned by X» sigue como antes.

### Pruebas

Del seguimiento con cupón, sobre dato capturado de ATT1: el catálogo del inbox 11 por la API (`tests/fixtures/chatwoot_inbox_11_message_templates_20261010.json`, con el cupón redactado) y la conversación 21 anonimizada (`tests/fixtures/chatwoot_followup_candidate_inbox_11_20261010.json`: la plantilla del carrito, la respuesta, el enlace del agente y el silencio).

- `tests/test_followup_discount.py`: la plantilla real de ATT1 con el cupón solo en el botón y la de Johanna con el cupón también en el texto; el botón con el enlace que emite la reserva portable para un caso del formulario de la landing -d (el `sck` real del anuncio, con `~` y guiones, que en la captura de la 21 está redactado); el modo de un solo teléfono con las dos formas mexicanas, el horario y sus bordes (`00-24` incluido), el filtro del nombre, la identidad resuelta en la reserva y en la autorización, la falla del resolvedor, y que sin manifiesto la llamada a la reserva es la de siempre.
- `tests/test_supabase.py`: cada reserva pega en su RPC con el mismo cuerpo, y las dos fallan cerrado igual ante un resultado desconocido, un `claimed` sin enlace, cero filas o un `404 PGRST202` (el bridge desplegado antes que la migración).
- `tests/test_followup_discount_wiring.py`, `tests/test_instance_wiring.py`, `tests/test_att1_production_settings.py` y `tests/test_commercial_ally_portability.py`: las variables, cada guarda del arranque, el barredor que arma cada runtime y `/ready`. Johanna arma el barredor con los valores por defecto y su `/ready` no cambia.
- `tests/test_att1_production_settings.py`, además, corre un barrido completo del barredor que arma el set de producción, con el `ChatwootClient` y el `SupabaseClient` reales sobre `httpx.MockTransport` y la conversación 21: la reserva va a `claim_portable_conversation_followup_v1` con la identidad del formulario (`52…`) y la autorización con la misma. Los dobles de los otros tests no pasan por los clientes reales, y renombrar la kwarg del cliente o la del resolvedor los dejaba en verde.
- `tests/test_portable_conversation_followup_migration.py` y `tests/test_supabase_schema_inventory.py`: los tres textos de la migración contra la definición de `20261001000500`, el aviso sin la reserva portable, los permisos y la huella del inventario.
- `tests/sql/followup_engine/validate_portable_conversation_followup.mjs` (PGlite, en `npm test`), sobre la cadena completa y con las RPC reales como `service_role`:
  - la conversación 21, con el caso en `52` y la respuesta desde `521`, reserva con la oferta del formulario (`lead_intent`, `2uafw5bg`), anclada en nuestro último mensaje y con una sola intención viva; la compartida, con el `521`, da `issuance_blocked_identity`;
  - una conversación orgánica da `blocked_not_template_reply` sin escribir nada;
  - la compra con el `521` da `purchase_already_approved`, y una baja bajo el `521` en otra conversación da `issuance_blocked_opt_out`;
  - la migración agrega una sola función y no cambia ninguna otra; sin la reserva portable no crea nada y la huella da `fingerprint_absent`.

### Qué hace quien actualiza

Correr la migración `20261010000100`. Solo agrega una función, que nadie llama hasta prender el seguimiento con manifiesto. En una base sin la cadena portable del 2026-09-29 al 2026-10-01, como la de Johanna, avisa y no crea nada. Lo demás es opcional.

**La inferencia del primer nombre.** Para prenderla en una instancia: `LEAD_FIRST_NAME_INFERENCE_ENABLED=true`, con el saludo prendido, y redesplegar. En una instancia con manifiesto (Johanna no lo necesita):

- para volver a una imagen anterior a la 1.5.0, primero hay que sacar el flag: con el flag prendido, esa imagen no arranca;
- apagar la inferencia no devuelve el saludo a la regla, porque el saludo lee las filas guardadas. Para eso hay que borrar en la base las filas creadas desde la activación (psql, por `created_at`);
- para cortar el adaptador de GHL con la inferencia prendida, hay que sacar en el mismo redeploy los flags del adaptador, del primer contacto y de la inferencia.

**El seguimiento con cupón, en una instancia con manifiesto:**

1. La migración aplicada: `scripts/supabase_schema_inventory.sql` da `fingerprint_present` en su fila (`portable_conversation_followup_claim`) y `scripts/supabase_acl_inventory.sql` da `ok`.
2. En el manifiesto, por PR en el repo de la instancia: la plantilla en `[plantillas].descuento`, con el nombre y el idioma aprobados en Meta, y `descuento = true` en `[flujos]`. El manifiesto solo no manda nada.
3. En las variables del bridge: `CONVERSATION_FOLLOWUP_TEMPLATE_NAME`, `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE`, `CONVERSATION_FOLLOWUP_COUPON_CODE`, `CONVERSATION_FOLLOWUP_PRODUCT_NAME` y `CONVERSATION_FOLLOWUP_SEND_HOURS`, y para la prueba `CONVERSATION_FOLLOWUP_ONLY_PHONE`. Después, `CONVERSATION_FOLLOWUP_ENABLED=true` y redesplegar. Un valor que no coincide con el manifiesto deja el bridge sin arrancar, y con él el entrante y los demás flujos: conviene probar el arranque con el archivo de variables antes de redesplegar.
4. Para abrirlo a todo el inbox: sacar `CONVERSATION_FOLLOWUP_ONLY_PHONE` y el `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS` de prueba si se usó, y redesplegar. Si queda el de prueba sin el teléfono, el bridge no arranca. Abierto, `/ready` lo confirma por presencia: `conversation_followup_audience` en `inbox` y `conversation_followup_min_age_seconds` en `86400`.

Para volver atrás alcanza con sacar `CONVERSATION_FOLLOWUP_ENABLED` y redesplegar. La migración queda, porque solo agrega una función, y el manifiesto no tiene claves nuevas. Para volver a una imagen anterior a la 1.5.0 también hay que sacar antes el flag: con el flag prendido, esa imagen no arranca. Pausar el piloto o apagar `META_FINAL_EFFECT_ENABLED` no frena el seguimiento.

**La revisión diaria.** Nada: cada variable nueva es opcional, vacía cuenta como ausente y su valor por defecto es el comportamiento de la 1.4.0. Johanna no carga ninguna, y su `/ready` devuelve el mismo JSON. `deploy/daily-feedback-compose.yaml` las pasa con `${VAR:-}` y `deploy/daily-feedback.env.example` las trae comentadas, con la excepción que declara cada una. Para una instancia autohospedada:

- `deploy/daily-feedback-compose.yaml` sigue exigiendo la evidencia de cifrado, así que no sirve con `storage_unencrypted_risk_accepted`: con la evidencia y la aceptación a la vez, el servicio no arranca. Esa instancia define su propio servicio;
- el valor de ejemplo de `DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF` en el env.example no es una URL: copiado sin reemplazar, el servicio no arranca (`invalid_storage_risk_acceptance_ref`). Con `VERIFIED=false` y sin la aceptación, el error es `storage_encryption_not_verified`, como hasta la 1.4.0: lo que falta es la aceptación;
- sin `DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING`, el modo es el de siempre, y un inbox sin bot no arranca, con `chatwoot_scope_verification_failed`;
- con alguna excepción, una imagen anterior a la 1.5.0 no arranca. Volver atrás es sacar el servicio, después de que se purgue el último lote;
- la prueba con el run-now son dos llamadas: la primera confirma el lote (`collected: true`, `notified: false`) y la segunda manda la tarjeta (`collected: false`, `notified: true`). No es nuevo, también pasa en Johanna: ahora está escrito en el contrato.

### Lo que queda fuera

- **El seguimiento con cupón no pasa por el piloto:** ni por su pausa, ni por sus topes, ni por el tope de mensajes proactivos por persona de la 1.4.0. Tampoco tiene tope diario: uno por conversación y `CONVERSATION_FOLLOWUP_MAX_SENDS_PER_SCAN` por barrido.
- **Queda `sent` cuando Chatwoot acepta el mensaje.** Si Meta lo rechaza después, el único envío de esa conversación queda gastado.
- **El agente no sabe del cupón.** Quien pide el enlace después de recibirlo lo recibe a precio completo, salvo que use el botón o el código, y un pedido de descuento se deriva a una persona.
- **`validate` no distingue cuál de los dos flags que cuelgan de `descuento` habilita el flujo:** el seguimiento o el descuento posterior a la respuesta, que nació con el binding v1.
- **Sin prueba de punta a punta en una instancia:** el E2E es con el modo de un solo teléfono, después del despliegue.

## [1.4.0] - 2026-10-10

Suma dos cosas, las dos apagadas por defecto:

- un tope de mensajes proactivos por persona entre flujos, en la puerta común de los tres arranques (primer contacto, carrito y pago fallido). Sin el renglón del tenant en la tabla nueva no hay tope y todo queda como hoy. Trae una migración;
- que el adaptador de GHL tome la landing del formulario y no la de la página donde empezó la visita. Son dos claves opcionales de `[adaptadores.ghl]` en el manifiesto; sin ellas todo queda como hoy.

Y arregla la respuesta a la plantilla del carrito o del pago fallido, que no llegaba al agente mientras el primer contacto de esa persona seguía pendiente (ver «Arreglado»).

Ni variables nuevas ni campos obligatorios.

### Arreglado

- **La respuesta a la plantilla del carrito o del pago fallido no llegaba al agente mientras el primer contacto de esa persona seguía pendiente** (migración `20261009000200`, [contrato](docs/contracts/inbound-commercial-case-admission-v1.md)). La adopción de `20261001000400` (H7) no toma la conversación mientras haya un paso pendiente de la persona. El primer contacto queda en `pending` hasta su hora aunque el carrito, el pago fallido o una compra ya lo hayan reemplazado, porque la reevaluación lo cancela recién cuando el despachador lo toma. Medido en ATT1 el 2026-10-09, el día que se armó el primer contacto: una persona tocó «Envíame el enlace» en la plantilla del carrito, la admisión dio `22000 inbound_canonical_conversation_conflict` en los 8 intentos del bridge (unos 5 minutos) y el agente no le contestó; su primer contacto se canceló dos minutos después del último intento.
  - El freno ya no cuenta un primer contacto en `pending`, `deferred` o `retryable_failed` cuya intención tiene la clasificación del carrito o del pago fallido (`confirmed_abandonment`, `payment_failure_supported`) o una compra (`intent_purchased`, `purchase_by_identity`, `intent_purchase_ambiguous`). Con cualquiera de esos, `_portable_precheckout_stop_reason` da un motivo y la reevaluación lo cancela siempre.
  - Siguen frenando como antes: el primer contacto que todavía va a salir, el que está en `delivery_unknown`, el que solo frena un caso de Hotmart abierto de otra intención y las acciones de cualquier otro ancla.
  - Se aplica sobre la definición viva (el método de `20261005000100`) y es idempotente: si el bloque ya está, no cambia nada. En una base sin `20261001000400`, como la de Johanna, avisa y no cambia nada.

### Agregado

- **`pilot_proactive_contact_caps`** (migración `20261007000100`): un renglón por tenant con `max_request_starts` arranques en `request_window`. Lo carga el aprovisionamiento de cada instancia con el valor que decide su dueño; la migración no siembra ninguno.
- **El bloque en `authorize_lancemos_pilot_request_start`**, la función que llaman `mark_lancemos_pilot_request_started`, `mark_portable_payment_failure_request_started` y `mark_portable_precheckout_request_started`.
  - Con tope, cuenta los arranques autorizados de la persona en **todos** los scopes del tenant dentro de la ventana. La persona es el contacto y cualquier otro contacto de la misma cuenta con una identidad de WhatsApp en alguna de las dos formas del teléfono (52/521, 54/549).
  - Llegado el tope devuelve `pilot_contact_proactive_cap_reached`: no autoriza, no consume cupo del scope y no deja evento.
  - Va después del replay, la cohorte y la audiencia, y antes de los topes del scope. Un `pg_advisory_xact_lock` por tenant y teléfono canónico serializa a dos scopes que arrancan a la vez para la misma persona (el lock del control es por scope).
  - Un arranque frenado se comporta como uno con el tope del scope agotado: el intento queda reservado, se reintenta en cada lease y sale si la ventana se libera antes de que la acción venza.
- La migración modifica la función sobre su definición viva, con el texto exacto contado (el método de `20261005000100`). El texto está igual en las cuatro definiciones, así que vale para la base de Johanna, que no tiene las migraciones del 2026-09-29 al 2026-10-01, y para la de ATT1.
- **`landing_por_formulario` y `landing_por_formulario_modo` en `[adaptadores.ghl]`**, las dos o ninguna ([contrato](docs/contracts/ghl-precheckout-adapter-v1.md#la-landing-de-un-formulario-declarado-140)). GHL manda en `attributionSource.url` la página donde empezó la visita. Medido en ATT1 el 2026-10-08: de 30 carritos reales desde la conexión del orgánico, los 2 que no se cubrieron llenaron un formulario y los perdió esto. Uno de la landing B quedó con la oferta de la D, y uno de la D quedó en `422 ghl_landing_unknown` porque la visita empezó en otra página.
  - La tabla va del id del formulario al `landing_id` de una oferta. Un formulario que vive en más de una página no se declara y sigue por la URL.
  - `sombra`: la landing sigue saliendo de la URL; la línea de log del pedido termina en `form_landing=would_fix:<landing_id>` y sale como warning cuando el formulario la habría cambiado, también en un rechazo por landing desconocida.
  - `activo`: la landing sale del formulario (`form_landing=fixed:<landing_id>`, warning). La atribución sigue saliendo de la query de la URL, la del anuncio.
  - El manifiesto no carga si falta una de las dos claves, si el formulario no está en `formularios` o si el `landing_id` no es el de exactamente una oferta.
  - `/ready` suma `ghl_form_landings: <modo>:<n>` con el adaptador prendido, y `validate` lista los pares.

### Pruebas

- `tests/sql/followup_engine/validate_pilot_proactive_contact_cap.mjs` (PGlite, en `npm test`), diez casos:
  - sin renglón no hay tope;
  - con tope, la persona autorizada en un scope queda frenada en el otro, sin ledger ni evento;
  - el replay no se frena;
  - otra persona entra;
  - otro contacto con la otra forma del teléfono se frena;
  - otro tenant no se cuenta;
  - pasada la ventana entra;
  - con 2 entran dos y el tercero no;
  - la tabla está cerrada a la API;
  - el bloque está una vez y antes de los topes del scope.
- `tests/sql/followup_engine/validate_portable_inbound_template_adoption.mjs` (PGlite, en `npm test`), caso 3b: formulario con el primer contacto planificado, carrito aceptado antes de su hora y la respuesta con el primer contacto en `pending`. Se adopta; sin la clasificación del carrito, dentro de una transacción que se deshace, frena (`22000`); y a su hora la reevaluación real lo cancela (`superseded_by_provider_event`) sin intento, con la conversación en `draft_only`. Sin la migración, el caso falla con el mismo `22000` que dio ATT1. La migración se corrió además dos veces sobre una copia de la base de ATT1 en `v1.3.3` (Postgres 17 local): la segunda no cambia nada, los permisos quedan iguales y el inventario de esquema da `fingerprint_present` con ella y `fingerprint_absent` sin ella.
- `tests/test_pilot_proactive_contact_cap_migration.py`: el texto del ancla está una sola vez en cada una de las cuatro definiciones de la función, y el inventario de esquema la cubre.
- La landing por formulario, con variantes derivadas de las dos capturas de GHL del 2026-09-29 (cambiando el `mediumId` o el host y la ruta de la URL): el traductor en los tres modos, una URL que no se parte, un binding sin la landing declarada y un teléfono rechazado (`tests/test_ghl_precheckout_adapter.py`); la ruta, con el nivel y el final de la línea de log (`tests/test_ghl_precheckout_adapter_http.py`); las reglas del manifiesto y que el ejemplo de la documentación carga (`tests/test_instance_manifest.py`); `/ready` (`tests/test_instance_wiring.py`) y `validate` (`tests/test_instance_cli.py`).
- `tests/sql/followup_engine/validate_ghl_precheckout_adapter_form_landing.mjs` (PGlite, en `npm test`): el golden nuevo del envío de UHTa traducido en `activo` (`ghl_form_webhook_uhta_from_landing_d_active_derived_20260929`) entra por la RPC real con el binding de ATT1 de hoy (`bmaztyhg` en la landing B): intención en `alimenta-tu-tiroides` / `bmaztyhg`, con permiso, `consented_intent_ok` y el `sck` del anuncio de la -d. La misma persona por la -d deja su propia intención.

### Qué hace quien actualiza

Correr las migraciones. La de `20261009000200` se puede aplicar antes que el resto, sobre `v1.3.3` (ATT1 lo hace así): cuando la cadena llegue a ella, ve el bloque y no cambia nada. Sin renglón en `pilot_proactive_contact_caps` no cambia nada. Para prender el tope en una instancia, su aprovisionamiento carga el renglón del tenant. El tope no necesita redesplegar el bridge.

La landing por formulario sí: sumar las dos claves al manifiesto de la instancia y redesplegar el bridge, primero con `"sombra"` y, cuando las líneas `would_fix` muestren lo esperado, con `"activo"`. Para volver atrás, sacar las dos claves y redesplegar **antes** de bajar a una versión anterior a la 1.4.0: su parser no conoce las claves y no carga un manifiesto que las tenga.

### Lo que queda fuera

- **La carrera entre dos scopes no se prueba con dos conexiones:** PGlite es una sola. La cubre el lock por diseño; la prueba con Postgres real queda pendiente.
- **La reactivación y el seguimiento con cupón no pasan por esta puerta**, así que no cuentan ni se frenan.

## [1.3.3] - 2026-10-06

Arregla la respuesta a una nota de voz, que no salía nunca. Le cambia algo a toda instancia con la transcripción prendida (`CHATWOOT_AUDIO_TRANSCRIPTION_ENABLED`), que hoy son Johanna y ATT1. No trae migración ni variables nuevas.

### Arreglado

- **La respuesta a un audio se descartaba en el último control antes de salir.** Antes de mandar cada parte, el bridge relee de Chatwoot el mensaje que disparó el turno (`_current_authorization_result`), y ese control exigía texto. Una nota de voz llega con `content: null` y un adjunto de audio, así que devolvía `blocked` / `invalid_trigger_message` y la respuesta se abandonaba sin una línea en el log. Para entonces, la transcripción, la propuesta del agente y el divisor ya habían corrido.
  - Ahora el control acepta un mensaje del lead que trae texto **o** un audio. El criterio del audio es `needs_audio_transcription`, el mismo que decide transcribirlo.
  - Un entrante sin texto ni audio (una imagen, un sticker) se sigue bloqueando, como antes.
  - Pasó en ATT1 el 2026-10-05 a las 23:59Z (conversación 1, mensaje 2682). En Johanna no se perdió ninguna respuesta: al 2026-10-06 no había transcripto ningún audio (su cache de transcripciones no existe).
  - El test de la app con audio estaba verde porque usa un cliente de Chatwoot falso, que no pasa por este control.

### Pruebas

- `tests/test_chatwoot_audio_trigger.py` corre el cliente real contra la conversación capturada en ATT1 (`tests/fixtures/chatwoot_audio_trigger_inbox_11_conv_1_20261006.json`, anonimizada adentro del contenedor). La respuesta al audio sale, y el mismo mensaje sin el audio, o con una imagen, se sigue bloqueando. Sin el arreglo, la primera prueba da `invalid_trigger_message`, igual que en producción.

### Qué hace quien actualiza

Pasar el bridge a la imagen 1.3.3, y nada más. Volver atrás es volver a la imagen anterior.

### Lo que queda fuera

- **El monitor de conversaciones trabadas no rescata un audio.** Para tomar una conversación pide texto en el último mensaje del lead (`list_stalled_conversations`). Un audio cuyo webhook no llegó al bridge sigue sin respuesta.

## [1.3.2] - 2026-10-05

El marcador del recuperador en el `sck` se separa con `~`, como el del anuncio: es el estándar de Lancemos (E46) y lo decidió Dan el 2026-10-05 ([ADR-0022](docs/decisions/0022-el-marcador-del-sck-usa-la-tilde.md)). Además, la compra que trae el `sck` del anuncio delante del marcador deja de rechazarse. Trae una migración, que va **después** del bridge. Le cambia algo a Johanna y a ATT1: los links nuevos salen con `~`.

### Cambiado

- **El marcador se escribe con `~`:** queda `hermes~v1~<ULID>` y, con el `sck` del anuncio delante, `<sck del anuncio>~hermes~v1~<ULID>`. Antes se escribía con `|`.
  - Solo cambia el separador. Los campos, su orden, `src=hermes` y `sck_format_version = 'v1'` quedan igual.
  - La `~` viaja literal en la URL. La `|` que traiga el `sck` de un anuncio de un linaje viejo se sigue encodeando a `%7C`.
  - Lo escriben las dos reservas: la compartida y la portable de ATT1.
- **Los lectores aceptan las dos formas, para siempre.** Los links que ya salieron con `|` siguen llegando, y una reserva que quedó en `reserved` se reusa tal cual. Las dos formas no se mezclan: `hermes|v1~…` se rechaza.
  - En el bridge: `_HERMES_SCK_TAIL` y `_sck_carries_hermes_marker`.
  - En la base: los dos `CHECK` de `checkout_link_issuances`, el correlador y la admisión de la compra.

### Arreglado

- **Se rechazaba una compra hecha con un link que llevaba el `sck` del anuncio.** `admit_and_correlate_hotmart_checkout_issuance_v2` aceptaba solo el marcador solo (su única definición es la de `20260914000100`).
  - El bridge reconoce esa compra como del recuperador y la manda a esa función.
  - La base lanzaba `22023`, el bridge le contestaba `503` a Hotmart, y la compra no se guardaba ni marcaba la intención como comprada.
  - Hotmart reintenta y cuenta esas fallas para desactivar el webhook.

  Ahora la función acepta lo mismo que el correlador. Le pasaba a los links `full` y `sck_only`, que existen desde el 2026-09-22. En los logs que quedan del bridge de Johanna (del 2026-09-28 23:57Z al 2026-10-05) no hubo ningún `5xx` a Hotmart. Reproducido en PGlite: sin la migración, `ERROR 22023 hotmart_checkout_issuance_admission_invalid`; con ella, `inserted`.

### Agregado

- **El link de pago en el botón de una plantilla** ([PR #216](https://github.com/danschwab2002/ai-appointment-setter/pull/216)), mergeado después de 1.3.1. Queda apagado: se prende solo con `PAYMENT_LINK_TEMPLATE_NAME` y `PAYMENT_LINK_TEMPLATE_LANGUAGE`, y sin ellas el link sale como antes, en un solo mensaje.

### Qué hace quien actualiza

1. **Primero, el bridge.** El de 1.3.1 rechaza la fila con `~` que devuelve la reserva, y el link no sale. Tampoco reconoce una compra con `~`.
2. **Después, la migración `20261005000100`.**
   - Modifica cuatro funciones sobre su definición viva, reemplazando un texto exacto. Si alguna no tiene el texto esperado, falla con `55000` y no cambia nada.
   - No depende de las migraciones del 2026-09-29 al 2026-10-01. En Johanna se aplica sola, desde una copia de `supabase/` con las migraciones ya aplicadas más esta. Probado en PGlite con esa cadena parcial.
   - En una instancia con manifiesto, se aplica con `aplicar-migraciones.sh`.
3. **Se verifica con el próximo link que emita el agente:** `sck_value` tiene que terminar en `~hermes~v1~<ULID>`, y la URL tiene que llevarlo literal. Hay que tocar el link y ver que el checkout de Hotmart lo recibe entero.

**Sin vuelta atrás del bridge después de la migración.** Con la base escribiendo `~`, un bridge anterior a 1.3.2 rechaza la fila de cada reserva y el link no sale. Las filas ya reservadas con `~` son inmutables, así que tampoco salen por replay. Para volver a un bridge anterior, primero va la migración inversa: el mismo bloque con los textos invertidos, solo para las dos reservas. Antes de la migración, en cambio, volver atrás es solo cambiar de imagen. Por eso conviene dejar correr el bridge 1.3.2 un rato con tráfico real antes de aplicarla.

**Si después se aplican en Johanna las migraciones del 2026-09-29 al 2026-10-01**, `supabase db push` va a pedir `--include-all`, porque son anteriores a esta. Esa combinación está probada en `tests/sql/followup_engine/validate_sck_marker_on_johanna_chain.mjs`.

## [1.3.1] - 2026-10-01

Arregla el límite conocido de 1.3.0 que frenaba todo flujo de salida (H7): quien respondía a una plantilla del dispatcher (carrito, pago fallido o primer contacto) no pasaba la admisión entrante. Ahora, en una instancia con manifiesto, la respuesta adopta la conversación que abrió la plantilla y llega al agente. Trae dos migraciones, que van **antes** que el bridge. Johanna no cambia. No tiene E2E real: los tres botones se prueban con un teléfono de prueba en el inbox de la instancia antes de abrir un flujo de salida a personas reales.

### Arreglado

- **La respuesta a una plantilla del piloto entra a la admisión** (migración `20261001000400_portable_inbound_adopts_template_conversation.sql`). La aceptación del envío deja la conversación canónica del caso en `automation_status = 'enabled'`, y la admisión entrante solo toma una `draft_only` (`22000 inbound_canonical_conversation_conflict`): sin respuesta del agente, sin enlace y sin derivación. RPC nueva, solo para `service_role`: `admit_portable_inbound_commercial_case_v1`, con la misma firma y la misma salida que `admit_inbound_commercial_case_v2`.
  - Toma los mismos tres advisory locks que la admisión base, en el mismo orden. Si todavía no hay admisión para esa conversación de Chatwoot, bloquea la identidad que contesta y después su conversación (`enabled`, sin `human_takeover`, en un status vivo).
  - La adopta solo si se cumplen tres condiciones. Primera: una plantilla del piloto salió ahí (mensaje `durable_followup` → intento `accepted_by_chatwoot` → acción → caso de recuperación de esa identidad, contacto y conversación → su fila en `pilot_recovery_case_bindings`). Segunda: el contacto no está dado de baja (`opted_out`, `blocked`, `restricted` o `do_not_contact`) y no hay una baja de Chatwoot de ese móvil, en ninguna de sus dos formas, aunque no haya quedado aplicada a ese contacto (`unmatched` o `ambiguous`, por ejemplo si entró por la forma `521` sin identidad): los mismos estados que frena el arranque del piloto (`_portable_chatwoot_opt_out_stop`). Tercera: ninguna acción de recuperación de esa persona, en esta conversación o todavía sin conversación, está en `pending`, `deferred`, `retryable_failed` o `delivery_unknown` (un paso pendiente atado a otra conversación de la misma persona no frena).
  - Si la adopta, pasa la conversación a `draft_only` (`version + 1`) y deja un `conversation_events` `inbound_adopted_template_conversation` con la plantilla y su acción. Después delega siempre en la v2, sin tocarla. Si no adopta, el resultado es el de antes, y todo pasa en la misma transacción.
- **En una conversación adoptada, el caso es el `inbound_sales`** (migración `20261001000500_commercial_case_lookups_by_inbound_kind.sql`). La conversación adoptada tiene dos casos: el `cart_recovery` que copia el trigger de sombra y el `inbound_sales` de la respuesta. Cuatro RPC cuentan los casos de la conversación y con dos abortaban con `P0001 *_ambiguous_case`: `mark_human_handoff_attended`, `claim_conversation_reactivation`, `resume_paused_conversation` y `claim_conversation_followup_v1`. Una derivación («Necesito ayuda») no podía volver nunca al agente. La migración redefine las cuatro desde su definición vigente (`20260927000300` y `20260928000400`) con un solo cambio: **si la conversación tiene el evento `inbound_adopted_template_conversation`**, el conteo y el select miran solo `case_kind = 'inbound_sales'`. Sin el evento cuentan todos los casos, como antes. Dos `inbound_sales` en una conversación adoptada siguen dando ambiguo.
  - **Por qué el filtro depende del evento.** Una medición de solo lectura en la base de Johanna (2026-10-01, autorizada por Dan) dio 2 conversaciones con un caso que no es `inbound_sales` (un `cart_recovery` cada una) y ninguna con más de un caso. Un filtro para todas las conversaciones les habría cambiado el resultado: su único caso pasaba a `not_found`. Johanna nunca tiene el evento: ninguna función ni el bridge lo escriben salvo la `000400`, y su bridge no la llama. La tabla no lo impone (`event_type` no tiene check y `service_role` conserva el DML), así que un insert a mano de ese evento cambiaría el resultado en esa conversación; por eso la verificación de abajo se repite.
- **Con manifiesto, el bridge admite por la RPC portable** (`src/bridge/supabase.py`, `src/bridge/app.py`). Las tres llamadas a la admisión pasan por ella: la del mensaje, la reautorización antes de responder y la readmisión después de reanudar. Sin manifiesto, el bridge llama a `admit_inbound_commercial_case_v2` con los mismos argumentos de siempre. Los outcomes, los errores y su texto en el log (`inbound_commercial_case_admission_failed`) son los de la v2, así que el log no dice cuál de las dos falló. El respaldo de 1.3.0 para la baja se mantiene: si la admisión falla, un «No más mensajes» se registra igual. No hay flags nuevos.

### Qué cambia con cada botón de la plantilla, en ATT1

- **«Envíame el enlace»:** el agente ve la plantilla, como mensaje suyo, y después el botón. Lo prueba `test_the_agent_sees_the_template_before_the_button` con la forma del mensaje de plantilla capturado en Johanna (mandado por el agent bot); que el dispatcher de ATT1 lo mande con el agent bot configurado en el bridge no está medido en la instancia, y si saliera con otro remitente el agente vería solo el botón. El enlace se reserva sobre el caso que devolvió la admisión, por `reserve_portable_checkout_issuance_v2`, con la oferta, la intención y la atribución del formulario.
- **«Necesito ayuda»:** decide el agente, como en Johanna; no hay una regla que derive siempre. Si deriva, la derivación pasa. Cuando la persona del equipo contesta, la atención queda marcada, y el macro de reanudación devuelve la conversación al agente.
- **«No más mensajes»:** la conversación se adopta y se admite, y la baja la registra el camino normal. El agente no corre.

### Qué tiene que hacer una instancia

- **En la base, las dos migraciones en orden, antes de desplegar el bridge 1.3.1:** `20261001000400` y `20261001000500`. Solo crean y reemplazan funciones; no tocan tablas ni filas. Si el bridge sale primero, la RPC no existe: PostgREST responde `404` (`PGRST202`) y el bridge reintenta cada entrante sin tope hasta que se aplique. Mientras tanto, la baja se sigue registrando por el respaldo de 1.3.0.
- **Verificación:** `scripts/supabase_schema_inventory.sql` da `fingerprint_present` en las dos y `scripts/supabase_acl_inventory.sql` da `ok`, con `admit_portable_inbound_commercial_case_v1(text, integer, bigint, text)` solo para `service_role`.
- **Johanna:** aplicar las dos migraciones y nada más. Su bridge corre sin manifiesto y no llama a la `000400`. Las cuatro funciones de la `000500` devuelven lo mismo que antes para cada fila de su base, incluidas las dos conversaciones con un único `cart_recovery`. Después de aplicarlas, `select count(*) from public.conversation_events where event_type = 'inbound_adopted_template_conversation';` tiene que dar 0 en su base, y seguir en 0: si alguna vez da más, hay que mirar esa conversación, porque ahí las cuatro funciones pasan a mirar solo el `inbound_sales`.
- **Para cortar las respuestas, el interruptor es el agente.** Apagar el piloto frena los envíos, no las respuestas a plantillas que ya salieron: una conversación adoptada llega al agente aunque el scope esté pausado. Para que el agente no conteste se apaga `CHATWOOT_CUT_B_AGENT_ENABLED` (decisión de Dan del 2026-10-01).

### Lo que queda fuera

- **No se adopta (lo atiende una persona en Chatwoot, con el resultado de 1.3.0):** un contacto dado de baja, `blocked`, `restricted` o `do_not_contact`, o con una baja de Chatwoot de su móvil que no quedó aplicada al contacto, que responde a una plantilla vieja (decisión de Dan del 2026-10-01); una persona con un paso de recuperación todavía pendiente en esa conversación o sin conversación, porque ATT1 sigue con un solo toque; y un contacto con dos identidades del mismo móvil (la base da `23505`). En los tres casos la v2 da el conflicto y solo la baja se registra.
- **Una respuesta confirmada antes de la reconciliación de su plantilla.** Si el envío quedó en `delivery_unknown` y la respuesta entra antes de que la reconciliación lo acepte, no hay conversación que adoptar: la admisión crea una en `draft_only` y la aceptación tardía le ata el caso de recuperación. Quedan `cart_recovery` e `inbound_sales` sin el evento de adopción. El agente contesta, pero una derivación en esa conversación no se puede marcar atendida (sigue ambigua). Lo fija `real_postgres_portable_inbound_adoption.py`; arreglarlo es una decisión pendiente de Dan, con dos caminos: que la aceptación o la reconciliación que ata un caso a una conversación `draft_only` con `inbound_sales` escriba el evento (toca `record_and_finalize_followup_acceptance`, compartida con Johanna, y habría que medirlo), o que el filtro de la `000500` se condicione también a una admisión portable. Mientras tanto, la prueba de los tres botones con el teléfono de prueba responde recién cuando el intento de la plantilla está `accepted_by_chatwoot`; si quedó en `delivery_unknown`, se espera la reconciliación antes de apretar, o la prueba cae en este caso y no prueba la adopción.
- **El seguimiento con cupón en una conversación adoptada.** `claim_conversation_followup_v1` ya encuentra el caso, pero frena en `issuance_missing_default_offer`, porque reserva por `reserve_chatwoot_checkout_issuance_v2`, que necesita el catálogo de ofertas de Johanna. La planificación del descuento posterior a la respuesta (`plan_commercial_ally_post_inbound_discount`) pide la conversación en `enabled` (`20260905000100:276`), así que en una adoptada daría `conversation_not_applicable` (leído en el código, no medido). ATT1 tiene `descuento = false`. La reactivación, en cambio, reclama una conversación adoptada si `reactivacion` se prende.
- **No hay captura de la pulsación de un botón `QUICK_REPLY` en el inbox de ATT1.** Las pruebas usan la captura de la conversación 200 con el texto del botón, que sale del catálogo `chatwoot_inbox_11_message_templates_20261001.json`. Que un botón llega como texto en `content` está capturado en la conversación 158, del inbox 9.

### Pruebas

- `validate_portable_inbound_template_adoption.mjs` y `validate_commercial_case_lookups_by_inbound_kind.mjs` (PGlite, dentro de `npm test`): cada freno de la adopción (entre ellos la baja de la forma `521` que la RPC real deja `unmatched`, cada eslabón de la prueba de la plantilla roto de a uno y una conversación ya admitida que vuelve a `enabled`), la forma de Johanna sin fila previa y el ACL; y las cuatro RPC en cinco estados de conversación, comparadas con y sin la `000500`. Los estados son: adoptada con un `cart_recovery` al lado, adoptada con dos `inbound_sales`, y tres formas sin adoptar, entre ellas la de las dos conversaciones medidas en Johanna, que dan lo mismo en las dos bases.
- `validate_att1_portable_chain.mjs`, caso 10 (reemplaza al 9.4b, que fijaba el rechazo): las tres plantillas aceptadas por el dispatcher real, la respuesta, el enlace con la intención, la derivación, la atención, la reanudación, la readmisión y la baja de otro lead. Como control, la v2 directa sigue dando `22000`.
- `real_postgres_portable_inbound_adoption.py`, en el CI (paso *Verify portable inbound template adoption on PostgreSQL*): dos respuestas a la vez a la misma plantilla (una vez ordenadas y otra esperando las dos juntas detrás de una barrera, comprobado en `pg_stat_activity`), la respuesta contra la reconciliación de un intento en vuelo, y la respuesta contra una transacción que tiene tomada solo la fila de su conversación y la pausa (espera, no adopta y da lo mismo que la v2). Ese lock de la fila es defensa en profundidad: hoy todo camino que cambia esa conversación bloquea antes la identidad, así que la prueba usa un escritor directo.
- `tests/test_portable_inbound_template_adoption_migration.py` y `tests/test_commercial_case_lookups_by_inbound_kind_migration.py`: cada función contra su definición vigente. `tests/test_supabase.py` y `tests/test_whatsapp_inbound_equivalence.py`: la ruta, el cuerpo, los errores, los tres botones por la portable, la plantilla antes del botón en el contexto del agente, y que sin manifiesto la portable nunca se llama.

## [1.3.0] - 2026-10-01

Tres cosas para una instancia con manifiesto: el primer contacto tras el formulario de la landing, la equivalencia de teléfonos de WhatsApp entre fuentes, y la aceptación escrita del riesgo del adaptador de GHL. Todo lo nuevo está apagado por defecto o atado al manifiesto. Trae tres migraciones. Ningún flujo nuevo tiene todavía un E2E real: el envío del primer contacto y la forma de entrega del teléfono se confirman en el E2E de la instancia antes de prender.

### Agregado

- **Equivalencia de teléfonos de WhatsApp, en la base** (migración `20261001000100_whatsapp_phone_equivalence.sql`). El mismo móvil llega con dos formas: el formulario guarda `52` + 10 dígitos en México y `54` + 10 en Argentina; Hotmart y el `wa_id` traen `521` + 10 y `549` + 10. Toda comparación era exacta, así que el lead que dejó el formulario se perdía antes del permiso (`payment_failure_correlation_unresolved`) y la compra portable no marcaba `purchased`. Ahora el runtime portable compara en forma canónica y no reescribe nada al guardar.
  - `_whatsapp_phone_canonical` y `_whatsapp_phone_variants`: solo dígitos, y solo `521` + 10 → `52` + 10 y `549` + 10 → `54` + 10, anclado por largo. Brasil queda afuera: no hay medición.
  - `_correlate_portable_hotmart_purchase_intent`: el correlador portable, derivado del compartido. Lo llaman `admit_portable_hotmart_cart_abandonment` y `admit_portable_hotmart_payment_failure`; `admit_portable_hotmart_purchase_approved` compara el teléfono por sus formas.
  - `_portable_consented_intent_reason` suma dos chequeos, al planificar y al arrancar el envío, en carrito y pago fallido, en los modos de audiencia con consentimiento: `consented_intent_contact_phone_mismatch` (el teléfono del contacto, que es adonde sale el envío, tiene que ser el consentido) y `consented_intent_prior_opt_out` (no hay un opt-out de Chatwoot en ninguna de las dos formas).
  - `plan_portable_payment_failure_recovery` deja `phone_match` (`exact` o `whatsapp_equivalent`) en la evidencia del permiso.
  - `reserve_portable_checkout_issuance_v2`, solo para `service_role`: la reserva del enlace de pago del entrante, derivada de la compartida (ver *Cambiado*).
  - Solo crea y reemplaza funciones; no toca tablas ni filas. Contratos: `docs/contracts/commercial-ally-runtime-v1.md` y `docs/contracts/hotmart-purchase-intent-correlation-v1.md` (§8).
- **Equivalencia de teléfonos de WhatsApp, en el bridge**, solo con manifiesto (`src/bridge/phones.py`).
  - La resolución del evento de Hotmart busca el contacto por las dos formas, y planifica con la identidad de WhatsApp que el contacto ya tiene para ese móvil (la del formulario o la del `wa_id`) en vez de sumarle otra con el teléfono de Hotmart.
  - Un mensaje entrante usa la identidad que la base ya tenía si hay exactamente una entre las formas del `wa_id`; con más de una usa la textual y deja un warning sin el número. El chequeo de opt-out del entrante mira todas las formas.
  - Las proyecciones a Chatwoot (macro de opt-out, asignación y nota de la derivación) prueban la forma guardada y después la otra.
  - En un primer contacto, el dispatcher resuelve el destinatario en Chatwoot antes del gate final, buscando las dos formas: el gate recibe el `wa_id` que Meta va a recibir. Si no es el teléfono consentido cierra con `chatwoot_recipient_phone_mismatch`, sin reintento.
  - Con qué forma se manda y se crea el contacto de Chatwoot lo decide una sola función, `whatsapp_delivery_phone`: México como `521` + 10, Argentina como `54` + 10, lo demás igual. Sale de una medición del 2026-10-01 sobre el Chatwoot de producción (versión 4.13, otro inbox): los contactos mexicanos que escribieron tienen todos el `wa_id` con el `1`, Chatwoot no normaliza México, y sí normaliza Argentina hacia `54…`. No está probado en ATT1.
- **Primer contacto portable tras el formulario, en la base** (migración `20261001000200_portable_precheckout_first_contact.sql`). El envío del formulario con consentimiento de WhatsApp planifica un único primer contacto: caso de fuente `landing`, ancla `precheckout_intent`, demorado el `grace_period` de la política del scope contado desde el envío que dispara.
  - Se cancela (`cancelled`, nunca `won`) si antes de salir la persona compra, la compra queda ambigua, llega su carrito o su pago fallido, se da de baja en cualquiera de las dos formas del teléfono o pierde el consentimiento.
  - Tampoco sale si una conversación del contacto está derivada a una persona, pausada, cerrada o bloqueada (`precheckout_conversation_handoff`): es el criterio `blocked_handoff` del primer toque de Johanna, y se mira al planificar, al reevaluar y al arrancar.
  - Hay un solo primer contacto vivo por persona, y un toque aceptado (o de resultado desconocido) frena otro por 24 h.
  - El contacto se crea solo con el scope armado y nunca en `consented_intent_in_cohort`. Un formulario que llega con el scope desarmado no se planifica después.
  - La admisión del formulario no se pierde si el plan falla: el motivo queda en `portable_precheckout_first_contact_plans`, que guarda solo ids y códigos.
  - RPC nuevas, solo para `service_role`: `admit_and_plan_portable_lead_precheckout`, `reevaluate_portable_precheckout_action`, `mark_portable_precheckout_request_started` y `get_portable_precheckout_pilot_runtime_status`.
  - Tres checks suman un valor: `recovery_cases.source` admite `landing`; `recovery_case_events.event_role` y `followup_sequences.reason` admiten `precheckout_intent`. El de `source` nació sin nombre, así que la migración lo busca por definición (el único check de la tabla sobre `source` que acepta `hotmart` y `simulator`); con cero o con más de uno aborta entera con `recovery_cases_source_check_not_found`.
- **Primer contacto portable tras el formulario, en el bridge**, detrás de `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED` (apagado por defecto, solo con manifiesto).
  - `/webhooks/lead` y el adaptador de GHL admiten el formulario y planifican el primer contacto en una sola RPC. La respuesta HTTP no cambia.
  - El dispatcher manda la acción de ancla `precheckout_intent` con la plantilla de `[plantillas.precheckout]`, con la reevaluación y el arranque propios del flujo. Sin esa plantilla no manda nada (`first_touch_template_not_configured`): nunca usa la del carrito.
  - `/ready` suma `portable_precheckout_first_contact` con el estado del scope del formulario, y responde `503` si ese scope no está publicado para el flujo.
  - Variables nuevas, vacías por defecto: `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY`, `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION` y `WABA_PRECHECKOUT_TEMPLATE_NAME`. Están en `.env.example` y en `compose.yaml`.
  - Contrato: `docs/contracts/portable-precheckout-first-contact-v1.md`.
- **Aceptación escrita del riesgo del adaptador de GHL** (cabo LAN-054). `[adaptadores.ghl]` admite tres claves opcionales, todas o ninguna: `riesgo_aceptado_por`, `riesgo_aceptado_el` (fecha TOML) y `riesgo_contrato`, que tiene que ser `ghl-precheckout-adapter-v1`.
  - `riesgo_aceptado_por` no carga con `<` o `>`: el marcador de los ejemplos de la documentación, copiado sin editar, no levanta la guarda. `riesgo_aceptado_el` no carga si es posterior a hoy (con un día de margen sobre la fecha UTC).
  - Con la aceptación, `flujos.precheckout` y `flujos.pago_fallido` arrancan con la sección en el manifiesto, y el formulario de GHL admite y planifica el primer contacto.
  - `/ready` suma `ghl_adapter_risk` (`accepted:<fecha>:<contrato>`, `not_accepted` o `no_adapter_section`), sin el nombre de quien acepta, que va solo al log de arranque.
  - `instance_cli validate` informa la aceptación.
  - Migración `20261001000300_pilot_scope_audience_mode_read.sql`: suma `get_lancemos_pilot_scope_audience_mode(text,integer)`, solo para `service_role`. No reemplaza nada ni toca tablas.
  - Contrato: `docs/contracts/ghl-precheckout-adapter-v1.md`, *La aceptación escrita del riesgo*.
- **Pruebas de punta a punta de ATT1.** Fixture nuevo `tests/fixtures/chatwoot_inbox_11_message_templates_20261001.json`: el catálogo de plantillas del inbox 11, leído el 2026-10-01. Con él, el modo directo se prueba con las tres plantillas de primer contacto de ATT1 (`att1_carrito_abandonado_01`, `att1_compra_fallida_01` y `att1_interes_precheckout_01`: dos variables, `es_MX`, `MARKETING`, tres botones `QUICK_REPLY`), así que con esa captura ninguna necesita `WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY`. `validate_att1_portable_chain.mjs` suma el primer contacto tras el formulario con el scope y la política de la instancia, y Hotmart en `521`/`549` contra una intención en `52`/`54`. `tests/test_att1_production_settings.py` arranca el bridge desde variables de entorno con el set completo de cada flujo y con los tres juntos. No cambia nada en tiempo de ejecución.
- **La sonda de Postgres real del primer contacto corre en el CI** (`real_postgres_portable_precheckout_first_contact.py`, paso *Verify portable first contact on PostgreSQL*): aplica la cadena entera en un Postgres 17 con los privilegios por defecto de Supabase, corre el inventario de ACL completo y prueba con sesiones reales lo que PGlite no puede (la reevaluación contra un reenvío del formulario, la compra y el opt-out en vuelo, y el `lock timeout` adentro del plan).

### Cambiado

- **La guarda del adaptador de GHL cuelga del manifiesto, no del flag.** En 1.2.0, con `GHL_PRECHECKOUT_ADAPTER_ENABLED` el bridge no arrancaba con `flujos.precheckout`, sin salida. Ahora, con `[adaptadores.ghl]` en el manifiesto y sin la aceptación, no arranca con `flujos.precheckout` **ni con `flujos.pago_fallido`** en `true`, esté o no prendido el flag: apagarlo no saca de la base las intenciones ya admitidas, y el pago fallido concede el permiso desde la intención. Con `LANCEMOS_PILOT_BOUNDARY_ENABLED` tampoco arranca si el scope del piloto es de audiencia `consented_intent` o `consented_intent_in_cohort`, ni si ese modo no se puede leer; `/ready` responde `503` con `ghl_adapter_risk_not_accepted` o `ghl_adapter_risk_audience_unavailable`. Una instancia con la sección ve un warning `ghl_adapter_risk …` en cada arranque.
- **`instance_cli validate` da error, no aviso**, cuando un flujo bajo la guarda está prendido con la sección y sin la aceptación: ese manifiesto no arranca.
- **Con un opt-out previo, el pago fallido portable en un scope con consentimiento ya no se planifica** (`pilot_audience_consented_intent_prior_opt_out`). Antes se planificaba y lo frenaba la reevaluación. Nadie recibía el mensaje en ninguno de los dos casos.
- **El opt-out en la otra forma del teléfono ya frena el carrito y el pago fallido en `manual_cohort`** (migración `20261001000100`). Antes, un «No más mensajes» guardado sin contacto bajo el `wa_id` (`521…`, `549…`) no frenaba el carrito de esa cohorte cuando Hotmart traía el móvil en la otra forma (`52…`, `54…`), ni el pago fallido que usa el permiso que ese carrito concedió: el envío arrancaba y salía al mismo `wa_id` que pidió la baja. Lo que en 1.1.0 se prometió («el opt-out conserva precedencia en los tres modos») no se cumplía en ese caso. Ahora `mark_lancemos_pilot_request_started` y `mark_portable_payment_failure_request_started`, con el lock de opt-out de cada forma tomado (las de la identidad y las de `contacts.phone`), rechazan con `pilot_request_start_rejected` / `pilot_chatwoot_opt_out_stop` sin consumir cupo, en los tres modos de audiencia. La reevaluación no ve ese opt-out, así que el intento queda reservado y se rechaza en cada lease hasta que la acción vence. Contrato: `docs/contracts/lancemos-pilot-boundary-runtime-v1.md` (§3 y §9). Solo con la frontera del piloto: Johanna la tiene apagada y no cambia.
- **Con manifiesto, el enlace de pago del entrante busca la intención por las dos formas del móvil** (migración `20261001000100`). La reserva compartida usa el mismo id para exigir la identidad del caso y para buscar la intención, con igualdad exacta. Quien dejó el formulario con `52…` (`54…`) y escribía desde su `wa_id` `521…` (`549…`) sin identidad previa, o había escrito antes del formulario, recibía el enlace con la oferta por defecto, sin el `sck` ni el `fbclid` del formulario; la base fabricaba una segunda intención viva bajo el `wa_id`, y quien ya había comprado recibía otro enlace. Ahora el bridge reserva con `reserve_portable_checkout_issuance_v2`, derivada de la compartida con la intención y el opt-out mirados en cada forma: el enlace lleva la oferta, el `sck` y la intención del formulario, y quien compró recibe `purchase_already_approved` (pasa a una derivación). El chequeo de la identidad del caso sigue exacto; autorizar y cerrar no cambian. Contrato: `docs/contracts/commercial-ally-runtime-v1.md`. Sin manifiesto el bridge sigue llamando a la compartida.
- El worker le pasa el ancla de la acción al cliente de Supabase, que elige la RPC por ancla. Para toda ancla que no sea `precheckout_intent` llama a la misma RPC con el mismo cuerpo.
- **Con manifiesto, la baja no depende de la admisión entrante.** Si la admisión de un mensaje falla, el bridge mira igual el historial de la conversación: registra el opt-out de un «No más mensajes» y reconcilia el stop de quien ya se había dado de baja. Antes la baja se detectaba después de admitir, así que quien apretaba el botón de baja de una plantilla no quedaba registrado y su mensaje se reintentaba sin límite. Además, un rechazo determinista de la admisión (`22xxx` o un conflicto de dueño de la conversación) deja `chatwoot_cut_b_admission_rejected … reason=…` y termina como `failed` tras los intentos acotados. Sin manifiesto nada cambia.
- **En el modo directo, el nombre que va a la plantilla pasa un filtro** (`template_greeting_name_is_safe`), en los tres disparadores. El nombre llega del formulario público o de Hotmart con un teléfono que nadie verificó, y el saludo cae al nombre completo cuando la primera palabra no sirve (sin saludo, va tal cual): una URL, un email o un texto largo salían dentro de la plantilla aprobada («Hola, {{1}}. Soy del equipo de Dra. Nina.»). Ahora se admiten letras, marcas, emoji, espacios y `'’-.,`, hasta 60 caracteres y sin la forma de un dominio; si no pasa, el intento se cierra con `template_parameters_missing`, sin reintento y sin tocar Chatwoot, y el log no lleva el nombre. Los 19 patrones capturados del inbox 9 siguen saliendo. Contrato: `docs/contracts/approved-template-direct-dispatch-v1.md`, paso 4. Johanna no cambia: sin manifiesto no hay modo directo, y `resolve_greeting_name` es el mismo.
- **Un arranque que la frontera del piloto rechaza ya no corta el lote del dispatcher** (carrito, pago fallido y primer contacto). Antes el rechazo (`pilot_request_start_rejected`: un tope, el piloto desarmado, un freno tardío) subía como error sin atrapar, las demás acciones reclamadas quedaban con el lease tomado y el motivo se perdía. Ahora queda en el log (`durable_request_start_rejected … reason=…`) y el lote sigue. El intento queda reservado, igual que antes: lo resuelve el lease siguiente. Solo con la frontera del piloto: sin ella nada cambia.

### Qué tiene que hacer una instancia

- **En la base, las tres migraciones en orden, antes de desplegar el bridge 1.3.0:** `20261001000100`, `20261001000200` y `20261001000300`. La `000200` toma un lock exclusivo breve sobre `recovery_cases`, `recovery_case_events` y `followup_sequences`, y mientras corre también esperan las escrituras de `contacts`, `purchase_intents` y `precheckout_submissions` (las referencia la tabla nueva), con `lock_timeout` de 5 s: conviene aplicarla con poco tráfico. Si falla por el timeout no deja nada a medias y se reintenta. Las otras dos solo crean o reemplazan funciones.
- **Nada más, si no usa manifiesto o no prende nada nuevo**, salvo los dos casos siguientes.
- **Una instancia con manifiesto, antes de desplegar el bridge 1.3.0: que el proxy delante de PostgREST no registre la query.** Desde esta versión, cada mensaje entrante de un móvil de México o Argentina hace un `GET` a `/rest/v1/channel_identities` con las dos formas del teléfono en la query (`external_user_id=in.(…)`). La resolución de un evento de Hotmart ya ponía el email y el teléfono en la query de sus `GET` a `contact_points` (y ahora también a `channel_identities`), con mucha menos frecuencia: una vez por evento. Un nginx con el formato de log por defecto (`combined`) escribe la query entera, 200 incluido. Ejemplo y verificación: `docs/instalar.md`, paso 6. Queda la query en dos logs, solo cuando fallan PostgREST o la base: el de PostgREST en su nivel por defecto (`error`), en sus respuestas 5xx, y el `error_log` de nginx cuando PostgREST no responde. Con un proyecto de Supabase como base, ese log es de Supabase y la instancia no lo configura.
- **Una instancia con `[adaptadores.ghl]` en el manifiesto:** sin la aceptación escrita ya no arranca con `flujos.pago_fallido` en `true` ni con un scope del piloto de audiencia con consentimiento (ver *Cambiado*). Antes de actualizar hay que mirar si alguna de las dos cosas está prendida. Para prenderlas, quien decide por la instancia escribe las tres claves `riesgo_*` a mano en el manifiesto: ningún código del producto las escribe. Quitar la sección no saca de la base las intenciones que el adaptador ya admitió.
- **Orden con el manifiesto:** un bridge 1.2.0 no carga un manifiesto con las claves `riesgo_*`. Primero la imagen 1.3.0, después el manifiesto con la aceptación.
- **Para prender el primer contacto del formulario:**
  - publicar una política con el paso `first_contact` y un scope del piloto de fuente `landing` / `PRECHECKOUT_FORM_SUBMITTED`, que no sea `manual_cohort` y sea distinto del de recuperación, y cargar su clave y versión en `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY` y `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION`. El scope va antes que el flag: sin él `/ready` responde `503` y el healthcheck de `compose.yaml` deja el contenedor en `unhealthy`;
  - cargar `WABA_PRECHECKOUT_TEMPLATE_NAME` igual a `plantillas.precheckout.nombre`;
  - tener prendidos `flujos.precheckout`, una entrada del formulario (`LEAD_PRECHECKOUT_ENABLED` o `GHL_PRECHECKOUT_ADAPTER_ENABLED`), `LANCEMOS_PILOT_BOUNDARY_ENABLED`, `DURABLE_DISPATCHER_ENABLED`, `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`, `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED` con `HOTMART_HOTTOK`, y `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`;
  - recién entonces `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED=true`. Un formulario que llega con el scope desarmado no se planifica después.
  - Pasos y verificación: `docs/instalar.md`, paso 12, *El primer contacto tras el formulario*.
- **Dos datos para quien arma el entorno:** `HOTMART_PURCHASE_WORKER_ENABLED` va solo con `RESOLUTION_WORKER_ENABLED` (carrito o pago fallido), así que una instancia que prenda solo el primer contacto no lo carga; y el arranque exige `HOTMART_HOTTOK` solo para el primer contacto, así que para carrito y pago fallido hay que cargarlo a mano antes de apuntar el webhook de Hotmart (sin él responde `503`).
- **Antes de prender cualquier flujo de salida en una instancia nueva:** confirmar en su propio inbox la forma de entrega del teléfono (un móvil mexicano y uno argentino). La regla sale de una medición sobre otro inbox.
- **Johanna:** aplicar las tres migraciones y nada más. Corre sin manifiesto: ninguna función que ejecuta cambia, los tres checks aceptan todo lo que aceptaban, el flag nuevo no arranca sin manifiesto, su `/ready` es el mismo y su entrante no hace el `GET` nuevo a `channel_identities`.

### Lo que queda fuera

- **La respuesta a una plantilla de salida (H7), resuelta en 1.3.1.** En esta versión, quien responde a una plantilla del dispatcher (carrito, pago fallido o primer contacto) no pasa la admisión entrante: la aceptación deja la conversación en `enabled` y la admisión solo toma una `draft_only` (`22000 inbound_canonical_conversation_conflict`). No hay respuesta del agente, ni enlace, ni derivación. Esta versión solo asegura la baja (ver *Cambiado*). Lo arregla 1.3.1, con la adopción de esa conversación y dos migraciones nuevas (ver `[1.3.1]`). **Ningún flujo de salida se abre a personas reales con el bridge 1.3.0.**
- El tope de mensajes proactivos por persona entre flujos: quien recibió el primer contacto del formulario puede recibir después el del carrito. Se decide antes de abrir carrito y primer contacto juntos en `consented_intent`. **Resuelto en [1.4.0]**, apagado por defecto.
- Quien escribió primero por WhatsApp y después abandona el carrito o falla el pago: ese evento de Hotmart no encuentra el contacto del entrante y falla cerrado (se pierde la recuperación, no se manda nada indebido).
- **El enlace del entrante en Johanna.** Sin manifiesto sigue la reserva compartida: un lead mexicano que dejó el formulario con `52…` y escribe desde `521…` recibe el enlace sin la intención del formulario (oferta por defecto, sin su `sck`), como se midió en sus conversaciones 172, 184 y 211. Cambiarlo cambia el comportamiento de la instancia que factura y queda para una decisión aparte. El enlace del seguimiento con descuento (`claim_conversation_followup_v1`) también sale por la compartida; ATT1 no siembra políticas de descuento.
- La verificación fuera de banda de cada envío del adaptador de GHL contra la API de GHL.

## [1.2.0] - 2026-10-01

El adaptador del formulario de GHL. Una instancia cuya landing usa un formulario de GHL deja su intención de compra con consentimiento sin cambiar la landing: es lo que necesitan el permiso del pago fallido y la audiencia `consented_intent` de 1.1.0. Apagado por defecto: sin `GHL_PRECHECKOUT_ADAPTER_ENABLED` el bridge arranca igual que en 1.1.0.

### Agregado

- `GHL_PRECHECKOUT_ADAPTER_ENABLED` (apagado por defecto, solo con manifiesto): el adaptador del formulario de GHL, `POST /webhooks/adapters/ghl/lead-precheckout`. Traduce el webhook de un workflow *Form submitted* de GHL a `lead.precheckout` 1.1.0 y lo admite por la admisión portable, la misma de un formulario de landing: así una landing cuyo formulario es de GHL (ATT1) deja su intención con consentimiento sin cambiar la landing. Los formularios admitidos se declaran en `[adaptadores.ghl]` del manifiesto; la landing y la oferta salen de la URL del envío, comparada con `[[hotmart.ofertas]]`; las UTM y el `sck` se componen de la query con el port literal del core de Lancemos. Autentica con `GHL_PRECHECKOUT_ADAPTER_TOKEN` (header `X-Setter-Adapter-Token` o `customData.setter_token`), que es la única barrera: el id del formulario y las URL son públicos. Rechaza con `422`, sin tocar la base, la prueba del editor de GHL, un formulario fuera de la lista, una landing desconocida y un teléfono inválido (el móvil mexicano con `+521` se normaliza a `+52`); con `400` una clave repetida en el JSON o un email de más de 254 caracteres (el tope de una dirección SMTP; uno de varios KB no entra en la fila del índice y la RPC fallaría en cada entrega). El `id` de cada entrega es un ULID aleatorio: un reintento de GHL es otra submission de la misma intención y no deja un conflicto que le quite el consentimiento (`validate_ghl_precheckout_adapter.mjs`, dentro de `npm test`, lo prueba contra la RPC real, con el caso de control del id repetido). Prendido, el bridge no arranca sin `intencion` en `eventos`, sin formularios, con un token de menos de 32 caracteres o igual a otro secreto, o con `[flujos].precheckout` prendido (hasta que exista la verificación fuera de banda del envío; `validate` lo avisa), y `/ready` informa `ghl_precheckout_adapter`. Fixtures: los dos envíos capturados el 2026-09-29, en `tests/fixtures/ghl/`. Contrato: `docs/contracts/ghl-precheckout-adapter-v1.md`. Johanna corre sin manifiesto: `/webhooks/lead` y su arranque no cambian.

### Qué tiene que hacer una instancia

- Nada, si no usa el adaptador. Esta versión no trae migraciones.
- Para que el formulario de GHL deje la intención: `"intencion"` en `eventos`, `[adaptadores.ghl]` con los formularios que muestran la aclaración del consentimiento, `GHL_PRECHECKOUT_ADAPTER_TOKEN` y `GHL_PRECHECKOUT_ADAPTER_ENABLED=true`, y en GHL una acción *Webhook* al bridge con el token en *Custom Data* (`setter_token`). Antes, la migración `20260930000200` y la fila del binding con sus landings. Pasos y verificación: `docs/instalar.md`, paso 12. No prende ningún mensaje; prender `[flujos].precheckout` o una audiencia `consented_intent` alimentada por el adaptador exige además una verificación fuera de banda del envío o la aceptación del riesgo por escrito (contrato, Riesgos). Johanna: nada.

## [1.1.0] - 2026-09-30

El bridge lee el manifiesto de la instancia. Es la primera versión con la que una aliada se instala desde su repo (`setter-instancia-<aliada>`) sin código propio. Apagado por defecto: sin `INSTANCE_MANIFEST_PATH` el bridge arranca igual que en 1.0.0.

### Agregado

- `INSTANCE_MANIFEST_PATH`: el binding de la aliada sale de `instancia.toml` (excluyente con `COMMERCIAL_ALLY_CONFIG_PATH`). La oferta por defecto es la del binding y las demás landings van en `additional_offer_codes`, así los eventos de Hotmart de cualquier landing de la instancia se admiten.
- `COMMERCIAL_KNOWLEDGE_ENABLED`: el conocimiento aprobado de `agente.conocimiento` viaja como mensaje `system` en cada pedido a Hermes. Con manifiesto, las respuestas automáticas lo exigen.
- El manifiesto es el techo de los flags del runtime: un flag prendido con su flujo en `false` no arranca, y `HERMES_MODEL_NAME` tiene que coincidir con `agente.modelo`. La tabla flag → flujo está en `docs/contracts/instance-runtime-v2.md`.
- La guarda de medicación usa `guardas.terminos_sensibles` y `guardas.acciones_sensibles` del manifiesto en vez de la lista por defecto.
- `/ready` informa `instance_ally`, `instance_product_version` y `commercial_knowledge` (versión y hash del bloque).
- SOUL común del agente comercial en `profiles/agente-comercial-comun/SOUL.md`: el de Johanna sin lo que es de Johanna, con un test que compara las secciones invariantes byte a byte.
- `docs/instalar.md`: la guía de instalación, escrita paso a paso con ATT1.
- La frontera del piloto acepta varias ofertas por scope (migración `20260929000100`): `pilot_scope_versions.additional_offer_codes`, con la misma forma que en el binding. Sin eso, un carrito o un pago fallido de una landing que no es la de la oferta por defecto pasaba la admisión y moría al planificarse (`pilot_offer_mismatch`, `payment_failure_scope_binding_mismatch`) o al arrancar el envío. Los scopes existentes quedan con el conjunto vacío: Johanna no cambia.
- Un scope del piloto acepta carrito y pago fallido a la vez (migración `20260929000200`): `pilot_scope_versions.additional_source_event_types`, con un solo tipo adicional de Hotmart (`PURCHASE_OUT_OF_SHOPPING_CART` o `PURCHASE_CANCELED`). El bridge arma una sola frontera con una sola `LANCEMOS_PILOT_SCOPE_KEY` y una sola política para todos sus caminos, así que con un tipo por scope una instancia perdía el carrito o el pago fallido en `pilot_source_event_mismatch`. La política, los topes de envío y la cohorte del scope pasan a ser compartidos por los dos tipos. El estado del runtime del piloto exige que el scope acepte carritos, como tipo principal o adicional. Los scopes existentes quedan con el conjunto vacío: Johanna no cambia.
- El evento de Hotmart guarda el motivo con que la frontera del piloto rechaza la planificación: `webhook_events.processing_error` pasa de `create_recovery_case_failed` a, por ejemplo, `pilot_scope_rejected:pilot_contact_not_in_cohort` o `payment_failure_correlation_unresolved`. Así un rechazo a propósito (cohorte, oferta, correlación) no se confunde con una falla de la base. Solo aplica a las dos RPC del piloto; Johanna no las llama y no cambia.
- El dispatcher autoriza al destinatario de un runtime portable con cualquier oferta del binding (la por defecto y `additional_offer_codes`), el mismo conjunto que ya aceptan la admisión y la frontera del piloto. Antes comparaba solo la oferta por defecto, así que un carrito de otra landing de la instancia se planificaba y moría al despacharse. Johanna usa la rama del JID y no cambia.
- Las plantillas de primer contacto del manifiesto (`precheckout`, `carrito` y `pago_fallido`) declaran sus variables con `parametros` (`["nombre"]`, `["nombre", "producto"]`, `["producto", "nombre"]`). Antes el bridge mandaba siempre nombre y producto, así que una plantilla aprobada con una sola variable recibía una de más y Meta podía rechazar el envío. Sin `parametros` sigue saliendo nombre y producto. Con `carrito` o `pago_fallido` en `true` y salida por WABA, `WABA_FIRST_TOUCH_TEMPLATE_NAME`, `WABA_PAYMENT_FAILURE_TEMPLATE_NAME` y `WABA_TEMPLATE_LANGUAGE` tienen que coincidir con `[plantillas]`, o el bridge no arranca. Sin manifiesto nada cambia: Johanna no declara variables.
- `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED` (apagado por defecto, solo con manifiesto): el dispatcher manda el primer contacto del carrito y del pago fallido con el cuerpo aprobado del catálogo de Chatwoot, leído en cada envío, y no le pide borrador a Hermes. El texto de Hermes nunca llegaba a Meta y el SOUL común le prohíbe al agente escribir primero. Si la plantilla no está aprobada, no coincide con idioma, categoría o `parametros`, o tiene un botón que no es `QUICK_REPLY`, el intento se cierra sin POST con `approved_template_unavailable` o `approved_template_mismatch`. Con manifiesto, `META_FINAL_EFFECT_ENABLED=true` junto con `DURABLE_OUTBOUND_ENABLED=true` exige este modo. `LEAD_FIRST_NAME_GREETING_ENABLED` pasa a estar admitido en un runtime portable, solo junto con el modo directo (sin él el bridge no arranca, porque nadie más lo usaría): la variable `nombre` lleva el saludo, y el hash del gate final es el del texto con ese saludo. Contrato: `docs/contracts/approved-template-direct-dispatch-v1.md`. Johanna corre sin manifiesto y no cambia.
- `WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY` (vacía por defecto, solo con manifiesto): la categoría de Meta de la plantilla del pago fallido cuando no es la del carrito. El modo directo compara cada plantilla con su propia categoría; antes las dos se comparaban con la única `WABA_TEMPLATE_CATEGORY`, así que con el carrito `MARKETING` y el pago fallido `UTILITY` uno de los dos flujos quedaba siempre en `approved_template_mismatch`. El envío a Chatwoot lleva esa categoría. Vacía, todo sigue como antes. Johanna no la puede definir.
- En el modo directo, lo que no cambia solo cierra la acción en el primer intento como `permanent_failed`: una acción que no es primer contacto, las variables que el runtime no declara y un valor del caso que falta. Antes pedían otro intento al minuto y la base los reintentaba hasta agotar `max_execution_retries` (cuatro intentos, releyendo el catálogo en cada uno) para llegar al mismo cierre. Un catálogo que no responde o no cierra se sigue reintentando.
- Con variables declaradas en el manifiesto, cada valor de la plantilla sale con los espacios colapsados: Meta rechaza un parámetro con salto de línea, tabulación o más de cuatro espacios seguidos, y un nombre de Hotmart puede traerlos. El rechazo llegaba después de empezado el pedido. El modo directo arma el texto que hashea con los mismos valores, y el render rechaza con `template_parameters_missing` cualquier valor que no cumpla, antes del gate final. Sin manifiesto (Johanna) los valores salen como hasta ahora.
- El permiso de contacto del pago fallido lo concede la intención con consentimiento (migración `20260930000100`). Antes el pago fallido portable se planificaba sin ningún permiso y la reevaluación lo escalaba con `contact_authorization_unknown`, así que no podía salir; los tests insertaban el permiso a mano. Ahora `plan_portable_payment_failure_recovery` inserta `contact_authorizations` `allowed` (fuente `system`) cuando la intención con la que se correlacionó el evento sigue viva, tiene los dos permisos del formulario, es del teléfono de destino y tiene un envío 1.1.0 con opt-in de WhatsApp y la `consent_copy_version` del binding. Una fila de permiso activa, incluida la de un opt-out, gana y no se pisa. El evento de Hotmart sigue sin conceder permiso por sí solo. Además, un teléfono que el contacto tiene como punto `system` (el bootstrap del formulario) ya no rompe la planificación con `payment_failure_contact_mismatch`. Contrato: `docs/contracts/commercial-ally-runtime-v1.md`. Johanna no pasa por esta función y no cambia.
- El formulario del precheckout portable se admite en cada landing del binding (migración `20260930000200`). Antes `admit_portable_observed_lead_precheckout` y `/webhooks/lead` exigían la landing, el sitio, el host, la ruta y la oferta por defecto, así que el formulario de otra landing se rechazaba y, sin esa intención, el pago fallido de su oferta terminaba en `payment_failure_correlation_unresolved`. El binding suma `additional_offer_landings`: la landing de cada oferta adicional (`offer_code`, `site`, `landing_id`, `page_host`, `page_path`), en el orden de `additional_offer_codes`, con un check de forma en la base. La RPC resuelve la oferta del envío y compara la landing, el sitio, el host y la ruta de esa oferta; un par cruzado se rechaza. La intención es una por oferta. Con manifiesto v2 la lista sale de `[[hotmart.ofertas]]`. Vacía (el valor por defecto) se comporta como antes: el formulario entra solo por la landing por defecto. Contratos: `docs/contracts/lead-precheckout-v1.md` y `docs/contracts/commercial-ally-runtime-v1.md`. Johanna usa `admit_observed_lead_precheckout` y no cambia.
- Prueba de la cadena de ATT1 de punta a punta en la base (`tests/sql/followup_engine/validate_att1_portable_chain.mjs`, dentro de `npm test`). Con el binding del fixture de ATT1 y la política `freeform` de dos pasos de la instancia recorre formulario, evento de Hotmart, plan del piloto, claim, la reevaluación real, reserva, arranque del envío y aceptación. Cubre el carrito de las tres ofertas, el pago fallido con y sin carrito previo, el rechazo con la cohorte vacía (`pilot_contact_not_in_cohort`) y que después de aceptar no aparezca ninguna acción nueva. También fija un riesgo de esa política: con `max_automatic_messages = 2`, la aceptación del carrito falla con `invalid_next_policy_step`, porque el paso siguiente se elige por posición. Hasta ahora ningún test corría la reevaluación real de esta cadena: `tests/e2e/test_att1_production_like_final_gate.py` la simula y ahora lo dice. La ventana de envío de la prueba es de 24 h, porque el arranque del envío exige el reloj real de la base. El modo de la reserva y la RPC de arranque que usa la prueba (elegida por el `anchor_type` que escribe el planificador) se comparan con lo que manda el `SupabaseClient` real del dispatcher en `tests/test_durable_dispatcher_approved_template.py`. No cambia nada en tiempo de ejecución.
- La audiencia del scope del piloto (migración `20260930000300`): `pilot_scope_versions.audience_mode`, fijo por versión publicada, con tres valores. `manual_cohort` (por defecto, el de todo scope existente) es lo de hoy: solo sale un contacto inscripto a mano en la cohorte. `consented_intent` es el criterio de Johanna: entra, sin cohorte, el contacto cuyo evento de Hotmart quedó correlacionado con una intención de compra viva, del mismo teléfono, producto y oferta del evento, con el opt-in de WhatsApp del formulario 1.1.0 (`_portable_consented_intent_reason`, de `20260930000100`). `consented_intent_in_cohort` exige las dos cosas y es para el primer E2E con un solo teléfono. Los topes total y diario siguen siendo el freno. La evidencia se exige al planificar (rechazo `pilot_scope_rejected` con un motivo `pilot_audience_*`, que el bridge guarda en `webhook_events.processing_error` con estado `failed`) y se vuelve a verificar al arrancar el envío, antes de consumir presupuesto: si entre el plan y el envío la intención se compró, pasó a `identity_conflict` por otro formulario de la misma oferta o salió del mapeo activo, el envío no arranca y no consume cupo. La compra la corta primero el worker de compras, que cierra el caso; la re-verificación cubre el envío que ya estaba en vuelo. El lock compartido de esa verificación serializa solo con quien cambia la fila de la intención. `pilot_recovery_case_bindings` registra el modo, la intención y el envío 1.1.0 del formulario que dio el consentimiento (`audience_precheckout_submission_id`), y fuera de `manual_cohort` el evento `pilot_outbound_request_authorized` también. El opt-out conserva precedencia en los tres modos: la audiencia no lo mira, y lo frenan la reevaluación, el arranque y el permiso activo, sin consumir cupo. Diseño: `docs/design/lancemos-pilot-boundary.md` (§2.4). Pruebas: `validate_pilot_scope_audience_mode.mjs` (los tres modos con carrito y pago fallido, la regresión de `manual_cohort`, el opt-out previo y en vuelo, la pérdida del consentimiento, la compra y el mapeo apagado antes del envío, el replay, cada condición del helper por separado, y que toda función SQL que llama a `evaluate_lancemos_pilot_scope` llame también al helper) y la cadena de ATT1, que ahora publica una v2 con `consented_intent`. Johanna no pasa por la frontera del piloto; sus scopes quedan en `manual_cohort`, y en ese modo las cuatro funciones y el `data` de auditoría quedan idénticos.

### Qué tiene que hacer una instancia

- Nada, si no usa manifiesto: Johanna sigue con el binding del código.
- Una instancia nueva monta `instancia.toml`, define `INSTANCE_MANIFEST_PATH` y, para responder automáticamente, `COMMERCIAL_KNOWLEDGE_ENABLED=true` con el conocimiento en `estado = "aprobado"`.
- En la base: aplicar la migración `20260929000100` (agrega una columna con valor por defecto; no toca filas). Una instancia con más de una landing carga en su scope del piloto las otras ofertas en `additional_offer_codes`; el scope todavía se siembra a mano (no lo crea ninguna migración del producto ni la guía).
- En la base: aplicar la migración `20260929000200` (agrega una columna con valor por defecto; no toca filas). Una instancia que recupera carritos y pagos fallidos siembra un solo scope con `source_event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'` y `additional_source_event_types = '{PURCHASE_CANCELED}'`.
- En el manifiesto, opcional: `parametros` en `carrito`, `pago_fallido` y `precheckout`, según las variables del cuerpo aprobado en Meta. Sin la clave se mandan nombre y producto, como hasta ahora. Con `carrito` o `pago_fallido` en `true`, `WABA_*_TEMPLATE_NAME` y `WABA_TEMPLATE_LANGUAGE` tienen que coincidir con `[plantillas]`.
- Para que el dispatcher mande por Meta: `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED=true` antes de abrir `META_FINAL_EFFECT_ENABLED`. Hermes deja de ser necesario para esa salida. El token de control de Chatwoot tiene que poder leer el inbox (`GET /inboxes/{id}`), y `parametros` tiene que coincidir con el catálogo real de la instancia.
- En la base: aplicar la migración `20260930000100` (reemplaza una función y suma un helper privado; no toca filas). Para que un pago fallido salga, la persona tiene que haber dejado el formulario con opt-in de WhatsApp: sin intención con consentimiento el caso se planifica y la reevaluación lo escala.
- En la base: aplicar la migración `20260930000200` (suma una columna con `[]` por defecto y su check; reemplaza una función; no toca filas). Una instancia con manifiesto v2 y más de una oferta tiene que actualizar su fila del binding con `additional_offer_landings` (las ofertas que no son la por defecto, en el orden de `additional_offer_codes`) **antes** de desplegar este bridge: el manifiesto ahora las declara y, contra una fila sin ellas, el bridge ve drift y `/ready` responde `503 commercial_ally_binding_unavailable`. Orden: migración, fila del binding, bridge. Si la fila ya está sembrada se completa en el lugar, con el `UPDATE` y la verificación de `docs/instalar.md` (paso 9).
- En la base: aplicar la migración `20260930000300` (suma cuatro columnas, `audience_mode` en el scope y en el binding con `manual_cohort` por defecto, y `audience_purchase_intent_id` y `audience_precheckout_submission_id` nulas en el binding, con sus checks; un helper privado; reemplaza cuatro funciones; no toca filas). Las dos FK nuevas del binding bloquean, hasta el commit, las escrituras de `purchase_intents` y `precheckout_submissions`, que Johanna hace en caliente (formularios, correlación, compras): la migración no reescribe filas y dura poco, pero conviene aplicarla en un momento de poco tráfico. Si una transacción larga tiene tomada alguna de esas tablas, falla por `lock_timeout` (5 s) sin dejar nada a medias y se reintenta. Un scope existente queda en `manual_cohort` y no cambia. Para abrir la audiencia, un scope no se edita: se pausa, se publica la versión siguiente con `audience_mode` (primero `consented_intent_in_cohort` con `max_cohort_contacts = 1` para el E2E, después `consented_intent` con los topes aprobados; el total cuenta lo consumido por todas las versiones), se activa, se apunta `LANCEMOS_PILOT_SCOPE_VERSION` a la versión nueva, se reinicia el bridge y se arma. Sin formularios con opt-in admitidos (el evento `"intencion"` del manifiesto), `consented_intent` no le escribe a nadie. Con un scope con consentimiento, no rotar la `consent_copy_version` del binding mientras haya acciones pendientes: la re-verificación del arranque compara contra la copy vigente en ese momento, y un caso planificado con la anterior queda trabado en `pilot_audience_consented_intent_submission_missing` (el dispatcher no distingue ese rechazo y la acción no avanza).

## [1.0.0] - 2026-09-28

Primera versión con tag. **El código es el que corre para Johanna.** Servicios desplegados al 2026-09-29: bridge en `f7dd227`, revisión diaria en `a50bfa1`, conector de Slack en `bfae40c`. Contra `f7dd227` ninguno tiene diferencias en su código (la revisión diaria y el conector, solo en `Dockerfile`, `pyproject.toml` y `uv.lock`). Pasar un servicio a esta imagen no cambia su comportamiento.

### Agregado

- Workflow `release.yml`: un tag `vX.Y.Z` en `main` construye y publica las tres imágenes en GHCR y crea la release con el changelog y los digests. Antes de publicar, verifica que el tag coincida con `pyproject.toml`, que exista la entrada en este archivo y que el commit esté en `main`.
- Las imágenes llevan `SETTER_VERSION` y `GIT_SHA` adentro. Si el servicio no define `GIT_SHA`, el bridge toma la de la imagen para la procedencia de cada pedido al agente (`bridge_release`), en vez de `unknown`.

### Qué tiene que hacer una instancia

- Nada en la base ni en el manifiesto.
- Para usar la imagen en vez de construir desde el repo: seguir [release-por-imagen-v1.md](docs/operations/release-por-imagen-v1.md). Hace falta un token de GitHub de solo lectura de paquetes (`read:packages`) cargado en EasyPanel como credencial de registro.

### Probado con

- Hermes `nousresearch/hermes-agent:v2026.8.31`.
