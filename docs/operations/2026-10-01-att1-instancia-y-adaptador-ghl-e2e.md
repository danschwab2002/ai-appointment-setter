# ATT1: instancia instalada desde el manifiesto y adaptador de GHL con E2E

- Fecha: 2026-10-01.
- Tipo: evidencia operativa. Describe lo instalado y medido entre el 2026-09-30 04:14 UTC y el
  2026-10-01 04:00 UTC. No es contrato ni arquitectura.
- Alcance: la instancia de ATT1 (stack `setter-att1` del VPS), las versiones `v1.1.0` y `v1.2.0`
  del producto y el adaptador del formulario de GHL (PR #210). **No toca a Johanna**: sus
  servicios `infra_*` no cambiaron de imagen ni de configuración en este trabajo.
- Claim: `claude-att1-estado-instancia-y-adaptador-v1` (este documento).
- Contratos: `docs/contracts/ghl-precheckout-adapter-v1.md`, `docs/contracts/lead-precheckout-v1.md`,
  `docs/contracts/instance-runtime-v2.md`.

Este documento no contiene teléfonos, nombres, emails, tokens ni el host público del bridge.
Los identificadores de intención van recortados a ocho caracteres.

## 1. Qué quedó corriendo

Medido el 2026-10-01 03:58 UTC con `docker service ls`, `docker inspect` y `/ready` desde dentro
del contenedor.

| Servicio | Imagen | Iniciado (UTC) | Health |
|---|---|---|---|
| `setter-att1_att1-bridge` | `setter-bridge:v1.2.0` (`sha256:6054079a79b2…`) | 2026-10-01 03:41:49 | healthy |
| `setter-att1_att1-db` | `supabase/postgres:17.6.1.167` | 2026-09-30 04:14:42 | healthy |
| `setter-att1_att1-rest` | `supabase/postgrest:v14.15` | 2026-09-30 04:14:40 | sin healthcheck |
| `setter-att1_att1-gateway` | `nginx:alpine` | 2026-09-30 04:14:51 | sin healthcheck |

- El contenedor del bridge declara `SETTER_VERSION=1.2.0` y
  `GIT_SHA=74d11871b953c4f69391a05a7f117416c832faff`, el commit del tag `v1.2.0` (merge del
  PR #210). La imagen se compiló en el VPS desde `git archive v1.2.0`; no es la de GHCR
  (`ghcr.io/danschwab2002/setter-bridge:v1.2.0@sha256:b99592f8d1ee…`), así que el digest local
  no coincide con el de la release. Lo que se comparó es el commit, no el artefacto.
- `/ready` responde `200` con: `instance_ally: att1`, `instance_product_version: v1.2.0`,
  `commercial_ally_binding: active`, `automation_state: default_off`,
  `pilot_boundary: disabled`, `ghl_precheckout_adapter: enabled:2-forms`,
  `commercial_knowledge: v1:d3c0235910f0…`, `precheckout_delayed_first_touch: disabled`,
  `chatwoot_stalled_monitor: disabled`.
- Base propia: baseline más la cadena completa hasta `20260930000300` (95 entradas en el registro
  de migraciones de la instancia). Las filas que la cadena siembra para Johanna se borraron y se
  cargaron las de ATT1: binding activo con tres ofertas y sus landings, scope entrante
  `att1-inbound`, scope del piloto `att1-recuperacion` v1 publicado en `manual_cohort` con el
  control en `inactive`, política `att1-recuperacion-un-toque` y derivación al equipo 2.
- Agente: profile `att1-agente-comercial` en el Hermes compartido, puerto propio, modelo
  `z-ai/glm-5.3-flash` con clave propia de OpenRouter. Bridge → profile: `200` el 2026-10-01
  02:57 UTC. El conocimiento `knowledge-v1.toml` está en `aprobado` (2026-09-30) y cargado con
  `COMMERCIAL_KNOWLEDGE_ENABLED`.
- Chatwoot, cuenta de ATT1: etiquetas, macros de pausa, opt-out y reanudación, y un AgentBot sin
  URL y sin vínculo al inbox. **El webhook de la cuenta todavía no apunta a este bridge.**
- Hotmart: **el webhook del producto todavía no apunta a este bridge.**
- **Todos los `[flujos]` del manifiesto están en `false`.** En este trabajo no salió ningún
  mensaje a ningún lead.

El stack viejo `att1-production_*` (bridge dark, postgres, product-hermes y agent-profile en 0/1)
sigue definido y sin tocar.

## 2. Releases

| Tag | Commit | Publicada (UTC) | Qué trae |
|---|---|---|---|
| `v1.1.0` | `284fad0` | 2026-09-30 21:10 | Bloques A y C de la cadena portable (PR #208): destinatario con cualquier oferta, parámetros de plantilla por instancia, modo directo del dispatcher, permiso del pago fallido por intención consentida (`20260930000100`), formulario en cada landing del binding (`20260930000200`), `audience_mode` del scope (`20260930000300`). |
| `v1.2.0` | `74d1187` | 2026-10-01 02:51 | Adaptador del formulario de GHL (PR #210). Sin migraciones. |

El manifiesto de `v1.2.0` (`[adaptadores]`) no es válido para `v1.1.0`: una vuelta atrás del bridge
exige también volver el repo de la instancia al commit anterior.

## 3. Ruta pública

El bridge de ATT1 se publica por un archivo propio en la configuración dinámica del proxy, con
**tres rutas exactas**: `/webhooks/adapters/ghl/lead-precheckout`, `/webhooks/chatwoot` y
`/webhooks/hotmart`. Medido el 2026-10-01 03:01 UTC desde fuera del VPS: `GET` a las tres responde
`405`; `/ready`, `/health`, `/internal/*`, `/webhooks/lead` y `/` responden `404` del proxy; `http`
redirige a `https`. El bridge de Johanna, en cambio, publica todo su árbol de rutas.

## 4. Adaptador de GHL: E2E

`GHL_PRECHECKOUT_ADAPTER_ENABLED` se prendió el 2026-10-01 03:04 UTC, con autorización de Dan.

| Hora (UTC) | Qué se envió | Respuesta | Qué quedó en la base |
|---|---|---|---|
| 03:09 | Formulario de `ads-a`, acción *Webhook* nueva del workflow | `422 ghl_not_a_form_submission` | nada |
| 03:17:46 | Formulario de `ads-a`, acción original del workflow, navegador sin sesión previa | `200` | 1 envío, 0 conflictos, intención `19dd2d69` |
| 03:43:29 | Formulario de `alimenta-tu-tiroides-d`, mismo método | `200` | 1 envío, 0 conflictos, intención `6786f35d` |

- Las dos intenciones quedaron en `waiting_for_purchase`, con `whatsapp_contact_authorized` y
  `activation_authorized` en `true`, `provider_observed` en `true` y `provisional` en `false`.
  La de `ads-a` lleva la oferta `gopi6lh7`; la de `alimenta-tu-tiroides-d`, la `2uafw5bg`.
- Los dos envíos fueron del mismo teléfono. **Landing y oferta distintas dan dos intenciones**,
  no una: la correlación de una compra de Hotmart es por oferta.
- El segundo formulario entró a `[adaptadores.ghl].formularios` después de comprobar en su widget
  público que muestra el texto de `att1-whatsapp-contact-v1`. Antes de ese cambio el formulario no
  pedía contacto por WhatsApp. El bridge lee el manifiesto al arrancar: el cambio rigió desde el
  reinicio de las 03:41 UTC, y desde entonces `/ready` informa `enabled:2-forms`.
- El receptor de captura que había recibido los envíos del 2026-09-29 se dio de baja a las
  03:29 UTC, con su ruta y sus capturas crudas. Quedan solo los fixtures anonimizados de
  `tests/fixtures/ghl/`.

## 5. Lo que se aprendió midiendo

1. **Una acción *Webhook* nueva de GHL no manda el mismo cuerpo que la que ya existía.** La acción
   creada el 2026-10-01 mandó el cuerpo sin `attributionSource` de primer nivel, y el adaptador lo
   rechazó como corresponde (`ghl_not_a_form_submission`). La acción original del mismo workflow
   lo manda completo. Se resolvió cambiando la URL de la acción original. No está medido qué
   opción de GHL produce la diferencia.
2. **`attributionSource.url` parece ser la página donde empezó la sesión, no la del formulario.**
   Un envío hecho después de pasar por otra página del mismo dominio llegó con la URL de esa otra
   página. Es una sola observación: queda como hipótesis. Consecuencia práctica: las pruebas se
   hacen en un navegador sin sesión previa, entrando directo a la landing. Para un lead real, la
   landing resuelta puede no ser la del formulario que envió; el adaptador rechaza con
   `ghl_landing_unknown` la que no esté en el manifiesto.
3. **El móvil argentino queda guardado sin el 9** (`54` más diez dígitos). El adaptador normaliza
   solo el `+521` mexicano. WhatsApp identifica ese móvil con el 9.
4. **En Chatwoot los móviles mexicanos aparecen de las dos formas.** Medido el 2026-10-01 sobre el
   inbox de Johanna, solo prefijo y largo: 15 contactos con `521` más diez dígitos y 44 con `52`
   más diez dígitos. El teléfono del contacto es siempre el identificador de WhatsApp con `+`.
   Como el permiso del pago fallido y la audiencia `consented_intent` comparan el teléfono exacto
   (`_portable_consented_intent_reason`, migración `20260930000100`), un mismo móvil escrito de
   las dos formas no cruza. El contrato del adaptador ya lo nombraba como riesgo; ahora está
   medido que las dos formas conviven. No se corrige en este PR.
5. **Una admisión correcta no deja línea propia en el log.** El adaptador la registra en nivel
   `info` y bajo uvicorn solo llegan los `warning`: de un `200` se ve únicamente la línea de
   acceso. Un vigilante que espere la línea del adaptador no ve los éxitos. Los rechazos sí salen.
6. **Las tres plantillas de primer contacto coinciden con lo que el producto manda.** Catálogo del
   inbox de ATT1 leído el 2026-10-01: `att1_interes_precheckout_01`, `att1_carrito_abandonado_01` y
   `att1_compra_fallida_01` están `APPROVED` en `es_MX`, categoría `MARKETING`, con `{{1}}`
   (nombre) y `{{2}}` (producto) en el cuerpo y tres botones de respuesta rápida. Es la forma por
   defecto de los parámetros (`nombre`, `producto`).

## 6. Lo que este documento no prueba

- Ningún envío de una plantilla: la cadena `ResolutionWorker` → `DurableDispatcher` → gate de Meta
  no corrió en ATT1. Sus flags siguen apagados.
- Ninguna compra, carrito ni pago fallido de Hotmart llegó a este bridge.
- El agente de ATT1 no contestó ninguna conversación real: el webhook de Chatwoot no apunta acá.
- La condición del contrato del adaptador para prender el primer contacto o una audiencia
  `consented_intent` (verificación fuera de banda del envío o aceptación escrita del riesgo) sigue
  sin cumplirse.
- No hay tráfico real medido por el adaptador: los dos envíos admitidos son de prueba.

## 7. Cómo se vuelve atrás

- Apagar el adaptador: `GHL_PRECHECKOUT_ADAPTER_ENABLED=false` y reiniciar el bridge. La ruta pasa
  a responder `503 ghl_precheckout_adapter_not_enabled` y GHL reintenta.
- Volver a `v1.1.0`: la imagen `setter-bridge:v1.1.0` sigue en el VPS; el repo de la instancia
  tiene que volver al commit anterior a `[adaptadores]`.
- Quitar la ruta pública: borrar el archivo de la instancia en la configuración dinámica del proxy.
