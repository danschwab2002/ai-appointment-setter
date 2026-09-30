# Instalar el setter para un negocio nuevo

Para quien instala el setter en una aliada nueva sin haber participado de su desarrollo. **Cada paso dice cómo se verifica que salió bien:** si la verificación no da lo que dice, no se sigue al siguiente.

> **Estado de esta guía.** Se escribe mientras se instala la primera instancia (ATT1), paso por paso (regla 2 de `docs/design/setter-producto-instalable-v1.md` §8.1). Los pasos 1 a 5 se ejecutaron para ATT1 el 2026-09-28 (PR #199: manifiesto y conocimiento en `tests/fixtures/instances/att1/`; la salida de `validate` en `evidencia/` del repo de la instancia). Del 6 en adelante son la secuencia prevista: se completan con el detalle real a medida que se ejecutan.

## Qué necesitás antes de empezar

- Acceso de lectura a este repo y a GHCR (`ghcr.io/danschwab2002/setter-*`).
- La cuenta de Hotmart que vende el producto, con permiso para configurar webhooks.
- Un número de WhatsApp en WhatsApp Cloud (WABA), conectado como inbox en Chatwoot, y el WhatsApp Manager para crear plantillas.
- Un VPS con EasyPanel donde ya corran Chatwoot y Hermes, o donde se puedan levantar.
- Un canal de Slack para las derivaciones (opcional al principio).
- La landing del producto: su URL, y el `off=` de Hotmart que usa cada landing.

## 1. Crear el repo de la instancia

Un repo **privado** por aliada: `setter-instancia-<aliada>`. Estructura:

```text
setter-instancia-<aliada>/
├── README.md
├── instancia.toml
├── conocimiento/knowledge-v1.toml
├── despliegue/compose.yaml
├── .env.example
└── evidencia/
```

Ejemplo completo: el manifiesto y el conocimiento de ATT1 en `tests/fixtures/instances/att1/`.

**Verificación:** `gh repo view <dueño>/setter-instancia-<aliada> --json visibility` dice `PRIVATE`.

## 2. Relevar los datos del negocio

Todo se mide en la fuente, no se pregunta de memoria:

| Dato | Dónde se mide |
|---|---|
| Producto e hotlink de Hotmart | La landing publicada: el botón o el script de checkout apunta a `pay.hotmart.com/<hotlink>?off=<oferta>`. Se confirma que `https://pay.hotmart.com/<hotlink>?off=<oferta>` abre el checkout real |
| Una oferta por landing | Cada landing trae su `off=`. Se abre cada una en un navegador y se anota landing → oferta. Una oferta que ninguna landing usa **no** va al manifiesto |
| Cuenta, inbox y team de Chatwoot | En Chatwoot: *Settings → Inboxes* (el ID está en la URL del inbox) y *Settings → Teams* |
| Plantillas de Meta | En Chatwoot, las plantillas sincronizadas del inbox; o en el WhatsApp Manager. Se anota **nombre, idioma y estado exactos**: una plantilla aprobada en otro idioma es otra plantilla |
| Precio y moneda | El checkout de Hotmart |

**Verificación:** cada valor anotado lleva de dónde salió y la fecha. En ATT1 quedó en `conocimiento/knowledge-fuentes.md` del repo de la instancia.

## 3. Completar `instancia.toml`

Con la referencia de campos (`docs/referencia-manifiesto.md`). Reglas:

- **Todos los flujos en `false`.** Se prenden de a uno al final (paso 12).
- Una plantilla que todavía no existe o que está en otro idioma **se omite**. El flujo que la necesita queda sin poder prenderse, y `validate` lo avisa.
- En `eventos` va solo lo que ya llega de verdad. Si la landing todavía no manda el evento de intención, `intencion` no va.
- Nada secreto: tokens y claves van en el paso 7.

## 4. Escribir el conocimiento del agente

`conocimiento/knowledge-v1.toml`, con el contrato de `docs/contracts/commercial-knowledge-v1.md`:

- Cada dato sale de una fuente publicada o aprobada, y la fuente va en un comentario al lado.
- Lo que no se encontró **no se rellena**: va a `[no_confirmado]`. El agente lo deriva sin anunciar que falta.
- La voz se toma de conversaciones reales del equipo de la aliada, no se inventa.
- Si el conocimiento se contradice con la página (una cuenta regresiva, por ejemplo), gana la política: se anota en `[promesas_prohibidas]`.
- La presentación del agente coincide con la de las plantillas aprobadas (lo que dice el primer mensaje).

El archivo nace en `estado = "borrador"`. Lo aprueba quien tiene la autoridad comercial de la aliada: `estado = "aprobado"`, `aprobado_por`, `aprobado_el`. **Sin aprobación el agente no responde.**

## 5. Validar

```sh
uv run python -m bridge.instance_cli validate ../setter-instancia-<aliada>
```

**Verificación:** termina en `VALIDA`. Los avisos listan qué flujos no se pueden prender y por qué. Se guarda la salida en `evidencia/<fecha>-validate.txt`.

## 6. La base de la instancia

> Pendiente de ejecutar con ATT1. Decisión abierta: un proyecto de Supabase aparte, o Postgres de Supabase + PostgREST en el VPS.

Cada instancia tiene su base. Nunca se comparte con otra aliada.

## 7. Secretos

> Pendiente. Lista completa en `.env.example` del repo de la instancia.

## 8. El agente en Hermes

> Pendiente. Un profile por instancia (`agente.modelo` del manifiesto) con el SOUL común del producto (`profiles/agente-comercial-comun/SOUL.md`). El conocimiento no va en el profile: lo manda el bridge.

## 9. Levantar el bridge desde la imagen

> Pendiente. Imagen `ghcr.io/danschwab2002/setter-bridge:<producto>`, con `INSTANCE_MANIFEST_PATH` apuntando al manifiesto montado. Cómo se pasa un servicio de EasyPanel a una imagen: `docs/operations/release-por-imagen-v1.md`.

**Verificación prevista:** `/ready` da 200 con `instance_ally` igual a la aliada, `instance_product_version` igual a `producto` y, si el conocimiento está cargado, `commercial_knowledge` con su versión y hash.

### Actualizar el bridge de una instancia que ya tiene la base sembrada

El orden es siempre **migraciones → filas de la instancia → bridge → `/ready`**. La misma imagen corre en todas las instancias, así que el bridge nuevo llega a una base que puede no tener todavía lo que él espera. Un bridge viejo sobre una base migrada sigue andando (toma solo los campos que conoce); al revés no.

**Caso `20260930000200` (landings por oferta, CHANGELOG 1.1.0).** Desde esa versión el manifiesto v2 declara la landing de cada oferta adicional y el bridge compara la fila del binding campo por campo. Una fila sembrada antes de la migración queda con `additional_offer_landings = []` y el bridge nuevo ve drift: `/ready` responde `503 commercial_ally_binding_unavailable` y, con `SLACK_CONNECTOR_PROJECTION_ENABLED=true`, el arranque falla. Pasos, con los valores de ATT1 (salen de `[[hotmart.ofertas]]` del manifiesto, en su orden):

1. Aplicar la migración.
2. Ver si la fila ya existe y cómo está:

   ```sql
   select binding_version, status, additional_offer_codes, additional_offer_landings
   from public.commercial_ally_runtime_bindings
   where tenant_ref = 'lancemos' and funnel_ref = 'att1';
   ```

   Si no hay fila, se siembra ya con las landings (el `aprovisionar-*.sql` de la instancia las tiene que incluir en el insert y en su verificación). Si hay una fila `active` con `additional_offer_landings = []`, se sigue con el paso 3.
3. Completar las landings **en la misma fila**:

   ```sql
   begin;
   update public.commercial_ally_runtime_bindings
   set additional_offer_landings = '[
         {"offer_code": "bmaztyhg", "site": "metodoraizana", "landing_id": "org-a",
          "page_host": "www.metodoraizana.com", "page_path": "/att1/evg/vsl/org-a"},
         {"offer_code": "2uafw5bg", "site": "metodoraizana-mx", "landing_id": "alimenta-tu-tiroides-d",
          "page_host": "site.metodoraizana.com.mx", "page_path": "/alimenta-tu-tiroides-d"}
       ]'::jsonb,
       updated_at = now()
   where tenant_ref = 'lancemos'
     and funnel_ref = 'att1'
     and binding_version = 1
     and status = 'active'
     and additional_offer_codes = array['bmaztyhg', '2uafw5bg']::text[]
     and additional_offer_landings = '[]'::jsonb;
   commit;
   ```

   **Verificación:** dice `UPDATE 1`. `UPDATE 0` quiere decir que la fila no es la esperada (otras ofertas, otro orden, landings ya cargadas): no se sigue, se mira la fila. El check `commercial_ally_runtime_bindings_offer_landings_shape` rechaza una lista con otra forma. Correrlo dos veces no cambia nada.
4. Desplegar el bridge.
5. **Verificación:** `/ready` da 200 con `commercial_ally_binding: "active"`. Un 503 `commercial_ally_binding_unavailable` es drift: la fila y el manifiesto no coinciden en algún campo.

**Por qué se completa la fila y no se publica un `binding_version` nuevo.** Los eventos ya admitidos guardan su `binding_version` en la procedencia (`commercial_ally_hotmart_event_bindings`), y las RPC de planificación exigen que esa versión siga `active`; la política de compra (`commercial_ally_hotmart_purchase_policies`) también va por versión. Retirar la versión 1 dejaría sin planificar lo que ya entró. Completar las landings solo agrega: ninguna intención ni evento se validó contra ellas (antes el formulario de esas landings se rechazaba). Lo que no se toca una vez publicado son los valores de negocio (producto, precio, ofertas, consentimiento).

## 10. Chatwoot: AgentBot y webhook

> Pendiente. Un AgentBot conectado solo al inbox de la aliada, y el webhook de su cuenta apuntando al bridge de la instancia.

## 11. Hotmart

> Pendiente. Webhook del producto apuntando al bridge de la instancia.

## 12. Prender los flujos de a uno

> Pendiente. Orden previsto: `inbound` → `pago_fallido` → `carrito` → el resto. Cada uno: `true` en el manifiesto, PR en el repo de la instancia, flag en el servicio, una prueba controlada y la medición del efecto antes de pasar al siguiente.

### El formulario de la landing cuando es de GHL

> Pendiente de ejecutar con ATT1. Es la secuencia prevista para una landing cuyo formulario es de GHL: el envío entra como `intencion` por el adaptador (`docs/contracts/ghl-precheckout-adapter-v1.md`), sin tocar la landing. Nadie recibe un mensaje por esto: el adaptador solo admite la intención.

1. **Antes:** la base tiene la migración `20260930000200` y la fila del binding con `additional_offer_landings` (paso 9). Sin eso, solo entra el formulario de la landing por defecto y el resto da `503` (que GHL reintenta).
2. **Manifiesto** (PR en el repo de la instancia): `"intencion"` en `eventos` y `[adaptadores.ghl]` con el id de cada formulario (el `attributionSource.mediumId` del webhook, 20 letras o números). Un formulario se lista solo después de verificar que muestra la aclaración de `consentimiento.copy_version`: listarlo afirma `whatsapp_contact = true` para cada envío. `validate` tiene que dar verde.
3. **Secretos:** `GHL_PRECHECKOUT_ADAPTER_TOKEN`, aleatorio, de 32 caracteres o más y distinto de todo otro secreto del bridge (el arranque lo compara contra toda la configuración y nombra el que repite), generado como el resto de los secretos de la instancia (paso 7). No rota en cada despliegue: GHL lo tiene copiado.
4. **Bridge:** `GHL_PRECHECKOUT_ADAPTER_ENABLED=true` y redespliegue. El proxy tiene que enrutar `/webhooks/adapters/ghl/lead-precheckout` al bridge.
   **Verificación:** `/ready` da 200 con `ghl_precheckout_adapter: "enabled:<n>-forms"`, con `n` igual a los formularios del manifiesto. Si el bridge no arranca, el error nombra lo que falta (`runtime flags exceed the instance manifest flows: ghl_precheckout_adapter_enabled->intencion` o `->adaptadores.ghl`, o el token), o dice que `flujos.precheckout` está prendido: con el adaptador, el primer contacto del formulario espera la verificación fuera de banda del envío (sección *Riesgos* del contrato).
5. **GHL:** en el workflow con disparador *Form submitted* filtrado por esos formularios, una acción *Webhook* `POST` a `https://<dominio del bridge>/webhooks/adapters/ghl/lead-precheckout`, con el token en *Custom Data* como `setter_token` (o en el header `X-Setter-Adapter-Token`, si la acción admite headers). Nunca en la URL.
6. **Prueba controlada:** un envío real del formulario desde la URL de la landing.
   **Verificación:** la ejecución del workflow en GHL registra `200`; en la base hay una fila nueva en `precheckout_submissions` y la intención en `purchase_intents` con la `landing_ref` y la `offer_ref` de esa landing y `whatsapp_contact_authorized = true`. Un `422` dice por qué en el log del bridge (`ghl_form_not_allowed`, `ghl_landing_unknown`, `ghl_phone_unusable`) y no toca la base.

**Condición antes de prender** `[flujos].precheckout`, o una audiencia `consented_intent` alimentada por estas intenciones: el token es la única barrera (el id del formulario y las URL son públicos), así que hace falta una verificación fuera de banda de cada envío o la aceptación del riesgo por escrito del responsable de la instancia (contrato, sección Riesgos).
