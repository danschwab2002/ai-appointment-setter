# contact-export-v1: exportar contactos y listas (propuesta)

Estado: **propuesto**. Nada de esto corre en ninguna instancia hasta que se publique una version que lo traiga y se
prenda `CONTACT_EXPORT_ENABLED` en esa instancia. Funcion 1.3 de la lista definitiva del 2026-11-20 («Exportar
contactos y listas»), pieza 4 del mapa de las funciones faltantes de Z1 del 2026-11-26 (decisiones D7 y D12).

## Para que

Sin GHL no hay de donde sacar una lista para el mail, un sorteo o la planilla del asesor. Esto la saca de la base
de la instancia: el Automation Expert arma el pedido con filtros y un aprobador baja el CSV en la pagina.

Exportar no manda nada a nadie. Pero es dato personal: ningun telefono, mail ni nombre va al hilo de Slack ni a un
log.

## El circuito

1. Alguien le pide la lista al AE por Slack. El AE llama a `export_contacts` (plugin `automation-expert-change`).
2. El bridge (`POST /internal/automation-expert/v1/contact-exports`) valida los filtros, lee la base y, si hace
   falta, las etiquetas de Chatwoot, cuenta cuantas personas cumplen y guarda el **pedido** (los filtros, las
   columnas, el conteo y quien lo pidio; ningun telefono) con `create_contact_export_v1`.
3. Al hilo vuelven **solo el conteo y el link** de la pagina (`/flow-approval/exportar/<id>`), con su vencimiento.
4. En la pagina entra un aprobador con «Entrar con Slack» (la misma sesion y la misma lista de aprobadores que la
   pagina de aprobacion de flujos y la del masivo). Ve los filtros, cuantas coincidian al pedirlo y un boton
   «Bajar el CSV».
5. Al bajarlo, el bridge vuelve a leer la base y las etiquetas en ese momento (el CSV es de ahora, no del momento
   del pedido), arma el CSV y recien despues marca el pedido como bajado. **Se baja una vez**: un segundo intento,
   o uno pasados **30 minutos** desde el pedido, no baja nada.

## Quien es una persona

Un telefono, en su forma canonica (`_whatsapp_phone_canonical`: `52`/`54` + 10 para Mexico y Argentina), que
aparece en alguna de estas fuentes de la base de la instancia:

| Fuente | Que aporta |
|---|---|
| `contacts` (`phone`), `contact_points` (`phone`) y `channel_identities` (`whatsapp`) | el telefono, el nombre (`full_name`) y el mail (`email`) del contacto |
| `purchase_intents` del tenant (el formulario de la landing) | el telefono, el mail, la fecha y el producto de cada formulario; el nombre sale del `canonical_payload.lead.full_name` de su envio (`precheckout_submissions`) |
| las compras aprobadas de Hotmart sin reembolso ni contracargo, atadas al tenant (`portable_hotmart_purchase_correlations`) | el telefono, el nombre y el mail del comprador, y el producto comprado. Es la misma lectura que «compro» en la audiencia y en los vetos del masivo |
| `contact_opt_out_events` (`whatsapp`) y `contact_authorizations` denegadas vigentes | la baja («No más mensajes»), por el telefono en cualquiera de sus formas o por el contacto. La misma regla que los vetos del masivo |
| Chatwoot (`ChatwootLabelIndex`, el listado de conversaciones de la bandeja) | las etiquetas, por telefono, de las conversaciones con actividad en los ultimos `etiquetas_dias` (90 por defecto, hasta 365) |

El nombre y el mail se eligen en ese orden: el del contacto, el del formulario mas nuevo, el del comprador mas nuevo.

Supuesto: **una base por instancia** (`contacts` no lleva `tenant_ref`). Medido en solo lectura el 2026-11-26: en
la base de ATT1 y en la de Maribel `purchase_intents` tiene un solo `tenant_ref` y `channel_identities` una sola
cuenta.

## Los filtros (`filtros`)

Todos opcionales; sin ninguno, salen todas las personas que no se dieron de baja.

| Clave | Que hace |
|---|---|
| `etiquetas` | tiene al menos una de estas etiquetas de Chatwoot (hasta 10) |
| `sin_etiquetas` | no tiene ninguna de estas (hasta 10) |
| `compro` | `true`: compro; `false`: no compro |
| `producto` | un producto de la instancia (`funnel_ref`): `compro` y el formulario se miran solo para ese producto. Solo junto con `compro` o con la ventana del formulario |
| `formulario_desde`, `formulario_hasta` | dejo un formulario entre esas fechas (`AAAA-MM-DD`, en la zona de la instancia, las dos incluidas; una sola alcanza) |
| `paises` | el pais del telefono (ISO de dos letras, hasta 10), por el indicativo |
| `incluir_bajas` | `false` por defecto: las bajas no salen |
| `etiquetas_dias` | cuantos dias de conversaciones se miran para las etiquetas (1 a 365, 90 por defecto) |

## Las columnas (`columnas`)

Por defecto: `nombre`, `telefono` (E.164: Mexico `+52` + 10, Argentina `+549` + 10, el resto `+` y los digitos),
`email`, `etiquetas` (separadas por `;`), `compro` (`si`/`no`) y `ultimo_formulario` (ISO 8601 en la zona de la
instancia). Opcionales: `pais`, `baja` (`si`/`no`) y `productos` (los comprados, separados por `;`).

Toda celda que empieza con `=`, `+`, `-`, `@`, tabulador o retorno se escribe con un apostrofo adelante (la misma
regla `_SAFE_CSV_START` de la bajada del masivo), para que la planilla no la lea como formula. El telefono va con
`+` adelante, asi que siempre lleva el apostrofo.

## Topes

- La lectura de la base va en paginas de 1.000 personas con cursor por telefono, y corta en **20.000**: una
  instancia con mas personas devuelve `contact_export_too_large` en vez de un CSV a medias.
- Si hacen falta las etiquetas (filtro o columna) y Chatwoot no las da enteras, no sale nada
  (`contact_export_labels_unavailable`): una lista por etiqueta no sale a medias.
- Hasta 20 pedidos por instancia por hora.

## La base (`20261126300000_contact_export_v1.sql`)

Tabla `contact_exports` (sin telefonos). Funciones del bridge, solo `service_role`: `create_contact_export_v1`,
`read_contact_export_v1` y `read_contact_export_people_v1`. Funciones de la pagina, solo `flow_change_approver`, con
la sesion de «Entrar con Slack»: `approver_read_contact_export_v1`, `approver_read_contact_export_people_v1` (solo
con el pedido sin bajar y sin vencer) y `approver_mark_contact_export_downloaded_v1` (una vez, antes del
vencimiento). Interna, de nadie: `_contact_export_people`.

## El flag

`CONTACT_EXPORT_ENABLED` (apagado por defecto). Sin el, las rutas del AE y de la pagina no existen (404), el bridge
no llama a ninguna de estas funciones y `/ready` no cambia. Con el, exige `AUTOMATION_EXPERT_CHANGE_ENABLED`,
`FLOW_APPROVAL_ENABLED` con `FLOW_APPROVAL_BASE_URL`, el manifiesto de la instancia y `SUPABASE_BASE_URL` con
`SUPABASE_SERVICE_ROLE_KEY`, y `/ready` dice `contact_export: enabled`.
