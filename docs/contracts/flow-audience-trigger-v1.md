# flow-audience-trigger-v1: el disparo por audiencia (fecha, cada, manual)

Versión 0.3.0 (propuesta, E-484 sobre E-483, 13/11/2026). Extiende `setter-flujo/v1` (`flow-definition-v1.md`) y el ciclo (`flow-change-cycle-v1.md`). Diseño: hito H6 del mapa «el AE maneja todos los flujos» (vault, `coordinacion/etapa-1/mapa-ae-todos-los-flujos.md` §2), decisiones D6, D7, D8 y D10.

Los valores marcados **DEFAULT_REVISAR_DAN** son los del mapa y los tiene que revisar Dan antes de prender nada.

## Para qué

Hasta la v1.36.0 un flujo nace de una persona: un evento (`carrito`, `pago_fallido`, `compra`…) o un silencio. Los tres disparadores nuevos nacen de un momento, y el «a quién» es una audiencia: un recordatorio de Black a quienes dejaron el formulario, un aviso a las clientas cada lunes, un envío que una persona lanza cuando quiere.

## La forma

```yaml
dispara:
  fecha:  {el: "2026-12-01T10:00", zona: America/Mexico_City}      # una vez
  # o
  cada:   {dias: [lun, jue], hora: "10:00", zona: America/Mexico_City, desde: "2026-12-01", hasta: "2026-12-31"}
  # o
  manual: {}                                                        # lo lanza una persona
para:
  es: prospecta            # o clienta (obligatorio)
  etiquetas: [black-2026]  # de Chatwoot; tiene que tener todas
  excluye: [vip]           # ninguna de estas
  producto: el-protocolo   # opcional; si está, es el producto del flujo (otro es invalid_value): «compró» es de ese producto
  formulario_en: 30d       # con prospecta: dejó el formulario con permiso en los últimos N días (DEFAULT_REVISAR_DAN: 30d)
ritmo:
  por_hora: 200            # DEFAULT_REVISAR_DAN (D10)
  por_ocurrencia: 1000     # DEFAULT_REVISAR_DAN (D10)
entra:
  permiso: formulario      # con prospecta; compra con clienta
```

- `cada` sale una vez por día como mucho (un día de la semana y una hora): es el intervalo mínimo de 24 h del mapa.
- `para` es obligatorio con las tres formas (`audience_requires_para`, regla 2); `para` y `ritmo` con un evento o un silencio son `para_requires_audience_trigger` (regla 2).
- **Quién recibe (D6):** una prospecta solo con `permiso: formulario`; una clienta solo con `permiso: compra`, que además exige el `ok_del_cliente` del ítem y una sola plantilla de utilidad (reglas del ciclo y 17, sin cambios). `conversacion` no alcanza: `audience_permission_mismatch` (regla 3).
- **Una sola definición de «compró» y del formulario (E-483, corrección de la revisión del 13/11; escrito al construirlo, antes del merge, no afirma nada desplegado).** El validador rechaza un `para.producto` distinto del producto del flujo (`invalid_value` en `/para/producto`). La lista (`read_flow_audience_candidates_v1`), el armado (`materialize_flow_audience_occurrence_v1`), la selección del bridge (`select_members`) y la admisión nativa (`_flow_engine_native_audience_reason`, migración `20261112210600`) miran la compra en el producto del flujo, sin reembolso ni contracargo posterior de la misma transacción; el formulario es el del producto del flujo, con `whatsapp_contact_authorized` y dentro de `formulario_en`, que en todas las capas es de `1d` a `365d` (`30d` sin la clave; otra cosa: la lista y el armado fallan con `flow_audience_form_window_invalid` y la admisión da `no_entra:permiso`). Queda distinto: la admisión cuenta además la compra con nuestro link (`_portable_checkout_link_purchase_exists`), que no mira reembolsos.
- Al normalizar se escriben `ritmo`, `formulario_en` y la repetición `una_por: persona, otra_despues_de: 7d` (DEFAULT_REVISAR_DAN).
- Con `FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED` el validador abre las tres formas en el motor nativo (`NativeRuntime.native_multitouch`; `flow-engine-v2` §34, E-483): hasta tres toques con nombre y producto, sin cupón. Sin ese flag, un flujo así vuelve con `motor_nativo:disparador` («todavía no»), como cualquier disparador que el motor no ejecuta.

## Cómo corre (propuesta, se mide en el E2E)

1. **Ocurrencia.** `fecha`: la única, entre su hora y 6 h después. `cada`: una por día local pedido, dentro de `desde`/`hasta`, en la misma gracia. Pasada la gracia no sale tarde. `manual`: la arma una persona desde la página.
2. **Lista congelada.** Al vencer, se materializa la audiencia en `flow_audience_occurrences` + `flow_audience_members`, idempotente por (flujo, versión, ocurrencia): un reinicio no la vuelve a armar. La base vuelve a mirar la baja (`contact_authorizations` denegada) y, con `formulario`, la intención con permiso de WhatsApp en la ventana; quien no pasa queda `omitida` con su motivo.
3. **Confirmación (D7).** El AE nunca dispara. Una ocurrencia `manual` la arma una persona desde la página (`/flow-approval/envios`, «Armar la lista»: el bridge lee la audiencia y la base la congela con `materialized_by: slack:<id>`) y queda `materializada` hasta que una persona de la lista de aprobadores la confirma **viendo cuántas van a salir**, escribiendo ese número y marcando «Confirmo» (`approver_confirm_flow_audience_v1`, con la sesión de «Entrar con Slack»; queda `confirmed_by: slack:<id>`). Un número distinto, sin la marca, sin sesión o con el flujo ya en otra versión no confirma. `fecha` y `cada` quedan confirmadas por la aprobación del flujo al publicarse. Desde la página también se frena a mano (`a_mano`).
4. **Freno por salud (D8).** Antes de soltar, el barredor lee la última lectura de salud de Meta de la instancia: el `estado.json` que escribe `meta_salud.py` (P22), montado en el contenedor en `FLOW_AUDIENCE_META_HEALTH_PATH`, con la clave de la instancia en `FLOW_AUDIENCE_META_HEALTH_KEY` (o una lectura suelta `{calidad, leido_en}`). La hora es la `leido_en` de la lectura de la instancia adentro del JSON, nunca la de la escritura del archivo (otro proceso, el timer de las entregas, lo reescribe cada hora sin volver a leer la salud); sin `leido_en`, la lectura cuenta como desconocida y frena (corrección de la revisión del 13/11, escrita antes del merge). Requisito del OS, fuera de este repo: `meta_salud.py` tiene que escribir `leido_en` en la lectura de cada instancia del `estado.json`; hasta entonces, todo envío a una audiencia queda frenado. Rojo o amarillo, o `puede_enviar` `BLOCKED`/`LIMITED`, frena la ocurrencia (`frenada`, con el motivo `meta_salud:<que>`); una lectura que no se puede leer, `UNKNOWN` o con más de 26 h también frena (fail-closed). Cada freno avisa una vez en el canal de alertas de la aliada (`SYS-007`, «Automatización pausada automáticamente», con `component: flow_audience:<flujo>` y el motivo, sin personas). DEFAULT_REVISAR_DAN: amarillo frena.
5. **Ritmo.** Se sueltan a lo sumo `por_hora` personas por hora; cada una abre un recorrido nativo (la admisión nativa de `flow-engine-v2` §34: `admit_flow_engine_event(instance, evento, audience=True)`, con exactamente `fuente: audiencia`, `tipo: <forma>`, `evento_id: audiencia:<ocurrencia>:<contacto>`, `flujo_id`, `version`, `ocurrencia_id`, `producto_ref`, `contacto_id`, `momento`, `chatwoot {account_id, inbox_id}` y `destino`; la base contesta `motor_arranco`, `motor_no_arranca` con su motivo, o `sin_flujo` si la versión activa ya no es la de la lista) y desde ahí rige la puerta común (horario, topes, baja, conversación viva, salida `compra`). El barredor le asienta a la base lo que contestó el motor (`arranco`, `no_arranca`, `sin_flujo`); si el motor no contestó, no asienta y la base vuelve a dar a esa persona a los 10 minutos sin contarla otra vez en el ritmo (el `evento_id` único hace que abrir dos veces devuelva el mismo recorrido). La ocurrencia termina cuando no queda nadie pendiente ni sin asentar.
6. **Repetición.** La misma persona no entra en otra ocurrencia del mismo flujo antes de `topes.repeticion.otra_despues_de` (DEFAULT_REVISAR_DAN: 7d; `nunca`, una sola vez): queda `omitida` como `repetida`.
7. **La audiencia posible** sale de la base (`read_flow_audience_candidates_v1`): prospectas = el formulario con permiso de WhatsApp del producto del flujo en la ventana; clientas = una compra aprobada sin reembolso ni contracargo del producto del flujo, atadas a un solo contacto por teléfono (52/521). Las etiquetas (`etiquetas`, `excluye`) se leen de las conversaciones de la bandeja en Chatwoot (con actividad en la ventana; 90 días para clientas); si el listado no se puede leer entero, esa ocurrencia no se arma (fail-closed) y se reintenta dentro de la gracia.

## Flags

- `FLOW_AUDIENCE_TRIGGERS_ENABLED` (default `false`): sin él el barredor no lee, no materializa ni suelta nada, y la página `/flow-approval/envios` no existe. Prendido exige `FLOW_AUDIENCE_META_HEALTH_PATH` y el manifiesto; sin el motor nativo (`FLOW_ENGINE_ENABLED`) o sin `FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED` (sin él la base rechazaría a cada persona con 22023) arma y frena pero no suelta (`/ready`: `flow_audience: sin_motor`).
- `FLOW_AUDIENCE_META_HEALTH_PATH`, `FLOW_AUDIENCE_META_HEALTH_KEY`: la lectura de la salud (arriba).

## Historial

- 0.1.0 (12/11/2026, E-484): forma, reglas 2 y 3, la base y el barredor detrás del flag. Sin E2E.
- 0.2.0 (12/11/2026, E-484): la página de confirmación con la sesión del aprobador, el asiento y la re-entrega, la repetición, la audiencia posible desde la base y Chatwoot, la lectura de `meta_salud.py`, el aviso `SYS-007` y la admisión nativa por evento `audiencia` (la base del motor tiene que aceptarla al integrar E-483). Sin E2E.
- 0.3.0 (13/11/2026, E-484 sobre E-483): la migración pasa a `20261112220000` (después de las de E-483); el opener pasa `audience=True` a la admisión de `flow-engine-v2` §34 y se arma solo con `FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED`; con ese flag el validador abre fecha, cada y manual. Escrito al construirlo, antes del merge: no afirma nada desplegado. Prueba de punta a punta en PGlite: `validate_flow_audience_native_e2e.mjs`.
