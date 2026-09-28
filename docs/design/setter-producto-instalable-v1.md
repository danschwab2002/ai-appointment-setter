# Diseño: el setter como producto instalable (producto e instancia) v1

- **Estado:** propuesta para revisión de Dan. No hay nada implementado de lo que describe.
- **Fecha:** 2026-09-27
- **Mide contra:** `origin/main` en `246de1b` (2026-09-27)
- **Parte de:** [ADR-0005](../decisions/0005-reproducible-client-deployments.md) (single-tenant, monorepo, imágenes fijadas, cuatro capas), [runtime portable single-tenant v1](portable-single-tenant-runtime-v1.md), [contrato del runtime por aliada](../contracts/commercial-ally-runtime-v1.md), [ADR-0017](../decisions/0017-central-slack-operations-connector.md).
- **Absorbe:** el diseño del SOUL genérico con conocimiento por infoproductor (26/09, vault de Dan: `productos/soporte-infoproductores/diseno/soul-generico-y-conocimiento-por-infoproductor.md`), que queda como la pieza del agente de este documento.

## 1. Para qué existe este documento

El setter corre para una sola aliada (Johanna) y el segundo caso (ATT1, de la Dra. Nina Garza) está por empezar. Hacerlo como una adaptación del código de Johanna funciona una vez, y deja un sistema que solo sabe instalar quien lo construyó.

El objetivo es el contrario: **que una persona que no participó del desarrollo pueda instalar el setter para un negocio nuevo leyendo solamente la guía de instalación, sin tocar código y sin preguntarle nada a nadie.** Esa es la prueba de aceptación de todo lo que sigue, y ATT1 es la primera vez que se corre.

La regla que ordena el diseño es una sola:

> **El código no sabe quién es el cliente.** Si sumar un cliente obliga a tocar el código, es un defecto del producto y se arregla en el producto.

## 2. Dónde estamos

El ADR-0005 (31/07) ya fijó la dirección: una instalación aislada por cliente, un monorepo, imágenes con versión fijada y cuatro capas separadas (producto, configuración del cliente, secretos, estado). El diseño portable (01/09) construyó la primera pieza: un manifiesto por aliada (`CommercialAllyConfig`) con una autoridad durable en la base. Lo que no se construyó es el tramo que va del manifiesto a una instalación que funcione. Medido el 27/09:

| Lo que el ADR-0005 pide | Lo que hay |
|---|---|
| La configuración del cliente fuera del código | La de Johanna está escrita en `src/bridge/commercial_ally.py:127-148` (`JOHANNA_COMMERCIAL_ALLY`). Sin manifiesto, el bridge asume Johanna (`app.py:466-475`). |
| El producto no contiene datos de un cliente | Los tres caminos que mandan mensajes (carrito abandonado de Hotmart, primer contacto después del formulario, pago fallido) exigen la cuenta 1 y el inbox 9 (`app.py:2255`, `2326-2333`, `2365-2367`), usan plantillas con nombre de Johanna (`app.py:135-140`, `2277`; `worker.py:1530`), el idioma `es_EC` y textos como «Te habla el equipo de Johanna… formulario de Libre de Ansiedad» (`app.py:5379`, `worker.py:1588`). |
| Los datos del cliente viajan en su configuración | Los 6 pares landing → oferta de Johanna están en el código (`lead_precheckout.py:49-58`) y en migraciones (`20260831000300`, `20260914000100`, `20260922000100`). La del catálogo falla si no hay exactamente 6. |
| El aprovisionamiento es versionado e idempotente (§7) | El scope inbound `libre-de-ansiedad-inbound` v2 y la política de derivación (`human_handoff_projection_policies.expected_team_id`) se cargaron a mano en producción; no están en ninguna migración ni script del repo. |
| La definición del agente como plantilla sanitizada (§3) | El SOUL de Johanna (`profiles/agente-comercial/SOUL.md`, 390 líneas) mezcla el comportamiento común con un tercio de datos de Johanna. El de ATT1 (`profiles/att1/`) es un candidato inerte del 06/09 al que le faltan los 7 cambios posteriores. |
| Imágenes inmutables con versión fijada (§5) | EasyPanel construye el bridge desde el repo y publica `easypanel/infra/appointment-bridge:latest`. El repo tiene cero tags. `docker service rollback` no devuelve el código anterior (`docs/operations/appointment-bridge-release-runbook-v1.md`). |
| Servicios compartidos que no conocen clientes | La revisión diaria escribe «Johanna» en su HTML (`daily_feedback_service.py:1204`) y el conector de Slack acepta una lista cerrada de tenants `{johanna, att1}` (`slack_correlation/app.py:47-48`). |
| Una aliada nueva se instala con su configuración | El camino portable admite eventos de ATT1 y no manda nada (`portable-single-tenant-runtime-v1.md`, «Límites deliberados»). Su manifiesto admite un solo par landing/oferta. |

Nada de esto fue un error: cada pieza se construyó para que Johanna funcionara, que era lo correcto con un solo caso. Con el segundo caso llega el momento de pagar la separación, que es el criterio que ya se venía usando («se abstrae cuando aparece el segundo caso»).

## 3. El modelo: producto e instancia

| Capa | Qué contiene | Dónde vive | Quién la cambia | Cómo llega al servidor |
|---|---|---|---|---|
| **Producto** | Código, SOUL común, esquema de base, instalador, tests, guía de instalación | Este repo | Quien desarrolla el setter | Una release con tag `vX.Y.Z` e imágenes publicadas |
| **Instancia** | Manifiesto, conocimiento del agente, versión del producto que usa, despliegue | Un repo privado por instancia | Quien opera ese negocio | El instalador la lee y aprovisiona |
| **Secretos** | Tokens, claves, hottok, secreto del formulario | EasyPanel o gestor equivalente, nunca git | Quien opera | Variables del servicio |
| **Estado** | Conversaciones, casos, emisiones, memoria, logs | Base, Chatwoot y volúmenes de esa instancia | El sistema | Nunca se copia entre instancias |

**La unidad de instancia es una aliada**, o sea un número de WhatsApp con su voz y sus productos. Es lo que ya dicen el ADR-0017 («un bridge por aliada») y el diseño portable. La empresa que opera (hoy Lancemos) puede compartir entre sus instancias la infraestructura que ya es suya: el Chatwoot (una cuenta, un inbox por aliada), el workspace de Slack (un canal por aliada) y el VPS. Cada instancia tiene propios el bridge, el profile de Hermes, la base y los secretos. Esto afina el ADR-0005, que dibuja un Chatwoot por cliente: la frontera de aislamiento queda entre empresas, y dentro de una empresa, entre aliadas.

## 4. El producto

### 4.1 Releases e imágenes

- Cada release es un tag semántico (`v1.0.0`) en `main` con su entrada en `CHANGELOG.md`, escrita para quien actualiza una instancia: qué cambia y qué tiene que hacer (nada, correr una migración, sumar un campo al manifiesto).
- Un workflow de GitHub Actions, disparado por el tag, construye y publica las imágenes en GitHub Container Registry: `ghcr.io/danschwab2002/setter-bridge`, `setter-slack-connector` y `setter-daily-feedback`, con el tag de la versión. El digest queda en la release.
- EasyPanel deja de construir desde el repo y **usa la imagen por tag**. Actualizar una instancia es cambiar el tag; volver atrás es poner el anterior. El problema del runbook del bridge (el rollback que no devuelve el código) desaparece por construcción.
- **Mayor** cuando una instancia tiene que cambiar algo para actualizar (un campo nuevo obligatorio en el manifiesto, una migración con paso manual). **Menor** para funcionalidad nueva apagada por defecto. **Parche** para arreglos.
- Hermes sigue fijado a su imagen oficial por versión (`nousresearch/hermes-agent:v2026.8.31`); la release del setter declara con qué versión de Hermes se probó.

### 4.2 La base

- **Ninguna migración nueva inserta datos de un cliente.** Crean estructura y funciones. Los datos de un cliente entran por el aprovisionamiento (4.5), leídos de su manifiesto.
- Las migraciones viejas con datos de Johanna no se editan: son historia aplicada. Pero una instalación nueva no puede correrlas, porque dejaría las ofertas de Johanna en la base de ATT1. Hay dos caminos (decisión 3, §8): **(a)** una línea de base de solo estructura para instalaciones nuevas, generada del esquema vigente y verificada contra la cadena completa, o **(b)** envolver cada migración con datos en una guarda que la saltee si la instancia no es Johanna.

### 4.3 El código

- Los tres caminos con envío (carrito, pago fallido, primer contacto) toman del manifiesto la cuenta, el inbox, las plantillas con su idioma, el texto y el nombre de la marca. Los chequeos de cuenta 1 e inbox 9 pasan a ser «la cuenta y el inbox del manifiesto».
- El catálogo de ofertas admite varias ofertas por aliada con una por defecto, como ya hace Johanna, pero cargado desde el manifiesto.
- La guarda de términos sensibles (`app.py:178-183`, hoy de ansiedad: `antidepresiv`, `ansiolitic`) pasa al manifiesto, así ATT1 declara los suyos (levotiroxina, suplementos).
- La revisión diaria y el conector de Slack leen la marca y los tenants de la configuración.
- **Los nombres con «johanna» en funciones, tablas y RPCs pueden quedar** (decisión 4): renombrarlos toca los inventarios de ACL, los tests de cola y las migraciones, y no cambia nada de lo que hace el sistema. El código nuevo usa nombres genéricos. La prueba de que la separación terminó: ningún **valor** que cambie el comportamiento menciona a Johanna en `src/`.

### 4.4 El agente

Se implementa el diseño del 26/09 sin cambios:

- **Un SOUL común** en el producto: contrato de entrada y salida, link de pago, política de resolución y derivación, transparencia y barandas.
- **Un archivo de conocimiento por instancia** (`knowledge-v<N>.yaml`): identidad y voz, oferta, contenido del programa, lo no confirmado, promesas prohibidas, límites sensibles de la vertical, preguntas frecuentes aprobadas y una cabecera con versión y estado.
- **El bridge lo valida al arrancar y lo manda como mensaje `system` en cada pedido.** Hermes lo apila sobre el SOUL (verificado en `api_server.py` de la imagen fijada). `/ready` muestra la versión y el hash del conocimiento.
- El primer `knowledge-v1.yaml` de Johanna es el texto de hoy copiado tal cual, con un test dorado que compara el bloque renderizado contra las secciones del SOUL actual, byte a byte.

Los hechos que falten no bloquean una instalación: el agente deriva lo que el conocimiento no confirma, y la revisión diaria es el lugar donde aparecen y se suman.

### 4.5 El instalador

Una CLI en el producto (`uv run setter <comando>`), idempotente y con modo de prueba por defecto, como pide el ADR-0005 §7:

| Comando | Qué hace | Toca algo externo |
|---|---|---|
| `setter validate <instancia>` | Valida el manifiesto y el conocimiento contra su esquema, sin red | No |
| `setter provision <instancia>` | Carga en la base de la instancia el binding (`draft` → `active`), el catálogo de ofertas, los scopes, la política de derivación y el lookback de compras. Consulta antes de crear; no duplica | Solo su base, y con `--aplicar` |
| `setter doctor <instancia>` | Verifica lo externo: el inbox y el AgentBot existen en Chatwoot, las plantillas del manifiesto están aprobadas en Meta con ese nombre e idioma, el webhook de Hotmart apunta al bridge, el canal de Slack existe, `/ready` da 200. Lista lo que falta en castellano | Solo lectura |
| `setter status <instancia>` | Lo que hoy hace `recuperador_estado.py` desde el OS: links, ventas correlacionadas, conversaciones esperando, salud | Solo lectura |

Cada corrida deja evidencia sanitizada en la carpeta `evidencia/` de la instancia.

## 5. La instancia

### 5.1 El repo de la instancia

Cada instancia es un repo privado creado desde un repo plantilla (`setter-instancia-plantilla`):

```text
setter-instancia-att1/
├── README.md              qué es esta instancia, quién la opera, qué versión usa
├── instancia.yaml         el manifiesto
├── conocimiento/
│   └── knowledge-v1.yaml  lo que sabe el agente de este negocio
├── despliegue/
│   └── compose.yaml       los servicios, con la imagen fijada a la versión del producto
├── .env.example           cada secreto que hace falta, qué es y de dónde se saca
└── evidencia/             la salida de validate, provision y doctor
```

### 5.2 El manifiesto

Evoluciona el `CommercialAllyConfig` actual (20 claves, un solo par landing/oferta) a una versión 2. Ejemplo con los datos de ATT1 medidos el 27/09 en la landing publicada (`draninagarza`, `src/pages/att1/evg/vsl/`), el reporte de ventas (`alimenta-tu-tiroides-att1.yaml`) y el inventario de productos; los marcados `# a confirmar` no se pudieron medir:

```yaml
schema: setter-instancia/v2
producto: v1.0.0                     # versión del setter que usa esta instancia

instancia:
  tenant_ref: lancemos               # la empresa que opera
  ally_ref: att1                     # la aliada
  marca: "Dra. Nina Garza"           # a confirmar: cómo firma el equipo
  zona_horaria: America/Mexico_City  # a confirmar
  idioma_plantillas: es_MX

hotmart:
  product_id: 5071808                # Alimenta tu Tiroides
  hotlink: D98014973Y
  moneda: USD
  precio: 47
  ofertas:
    - codigo: gopi6lh7               # pauta
      landing: https://www.metodoraizana.com/att1/evg/vsl/ads-a
      por_defecto: true
    - codigo: bmaztyhg               # orgánico
      landing: https://www.metodoraizana.com/att1/evg/vsl/org-a
    - codigo: 2uafw5bg               # pauta, landing en site.metodoraizana.com.mx (a confirmar)
  # 83utgyow queda afuera a propósito: es la oferta de la recuperación de GHL que
  # este setter reemplaza. El lead vuelve a la oferta que vio.

chatwoot:
  account_id: 1                      # a confirmar
  inbox_id: null                     # a confirmar: número y WABA de ATT1
  equipo_derivacion: null            # a confirmar: el team de Mariana

plantillas:                          # nombre e idioma, verificados por `setter doctor`
  precheckout: att1_interes_precheckout_01
  carrito: att1_carrito_abandonado_01
  pago_fallido: att1_compra_fallida_01
  reactivacion: null                 # no existe todavía: hay que crearla en Meta
  descuento: att1_descuento_10_post_respuesta_01   # registrada en revisión y en inglés el 12/09

flujos:                              # todos apagados hasta el E2E de cada uno
  inbound: false
  carrito: false
  pago_fallido: false
  precheckout: false
  reactivacion: false

guardas:
  terminos_sensibles: [levotiroxina, eutirox, suplemento]   # a confirmar

slack:
  canal: null                        # a crear

revision_diaria:
  revisores: []                      # a confirmar
```

Johanna se escribe con el mismo esquema y los valores que hoy están en el código: `ally_ref: johanna`, `product_id: 8104005`, `hotlink: F106691755G`, USD 49, `bxjge6zq` por defecto más los 6 pares de `lead_precheckout.py:49-58`, cuenta 1 e inbox 9, plantillas `johanna_*` en `es_EC`, zona `America/Bogota`, canal `C0C0YEACVT2`.

Ningún campo del manifiesto es secreto. Si un dato es sensible, va al `.env` y el manifiesto lo nombra.

## 6. La documentación

Para instalar alcanzan tres documentos, escritos para alguien que no estuvo:

1. **Guía de instalación** (`docs/instalar.md`): los requisitos (VPS con EasyPanel, Chatwoot, número de WhatsApp en WABA con plantillas aprobadas, acceso a Hotmart, proyecto de Supabase, workspace de Slack) y los pasos en orden: crear el repo de la instancia desde la plantilla, completar manifiesto y conocimiento, `validate`, cargar secretos, levantar los servicios con la imagen de la versión, `provision`, `doctor` en verde, conectar la landing y el webhook de Hotmart, prender los flujos de a uno con su prueba. **Cada paso dice cómo se verifica que salió bien.**
2. **Referencia del manifiesto** (`docs/referencia-manifiesto.md`): cada campo, qué hace, qué pasa si está mal, un ejemplo.
3. **Contrato de la landing** (`docs/contracts/lead-precheckout-v1.md`, ya existe): qué evento manda el formulario, cómo se firma, qué campos lleva.

Los ADR, el `current-state`, las bitácoras y `docs/operations/` siguen siendo del desarrollo del producto. Quien instala no necesita leerlos, y es la señal de que la guía está completa.

## 7. Plan por fases

Cada fase termina en un efecto medido (política del 20/09) y se reporta con la escala abierto → mergeado → desplegado → activado → E2E.

| Fase | Qué | Cambia el comportamiento de Johanna | Termina cuando |
|---|---|---|---|
| **F1** Releases | Tags, `CHANGELOG.md`, workflow que publica imágenes en GHCR. Johanna pasa a correr desde `v1.0.0` fijada | No | Johanna corre desde la imagen de GHCR y un rollback de prueba vuelve al tag anterior |
| **F2** Johanna es la instancia 1 | Su manifiesto sale del código a `setter-instancia-johanna`. El scope y la política cargados a mano pasan a `provision`. SOUL común + `knowledge-v1` con el test dorado | No: es la fase delicada | Una semana con los mismos números en `status` (links, correlaciones, derivaciones) y las respuestas del agente comparadas contra conversaciones reales |
| **F3** Separar los caminos | Los tres caminos con envío, varias ofertas, guardas, marca de la revisión diaria y tenants de Slack desde la configuración | No | Una matriz de tests Johanna/ATT1 sin cruces, con fixtures capturados |
| **F4** Instalador y base | `validate`, `provision`, `doctor`, `status` y la base nueva sin datos de clientes | No | Una instalación desde cero en un entorno de prueba pasa `doctor` en verde |
| **F5** La prueba del extraño | Se instala ATT1 siguiendo solo `docs/instalar.md`. Quien la corre no tiene el historial: una sesión nueva sin el vault ni este repo más allá de la guía. Cada fricción se arregla en el producto, no en ATT1 | No | ATT1 instalada con `doctor` en verde, con los flujos apagados |
| **F6** Activar ATT1 | La landing manda `lead.precheckout` (en el core de Lancemos, apagado por defecto), el webhook de Hotmart apunta al bridge de ATT1, se prenden los flujos de a uno y se apaga la recuperación de GHL | Johanna no | Primera venta de ATT1 correlacionada al setter |

**Calendario con Lancemos:** F1 a F5 no tocan nada de lo que Lancemos mide. F6 cambia el formulario de la landing de ATT1, que ese mismo equipo muda el 29/09 con lectura el 06/10; F6 va después de esa lectura, para no medir dos cambios juntos.

## 8. Decisiones que pide este documento

Cada una lleva la recomendación primero.

1. **Dónde vive una instancia.** Recomendado: un repo privado por instancia, creado desde un repo plantilla. Se le puede entregar a quien opera ese negocio sin mostrarle las demás. Alternativa: un solo repo privado con una carpeta por instancia, más simple mientras sean dos.
2. **Registro de imágenes.** Recomendado: GitHub Container Registry, que ya está en la cuenta y no tiene costo para imágenes privadas en este volumen. EasyPanel las baja con un token de lectura.
3. **La base de una instalación nueva.** Recomendado: línea de base de solo estructura (4.2 a). Deja las migraciones viejas intactas y una instalación nueva limpia por construcción. La alternativa (guardas) ensucia migraciones ya aplicadas.
4. **Los nombres con «johanna».** Recomendado: quedan; el código nuevo usa nombres genéricos. Renombrar cuesta y no cambia comportamiento.
5. **El SOUL genérico.** Recomendado: se implementa en F2 con el diseño del 26/09 tal como está.
6. **La unidad de instancia.** Recomendado: la aliada, compartiendo Chatwoot, Slack y VPS dentro de la misma empresa (§3). Alternativa: una instancia por empresa con varias aliadas adentro, que ahorra servicios pero obliga a rutear por inbox en todo el código y agranda el radio de un error.

## 9. Lo que falta saber de ATT1

- El número de WhatsApp de ATT1, su WABA y si ya tiene inbox en Chatwoot.
- El estado real en Meta de las cuatro plantillas `att1_*`; la de reactivación no existe.
- La landing de la oferta `2uafw5bg` y si sigue viva.
- La marca con la que firma el equipo, la zona horaria y los términos sensibles.
- Quién revisa las conversaciones de ATT1 en la revisión diaria.

Ninguno bloquea F1 a F4.
