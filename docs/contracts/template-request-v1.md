# template-request-v1: la solicitud de plantilla de Meta

**Versión:** 1.0.0 (F-146, partes 1 y 2: la base, el validador y las rutas del Automation Expert).
**Migración:** `20261102000200_meta_template_requests_v1.sql`.
**Código:** `src/bridge/template_request.py` (puro) y `src/bridge/template_request_api.py` (las rutas).
**Flag:** `META_TEMPLATES_ENABLED`, apagado por defecto. Apagado, las rutas no existen y nada cambia.

Cuando un flujo necesita una plantilla de WhatsApp que no existe, el Automation Expert (AE) la redacta con el dueño y la deja como **solicitud**: un objeto del almacén hermano de la propuesta de flujo (`flow-change-cycle-v1`), con revisiones mientras es borrador, una validación que lo congela con su huella por 24 horas, la aprobación de una persona de la lista de la instancia, y después el envío a Meta y lo que Meta contesta.

**Lo que esta versión no hace.** Nada llama a Meta, ni para leer. La página que muestra la solicitud y la manda (parte 3), el cliente cerrado de Meta, el sondeo del estado y la sincronización del catálogo de Chatwoot (parte 4), las herramientas del plugin y el SOUL (parte 5) y el token y el manifiesto de la instancia (parte 6) vienen después. La base ya tiene las funciones que esas partes van a llamar.

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
- **`GET /template-requests?estado=abiertas|todas&limit=1..50`**.
- **`GET /template-requests/{id}`**: la solicitud, su vista previa y su resultado.

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
- **La URL fija:** solo de la lista de la instancia (D7). Hasta que el manifiesto la declare (`[plantillas].urls_fijas`, parte 6), la lista está vacía y ningún botón fijo pasa.
- **El texto:** ni links (tampoco un dominio suelto, como `marca.com.mx`) ni teléfonos ni mails, en el cuerpo, el encabezado, el pie y los botones. La regla de los teléfonos es la de la lectura del AE (siete dígitos seguidos), así que un monto como `1.497.000` también bloquea.
- **Encabezado, pie y botones:** en una sola línea, y el texto de un botón sin llaves (el bridge no llena datos en un botón). Formato o emojis en el encabezado dan un aviso: que Meta los rechace está a verificar.
- **Para un paso de flujo** (`for_flow`, al validar): el paso la espera con `{a_crear: <nombre>}`; si nombra idioma o variables, son los del borrador; su botón (`checkout` o `ficha`) es el primero de la plantilla, y sin botón en el paso la plantilla no lleva un link que cambia por persona; `precio` y `vence` van solo con cupón; y una de marketing no va en un flujo con permiso de compra.

**Lo que la v1 no cubre.** Una plantilla de marketing con el botón de checkout como único botón (la forma que pide el seguimiento con cupón nativo, `parse_followup_template`) no se puede pedir desde el AE: la de marketing exige «No más mensajes». En el OS existía la salida `_sin_baja_porque` (por `att1_seguimiento_descuento_01` y `maribel_seguimiento_descuento_01`); acá no se portó. Queda como decisión abierta para Dan.

**El pedido de Meta** (`template_request.meta_request`) es el de `meta_plantillas.pedido`, posicional y con `parameter_format: POSITIONAL` (es lo que Meta devuelve en los catálogos; que lo acepte al crear se confirma antes de la parte 3, y si no se omite: lo de Meta por defecto es posicional). La base compara su cuerpo con el del borrador numerado y su encabezado con el del borrador antes de congelarlo. Los ejemplos los pone el bridge:

- el nombre genérico fijo «María»;
- el producto, la oferta y el precio del manifiesto;
- la tienda de la ficha;
- para el checkout, una URL con el hotlink y la oferta por defecto;
- para la ficha, un hash inventado.

Contra el catálogo capturado de ATT1, el pedido de `att1_pago_efectivo_confirmado_01` y el de `att1_bienvenida_01` salen idénticos a lo que Meta aprobó.

## 5. Los permisos al arrancar

Con `META_TEMPLATES_ENABLED=true` el bridge exige el manifiesto, `AUTOMATION_EXPERT_CHANGE_ENABLED` y `FLOW_APPROVAL_ENABLED`: sin la página no hay quien apruebe. Validar exige además `SETTER_VERSION` y `GIT_SHA` en la imagen (si faltan, la validación contesta 503; el arranque no lo mira, igual que las rutas de cambio de flujos). El tope por día no es del validador: lo aplica la base al empezar el envío. El token de Meta y el WABA los exigirá la parte 3, que es la que los usa.
