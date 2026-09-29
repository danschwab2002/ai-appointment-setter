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

## 10. Chatwoot: AgentBot y webhook

> Pendiente. Un AgentBot conectado solo al inbox de la aliada, y el webhook de su cuenta apuntando al bridge de la instancia.

## 11. Hotmart

> Pendiente. Webhook del producto apuntando al bridge de la instancia.

## 12. Prender los flujos de a uno

> Pendiente. Orden previsto: `inbound` → `pago_fallido` → `carrito` → el resto. Cada uno: `true` en el manifiesto, PR en el repo de la instancia, flag en el servicio, una prueba controlada y la medición del efecto antes de pasar al siguiente.
