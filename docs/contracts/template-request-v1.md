# template-request-v1: la solicitud de plantilla de Meta

**Versión:** 1.2.0 (F-146, partes 1 a 3 y 5: la base, el validador, las rutas del Automation Expert, la página de aprobación, el envío a Meta, las herramientas del plugin y la skill `pedir-plantilla-a-meta`).
**Migración:** `20261102000200_meta_template_requests_v1.sql`.
**Código:** `src/bridge/template_request.py` (puro), `src/bridge/template_request_api.py` (las rutas del AE), `src/bridge/template_approval.py` (la página y el envío) y `src/bridge/meta_templates.py` (el cliente cerrado de Meta).
**Flag:** `META_TEMPLATES_ENABLED`, apagado por defecto. Apagado, las rutas no existen, el cliente de Meta no se crea y nada cambia.

Cuando un flujo necesita una plantilla de WhatsApp que no existe, el Automation Expert (AE) la redacta con el dueño y la deja como **solicitud**: un objeto del almacén hermano de la propuesta de flujo (`flow-change-cycle-v1`), con revisiones mientras es borrador, una validación que lo congela con su huella por 24 horas, la aprobación de una persona de la lista de la instancia, y después el envío a Meta y lo que Meta contesta.

**Lo que esta versión no hace.** El sondeo del estado, los avisos al canal de alertas y la sincronización del catálogo de Chatwoot (parte 4) vienen después. Mientras no esté la parte 4, `record_meta_template_status_v1` no se llama desde el bridge: una plantilla mandada queda `en_revision` en la base **y en la página**, que nunca muestra lo que Meta contestó después de crearla. El resultado se ve en la cuenta de WhatsApp (WhatsApp Manager). El AE no avisa: cuando el dueño vuelve, lee `resolved.state` de `get_template_request` o de la lista, que el bridge ya cruzó con el catálogo (el catálogo lo actualiza Chatwoot, con hasta 3 horas de atraso; ver §3).

## 1. Los estados

```
borrador ─validar─▶ validada ─aprobar─▶ aprobada ─empezar envío─▶ enviada ─Meta contesta─▶ en_revision
   │                  │  │                 │  │                       │                       │   │
   └─descartar        │  └─24 h─▶ vencida  │  └─24 h─▶ vencida        ├─APPROVED─▶ aprobada_meta ◀─┘   └─48 h─▶ sin_respuesta_de_meta
                      └─descartar          └─descartar                └─REJECTED─▶ rechazada                     │
                                                                                     aprobada_meta ─en el catálogo─▶ en_catalogo
```

- **Finales:** `rechazada`, `en_catalogo`, `vencida`, `descartada`. Un final no acepta ningún cambio (un disparador lo frena).
- **Descartar** solo lo que no llegó a Meta: `borrador`, `validada` o `aprobada`.
- **`sin_respuesta_de_meta`** sigue esperando: si Meta contesta tarde, pasa a `aprobada_meta` o a `rechazada`.
- **Recategorizada.** Si Meta aprueba con otra categoría que la pedida, la solicitud queda `aprobada_meta` con `recategorized = true`. Pasó con `maribel_retomar_proceso_01` y `att1_ficha_vence_manana_01` (pedidas de utilidad, aprobadas como marketing).
- **El nombre.** Un nombre con su idioma se usa una sola vez por instancia (índice único parcial). Lo liberan solo una `descartada` o una `vencida`, que nunca llegaron a Meta. Corregir un rechazo es otra solicitud con el número siguiente (`_02`): no se edita la rechazada (D5).

## 2. Las funciones de la base

Todas son `security definer` con `search_path` fijo y toman la instancia de `p_instance` (`_flow_store_instance`). Ninguna la ejecutan `anon` ni `authenticated`. Las tablas no tienen permisos para nadie de la API.

**Del AE** (`automation_expert_change`, con `AUTOMATION_EXPERT_CHANGE_DB_KEY`):

| Función | Qué hace |
|---|---|
| `save_meta_template_request_draft_v1(p_instance, p_request_id, p_expected_revision, p_draft, p_purpose, p_proposal_id, p_step_pointer, p_requested_by_ref)` | Crea un borrador o rehace uno con su revisión. Topes: 10 abiertas por instancia (un borrador, o una validada o aprobada sin vencer), 100 revisiones. Devuelve `meta_template_name_taken` si el nombre ya está tomado. |
| `record_meta_template_request_validation_v1(p_instance, p_request_id, p_revision, p_validator, p_result, p_meta_request, p_preview, p_requested_by_ref)` | Registra la validación que corrió el bridge. Con bloqueos, queda borrador. Sin bloqueos, exige el pedido de Meta de esa fila y congela por 24 horas con la huella. Tope: 100 validaciones. |
| `get_meta_template_request_v1(p_instance, p_request_id)` | Relee una, con sus últimos 50 eventos. |
| `list_meta_template_requests_v1(p_instance, p_only_open, p_limit)` | La lista, de la más nueva a la más vieja (de 1 a 50). Con `p_only_open`: las abiertas del tope y las que esperan a Meta o al catálogo. |

Ninguna lectura del AE trae un ID de Slack: ni quién la pidió ni quién la aprobó.

**De la página** (`flow_change_approver`, con la sesión viva de «Entrar con Slack»; el actor sale de la sesión, nunca de un parámetro):

| Función | Qué hace |
|---|---|
| `approver_get_meta_template_request_v1(p_instance, p_session_hash, p_request_id)` | Relee una, con quién la aprobó. |
| `approver_approve_meta_template_request_v1(…, p_request_id, p_fingerprint)` | La aprueba, un solo paso (D3): mandar una plantilla no le escribe a nadie. Exige la huella. Repetirla contesta `replayed`. |
| `approver_discard_meta_template_request_v1(…, p_request_id, p_reason)` | La descarta, con un motivo de una línea. |
| `approver_begin_meta_template_send_v1(…, p_request_id, p_fingerprint, p_daily_cap)` | Pasa `aprobada` a `enviada` y devuelve el pedido congelado: es lo único que el bridge manda a Meta. Aplica el tope por día (D8): cuenta las solicitudes con su primer envío en las últimas 24 horas (`p_daily_cap` de 1 a 20, lo pondrá `META_TEMPLATES_DAILY_CAP`, 5 por defecto). Un envío a medias se puede volver a empezar a los 2 minutos, hasta 5 intentos. El bridge relee la lista de Meta por nombre antes de crear, así un reintento no duplica (parte 3). |
| `approver_record_meta_template_sent_v1(…, p_request_id, p_meta_template_id, p_meta_status, p_meta_category, p_error_code)` | Lo que contestó Meta al crear (`PENDING` o `IN_APPEAL` → `en_revision`, `APPROVED` → `aprobada_meta`, `REJECTED` → `rechazada`), o el código del error (queda `enviada`). |

**Del sondeo** (`service_role`, el worker de la parte 4):

- `record_meta_template_status_v1(p_instance, p_request_id, p_meta_template_id, p_meta_status, p_meta_category, p_rejected_reason, p_in_catalog)`: lo que leyó `GET /{template_id}` y si la plantilla ya está `APPROVED` en el catálogo de la bandeja.
  - Con `PENDING` a las 48 horas del primer envío, pasa a `sin_respuesta_de_meta`.
  - Un estado que no es de la revisión (`PAUSED`, `DISABLED`…) se anota y no mueve la solicitud. Lo cubren el catálogo, que se relee en cada envío, y F-139.
  - El mismo estado de antes no deja evento.

- Un id de Meta es de una sola solicitud (índice único). Una `enviada` sin id que el sondeo encuentra por nombre pasa a la revisión con su id, aunque Meta diga `PAUSED`. Un `REJECTED` sobre una `aprobada_meta` deja el motivo en el evento y no mueve la solicitud.
- Una validada o aprobada vencida que nadie tocó se marca `vencida` cuando el agente guarda otra con el mismo nombre: así no retiene el nombre.

**Errores:**

- 22023: entrada inválida.
- 28000: sesión inválida (`flow_approval_session_invalid`).
- P0002: no existe o no es de la instancia.
- 55000: estado que no lo permite.

Los resultados del ciclo vuelven como `{"ok": false, "reason_code"}`: `meta_template_open_limit`, `meta_template_revision_limit`, `meta_template_validation_limit`, `meta_template_revision_mismatch`, `meta_template_not_draft`, `meta_template_name_taken`, `meta_template_request_expired`, `meta_template_not_validated`, `meta_template_not_approved`, `meta_template_daily_cap`, `meta_template_send_in_progress`, `meta_template_send_attempts_exhausted`, `meta_template_not_sending`, `meta_template_already_sent`, `meta_template_request_final`, `meta_template_not_sent`.

## 3. Las rutas del AE

Bajo `/internal/automation-expert/v1`, con `AUTOMATION_EXPERT_CHANGE_TOKEN`. El sobre es el de las rutas de cambio de flujos (`contract`, `contract_version`, `instance`, `generated_at`, `bridge_git_sha`, `data`).

- **`POST /template-requests`** (201) con `{"template", "purpose", "requested_by": {"slack_user"}}`. Opcionales: `request_id` y `revision` para rehacer, y `for_flow: {"proposal_id", "step"}` (JSON Pointer sobre `{"items": [...]}` de la propuesta, por ejemplo `/items/0/definicion/pasos/1`).
  - Corre las reglas con el catálogo de la bandeja si se puede leer. Con bloqueos contesta 422 `template_request_draft_blocked` y no guarda.
  - La respuesta trae `request_id`, `revision`, `status`, `variables` (el orden en que quedan numeradas), `warnings` y `burbuja`.
- **`POST /template-requests/{id}/validate`** con `{"revision", "requested_by"}`.
  - Vuelve a correr las reglas sobre lo guardado, con el catálogo, que es obligatorio (503 `template_catalog_unavailable`).
  - Si viene de una propuesta, el paso tiene que ser `{plantilla: {a_crear: <este nombre>}}`. Si no, bloquea con `template_flow_step_mismatch`.
  - Sin bloqueos, arma el pedido de Meta y la burbuja, y la base congela.
  - El validador lleva `SETTER_VERSION` y `GIT_SHA` de la imagen. Sin ellos, contesta 503.
- **`GET /template-requests?estado=abiertas|todas&limit=1..50`**. Cada fila suma `last_send_error`, `send_attempts`, `send_error_text` y `resolved`. La función de la base no trae el intento ni el borrador: el bridge relee, de a una y hasta 10, las filas `enviada` y las mandadas cuyo nombre está en el catálogo. Si una relectura falla corta ahí, y lo no leído queda `sin_confirmar` o con `catalog_checked: false`.
- **`GET /template-requests/{id}`**: la solicitud, su vista previa, su resultado, `last_send_error`, `send_attempts`, `send_error_text` (el motivo en palabras, el mismo de la página) y `resolved`.

**`resolved`, el estado ya cruzado** (`template_request.resolve_state`). `{state, catalog_checked, catalog_status, catalog_category}`. Lo cruza el bridge y no el modelo: dejárselo al modelo hacía que dijera «sigue en revisión» con la plantilla aprobada en la mano. Lo que nunca llegó a Meta se queda con su estado. Lo que se mandó, si está en el catálogo con el mismo nombre, idioma **y cuerpo**, toma el estado del catálogo: un nombre igual con otro cuerpo no presta su estado, y un `PENDING` del catálogo no pisa una respuesta ya guardada. Leer el catálogo espera hasta 5 segundos: si falla, la lectura contesta igual, con `catalog_checked: false`.

| `state` | Qué quiere decir |
|---|---|
| `borrador` | Todavía no quedó validada. |
| `esperando_que_la_manden` | `validada`: lista, nadie la mandó. |
| `aprobada_sin_mandar` | `aprobada`: la aprobó una persona y **no se mandó a Meta** (por ejemplo, tope del día). |
| `vencida`, `descartada` | No llegó a Meta. |
| `mandandose` | `enviada` sin error: Meta todavía no confirmó. |
| `no_llego_a_meta` | `enviada` con error: se puede volver a mandar desde la página. |
| `no_llego_a_meta_sin_reintentos` | Con error y los 5 intentos gastados. |
| `nombre_ocupado_en_meta` | `meta_name_taken_*`: hace falta el número siguiente. |
| `sin_confirmar` | `enviada` de la que no se pudo leer el intento. |
| `en_revision`, `sin_respuesta_de_meta` | Mandada; `PENDING` o `IN_APPEAL` en el catálogo, o todavía no está. |
| `aprobada_lista` | `APPROVED` en el catálogo y de la categoría pedida: el flujo la puede usar. |
| `aprobada_otro_tipo` | Aprobada con otra categoría que la pedida. |
| `aprobada_falta_catalogo` | Meta la aprobó y todavía no llegó al catálogo. |
| `rechazada` | La rechazó Meta. |
| `pausada_en_meta`, `desactivada_en_meta` | `PAUSED` o `DISABLED` en el catálogo: no se puede usar. |
| `estado_de_meta_desconocido` | Otro estado del catálogo: no se interpreta. |

**El borrador (`template`).**

```json
{"nombre": "att1_pago_efectivo_confirmado_02", "idioma": "es_MX", "categoria": "UTILITY",
 "encabezado": "opcional, texto fijo", "cuerpo": "Hola, {{nombre}}. Se confirmó tu pago de {{producto}}...",
 "pie": "opcional, texto fijo",
 "botones": [{"tipo": "checkout", "texto": "Completar mi pago"}, {"tipo": "respuesta", "texto": "No más mensajes"}]}
```

- Los datos van con nombre, del vocabulario del flujo (`flow-definition-v1`): `nombre`, `producto`, `medio_de_pago`, `cuando`, `precio`, `vence`.
- Los botones van por lo que significan:
  - `checkout`: `https://pay.hotmart.com/{{1}}`;
  - `ficha`: la base de `[[ficha.medios]]`, con `medio` si hay más de una;
  - `url_fija`: con su `url`;
  - `respuesta`: una respuesta rápida.

**Las herramientas del plugin** (`automation-expert-change`, F-146 parte 5). `propose_template` (`template`, `purpose`, opcionales `for_flow`, `request_id` y `revision`), `validate_template` (`request_id`, `revision`), `list_template_requests` (`estado`, `limit`) y `get_template_request` (`request_id`). Quien pide sale de la sesión de Slack, nunca del modelo. Ninguna habla con Meta ni llama a la página. Sin el flag (`404` genérico) contestan `template_requests_not_enabled`. Validada y sin bloqueos, `validate_template` postea en el DM el botón «Revisar y mandar a Meta», que abre `/flow-approval/t/{id}`, y suma `approval_button` a la respuesta (`posted` o `not_posted`, sin el link). Con `FLOW_APPROVAL_ENABLED` apagado en las variables del profile (distinto del flag del bridge) la respuesta no trae `approval_button`. El botón no se reemite: volver a validar contesta `meta_template_not_draft`, y `scripts/flow_change_operator.py` no maneja plantillas; una solicitud sin botón vence a las 24 horas y se guarda de nuevo con el mismo nombre. Lo usa la skill `flujos/pedir-plantilla-a-meta` del profile.

## 4. Las reglas (`template_request.check`)

Las de Meta son las de `meta_plantillas.validar` del OS, portadas:

- el cuerpo de hasta 1.024 caracteres;
- los datos con nombre y no numerados;
- cada dato una sola vez;
- no empezar ni terminar con un dato, ni dos seguidos;
- no más de una línea en blanco seguida;
- botones de hasta 25 caracteres y hasta 10;
- encabezado y pie fijos, de 1 a 60;
- la URL fija con `https://` y sin datos;
- una de marketing con la respuesta «No más mensajes». Sin el acento también vale; con puntuación de más no (`opt_out.py` la detectaría, pero acá se pide el texto exacto).

Avisos, que no frenan: pocas palabras para tantos datos, un link con respuestas rápidas, y una de utilidad que habla de descuento, oferta, promoción, cupón o gratis.

Las del producto:

- **El nombre:** `<instancia>_<propósito>_NN`.
- **El idioma:** el de las plantillas del manifiesto.
- **El nombre, fuera del catálogo:** no puede estar en el catálogo de la bandeja, en ningún estado ni idioma (más estricto que Meta, que mira nombre e idioma). Chatwoot puede tener hasta 3 horas de atraso, y un nombre recién borrado en Meta no figura: ese error lo maneja el envío (parte 3).
- **Los marcadores:** un dato va entre dos llaves y con su nombre sin espacios. Una llave suelta, tres llaves o un nombre con espacios bloquean (`template_variable_malformed`).
- **`precio`:** solo si el manifiesto tiene precio (el ejemplo para Meta sale de ahí).
- **Los datos:** solo los del vocabulario del flujo.
- **Los botones de link:** uno solo, y primero.
- **La ficha:** solo con un medio de `[[ficha.medios]]` habilitado.
- **La URL fija:** solo de la lista de la instancia, `[plantillas].urls_fijas` del manifiesto (D7). Sin la lista, ningún botón fijo pasa.
- **El texto:** ni links (tampoco un dominio suelto, como `marca.com.mx`) ni teléfonos ni mails, en el cuerpo, el encabezado, el pie y los botones. La regla de los teléfonos es la de la lectura del AE (siete dígitos seguidos), así que un monto como `1.497.000` también bloquea.
- **Encabezado, pie y botones:** en una sola línea, y el texto de un botón sin llaves (el bridge no llena datos en un botón). Formato o emojis en el encabezado dan un aviso: que Meta los rechace está a verificar.
- **Para un paso de flujo** (`for_flow`, al validar): el paso la espera con `{a_crear: <nombre>}`; si nombra idioma o variables, son los del borrador; su botón (`checkout` o `ficha`) es el primero de la plantilla, y sin botón en el paso la plantilla no lleva un link que cambia por persona; `precio` y `vence` van solo con cupón; y una de marketing no va en un flujo con permiso de compra.

**El seguimiento con cupón** (E-272, decisión de Dan del 2026-11-02: toda plantilla de marketing del recuperador lleva «No más mensajes», sin excepción). Se pide con el botón `checkout` primero y la respuesta `No más mensajes` después, la forma que `parse_followup_template` acepta desde E-272; sin la respuesta, la de marketing se sigue frenando con `template_marketing_without_opt_out`. No hay salida para una de marketing sin la baja.

**El pedido de Meta** (`template_request.meta_request`) es el de `meta_plantillas.pedido`, posicional y con `parameter_format: POSITIONAL` (es lo que Meta devuelve en los catálogos; la documentación de Meta lo muestra en el ejemplo de creación de v23.0, leída el 2026-11-08; el envío real lo confirma el primer día en Maribel). La base compara su cuerpo con el del borrador numerado y su encabezado con el del borrador antes de congelarlo. Los ejemplos los pone el bridge:

- el nombre genérico fijo «María»;
- el producto, la oferta y el precio del manifiesto;
- la tienda de la ficha;
- para el checkout, una URL con el hotlink y la oferta por defecto;
- para la ficha, un hash inventado.

Contra el catálogo capturado de ATT1, el pedido de `att1_pago_efectivo_confirmado_01` y el de `att1_bienvenida_01` salen idénticos a lo que Meta aprobó.

## 5. La página y el envío (parte 3)

Con la misma sesión de «Entrar con Slack» y la misma lista de aprobadores que la página de flujos. La página sale en capturas, así que muestra la plantilla y su estado en lenguaje de negocio: sin el id, la huella, el id de Slack ni el nombre técnico a la vista (el nombre va plegado, para buscarla en WhatsApp Manager una vez mandada).

- **`GET /flow-approval/t/{id}`**: la burbuja guardada al validar, qué significa la categoría, los avisos del validador y el estado. Entrando por el link, vuelve a la solicitud: el id viaja adentro del `state` del OIDC, porque la base solo acepta volver a la lista o a una propuesta.
- **`POST /flow-approval/t/{id}/send`** con `csrf_token` y `fingerprint`. Origin, sesión viva y en la lista, CSRF y huella. Aprueba (`approver_approve_meta_template_request_v1`), empieza el envío con el tope por día, y recién ahí habla con Meta. Lo mandado es el `meta_request` congelado, nunca algo del formulario.
- **`POST /flow-approval/t/{id}/discard`** con `csrf_token` y `motivo` (opcional). Solo si no llegó a Meta.

**La idempotencia por nombre.** Antes de crear, se recorre la lista de plantillas de la cuenta y se busca el nombre con su idioma, porque la referencia de Meta no documenta un filtro por nombre en esa ruta. Si está con el mismo encabezado, cuerpo, pie y botones, se adopta y no se crea otra: un reintento después de un timeout no duplica. Si difiere en cualquiera de esos (`meta_name_taken_other_text`) o pausada o desactivada (`meta_name_taken_unusable`), no se adopta. Si la lista no se puede leer hasta el final, no se crea.

**Los errores.** Se guarda solo el código (`last_send_error`): `meta:<code>`, `meta:<code>:<subcode>`, `meta_http:<status>`, `meta_unreachable`, `meta_unexpected_answer`, `meta_list_too_long`, `meta_name_taken_*`. Ningún texto de Meta sale de `meta_templates.py`. La página los traduce (`describe_send_error`) y nunca muestra el código. Con error la solicitud queda `enviada` y se puede volver a mandar a los 2 minutos, hasta 5 intentos.

**El cliente de Meta** (`meta_templates.py`) tiene tres llamadas: crear (el único POST), buscar por nombre y leer por id. Una prueba falla si aparece otra ruta u otro verbo. El token va en el header, y la página siguiente se pide con el cursor: no se sigue la URL `paging.next`.

| Variable | Qué es |
|---|---|
| `META_TEMPLATES_ENABLED` | El flag, `false` por defecto. |
| `META_TEMPLATES_ACCESS_TOKEN` | Secreto, solo con `whatsapp_business_management`, distinto de todo otro valor. Mínimo 32 caracteres. |
| `META_WABA_ID` | El id de la cuenta de WhatsApp. |
| `META_GRAPH_API_VERSION` | `v23.0` por defecto. |
| `META_TEMPLATES_DAILY_CAP` | De 1 a 20, 5 por defecto (D8). |

## 6. Los permisos al arrancar

Con `META_TEMPLATES_ENABLED=true` el bridge exige el manifiesto, `AUTOMATION_EXPERT_CHANGE_ENABLED`, `FLOW_APPROVAL_ENABLED` (sin la página no hay quien apruebe), el token, el WABA, la versión de la Graph API y el tope. Validar exige además `SETTER_VERSION` y `GIT_SHA` en la imagen (si faltan, la validación contesta 503; el arranque no lo mira, igual que las rutas de cambio de flujos). El tope por día no es del validador: lo aplica la base al empezar el envío.
