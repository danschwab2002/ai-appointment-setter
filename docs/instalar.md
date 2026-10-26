# Instalar el setter para un negocio nuevo

Para quien instala el setter en una aliada nueva sin haber participado de su desarrollo. **Cada paso dice cómo se verifica que salió bien:** si la verificación no da lo que dice, no se sigue al siguiente.

La instalación tiene dos tiempos, y esta guía los separa:
- **Instalar** (los pasos 1 a 15, el **día 0**): la instancia queda corriendo entera en «solo escucha». Recibe los eventos reales de Hotmart, tiene el agente y el Automation Expert instalados, las alertas y el respaldo andando, y **no le manda un mensaje a nadie**. Termina con `despliegue/chequear.sh` del repo de la instancia en `INSTALADA`.
- **Prender** (los pasos 16 y 17, mucho después): el número de WhatsApp, Meta, las plantillas, el conocimiento aprobado, las firmas y cada flujo, de a uno y con su prueba.

Se instala temprano para aprender con datos reales; se prende recién con lo que hace falta para hablarle a personas.

Lo que se usa del repo de la instancia (creado desde la plantilla `danschwab2002/setter-instancia-plantilla`):
- **`despliegue/dia-0.md`, la lista del día 0:** lo que tiene que existir antes (accesos, cuentas, las apps de Slack, R2, healthchecks.io, los hosts) y lo que se hace ese día, en orden, con cuánto tarda cada paso. Esta guía explica cada paso; la lista es lo que se tiene abierto el día 0.
- **`despliegue/chequear.sh`, el chequeo:** solo lee, y dice `INSTALADA`, qué falta o qué está prendido de más (paso 15).
- **`despliegue/camino-a-produccion.md`:** de la instancia instalada a la primera recuperación (los pasos 16 y 17 de esta guía, con los comandos de la instancia).

> **Estado de esta guía (2026-10-23).** La reescribió E-034 del programa etapa-1 para la plantilla de ese día, en el orden en que se instala. Lo que tiene evidencia:
>
> - **La primera instancia, ATT1, se instaló a mano** mientras se escribía la primera versión de esta guía, sin plantilla (primer commit `4590f27`, 2026-09-27). Sus fechas y PR están en cada paso, como «en ATT1». Su repo no sigue la regla de nombres (servicios `att1-*`, stack `setter-att1`) y no tiene `despliegue/despliegue.toml`.
> - **La plantilla se probó entera sin VPS el 2026-10-23,** contra el árbol del tag `v1.16.4` del producto, con `pruebas/probar_plantilla.sh` de la plantilla (PGlite, docker falso y la imagen del bridge compilada en local): la versión cruda (setter-instancia-plantilla#4), el Automation Expert (#5), la subida genérica (#6), el respaldo (#7), la revisión diaria (#8) y las alertas (#9). La salida de cada una está en `evidencia/2026-10-23-*` de la plantilla. El chequeo y la lista del día 0 los suma setter-instancia-plantilla#10, con su prueba contra un árbol falso.
> - **Lo que todavía no se recorrió con la plantilla en ningún VPS:** el stack en un Swarm real, la base desde cero en el Postgres de Supabase, Hermes con los dos profiles, las alertas, el respaldo, la ruta pública y `chequear.sh` contra un servidor de verdad. Es la prueba de aceptación de esta guía (`docs/design/setter-producto-instalable-v1.md` §8.1): la próxima instalación, siguiéndola sin preguntar.
> - **El conector de Slack propio de la instancia** está en el producto desde la `1.17.0` (#315, `CHANGELOG.md`), y entra a la plantilla en su propio PR, con el tag. El paso 14 dice lo que se sabe hoy y lo marca.
>
> Lo que en un paso dice «en ATT1» es evidencia de esa instancia, no de la que estás instalando.

## Cómo leer esta guía

- `<aliada>` es el nombre corto de la instancia, con la regla de [`referencia-nombres.md`](referencia-nombres.md): minúsculas, letras, números y guiones, y empieza con una letra. Es el mismo en el repo, en el servidor, en Slack y en el manifiesto.
- `<stack>` es el stack de Swarm de la instancia, y `/opt/<stack>/` su carpeta en el servidor. En una instancia nueva, los dos llevan el nombre corto de la aliada (`<aliada>` y `/opt/<aliada>/`): salen de `[servidor]` de `despliegue/despliegue.toml`. Los servicios adentro del stack son `db`, `rest`, `gateway`, `appointment-bridge` y `hermes` (Swarm los muestra `<stack>_<servicio>`). En ATT1, anterior a la regla, son `setter-att1` y `/opt/setter-att1/`, con servicios `att1-*`.
- `<servidor>` es el VPS por ssh. `<dominio del bridge>` es la ruta pública del bridge, `[hosts].bridge` de `despliegue.toml`.
- **El día 0** es el día de la instalación en solo escucha. La llamada de arranque va antes.
- **Nombres del producto que llevan el de una empresa.** `LANCEMOS_PILOT_*`, `AGENT_PROVENANCE_TENANT_REF`, el `tenant_ref` del manifiesto y las funciones `*_lancemos_pilot_*` de la base son nombres del código del producto, no datos de un cliente. Se usan tal cual en cualquier instancia.
- Un valor de ATT1 aparece solo como ejemplo, y lo dice al lado. Los flujos concretos de ATT1 (el carrito de tres toques, las variantes del pago rechazado, la ficha de pago) son configuración de esa aliada, no pasos de la instalación: el producto se instala crudo (decisión del 2026-10-23).

## Qué necesitás antes de empezar

- Lo de «Antes del día 0» de `despliegue/dia-0.md` del repo de la instancia: los accesos (el OK para crear la instancia en el VPS, ssh como root, GitHub, el tag del producto), las cuentas del negocio, las apps de Slack, R2, healthchecks.io y los hosts.
- Un VPS con Docker Swarm y EasyPanel, donde ya corra Chatwoot o donde se pueda levantar.
- Los recursos de terceros del paso 0. Los que hacen falta para prender (el número, Meta, las plantillas) no frenan el día 0, pero se piden igual desde el primer día: son los que tardan.

## 0. La llamada de arranque y los pedidos a terceros

**Por qué va primero:** lo que depende de terceros tarda, y lo nuestro no. Para el día 0 hace falta poco de afuera (Hotmart, Chatwoot, Slack, R2, OpenRouter). Para prender, el camino crítico es el número de WhatsApp, el portfolio de Meta con un admin y el medio de pago, la cuenta de mensajería con el número verificado, las plantillas (Meta las revisa en hasta 24 h) y el primer envío. El segundo camino largo es la información del producto, porque depende de que el infoproductor conteste.

El detalle de cada recurso (las preguntas, las ramas según la respuesta, quién lo hace y qué se anota) lo lleva el runbook del operador, que vive fuera de este repo. Acá va el resumen: el orden, la primera opción de cada recurso, qué hacer si falla y cuándo está hecho. Lo que se anota de cada recurso va al repo privado de la instancia, nunca un secreto: de un PIN o un token se anota dónde está guardado, no el valor.

### El orden

- **La llamada de arranque (≈45 min).** La conduce quien instala. Se hacen todas las preguntas que eligen una rama: el número, el portfolio, la moneda, el nombre visible, la cuenta de Hotmart y el `off=` de cada landing, las landings y sus formularios, el orgánico, Slack, quién atiende las derivaciones y en qué horario, si hay un lanzamiento cerca, los nombres de cada rol, todo lo que ya le escribe a sus leads, **en qué país vende y cuántos carritos abandonados tiene por día**. Se piden también los datos mínimos para redactar las plantillas: el nombre del producto, el país y el idioma, el cupón y si vence, el link del checkout y cómo se presenta el asistente.
- **El día siguiente a la llamada, todo lo que depende de terceros, junto:** los accesos de Hotmart, de GHL y de Slack; las apps de Slack de la instancia (`dia-0.md`); el bucket de R2 y los checks de healthchecks.io; la key de OpenRouter. Y, para prender más adelante: el número; el portfolio, el admin, la moneda y el medio de pago; la app de Meta y la cuenta de mensajería; las plantillas a revisión; la verificación del negocio; el formulario del conocimiento al infoproductor y a quien contesta hoy los chats; y el alta de las personas en Chatwoot y en Slack.
- **Lo nuestro, sin depender de nadie:** los pasos 1 a 5 (el repo desde la plantilla, completo y validado).
- **El día 0:** los pasos 6 a 15, en el orden de `despliegue/dia-0.md`. Unas 4 horas de trabajo (estimado).
- **Para prender, cuando llegan el número, Meta y las firmas:** los pasos 16 y 17. Justo antes de prender el carrito, el corte de lo que ya le escribe a los leads, mirado en su panel (cortarlo antes deja días sin recuperar carritos).

### Los recursos

**El número de WhatsApp.** No hace falta para el día 0. Para prender, es lo primero que se define.
- *Primera opción:* un número nuevo y dedicado, con un chip prepago del país que compra el infoproductor. Se verifica por SMS.
- *Si falla:* un número virtual que reciba llamadas, contratado a nombre del infoproductor y pagado por él, verificado por voz. Que un virtual pase la verificación no está probado: se prueba antes de pagar meses. Un número que ya está en la app de WhatsApp se puede registrar en la Cloud API borrando esa cuenta (con respaldo, porque se pierde el historial). La coexistencia (seguir usando el número en la app del celular) no se hace con esta instalación: exige ser Tech Provider, y la carga manual de Chatwoot no la soporta. Un número que está en GHL o en otro proveedor sigue ahí hasta que se corte su flujo, y se instala con uno dedicado.
- *Regla dura:* nunca dos números escribiéndole a la misma persona.
- *Hecho cuando:* el número recibe el código de Meta y queda verificado en *API Setup*. El PIN de la verificación en dos pasos lo elige y lo guarda el infoproductor: hace falta para volver a registrar el número después de aprobado el nombre visible.

**El portfolio comercial de Meta.** Para prender. No depende de nada.
- *Primera opción:* todo en el portfolio del infoproductor, con quien instala como admin: la app, la cuenta de WhatsApp, la cuenta de mensajería, el usuario del sistema y el token. Una cuenta de mensajería no se mueve a otro negocio después.
- *Si falla:* sin portfolio, se crea a nombre del infoproductor, nunca al de quien instala. Sin empresa registrada no hay verificación clásica: el techo de 250 destinatarios nuevos por día sube igual con 2.000 mensajes de buena calidad en 30 días.
- *Hecho cuando:* quien instala entra como admin y ve «Cuentas de WhatsApp» y «Pagos» en la configuración del negocio. La verificación del negocio se pide apenas se pueda y no se espera.

**La cuenta de mensajería, el medio de pago y el registro del número.** Para prender. Dependen del número y del portfolio.
- *Primera opción:* crear la app de Meta (caso de uso WhatsApp), agregar y verificar el número, cargar el **medio de pago en la cuenta de mensajería** y registrar el número. La moneda se elige al crear la cuenta y no se cambia.
- *Si falla:* el medio de pago de la cuenta publicitaria no sirve. Sin medio de pago, toda plantilla falla con `131042`. Una cuenta con la línea de crédito de un proveedor no deja cargar un medio de pago propio: el proveedor la tiene que retirar.
- *Hecho cuando:* el número figura conectado y la cuenta de mensajería muestra un medio de pago activo. Se anotan el App ID, el Phone Number ID y el ID de la cuenta, que son distintos.

**El nombre visible.** No bloquea el envío: mientras no esté aprobado, la persona ve el número.
- *Primera opción:* un nombre que se relacione con el nombre legal del portfolio y que esté escrito igual en el sitio. Se mira el nombre legal antes de pedirlo.
- *Si falla:* un nombre de persona solo, o escrito distinto que en el sitio, se rechaza. Se pide otro con el rubro al lado o con «by <empresa>».
- *Hecho cuando:* está pedido.

**Las plantillas.** Para prender. Dependen de la cuenta de mensajería, porque son de la cuenta y no del número.
- *Primera opción:* el juego por defecto del producto (`plantillas/README.md` de la instancia: primer contacto, carrito, pago fallido, reactivación y seguimiento con cupón), completado con los datos de la llamada. El infoproductor aprueba el texto y quien instala las manda a revisión apenas existe la cuenta. El carrito y todo lo que lleve cupón o urgencia va como marketing. El código del cupón va en el botón, no en el texto.
- *Si falla:* una plantilla rechazada se corrige y se vuelve a mandar. El idioma no se cambia después de crearla: una en otro idioma es otra plantilla, y se borra y se recrea. Sin la plantilla del carrito, el carrito no se prende; el entrante sí.
- *Hecho cuando:* el catálogo del inbox en Chatwoot las muestra aprobadas, con nombre, idioma y categoría exactos, y queda guardado en `evidencia/`.

**El token de Meta y el inbox de Chatwoot.** Dependen del número verificado. Se hacen en el paso 16.

**Hotmart.** Para el día 0. Depende del acceso del infoproductor, con permiso de webhooks.
- *Primera opción:* el acceso a la cuenta dueña del producto. Se preguntan los webhooks que ya existen (se pueden tener varios), el `off=` de cada landing, **si está prendido el recuperador propio de Hotmart o su WhatsApp de carrito**, a qué número manda el botón de WhatsApp del checkout y si hay un cupón.
- *Si falla:* si la cuenta de Hotmart es de otra empresa, el acceso lo da esa empresa.
- *Hecho cuando:* el evento de prueba de Hotmart, o uno real, llega al bridge y responde 200 (paso 10).

**GHL, si el infoproductor lo usa.** Depende del acceso a la subcuenta.
- *Primera opción:* la lista de cada workflow que manda WhatsApp hoy, con su disparador, **mirada en el panel** (el carrito y el pago fallido, sobre todo), y el ID de cada formulario.
- *Si falla:* si la subcuenta no está pagada, la entrada de los formularios se puede cortar sin aviso.
- *Hecho cuando:* está la lista de lo que escribe hoy.

**Las landings, los formularios y el consentimiento.** 🔴 **De esto depende a quién le escribe el carrito:** el carrito, el pago fallido y el primer contacto le escriben solo a quien dejó antes un formulario con la aclaración de WhatsApp (paso 17, `carrito`). Una aliada sin formularios de GHL en las landings que reciben tráfico no tiene a quién recuperarle un carrito: se sabe en la llamada, antes de instalar.
- *Primera opción:* cada formulario de GHL que se va a listar muestra la aclaración de WhatsApp, y la política del sitio nombra a WhatsApp. El texto exacto va a un archivo propio de la instancia (`consentimiento/<aliada>-whatsapp-contact-v1.md`), con la etiqueta que declara `consentimiento.copy_version`.
- *Si falla:* un formulario propio o de ManyChat hoy no tiene entrada (exige firmar el envío, y esos formularios no firman). Esas personas quedan fuera del primer flujo: se anota cuántas ventas vienen por ahí. Si la política no nombra WhatsApp, la cambia el infoproductor antes de prender. Se pregunta por el orgánico en la llamada.
- *Hecho cuando:* cada formulario que se va a listar muestra la aclaración (con captura) y la política nombra WhatsApp.

**El conocimiento del agente.** Para prender el entrante; el día 0 alcanza el borrador. Depende de que el infoproductor conteste.
- *Primera opción:* la v1 sale de lo público (la landing, el checkout y la ayuda de Hotmart) y prioriza las situaciones más típicas. Lo que no tiene fuente va a `[no_confirmado]` y el agente lo deriva. La fuente real es quien contesta hoy los chats.
- *Si falla:* sin los materiales prometidos, la v1 sale igual con lo público, y lo demás llega por las rondas de feedback. Sin chats exportados, la voz sale del SOUL común del producto. Los chats exportados son datos personales de los leads: nunca van al repo ni a un chat.
- *Hecho cuando:* el conocimiento está en `estado = "aprobado"`, firmado por quien aprueba el conocimiento, y `validate` da `VALIDA` (pasos 4 y 5).

**Slack.** Para el día 0, si la instancia lo usa: las apps se crean desde manifiestos versionados (`dia-0.md`).
- *Las apps:* la del Automation Expert (paso 11), la de las alertas (paso 12) y, con la `1.17.0`, la del conector propio (paso 14). La de la revisión diaria va cuando se prenda la revisión.
- *Sin Slack:* la derivación llega al team de Chatwoot y quien atiende las derivaciones la mira ahí; las alertas críticas llegan por email (healthchecks.io); el Automation Expert no tiene por dónde hablar, y su sección se saca de `hermes.toml` (paso 11).
- *Hecho cuando:* cada app está instalada, con su token guardado fuera de todo chat, y su bot en su canal.

**Las personas.** Se nombran en la llamada. Cada rol tiene un nombre: el responsable comercial (confirma los datos y aprueba el conocimiento), quien contesta hoy los chats (normalmente también quien atiende las derivaciones), quien aprueba el texto de las plantillas, quien toma las alertas, quien administra el Automation Expert, quien custodia la clave de los respaldos, los revisores de la revisión diaria si se prende, y el admin del portfolio si no es el infoproductor. Si el infoproductor está solo, cubre todos los roles del negocio.
- *Hecho cuando:* cada rol tiene un nombre, y quien atiende las derivaciones queda **sola** en el team de Chatwoot (el team reparte la conversación entre sus miembros).

**Las firmas.** Son decisiones escritas que el producto exige, y cada una la escribe quien decide, nunca quien instala. Ninguna hace falta para el día 0:
1. la aprobación del conocimiento (`aprobado_por`, `aprobado_el`);
2. la aceptación del riesgo del adaptador de GHL, si hay formularios de GHL (paso 17);
3. la revisión diaria en un disco sin cifrar (ADR 0024), solo si la revisión diaria se prende;
4. el OK comercial para abrir cada flujo;
5. los topes: cuántos mensajes por día y la primera cohorte, cerrada antes de abrir a todos.
- *Hecho cuando:* `validate` y `/ready` muestran las que el producto lee (el conocimiento aprobado, `ghl_adapter_risk: accepted:…`).

**Las pruebas.**
- Dos identidades de prueba con teléfono y email distintos: una para la corrida de envío y otra para la de compra. La que compra queda frenada para siempre (`purchase_by_identity`). Los teléfonos de prueba los elige quien instala en el momento: no se precargan en ningún archivo.
- Una compra real temprano, apenas anda el webhook de Hotmart (el día 0, si se puede): el nombre exacto del producto y la forma del teléfono se confirman recién con un payload real.
- Carritos reales para la prueba del carrito. Si no entra ninguno, la prueba real se corre al primero que entre.
- *Hecho cuando:* la corrida de envío llegó al teléfono y la respuesta cayó en Chatwoot, y la de compra canceló la secuencia.

**Lo que ya le escribe a los leads.** Regla dura: **nunca dos números, ni dos sistemas, escribiéndole a la misma persona.**
- *Primera opción:* cada automatización que manda lo mismo que un flujo nuestro (el carrito, el pago fallido, el recuperador o el WhatsApp de Hotmart) se corta justo antes de prender el nuestro, mirado en su panel. En GHL se corta **borrando el disparador** del workflow: pasarlo a borrador congela también a los que ya están adentro.
- *Si falla:* si nadie tiene el acceso para cortarlo, ese flujo nuestro no se prende. Una persona que escribe a mano desde el número viejo sigue con sus conversaciones y no le escribe a quien entró por un flujo nuestro.
- *Hecho cuando:* antes de prender el carrito, cada sistema que manda lo mismo figura cortado en su panel. El día 0 no se corta nada: la instancia no manda.

**Lo que es del producto y no se le pregunta a nadie.** Se resuelve antes del día 0:
- la versión: la de `producto` en el `instancia.toml` de la plantilla, o una posterior (paso 6);
- el conector de Slack: el propio de la instancia desde la `1.17.0`, o el compartido mientras la plantilla no lo traiga (paso 14);
- el teléfono por país: `src/bridge/phones.py` tiene una regla propia para México y Argentina. Un número de otro país pasa tal cual, y eso no está probado con ningún país fuera de esos dos.

**Verificación del paso 0:** el día siguiente a la llamada, cada recurso de terceros está pedido y tiene un responsable con nombre, y el repo de la instancia tiene anotado de cada uno de dónde salió y su estado (nunca un valor secreto). «Antes del día 0» de `dia-0.md` está completo antes de arrancar el paso 6.

## 1. Crear el repo de la instancia

Un repo **privado** por aliada, creado desde el repo plantilla:

```sh
gh repo create <dueño>/setter-instancia-<aliada> --private --template danschwab2002/setter-instancia-plantilla
gh repo clone <dueño>/setter-instancia-<aliada>
cd setter-instancia-<aliada>
python3 renombrar.py <aliada> --en-seco
python3 renombrar.py <aliada>
python3 verificar.py --recien-creada
```

`renombrar.py` reemplaza el token `INSTANCIA` de la plantilla por el nombre corto de la aliada, en el contenido y en los nombres de archivo (en los nombres de las plantillas de Meta, con guiones bajos), y saca del README la sección de la plantilla. `verificar.py --recien-creada` revisa que no quede el token ni el nombre de otra instancia; los valores del negocio quedan como `<COMPLETAR: …>`, y `verificar.py` sin opciones, antes de desplegar, exige que no quede ninguno. El README de la instancia dice qué se completa y en qué orden. La estructura que queda:

```text
setter-instancia-<aliada>/
├── README.md
├── instancia.toml          (el manifiesto)
├── hermes.toml             (el Hermes propio: el agente comercial y el Automation Expert)
├── conocimiento/           (knowledge-v1.toml y knowledge-fuentes.md)
├── consentimiento/
├── plantillas/README.md    (el juego de plantillas de Meta por defecto)
├── despliegue/
│   ├── despliegue.toml     (la infraestructura: la raíz, el stack, los servicios, los hosts, Chatwoot, las alertas, el respaldo)
│   ├── dia-0.md            (la lista del día 0)
│   ├── chequear.sh         (el chequeo de la instalación) y chequeo.py
│   ├── camino-a-produccion.md
│   ├── stack.yaml, base/, secretos/, hermes/, hotmart/, chatwoot/, slack/, traefik/, gateway/, db/
│   ├── alertas/, respaldo/, revision-diaria/ (apagada), subida/
├── pruebas/                (la prueba de la plantilla entera, sin VPS)
├── .env.example
└── evidencia/
```

Ejemplo de una instancia instalada a mano: el manifiesto y el conocimiento de ATT1 en `tests/fixtures/instances/att1/` de este repo.

**Verificación:** `gh repo view <dueño>/setter-instancia-<aliada> --json visibility` dice `PRIVATE`, y `verificar.py --recien-creada` termina con 0 errores.

## 2. Relevar los datos del negocio

Todo se mide en la fuente, no se pregunta de memoria:

| Dato | Dónde se mide |
|---|---|
| Producto e hotlink de Hotmart | La landing publicada: el botón o el script de checkout apunta a `pay.hotmart.com/<hotlink>?off=<oferta>`. Se confirma que `https://pay.hotmart.com/<hotlink>?off=<oferta>` abre el checkout real |
| Una oferta por landing | Cada landing trae su `off=`. Se abre cada una en un navegador y se anota landing → oferta. Una oferta que ninguna landing usa **no** va al manifiesto |
| Cuenta, inbox y team de Chatwoot | En Chatwoot: *Settings → Inboxes* (el ID está en la URL del inbox) y *Settings → Teams* |
| Plantillas de Meta | En Chatwoot, las plantillas sincronizadas del inbox; o en el WhatsApp Manager. Se anota **nombre, idioma y estado exactos**: una plantilla aprobada en otro idioma es otra plantilla |
| Precio y moneda | El checkout de Hotmart |
| Los hosts, el contenedor de la base de Chatwoot, el canal de las alertas | EasyPanel (Settings de cada servicio), `docker ps --format '{{.Names}}'` en el servidor y Slack. Van a `despliegue/despliegue.toml` |

**Verificación:** cada valor anotado lleva de dónde salió y la fecha, en `conocimiento/knowledge-fuentes.md` del repo de la instancia.

## 3. Completar `instancia.toml` y `despliegue.toml`

`instancia.toml`, con la referencia de campos (`docs/referencia-manifiesto.md`). Reglas:

- **Todos los flujos en `false`.** Se prenden de a uno al final (paso 17). `chequear.sh` frena el día 0 con uno en `true`.
- Una plantilla que todavía no existe o que está en otro idioma **se omite**. El flujo que la necesita queda sin poder prenderse, y `validate` lo avisa. El día 0 van todas comentadas.
- En `eventos` va solo lo que ya llega de verdad. Si la landing todavía no manda el evento de intención, `intencion` no va.
- `producto` es la versión que se instala (paso 6), y tiene que ser la de la imagen del bridge (`verificar.py` lo compara con `stack.yaml`).
- Nada secreto: tokens y claves van en los pasos 7, 10 a 14.

`despliegue/despliegue.toml` (no va en `instancia.toml`: el lector del manifiesto rechaza las secciones que no conoce): `[servidor]` (la raíz y el stack), `[hosts]`, `[chatwoot]` (el contenedor y la base de Chatwoot, que leen el operador de flujos y el chequeo), `[operador]`, `[alertas]` (el canal y quien toma las alertas), `[slack]` si la instancia usa Slack, `[servicios]` y `[subida]` (los dejan bien los de la plantilla), y `[respaldo]` (`chatwoot = false` salvo en la instancia dueña del Chatwoot compartido). `[revision_diaria]` queda con sus marcadores: la revisión nace apagada.

**Verificación:** el paso 5, y `python3 verificar.py` con 0 errores.

## 4. Escribir el conocimiento del agente

`conocimiento/knowledge-v1.toml`, con el contrato de `docs/contracts/commercial-knowledge-v1.md`:

- Cada dato sale de una fuente publicada o aprobada, y la fuente va en un comentario al lado.
- Lo que no se encontró **no se rellena**: va a `[no_confirmado]`. El agente lo deriva sin anunciar que falta.
- La voz se toma de conversaciones reales del equipo de la aliada, no se inventa.
- Si el conocimiento se contradice con la página (una cuenta regresiva, por ejemplo), gana la política: se anota en `[promesas_prohibidas]`.
- La presentación del agente coincide con la de las plantillas aprobadas (lo que dice el primer mensaje).

El archivo nace en `estado = "borrador"`, y así alcanza para el día 0. Lo aprueba quien aprueba el conocimiento (el responsable comercial de la aliada): `estado = "aprobado"`, `aprobado_por`, `aprobado_el`. **Sin aprobación el agente no responde.** Antes de aprobar una versión, se prueba con el modelo del agente; en ATT1, con el arnés de `conocimiento/prueba/` del repo de la instancia.

**Verificación:** el paso 5 da `VALIDA`; con el conocimiento aprobado, lo dice.

## 5. Validar

Desde el árbol del producto en el tag que se instala:

```sh
uv run python -m bridge.instance_cli validate ../setter-instancia-<aliada>
```

**Verificación:** termina en `VALIDA`. Los avisos listan qué flujos no se pueden prender y por qué. Se guarda la salida en `evidencia/<fecha>-validate.txt`. Después, el repo completo se mergea en `main`: el servidor recibe `origin/main`.

Si el manifiesto tiene `[adaptadores.ghl]`, `validate` dice además si la sección lleva la aceptación del riesgo del adaptador (quién, cuándo, qué contrato). Con `precheckout` o `pago_fallido` en `true` y sin esa aceptación da **error**, no aviso: ese manifiesto no arranca (`docs/referencia-manifiesto.md`, *La aceptación del riesgo*).

## 6. El repo, el producto y la imagen en el servidor

Empieza el día 0. Todo lo que sigue corre en el servidor como root; las rutas salen de `despliegue/despliegue.toml`.

**La versión:** la que dice `producto` en el `instancia.toml` de la plantilla, o una posterior. `producto` tiene que ser la de la imagen del bridge.

1. **El repo de la instancia y el árbol del producto en el tag,** los dos desde la fuente recién traída (una copia vieja levanta un bridge con un manifiesto viejo):

   ```sh
   git fetch origin && git archive origin/main | ssh <servidor> 'mkdir -p /opt/<stack>/instancia && tar -C /opt/<stack>/instancia -xf -'
   cd ai-appointment-setter && git fetch origin --tags
   V=<versión>; SHA=$(git rev-list -n 1 $V)
   git archive $V | ssh <servidor> "install -d /opt/<stack>/build/$V && tar -C /opt/<stack>/build/$V -xf -"
   ```

   Ese árbol es el que usan la base (paso 8), Hermes (paso 11) y el chequeo (paso 15).

2. **La imagen del bridge.** Dos caminos:
   - **Compilada en el servidor desde el tag**, mientras el servidor no tenga el token de GHCR. Es como corre ATT1 (cabecera de `despliegue/stack.yaml`). Con los mismos argumentos que el release (`SETTER_VERSION` sin la `v` y `GIT_SHA` = el commit del tag). Si otra instancia del mismo servidor ya la compiló con ese tag, está en `docker image ls setter-bridge`:

     ```sh
     ssh <servidor> "cd /opt/<stack>/build/$V && docker build -t setter-bridge:$V --build-arg SETTER_VERSION=${V#v} --build-arg GIT_SHA=$SHA ."
     ```

   - **De GHCR:** `ghcr.io/danschwab2002/setter-bridge:<versión>`, con el token de solo lectura del registro. Cómo se pasa un servicio a la imagen y cómo se vuelve atrás: `docs/operations/release-por-imagen-v1.md`.

**Verificación:** `ls /opt/<stack>/instancia/instancia.toml /opt/<stack>/build/<versión>/src/bridge/app.py` en el servidor, y `docker image ls setter-bridge` muestra el tag.

## 7. Secretos

Los secretos viven en el servidor, en `/opt/<stack>/secretos/`, con permisos 600. **Nunca van al repo, al vault ni a un chat.** Del repo sale solo la lista de nombres (`.env.example` de la instancia) y el generador, `despliegue/secretos/generar.py`, que escribe los archivos sin imprimir un valor:

```sh
ssh <servidor> 'python3 /opt/<stack>/instancia/despliegue/secretos/generar.py /opt/<stack>/secretos'
```

Es idempotente: conserva lo que ya está en el archivo y no rota la base. Al final imprime solo nombres: cuántas claves, cuáles quedan provisionales, qué flags están en `true` y qué constantes cambiaron. **Los flags de toda salida nacen escritos en `false`** (`APAGADOS` en `generar.py`), y también el motor de flujos y los dos del Automation Expert: así se ve en el archivo que la instancia arranca en solo escucha.

Un token o una clave que se copia de un panel se carga con su script, que lo pide sin mostrarlo (`ssh -t`), en la terminal de quien instala. Si alguien necesita pegar un valor en un panel (el token del adaptador en GHL, por ejemplo), lo lee en su terminal con `ssh` y `grep` sobre el archivo, y lo pega sin pasarlo por un chat.

### Los archivos

| Archivo | Lo lee | Lo escribe |
|---|---|---|
| `db.env` | La base | `generar.py` |
| `rest.env` | PostgREST | `generar.py` |
| `bridge.env` | El bridge | `generar.py`, los scripts de carga y, para los flags, una persona a mano |
| `hermes.env` | `scripts/hermes_instancia.py` (no lo lee Docker) | La carga de la key de OpenRouter (paso 11) |
| `automation-expert.env` | `scripts/hermes_instancia.py` | `despliegue/hermes/cargar-automation-expert.py` (paso 11) |
| `alertas.env` | El alertador del host | `despliegue/alertas/cargar-secretos.py` (paso 12) |
| `respaldo-rclone.conf`, `respaldo-ping.txt` | El respaldo del host | `despliegue/respaldo/cargar-secretos.py` (paso 13) |
| `revision.env` | La revisión diaria, si se prende | `despliegue/revision-diaria/generar-revision.py` |

Docker lee cada `env_file` del stack al desplegar: si falta uno, el deploy no aplica nada, tampoco al bridge.

### La lista completa del archivo del bridge

**Los que genera `generar.py`** (secretos, se conservan una vez creados):
- en `db.env`: `POSTGRES_PASSWORD`, `AUTHENTICATOR_PASSWORD`, `JWT_SECRET` y `JWT_EXP`;
- en `rest.env`: `PGRST_DB_URI` y `PGRST_JWT_SECRET`, derivados de los anteriores;
- en `bridge.env`: `SUPABASE_SERVICE_ROLE_KEY` (un JWT de rol `service_role` firmado con el `JWT_SECRET` de la base), `HERMES_API_KEY` (la clave con la que el bridge le habla a su Hermes; el paso 11 la pone en el profile), `GHL_PRECHECKOUT_ADAPTER_TOKEN` (no se rota en cada despliegue: GHL lo tiene copiado), `CHATWOOT_WEBHOOK_SECRET` (nace provisional; el paso 16 lo reemplaza por el que devuelve Chatwoot) y los tres del Automation Expert: `AUTOMATION_EXPERT_READ_TOKEN`, `AUTOMATION_EXPERT_CHANGE_TOKEN` y `AUTOMATION_EXPERT_CHANGE_DB_KEY` (un JWT de rol `automation_expert_change`, firmado con el mismo secreto que el de `service_role` y distinto de él).

**Los que escribe `generar.py` y no son secretos** (los fija la instancia; se conservan):
`INSTANCE_MANIFEST_PATH`, `SUPABASE_BASE_URL`, `CHATWOOT_BASE_URL` (el host **público** de Chatwoot: el bridge baja los audios solo de ese host), `CHATWOOT_ACCOUNT_ID`, `CHATWOOT_INBOX_ID`, `MESSAGING_CHANNEL=chatwoot`, `HERMES_MODEL_NAME` (igual a `[agente].modelo`), `HERMES_API_BASE_URL` (`http://hermes:8642/v1`), `COMMERCIAL_KNOWLEDGE_ENABLED`, `AGENT_PROVENANCE_TENANT_REF`, `AGENT_PROVENANCE_SCOPE_REF`, `CHATWOOT_CUT_B_SCOPE_KEY` y `_VERSION`, `HANDOFF_PROJECTION_POLICY_KEY` y `_VERSION`, `LANCEMOS_PILOT_SCOPE_KEY`, `LANCEMOS_PILOT_TENANT_KEY`, `LANCEMOS_PILOT_CHANNEL_PROVIDER=waba`, `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF=chatwoot-inbox:<inbox>`, `FOLLOWUP_POLICY_KEY` y `_VERSION`, y `GHL_PRECHECKOUT_ADAPTER_ENABLED=false`. Las claves de scopes y políticas son las que siembra el paso 8.

**Los flags escritos en `false`** (`APAGADOS`; se conservan, así que prender uno es editar el archivo a mano después de su prueba): `FLOW_ENGINE_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED`, `LANCEMOS_PILOT_BOUNDARY_ENABLED`, `DURABLE_DISPATCHER_ENABLED`, `DURABLE_OUTBOUND_ENABLED`, `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`, `META_FINAL_EFFECT_ENABLED`, `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED`, `PORTABLE_HOTMART_RECOVERY_ENABLED`, `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED`, `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED`, `CONVERSATION_FOLLOWUP_ENABLED`, `CONVERSATION_REACTIVATION_ENABLED`, `AUTOMATION_EXPERT_READ_ENABLED` y `AUTOMATION_EXPERT_CHANGE_ENABLED`.

**Las constantes del repo** (no son secretas y `generar.py` las **reescribe** en cada corrida; cambiar una es un PR al generador, no una edición en el servidor):
`LANCEMOS_PILOT_SCOPE_VERSION`, `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY`, `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION`, `WABA_FIRST_TOUCH_TEMPLATE_NAME` (el carrito), `WABA_PAYMENT_FAILURE_TEMPLATE_NAME`, `WABA_PRECHECKOUT_TEMPLATE_NAME`, `WABA_TEMPLATE_LANGUAGE`, `WABA_TEMPLATE_CATEGORY`, los tres ids de worker (`DURABLE_DISPATCHER_WORKER_ID`, `CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID`, `HUMAN_HANDOFF_PROJECTION_WORKER_ID`), las siete `CONVERSATION_FOLLOWUP_*` del seguimiento con cupón (plantilla, idioma, cupón, producto, horario, páginas e intervalo), `CHATWOOT_STALLED_MAX_PAGES` y `ALLOWED_WHATSAPP_JID` (vacío: con destinatario dinámico, el envío falla si tiene valor). Los nombres y el idioma de las plantillas tienen que ser los de `[plantillas]`: si no coinciden, el bridge no arranca ese flujo.

**Lo que no escribe a propósito:** las variables de los flujos concretos de otra aliada (en ATT1, el carrito de tres toques, las variantes del pago rechazado, la ficha de pago y los cupones del carrito). El bridge no las exige con sus flags apagados, y son configuración de cada aliada.

**Los que se copian de un panel o salen de una API** (nunca por chat):
- `HOTMART_HOTTOK`: el hottok de la configuración de webhooks de la cuenta de Hotmart. Lo pide `despliegue/hotmart/cargar-hottok.py` (paso 10), con `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED=true`.
- `OPENROUTER_API_KEY` en `hermes.env`: la key de OpenRouter de la instancia, con un tope de gasto (paso 11). La misma va en `bridge.env` para la transcripción de audios, copiada sin mostrarla (paso 17, `inbound`).
- Los tokens de la app de Slack del Automation Expert, en `automation-expert.env` (paso 11).
- Con Slack: `SLACK_CONNECTOR_BEARER_TOKEN`, `OPERATOR_CORRELATION_READ_TOKEN` y `OPERATOR_CORRELATION_WRITE_TOKEN`. Los genera `despliegue/slack/conectar-slack.py` (paso 14).
- Para prender (paso 16): `CHATWOOT_CONTROL_API_ACCESS_TOKEN`, `CHATWOOT_AGENT_BOT_ID` y `CHATWOOT_AGENT_BOT_ACCESS_TOKEN` (`despliegue/chatwoot/cargar-tokens-chatwoot.py`), `CHATWOOT_PAUSE_MACRO_ID`, `CHATWOOT_RESUME_MACRO_ID` y `CHATWOOT_OPT_OUT_MACRO_ID` (a mano, no son secretos) y `CHATWOOT_WEBHOOK_SECRET` (`despliegue/chatwoot/apuntar-webhook.py`).
- Fuera de los archivos: el token de GHCR de solo lectura (`read:packages`), si la imagen se baja de GHCR (paso 6). Va en el gestor de contraseñas del equipo.

**Los que se cargan a mano con cada flujo** (flags y sus variables): están en el paso 17, flujo por flujo, y en la cabecera de `generar.py` («LO QUE QUEDA A MANO»). Ninguno lo prende el generador.

**Verificación:**
- `generar.py` termina con `escritos: db.env, rest.env, bridge.env (600)`, nombra solo claves, nunca valores, y dice `flags en true: ninguno`.
- Hasta el paso 16, `provisionales:` lista los cuatro de Chatwoot (`CHATWOOT_WEBHOOK_SECRET`, `CHATWOOT_AGENT_BOT_ID`, `CHATWOOT_CONTROL_API_ACCESS_TOKEN`, `CHATWOOT_PAUSE_MACRO_ID`). En solo escucha es lo esperado: el bridge arranca y `/ready` responde, pero no hay conversación.
- `ls -l /opt/<stack>/secretos` muestra permisos `-rw-------`.
- Cada nombre de `.env.example` está en el archivo, salvo los flags de flujos que todavía no se prendieron. En ATT1 faltaban tres flags del entrante que nadie vio hasta el 2026-10-05 (setter-instancia-att1#18): la lista se compara nombre por nombre, no de memoria.

## 8. La base de la instancia

Cada instancia tiene su base, en su stack: Postgres de Supabase con PostgREST y un proxy delante. Nunca se comparte con otra aliada.

### El stack con la base vacía

```sh
ssh <servidor> 'cd /opt/<stack>/instancia && docker stack deploy --resolve-image never -c despliegue/stack.yaml <stack>'
```

El stack trae la base, PostgREST, el gateway, el bridge y Hermes (`despliegue/stack.yaml`, con su cabecera). La base nace vacía, y el bridge puede reiniciarse hasta el paso 9: todavía no hay estructura ni filas. **Verificación:** `docker service ls` muestra `<stack>_db`, `<stack>_rest`, `<stack>_gateway` y `<stack>_hermes` en `1/1`.

### La estructura

La base nace con `despliegue/base/crear-desde-cero.sh` del repo de la instancia, desde el árbol del producto del paso 6: `sh despliegue/base/crear-desde-cero.sh /opt/<stack>/build/<versión>` dice qué haría, y con `--aplicar` lo hace. Se niega si la base no está vacía. En este orden:

1. Los roles `anon`, `authenticated` y `service_role` (este con `bypassrls`), y los permisos por defecto sobre las funciones y las tablas de `public`.
2. `supabase/baseline/20260803_public_schema.sql`.
3. Todas las migraciones de `supabase/migrations/`, en orden, una por vez y con `ON_ERROR_STOP`, anotando cada una en el registro de migraciones aplicadas de la instancia (`/opt/<stack>/base-aplicadas.txt`, el que leen `aplicar-migraciones.sh` y el chequeo).
4. `vaciar-filas-sembradas.sql`: borra las filas que siembra la cadena (abajo) y verifica que queden en cero.
5. Los dos inventarios: `scripts/supabase_schema_inventory.sql` y `scripts/supabase_acl_inventory.sql`.
6. PostgREST relee el esquema: el script le manda `SIGUSR1` a su contenedor y espera `Schema cache loaded` en su log, o frena. PostgREST arrancó con la base vacía y no ve ninguna función hasta releer; sin esto, el bridge recibe `404` (`PGRST202`) en todo.

Es lo mismo que hacen las pruebas de Postgres de la CI (`tests/sql/followup_engine/real_postgres_portable_inbound_adoption.py`, función `install_schema`).

**La diferencia con ATT1.** La cadena de migraciones siembra la configuración de Johanna en nueve tablas (la lista está en la cabecera de `limpiar-datos-de-johanna.sql` del repo de ATT1). ATT1 se armó con esa cadena el 2026-09-30 y se limpió después con ese archivo, a mano (setter-instancia-att1#4). En la plantilla esa limpieza es parte de la base: `vaciar-filas-sembradas.sql`, la de ATT1 generalizada. Se niega si la base ya tiene una instancia aprovisionada, si una de las nueve tablas tiene más filas que las que siembra la cadena (45 en total, medido en PGlite el 2026-10-17 con la v1.9.0, y las mismas 45 el 2026-10-23 con la v1.16.4: `evidencia/2026-10-23-pruebas-de-la-plantilla-v1.16.4.txt` de la plantilla) o si una tabla hija tiene filas. Por eso dos huellas del inventario de esquema pueden dar en una base desde cero otro resultado que en ATT1: `20260825000400` y `20260829000500`, que el inventario reconoce por filas de Johanna. En ATT1 quedan en `fingerprint_partial` (medido el 2026-10-01; cabecera de `despliegue/base/aplicar-migraciones.sh` del repo de ATT1). En una base desde cero hay que verificar qué dan, y anotarlo como la línea de base de la instancia: las subidas siguientes comparan contra esa corrida, no contra «todo present».

**Verificación:**
- `scripts/supabase_acl_inventory.sql` da `ok` en todas las funciones.
- `scripts/supabase_schema_inventory.sql` da `fingerprint_present` en todas las migraciones de la cadena, salvo esas dos, que se verifican y se anotan.
- `crear-desde-cero.sh` vuelve a contar las nueve tablas y el binding por separado, y todas dan cero. Si alguna no, frena.
- La salida va a `evidencia/<fecha>-base-desde-cero.txt`.

### El proxy delante de PostgREST no registra la query

El bridge pone teléfonos y emails en la query de algunos `GET` (desde 1.3.0, uno por cada mensaje entrante de un móvil de México o Argentina), así que el log de acceso de ese proxy escribe la ruta sin la query. El proxy es el servicio `gateway` del stack, un nginx, con su config y su prueba local en el repo de la instancia (`despliegue/gateway/nginx.conf` y `probar-log.sh`):

```nginx
log_format sin_query '$remote_addr [$time_local] "$request_method $uri $server_protocol" '
                     '$status $body_bytes_sent $request_time "$http_user_agent"';

server {
    access_log /var/log/nginx/access.log sin_query;
    # ...
}
```

`log_format` va en el contexto `http` (un archivo de `conf.d/` ya está adentro), y nginx lee la config solo al arrancar: el servicio se reinicia después de cambiarla. Qué queda con la query y cuándo: `CHANGELOG.md`, `[1.3.0]`, *Qué tiene que hacer una instancia*.

**Verificación:** `probar-log.sh` en la Mac (con Docker) da ok, y en el servidor un `GET` con una query inventada deja en el log de acceso una línea con la ruta y sin `?`.

### Las filas de la instancia

Después de la estructura, el SQL de aprovisionamiento siembra lo que el bridge necesita para esta aliada, con los valores de `instancia.toml`. Lo corre `despliegue/base/aprovisionar.sh`, que lee `instancia.toml` y le pasa los valores a `aprovisionar.sql` (sin un valor copiado a mano): `sh despliegue/base/aprovisionar.sh` en seco, y con `--aplicar`. Antes de sembrar aborta si encuentra una fila de otra instancia. Siembra:

- el binding comercial (`commercial_ally_runtime_bindings`), **igual campo por campo** al que arma el bridge con el manifiesto: cualquier diferencia es `503 commercial_ally_binding_unavailable`;
- el scope entrante publicado (`inbound_commercial_scope_versions`);
- la política de derivación del scope entrante, con el team de derivaciones;
- la política de recuperación y el scope del piloto de recuperación, con su control **inactivo**;
- el catálogo de ofertas, los scopes de intención de compra y la política de compra del binding;
- la política y el scope del primer contacto, aparte del de recuperación, con su control inactivo.

Se corre como superusuario o como el dueño de las tablas (`service_role` no tiene DML sobre ellas, a propósito), en una transacción, y termina con un bloque que verifica cada fila. **Antes de la primera corrida,** `[hotmart].product_name` tiene que ser idéntico, mayúsculas incluidas, al `data.product.name` que manda Hotmart, y `[chatwoot].inbox_id` tiene que ser la bandeja definitiva: el binding, la política y el scope publicados no se editan, y corregirlos es otra versión. ⚠ **Sin número de WhatsApp el día 0 no hay inbox de WhatsApp Cloud:** el binding va con una bandeja provisoria (un inbox `Channel::Api` de la cuenta de la aliada), y cuando llega el número se pasa al inbox de WhatsApp con una versión nueva del binding: `scripts/publish_inbox_binding_version.py` (paso 16, «De la bandeja provisoria al inbox del número»).

**Verificación:** el SQL termina sin error y su bloque final no aborta; correrlo dos veces no cambia nada. `sh despliegue/base/piloto.sh estado` muestra los dos controles en `inactive`. El `/ready` del paso 9 da `commercial_ally_binding: "active"`.

## 9. El bridge

El servicio del bridge monta el repo de la instancia de solo lectura en `/instancia` (`INSTANCE_MANIFEST_PATH=/instancia/instancia.toml`), lee `bridge.env` y está en tres redes: la interna (la base, por el alias `<stack>-gateway`), la de Traefik (la ruta pública) y la de Hermes. El healthcheck mira `/health` y no `/ready`: con `/ready`, que espera a la base, el bridge de Johanna quedó 47 minutos caído el 2026-09-25 por tres probes lentos (cabecera de `despliegue/stack.yaml`).

1. **En seco, sin red,** con el archivo y la carpeta de la instancia:

   ```sh
   ssh <servidor> 'docker run --rm --network none --env-file /opt/<stack>/secretos/bridge.env -v /opt/<stack>/instancia:/instancia:ro setter-bridge:<versión> python -c "from bridge.app import Settings, build_app; Settings.from_env(); build_app(); print(\"BRIDGE_OK\")"'
   ```

2. **El redeploy**, con la base ya sembrada:

   ```sh
   ssh <servidor> 'cd /opt/<stack>/instancia && docker stack deploy --resolve-image never -c despliegue/stack.yaml <stack>'
   ssh <servidor> 'docker service update --force <stack>_appointment-bridge'   # el deploy no reinicia si el spec no cambió
   ```

3. **La ruta pública** en el Traefik de EasyPanel: `despliegue/traefik/<aliada>-appointment-bridge.yaml` del repo de la instancia, copiado tal cual a `/etc/easypanel/traefik/config/`. Trae solo los tres webhooks que llegan de afuera (`/webhooks/adapters/ghl/lead-precheckout`, `/webhooks/chatwoot` y `/webhooks/hotmart`) y, si se usa Slack, el backend de operador (`/internal/operator/correlations/`). Traefik relee el archivo solo; si se reinicia con un archivo que no puede leer, no carga ninguna ruta de la carpeta. Se da de baja borrándolo.

**Verificación:**
- La prueba en seco imprime `BRIDGE_OK`. Si no, el error nombra la variable que falta: se corrige antes del deploy.
- `/ready` desde adentro de la red da 200 con `instance_ally` igual a la aliada, `instance_product_version` igual a `producto`, `commercial_ally_binding: "active"` y `automation_state: inactive`. Con el conocimiento aprobado, `commercial_knowledge` con su versión y su hash.
- `docker service ls` muestra el bridge en `1/1`, y se mira, no se supone: en ATT1, el 2026-09-30, una copia vieja del repo dejó el bridge reiniciándose en loop por `HERMES_MODEL_NAME`.
- Desde afuera, `GET` a los tres webhooks da 405, y `/ready`, `/health` e `/internal/*` responden 404.
- La salida de `/ready` va a `evidencia/`.

### Actualizar el bridge de una instancia que ya tiene la base sembrada

El orden es siempre **migraciones → filas de la instancia → bridge → `/ready`**, salvo que la versión diga otra cosa. La misma imagen corre en todas las instancias, así que el bridge nuevo llega a una base que puede no tener todavía lo que él espera. Un bridge viejo sobre una base migrada sigue andando (toma solo los campos que conoce); al revés no. Qué hace falta en cada versión lo dice el `CHANGELOG.md`, en «Qué tiene que hacer una instancia» o «Qué hace quien actualiza».

**En una instancia creada desde la plantilla, la subida es `despliegue/subida/subir.sh --desde <v> --hasta <v> [--seco]`** (su README): lo que cambia en cada versión está en un archivo de pasos (`despliegue/subida/pasos/<v>.toml`, unas 10 líneas que se escriben con la release), y el script frena antes de tocar nada si el bridge no corre la versión de origen, si falta una imagen, si el archivo del bridge cambiaría algo que los pasos no declaran o si se perdería un caso en vuelo. No pausa ni arma ningún scope. Las migraciones quedan afuera, con `despliegue/base/aplicar-migraciones.sh` y el orden que dice el archivo de pasos. ATT1, anterior a la plantilla, sube con sus scripts a mano (README de su repo).

`aplicar-migraciones.sh` respalda la base antes, frena si una huella del inventario empeora y, si aplicó alguna, hace que PostgREST relea el esquema (`SIGUSR1` a su contenedor, verificado en su log). Esa recarga es parte del paso: el 2026-10-19, en ATT1, con la `20261019000100` aplicada y la ficha prendida, PostgREST siguió con la caché de antes, respondió `404` (`PGRST202`) a las funciones nuevas y `/ready` dio `503` durante 8 minutos (setter-instancia-att1#67). Un `NOTIFY pgrst` no alcanza: en el Swarm, la conexión con la que PostgREST escucha se corta en silencio cada ~2 h, y entre medio no le llega ningún aviso.

Los dos casos que siguen son de instancias sembradas antes de esas versiones. Una instancia desde la plantilla ya nace con todo.

**Caso `20260930000200` (landings por oferta, CHANGELOG 1.1.0).** Desde esa versión el manifiesto v2 declara la landing de cada oferta adicional y el bridge compara la fila del binding campo por campo. Una fila sembrada antes de la migración queda con `additional_offer_landings = []` y el bridge nuevo ve drift: `/ready` responde `503 commercial_ally_binding_unavailable` y, con `SLACK_CONNECTOR_PROJECTION_ENABLED=true`, el arranque falla. Una base nueva ya trae la migración y la siembra con las landings, así que esto aplica solo a una instancia sembrada antes. Pasos, con los valores de ATT1 como ejemplo (salen de `[[hotmart.ofertas]]` del manifiesto, en su orden; `tenant_ref = 'lancemos'` es el valor del manifiesto de ATT1):

1. Aplicar la migración.
2. Ver si la fila ya existe y cómo está:

   ```sql
   select binding_version, status, additional_offer_codes, additional_offer_landings
   from public.commercial_ally_runtime_bindings
   where tenant_ref = 'lancemos' and funnel_ref = 'att1';
   ```

   Si no hay fila, se siembra ya con las landings (el aprovisionamiento de la instancia las tiene que incluir en el insert y en su verificación). Si hay una fila `active` con `additional_offer_landings = []`, se sigue con el paso 3.
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

**Caso 1.3.0 y 1.3.1 (migraciones `20261001000100` a `20261001000500`).** Una instancia que sube a 1.3.1 desde 1.2.0 aplica las cinco y despliega directamente el bridge 1.3.1. ATT1 hizo esta subida el 2026-10-01 (setter-instancia-att1#10 y #11).

1. Aplicar las cinco migraciones, en ese orden, con poco tráfico (las de 1.3.0 son `000100`, `000200` y `000300`; las de 1.3.1, `000400` y `000500`). La `000100` solo crea y reemplaza funciones. La `000200` toma un lock exclusivo breve sobre `recovery_cases`, `recovery_case_events` y `followup_sequences` para sumar un valor a tres checks, y mientras corre también esperan las escrituras de `contacts`, `purchase_intents` y `precheckout_submissions` (las referencia la tabla nueva), con `lock_timeout` de 5 s: si una transacción larga tiene tomada alguna de esas tablas, falla sin dejar nada a medias y se reintenta. La `000300` solo crea una función. La `000400` solo crea una función (`admit_portable_inbound_commercial_case_v1`, solo para `service_role`) y la `000500` solo reemplaza cuatro, conservando su firma y sus permisos: ninguna de las dos toca tablas ni filas.
   **Verificación:** `scripts/supabase_schema_inventory.sql` da `fingerprint_present` en las cinco nuevas, y en las demás lo mismo que antes de aplicarlas; `scripts/supabase_acl_inventory.sql` da `ok` en todas. En una instancia armada con la cadena y limpiada de las filas de Johanna, dos filas quedan en `fingerprint_partial` por diseño, antes y después: `20260825000400` y `20260829000500` buscan filas de Johanna que la limpieza borra (medido en ATT1 el 2026-10-01). Por eso se compara contra la corrida de antes, no contra «todo present»: es lo que hace `despliegue/base/aplicar-migraciones.sh`, que falla solo si aparece un fingerprint no presente nuevo o un ACL distinto de `ok`.
2. Desplegar el bridge (1.3.1, o 1.3.0 si la instancia se queda ahí) con el manifiesto que ya tenía la instancia. Un bridge 1.2.0 sigue andando sobre la base migrada: las funciones que la `000100` y la `000500` reemplazan conservan su firma (con un flujo portable prendido ya comparan el teléfono por sus dos formas; sin una conversación adoptada, las cuatro de la `000500` devuelven lo mismo que antes). **El bridge 1.3.1 no sale antes que la `000400`:** con manifiesto admite cada entrante por esa función, y sin ella PostgREST responde `404` (`PGRST202`) y el bridge reintenta el entrante sin tope hasta que se aplique (la baja se sigue registrando por el respaldo de 1.3.0).
3. Recién con la imagen 1.3.0 o posterior corriendo, mergear el manifiesto que suma las claves `riesgo_*` de `[adaptadores.ghl]`, si la instancia las va a usar: un bridge 1.2.0 no carga un manifiesto con esas claves.
4. **Verificación:** `/ready` da 200. Con `[adaptadores.ghl]` suma `ghl_adapter_risk` (`accepted:<fecha>:<contrato>` o `not_accepted`). El log de arranque trae una línea `ghl_adapter_risk acceptance=…`.

En la base de Johanna, después de aplicar la `000400` y la `000500`: `select count(*) from public.conversation_events where event_type = 'inbound_adopted_template_conversation';` tiene que dar 0, y seguir en 0 en cada revisión posterior. Su bridge corre sin manifiesto y no llama a la `000400`, así que las cuatro funciones de la `000500` le devuelven lo mismo que antes. Ninguna función ni el bridge escriben ese evento salvo la `000400`, pero la tabla no lo impide (`event_type` no tiene check y `service_role` conserva el DML): un insert a mano de ese evento en una conversación de Johanna haría que las cuatro funciones miren solo su `inbound_sales`, y con un único `cart_recovery` darían `not_found`.

Una instancia con `[adaptadores.ghl]`, sin aceptación y con `LANCEMOS_PILOT_BOUNDARY_ENABLED=true` necesita la `000300` aplicada **antes** del bridge 1.3.0: el arranque lee el modo de audiencia del scope y, sin esa función, no arranca.

**Por qué se completa la fila y no se publica un `binding_version` nuevo.** Los eventos ya admitidos guardan su `binding_version` en la procedencia (`commercial_ally_hotmart_event_bindings`), y las RPC de planificación exigen que esa versión siga `active`; la política de compra (`commercial_ally_hotmart_purchase_policies`) también va por versión. Retirar la versión 1 dejaría sin planificar lo que ya entró. Completar las landings solo agrega: ninguna intención ni evento se validó contra ellas (antes el formulario de esas landings se rechazaba). Lo que no se toca una vez publicado son los valores de negocio (producto, precio, ofertas, consentimiento).

## 10. Hotmart entrando

Es lo que hace que la instancia «escuche». Hace falta alguien con acceso a la cuenta de Hotmart dueña del producto, con permiso de webhooks. **El orden importa:** primero el hottok en el bridge, después el webhook. Al revés, el bridge responde `503`, y Hotmart desactiva un webhook que falla.

1. **El hottok**, que sale de la configuración de webhooks de esa cuenta de Hotmart (Herramientas → Webhook → Autenticación):

   ```sh
   ssh -t <servidor> 'python3 /opt/<stack>/instancia/despliegue/hotmart/cargar-hottok.py'
   ```

   Lo pide sin mostrarlo y lo carga junto con `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED=true` (el freno por compra: el bridge no arranca con el hottok y esa capacidad apagada). Después, el redeploy del bridge.
2. **El webhook** en Hotmart, versión 2.0.0, del producto, con un nombre que diga la instancia («Setter <aliada>»), hacia `https://<dominio del bridge>/webhooks/hotmart`, con tres eventos: *Compra aprobada*, *Abandono de carrito* y *Compra cancelada*. Los webhooks que ya existen (GHL, Zapier) siguen: se pueden tener varios.

**Qué cambia:** una compra aprobada marca como comprada la intención de esa persona. El carrito y la compra cancelada se contestan `200 ignored` hasta que se prenda su flujo. Una oferta ajena se contesta con `200`, nunca con un error. **No sale ningún mensaje:** es el primero de los flags que el día 0 está en `true` a propósito (`despliegue/dia-0.md`).

**Verificación:**
- La prueba del panel de Hotmart manda un envío por evento, y los tres vuelven con `200` (`portable_purchase_stop_event_ignored`, sin guardar nada). En ATT1, el 2026-10-04 a las 23:16Z.
- El historial del webhook en Hotmart queda en `2xx`.
- **Con la primera compra real** (o una de prueba) se confirman dos datos que hasta ahí son supuestos: que `data.product.name` es exactamente `[hotmart].product_name`, y con qué forma llega el teléfono. Hotmart guarda el historial de envíos de cada webhook, y ahí se ve el payload. `chequear.sh` (paso 15) ve el último evento de Hotmart en la base.

## 11. Hermes: el agente comercial y el Automation Expert

Un Hermes propio por instancia, adentro de su stack, como el servicio `hermes`, con los profiles que declara el producto y nada más ([ADR-0025](decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md)): el agente comercial y el Automation Expert. El agente comercial usa el SOUL común del producto (`profiles/agente-comercial-comun/SOUL.md`); el conocimiento no va en el profile, lo manda el bridge. El formato y el uso del instalador están en [`hermes/README.md`](../hermes/README.md).

**Por qué el servicio se llama exactamente `hermes`:** el bridge acepta http solo hacia ese nombre o hacia localhost (`trusted_http_hosts` en `src/bridge/app.py`). Va en una red propia que comparte con el bridge, sin puertos publicados, y en esa red no puede haber otro servicio llamado `hermes`.

1. **La key de OpenRouter** de la instancia, con un tope de gasto, en `hermes.env`. Se carga sin mostrarla con `despliegue/hermes/cargar-key-openrouter.py`, que además la prueba contra los modelos de `hermes.toml`. Si la cuenta de OpenRouter tiene ZDR, un modelo que no lo cumple da 404: se prueban los dos modelos antes de seguir.
2. **La app de Slack del Automation Expert**, creada desde el manifiesto del producto `deploy/slack-app-manifest-automation-expert-v1.json` (Socket Mode, solo mensajes directos, ninguna URL pública; guía y origen de cada scope en `docs/operations/automation-expert-slack-app-runbook.md`). Sus tokens (el Bot User OAuth Token y un App-Level Token con `connections:write`) se cargan sin mostrarlos:

   ```sh
   ssh -t <servidor> 'python3 /opt/<stack>/instancia/despliegue/hermes/cargar-automation-expert.py'
   ```

   y `administran` de `hermes.toml` lleva el `U…` de quien administra la instancia (un PR al repo y el repo otra vez al servidor). ⚠ `hermes_instancia.py` lee las variables de los dos profiles antes de instalar cualquiera: **sin esta app y su archivo de secretos, el agente comercial tampoco se instala.** Una instancia sin la app borra la sección del Automation Expert de `hermes.toml`.
3. **`hermes.toml`** en la raíz del repo de la instancia: `stack`, el modelo y el proveedor de cada profile, su respaldo, y de qué archivo de secretos sale cada variable (`API_SERVER_KEY` sale de `HERMES_API_KEY` de `bridge.env`; `OPENROUTER_API_KEY`, de `hermes.env`; los tokens del Automation Expert, de `bridge.env` y de `automation-expert.env`). No va en `instancia.toml`: el lector del manifiesto rechaza las secciones que no conoce.
4. **El servicio `hermes` del stack** (paso 8), con la imagen que declara `hermes/declaracion.toml` del producto, fija por digest, y `command: ["sleep", "infinity"]` (así s6 levanta solo los gateways de los profiles que tienen que correr).
5. **Instalar y chequear**, desde el árbol del producto del paso 6:

   ```sh
   ssh <servidor> 'cd /opt/<stack>/build/<versión> && H=/opt/<stack>/instancia/hermes.toml && python3 scripts/hermes_instancia.py plan --instancia $H && python3 scripts/hermes_instancia.py aplicar --instancia $H && python3 scripts/hermes_instancia.py chequear --instancia $H; echo "salida: $?"'
   ```

6. **El Automation Expert contesta.** Nace apagado (`AUTOMATION_EXPERT_READ_ENABLED` y `AUTOMATION_EXPERT_CHANGE_ENABLED` en `false`): con el flag de lectura apagado el bridge no crea sus rutas y el plugin contesta error. Para probarlo, `AUTOMATION_EXPERT_READ_ENABLED=true` en `bridge.env` (con respaldo), el redeploy del bridge y, por mensaje directo a su app, `/flows`. Es el segundo de los flags que el día 0 está en `true` a propósito: le contesta a quien administra la instancia, no a un lead. Las rutas de cambio (`AUTOMATION_EXPERT_CHANGE_ENABLED`) se prenden después, cuando haya algo que cambiar; el operador del almacén es `despliegue/base/operador-de-flujos.sh`.

**Verificación:**
- `plan` dice qué haría: crear los profiles, instalar la distribución, poner las variables y arrancar los gateways.
- `aplicar` termina en `VEREDICTO: igual a la declaracion`.
- `chequear` termina en `salida: 0`: el SOUL igual a Git, el `config.yaml` igual a lo declarado, las herramientas exactas (cero en el comercial), las variables iguales a los secretos, los gateways corriendo y `automation-expert: Slack conectado`.
- `/flows` por mensaje directo contesta que no hay flujos.
- Una prueba en seco del comercial: un turno con la forma del pedido del bridge contra el API del Hermes nuevo, desde adentro del stack, devuelve una propuesta que el bridge acepta. En ATT1, `evidencia/2026-10-14-hermes-propio-prueba-en-seco.md` de su repo.

Rotar `API_SERVER_KEY` exige reiniciar el gateway (`aplicar --reiniciar`) y el bridge, que lee `HERMES_API_KEY` al arrancar. El SOUL, el `config.yaml` y la key de OpenRouter se releen sin reiniciar; los tokens y los plugins del Automation Expert, solo al arrancar (`hermes/README.md`, *Cuándo hace falta reiniciar*).

## 12. Las alertas

Un timer de systemd en el host que cada minuto mide el bridge, su base, `/ready`, el Hermes propio, si entra Hotmart, los topes, los scopes, las derivaciones y las tarjetas, y avisa en un canal de Slack con su propia app. **Se prenden el día 0:** es cuando más sirven, porque avisan si Hotmart no entra. Lo que solo aplica con un flujo prendido (topes, scopes, derivaciones, tarjetas) queda callado mientras todos los `[flujos]` estén en `false`, así que en solo escucha no hay falsos rojos. Todo está en `despliegue/alertas/README.md` del repo de la instancia; lo propio de la instancia lo lee de `[alertas]` de `despliegue.toml` y de `instancia.toml`.

1. **Antes, en el navegador:** la app desde `despliegue/alertas/slack-app-manifest.json`, el canal con `/invite @<aliada> Alertas`, y en healthchecks.io dos checks (`<aliada>-alertas-vivo` y `<aliada>-alertas-criticas`, Period 1 minuto, Grace 5 minutos). Las dos Ping URLs son credenciales.
2. **En el servidor**, los pasos 2 a 6 de «Instalar» del README de las alertas: la carpeta al VPS bajo el mismo `flock` del ciclo; `ssh -t <servidor> 'python3 /opt/<stack>/alertas/cargar-secretos.py'` (el token del bot y las dos Ping URLs, sin que se vean); `alertador.py --probar` (mide una vez y no escribe nada); las unidades y el timer; y `alertador.py --prueba` en el canal, hasta `PRUEBA_OK`. Hasta el `PRUEBA_OK` el alertador mide y no publica.

**Verificación:** `alertador.py --estado` dice `modo prendida · último ciclo hace N s · vigía: vivo OK, críticas OK · Slack: publica`, con N menor que 60, y los dos checks de healthchecks.io en verde. El primer aviso real suele ser `hotmart` («Hotmart nunca entró», a las `[alertas].hotmart_horas` de instalado), si el webhook del paso 10 no entrega.

## 13. El respaldo

El respaldo diario cifrado fuera del VPS: la base de la instancia, sus secretos, el volumen de Hermes y, solo en la instancia dueña del Chatwoot compartido, la base de Chatwoot (`[respaldo].chatwoot`). Cifra con la clave **pública** `despliegue/respaldo/offsite.pub` (la privada no entra a ningún repo), sube a Cloudflare R2 y avisa a healthchecks.io. Todo está en `despliegue/respaldo/README.md` del repo de la instancia.

1. **Antes, en el navegador (≈10 min):** el bucket `setter-<aliada>-backups` con un Bucket Lock de 90 días, su token de R2 acotado a ese bucket, y el check `<aliada>-respaldo` (Period 1 día, Grace 6 horas).
2. **En el servidor,** los pasos 5 a 8 del README: `instalar-herramientas.sh` (rclone y age verificados por sha256), `cargar-secretos.py` con `ssh -t` hasta `CARGA_OK` (prueba el bucket, el alcance del token y el candado), el ensayo hasta `ENSAYO_OK` y la primera corrida hasta `RESPALDO_OK`.
3. **La restauración de prueba** (paso 9 del README), en la Mac de quien custodia la clave privada: `restaurar-y-verificar.sh --vps <servidor>` hasta `RESTAURACION_OK`. Es lo único que prueba que un respaldo sirve.
4. **El timer** (paso 10 del README).

**Verificación:** `RESPALDO_OK`, `RESTAURACION_OK`, el check de healthchecks.io en verde y `systemctl list-timers <aliada>-respaldo.timer`. Al día siguiente, `journalctl -u <aliada>-respaldo --since yesterday` termina en `RESPALDO_OK`.

## 14. Slack: el conector y las tarjetas

Opcional. Sin Slack, se borra `[slack]` de `instancia.toml` y las derivaciones llegan solo al team de Chatwoot. Con Slack, el bridge le manda las tarjetas (las derivaciones, las compras) al conector de Slack, y el conector las publica en el canal de la instancia. El día 0 no sale ninguna tarjeta, porque no hay derivaciones: se deja conectado y se prueba al prender el entrante.

**El conector propio de la instancia (llega con la `1.17.0`).** Desde la `1.17.0` del producto (#315) el conector puede correr una sola instancia: el mismo conector, con **una sola clave** en cada mapa `SLACK_TENANT_*_JSON` (la `ally_ref` de la instancia), y el `http` interno entre el bridge y el conector del mismo stack, acotado a un `host:puerto` declarado (`SLACK_OPERATOR_INTERNAL_HOST` en el conector, `SLACK_CONNECTOR_INTERNAL_HOST` en el bridge). La forma de la configuración y las reglas de esa excepción están en `docs/contracts/slack-operations-connector-v1.md` §10. La app es una por instancia, desde `deploy/slack-app-manifest-instancia-v1.json`: solo `chat:write`, la interactividad hacia `https://<host del conector>/slack/interactions`, sin Socket Mode, sin Events API y sin slash commands. Cómo se crea: `docs/operations/slack-app-connection-runbook.md` §8. **El servicio del conector en el stack de la plantilla, su ruta de Traefik y el script que escribe su archivo de variables entran con el tag de la `1.17.0`, en su propio PR de la plantilla:** sus nombres finales los fija ese PR, y este paso se completa con él. Lo que no cubre todavía: la revisión diaria valida `SLACK_CONNECTOR_BASE_URL` como `https` por su cuenta (`src/bridge/daily_feedback_app.py`), así que sigue hablándole al conector por su host público.

**Mientras tanto, el conector compartido.** El orden es el canal, el bot en el canal, el tenant en el conector y recién después el bridge: si el conector no conoce al tenant contesta 401, y el bridge deja ese aviso en `terminal_failed`, sin reintento. El tenant del conector es `<aliada>` (el `ally_ref` del manifiesto): su canal en `SLACK_TENANT_CHANNELS_JSON`, su token en `SLACK_TENANT_TOKENS_JSON` y, si se quiere, su nombre en `SLACK_TENANT_LABELS_JSON` (`deploy/slack-connector.env.example`). `despliegue/slack/conectar-slack.py` genera los tokens, prueba en seco el conector y el bridge con su código real (`CONECTOR_OK` y `BRIDGE_OK`) y recién ahí escribe. El conector es compartido y no arranca con una configuración inválida: la línea nueva se prueba en seco antes de redesplegarlo, porque un error deja sin Slack también a las otras instancias.

En el bridge son cuatro flags, que se prenden con el entrante (paso 17), no el día 0: `SLACK_CONNECTOR_PROJECTION_ENABLED`, `CORRELATION_PRERESOLUTION_ENABLED`, `OPERATOR_CORRELATION_READ_ENABLED` y `OPERATOR_CORRELATION_WRITE_ENABLED`.

**Verificación:** el día 0, `conectar-slack.py` termina en `CONECTOR_OK` y `BRIDGE_OK` y el bridge tiene `SLACK_CONNECTOR_BASE_URL` (`chequear.sh`, punto 13). Con el entrante prendido, una derivación de prueba saca una tarjeta en el canal con el link a la conversación. En ATT1, el 2026-10-06 a las 16:01Z (setter-instancia-att1#21).

## 15. El chequeo: instalada en solo escucha

```sh
ssh <servidor> 'sh /opt/<stack>/instancia/despliegue/chequear.sh'
```

`despliegue/chequear.sh` del repo de la instancia **solo lee**: no despliega, no reinicia, no escribe en la base ni en un archivo. Los nombres los toma de `despliegue.toml` e `instancia.toml`. Mira cada pieza del día 0 y que no salga nada hacia nadie:

1. los dos archivos de configuración sin marcadores en lo que usa la instalación;
2. la copia del repo y el árbol del producto en el servidor;
3. cada servicio del stack en `1/1` y el bridge healthy;
4. `/ready` en 200 con la aliada, la versión y el binding activo;
5. la base contestando en una sesión de solo lectura, sin ninguna migración del tag pendiente;
6. **solo escucha:** los `[flujos]` en `false`, ningún flag de salida en `true`, ningún control del piloto en `armed`, ninguna plantilla declarada, y el inbox de Chatwoot sin un número de WhatsApp;
7. Hotmart entrando;
8. Hermes, con `hermes_instancia.py chequear`;
9. la ruta pública: los webhooks contestan y `/ready` no se ve desde afuera;
10. las alertas, con `alertador.py --estado`;
11. el respaldo: el timer, la última corrida y un respaldo de las últimas 26 horas;
12. la revisión diaria, apagada;
13. Slack.

Cada línea dice `ok`, `FALTA` (con el paso que lo arregla), `AVISO` o `SALIDA`. Termina con una de tres:
- **`INSTALADA`** (sale con 0): la instancia corre en solo escucha. Los `AVISO` no frenan, pero se leen.
- **`FALTAN N`** (sale con 1): algo de la instalación no está.
- **`NO ES SOLO ESCUCHA`** (sale con 3): algo prendido le puede mandar un mensaje a una persona. Se apaga **antes que nada**, con el respaldo del archivo del bridge o `piloto.sh pausar`, y se vuelve a correr.

**Qué queda apagado el día 0, y cómo se ve que no sale nada:**
- **Los flags de salida en `false`**, como los deja `generar.py` (paso 7). Los únicos en `true` a propósito son `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED` (paso 10), `AUTOMATION_EXPERT_READ_ENABLED` (paso 11) y, si los formularios de GHL ya se conectan, `GHL_PRECHECKOUT_ADAPTER_ENABLED`, que deja intenciones y no manda. `chequear.sh` los nombra como «prendidos a propósito» y da `SALIDA` con cualquier flag de salida en `true` (la lista está en `despliegue/chequeo.py`).
- **Los controles del piloto en `inactive`**, como los siembra el paso 8: `sh /opt/<stack>/instancia/despliegue/base/piloto.sh estado`. Con un control en `armed` y sus flags prendidos salen mensajes: el chequeo da `SALIDA`.
- **Sin número de WhatsApp:** no hay inbox de WhatsApp Cloud conectado. El chequeo lee el tipo de la bandeja en la base de Chatwoot, en solo lectura, y da `AVISO` si es de WhatsApp.
- **Las plantillas de Meta comentadas** en `[plantillas]` y **los `[flujos]` en `false`**.

La prueba del chequeo sin VPS está en la plantilla (`pruebas/chequeo/correr.sh`): una instancia sana, un servicio caído, un flag de salida prendido y lo demás, con docker, curl y systemctl falsos.

**Verificación:** termina en `INSTALADA`. La salida, con cuánto tardó cada paso, va a `evidencia/<fecha>-dia-0.md` del repo de la instancia. Al día siguiente se corre otra vez: el respaldo de la noche, las alertas y Hotmart.

## 16. Chatwoot: el inbox, el AgentBot y el webhook

Para prender: depende del número verificado (paso 0) y de la ruta pública del bridge (paso 9).

1. **El token de Meta.** Un usuario del sistema admin en el portfolio, con la app y las dos cuentas (WhatsApp y mensajería) asignadas con control total. El token se genera con vencimiento **Nunca** y los permisos `whatsapp_business_management` y `whatsapp_business_messaging`. Meta lo muestra una sola vez: va directo al inbox de Chatwoot, nunca a un chat. Un token de usuario común vence a los 60 días y corta todo.
2. **El inbox.** En Chatwoot, un inbox de WhatsApp Cloud por la carga manual, con el Phone Number ID, el ID de la cuenta y el token. Chatwoot configura el webhook en Meta. Se anotan los IDs de la cuenta y del inbox, y se pasan al manifiesto.

   **De la bandeja provisoria al inbox del número.** Si la instancia se aprovisionó con la bandeja provisoria (paso 8), el inbox nuevo no se edita en el binding: va en una versión nueva, que publica `scripts/publish_inbox_binding_version.py` desde el árbol del producto de la versión que corre. En una transacción, publica el binding `n + 1` con el inbox nuevo (el `n` pasa a `retired`) y su política de compra, una versión nueva del scope entrante con sus filas del catálogo y su política de derivación, y una versión nueva de cada scope del piloto que estaba en el inbox viejo, con el control apuntado a ella y en `inactive` (no arma nada). Frena sin escribir si el inbox nuevo no es de WhatsApp o no es de la cuenta del binding, si algún control de esos scopes está `armed` (se pausa antes), o si hay políticas de descuento publicadas para el binding vigente (no las copia). Va justo antes de prender el entrante, con «solo guardar» y sin casos en vuelo.

   ```sh
   # en el VPS, como root; CONTENEDOR_DB, CONTENEDOR_CHATWOOT_DB y BASE_CHATWOOT_DB como en el operador de flujos
   python3 <árbol del producto>/scripts/publish_inbox_binding_version.py --tenant <tenant_ref> --inbox-nuevo <N> --quien <q> --motivo "..."            # en seco: el plan, y rollback
   python3 <árbol del producto>/scripts/publish_inbox_binding_version.py --tenant <tenant_ref> --inbox-nuevo <N> --quien <q> --motivo "..." --aplicar  # pide un código por la terminal
   ```

   El en seco dice qué cambia y qué hay que tocar a mano, que el comando no toca: en el archivo de variables del bridge (con respaldo; `generar.py` conserva los valores que ya existen) `CHATWOOT_INBOX_ID`, `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF`, `CHATWOOT_CUT_B_SCOPE_VERSION`, `HANDOFF_PROJECTION_POLICY_VERSION` y la `*_SCOPE_VERSION` de cada scope del piloto; en el manifiesto, por PR, `[instancia].binding_version`, `[chatwoot].inbox_id` e `[inbound].scope_version`. Después, el redeploy del bridge. Entre `--aplicar` y el redeploy el bridge no resuelve su binding (`/ready` en 503 `commercial_ally_binding_unavailable`), así que se aplica con el PR y las variables listos y el redeploy va enseguida. Si hay eventos de Hotmart guardados con la versión vieja, `--aplicar` exige `--acepto-eventos-de-la-version-vieja`: al retirarla, su planificación deja de resolver el binding (paso 8, «Por qué se completa la fila»). Con `CONTENEDOR_BRIDGE`, `--verificar` mira que el binding activo tenga el inbox nuevo y que `/ready` dé 200 con `commercial_ally_binding: "active"`. **Volver atrás:** el mismo comando con el inbox de antes, que publica otra versión.
3. **La cuenta de Chatwoot**, en el panel:
   - *Settings → Labels:* `automation_paused` y `automation_opted_out`. Las etiquetas son por cuenta, y el bridge busca esos nombres exactos.
   - *Settings → Macros*, con visibilidad pública, y los tres IDs anotados en `bridge.env`: **Pausar** (`CHATWOOT_PAUSE_MACRO_ID`, agrega `automation_paused`), **Reanudar** (`CHATWOOT_RESUME_MACRO_ID`, quita `automation_paused`) y **No más mensajes** (`CHATWOOT_OPT_OUT_MACRO_ID`, agrega `automation_opted_out` y `automation_paused`). El bridge verifica el resultado después de ejecutarlos.
   - *Settings → Bots:* el AgentBot de la instancia, **sin vincularlo al inbox.** Vinculado, las conversaciones nacen en `pending`. Si el panel exige una *Webhook URL*, va una de relleno, y el script del punto 4 la vacía por la API.
   - *Settings → Teams:* el team de derivaciones, **solo con quien atiende las derivaciones.** El team reparte la conversación entre sus miembros.
   - En el inbox, **la asignación automática apagada.** Prendida, cada conversación nueva queda asignada a quien esté conectado, y el bridge no contesta una conversación con una persona asignada (`human_assignee_present`, sin una línea en el log). Pasó en Johanna el 2026-08-24 y en ATT1 el 2026-10-05.
4. **Los tokens de Chatwoot**, sin que pasen por un chat:

   ```sh
   ssh -t <servidor> 'python3 /opt/<stack>/instancia/despliegue/chatwoot/cargar-tokens-chatwoot.py'
   ```

   Pide solo el *Access Token* del perfil de un administrador de la cuenta (se pega sin que se vea; copiarlo con el botón, porque seleccionado a mano se copian los puntos de la máscara). Con él busca el bot por nombre, toma su token de la API, le vacía la URL, verifica que el inbox no tenga bot vinculado y escribe `CHATWOOT_AGENT_BOT_ID`, `CHATWOOT_AGENT_BOT_ACCESS_TOKEN` y `CHATWOOT_CONTROL_API_ACCESS_TOKEN`. Si el token o el bot no cierran, sale sin escribir.
5. **El webhook de la cuenta hacia el bridge.** Primero mirar, después aplicar:

   ```sh
   ssh <servidor> 'python3 /opt/<stack>/instancia/despliegue/chatwoot/apuntar-webhook.py'            # solo mira
   ssh <servidor> 'python3 /opt/<stack>/instancia/despliegue/chatwoot/apuntar-webhook.py --aplicar'
   ```

   Crea el webhook de la cuenta (o mueve el que ya existe) a `https://<dominio del bridge>/webhooks/chatwoot`, lo suscribe a `message_created` y `conversation_updated` (este avisa cuando una persona saca la etiqueta de pausa) y escribe en `CHATWOOT_WEBHOOK_SECRET` el secreto que devuelve Chatwoot, sin mostrarlo. Desde que cambia la URL, el bridge rechaza los eventos (`401 invalid_signature`) hasta que se redespliega con el secreto nuevo. Enseguida:

   ```sh
   ssh <servidor> 'python3 /opt/<stack>/instancia/despliegue/secretos/generar.py /opt/<stack>/secretos'
   ssh <servidor> 'cd /opt/<stack>/instancia && docker stack deploy --resolve-image never -c despliegue/stack.yaml <stack>'
   ssh <servidor> 'docker service update --force <stack>_appointment-bridge'   # el deploy no reinicia si el spec no cambió
   ```

6. **El host de Chatwoot que usa el bridge es el público** (`CHATWOOT_BASE_URL`). Con el interno fallan los audios: el bridge los baja solo del host de esa variable, y el adjunto trae el público (en ATT1 el primer audio derivó con `audio_url_host_not_allowed` hasta el cambio del 2026-10-05; setter-instancia-att1#19).

**Verificación:**
- `generar.py` dice `provisionales: ninguna`.
- Un mensaje cualquiera desde un teléfono de prueba entra al inbox y deja en el log del bridge `chatwoot_webhook_ignored reason=sender_not_allowed` (y no `invalid_signature`): con el entrante apagado no pasa nada más. En ATT1, el 2026-10-04 a las 23:05Z.
- La asignación automática del inbox figura apagada y el team de derivaciones tiene un solo miembro.

## 17. Prender los flujos de a uno

**El orden:** `inbound` → `carrito` → `pago_fallido` → el resto. El carrito va primero entre los de salida porque es el de más volumen y el que ya se probó de punta a punta en ATT1 (decisión del 2026-10-17). Cada flujo: `true` en el manifiesto (PR en el repo de la instancia), sus flags en `bridge.env`, el redeploy, una prueba controlada y la medición del efecto antes de pasar al siguiente. Desde acá `chequear.sh` deja de terminar en `INSTALADA`: lo que antes era `SALIDA` es lo que se prendió a propósito. El paso a paso con los comandos de la instancia está en `despliegue/camino-a-produccion.md`.

**El manifiesto es el techo:** un flag prendido con su flujo en `false` no deja arrancar al bridge. Y al revés, el flujo en `true` sin su flag no hace nada: el bridge mira el flag. Por eso el manifiesto se puede mergear antes que el flag.

**Cada flag se agrega con respaldo y se verifica:** antes de editar `bridge.env`, una copia fechada (`cp -p bridge.env bridge.env.<fecha>-antes-de-<flujo>`). Un nombre de variable mal escrito se ignora sin aviso, así que se cuenta: `grep -c "_ENABLED=true$" bridge.env` antes y después.

**Antes de abrir un flujo de salida a personas reales:** con el bridge 1.3.0 el agente no contesta en la conversación que abre una plantilla (la admisión entrante la rechaza) y lo único que funciona ahí es la baja. El bridge 1.3.1, con la `000400` y la `000500` aplicadas, adopta esa conversación y el agente contesta (ver *La respuesta a la plantilla* en `docs/contracts/portable-precheckout-first-contact-v1.md`). Ningún flujo de salida se abre con 1.3.0. La prueba controlada de cada flujo de salida incluye apretar los tres botones de la plantilla desde el teléfono de prueba y mirar qué queda en la base y en Chatwoot: un evento `inbound_adopted_template_conversation` en esa conversación, el enlace con la oferta de la intención para «Envíame el enlace», la respuesta o la derivación para «Necesito ayuda», y el opt-out para «No más mensajes». Mientras alguno no tenga respuesta, el flujo no se abre. Se aprieta recién con el intento de la plantilla en `accepted_by_chatwoot`: si el envío quedó en `delivery_unknown` y se responde antes de la reconciliación, la conversación no se adopta (límite conocido de 1.3.1, ver el CHANGELOG) y la prueba no prueba lo que tiene que probar. «Necesito ayuda» deja la conversación derivada y «No más mensajes» deja ese teléfono frenado para todo flujo de salida: los tres se aprietan en conversaciones o teléfonos distintos, o al final.

**Para cortar las respuestas, el interruptor es el agente.** Pausar o desarmar el scope del piloto frena los envíos, no la respuesta a una plantilla que ya salió. Para que el agente no conteste, se apaga `CHATWOOT_CUT_B_AGENT_ENABLED`. Un contacto dado de baja que responde a una plantilla vieja no se adopta: lo atiende una persona en Chatwoot.

**Las operaciones sobre el piloto** van por `despliegue/base/piloto.sh`, en el servidor, que llama a la función del producto que corresponde y no imprime datos personales:

| Operación | Qué hace | Escribe |
|---|---|---|
| `estado` | Los scopes publicados, cada control (versión activa, estado, generación, cohorte) y los últimos planes del primer contacto | No |
| `intenciones` | Las últimas intenciones: prefijo del id, landing, oferta, la forma del teléfono y los permisos. Nunca un número ni un email | No |
| `activar <scope> <versión> <quien> "<motivo>"` | Apunta el control a esa versión. Solo con el control en `inactive` o `paused`, y lo deja como estaba | Sí |
| `armar <scope> <versión> <quien> "<motivo>"` | Pasa el control a `armed`. **Con el flujo y sus flags prendidos, armar es lo que deja salir mensajes** | Sí |
| `pausar <scope> <versión> <quien> "<motivo>"` | Pasa el control a `paused`: frena los envíos de ese scope en el acto | Sí |
| `sembrar <scope> <versión> <intención> <quien>` | Deja el contacto de una persona de prueba y lo inscribe en la cohorte del scope | Sí |
| `cerrar-intencion <intención> <quien> cerrar-intencion-de-prueba` | Cancela la intención de una persona de prueba, para que su próximo formulario cree una nueva. Sin la frase exacta, mira y dice qué haría. **Nunca sobre la intención de un lead real: le quita la recuperación** | Sí, con la frase |

`<quien>` es el nombre corto de quien opera, y queda en el registro del control.

**Los flujos como configuración de la aliada.** Lo que sigue son los flujos que trae el producto crudo (el carrito de un toque y el primer contacto los siembra `aprovisionar.sql`, apagados). Los flujos de más de un toque de ATT1 (el carrito con A1, A2 y Z, las variantes del pago rechazado, la ficha de pago) son su configuración: se arman para cada aliada, y desde el motor de flujos, con el Automation Expert y el operador del almacén (`despliegue/base/operador-de-flujos.sh`).

### `inbound`, el entrante

No es un flujo de salida: el agente contesta a quien escribe primero. Va antes de cualquier plantilla, porque sin él un «No más mensajes» o un pedido de ayuda del lead se pierden. Antes, el conocimiento aprobado (firma 1) y quien atiende las derivaciones dada de alta y sola en el team (paso 16).

1. PR a `instancia.toml` con `inbound = true`.
2. En `bridge.env`, los flags del entrante: `HERMES_SHADOW_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED`, `CHATWOOT_CUT_B_ADMISSION_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`, `CHATWOOT_DURABLE_OPT_OUT_ENABLED`, `CHATWOOT_HUMAN_PAUSE_ENABLED`, `HUMAN_HANDOFF_ADMISSION_ENABLED`, `HUMAN_HANDOFF_PROJECTION_ENABLED`, `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`, `PAYMENT_LINK_ENABLED` y la reanudación, `CONVERSATION_RESUME_ENABLED=true` con `CONVERSATION_RESUME_MAX=10`. Sin la reanudación, una conversación derivada no vuelve nunca al agente. Para que la derivación le avise a la persona con un mensaje corto y fijo («Le paso tu consulta al equipo y te responden por este mismo chat.», sin plazo ni nombre), `HANDOFF_NOTICE_ENABLED=true` (F-034; exige `HUMAN_HANDOFF_ADMISSION_ENABLED`). Apagado, la derivación no le escribe nada. Para que una baja nueva reciba una sola línea de confirmación, sin vender, `OPT_OUT_CONFIRMATION_ENABLED=true` (E-107; exige `CHATWOOT_DURABLE_OPT_OUT_ENABLED` y `CHATWOOT_AUTOMATED_REPLIES_ENABLED`), con el texto de la instancia en `OPT_OUT_CONFIRMATION_TEXT` si no sirve el del producto («Listo, no te escribimos más.»): una línea, sin link, cupón, precio ni pregunta, o el bridge no arranca. Apagado, la baja se registra y no se contesta. Y `COMMERCIAL_KNOWLEDGE_ENABLED=true` con el conocimiento aprobado.
3. Los tres que completan el entrante:
   - `CHATWOOT_REPLY_SPLITTER_ENABLED`, el divisor de respuestas, con `HERMES_REPLY_SPLITTER_PROVIDER` y `HERMES_REPLY_SPLITTER_MODEL_NAME`. El proveedor tiene que tener credenciales en el Hermes de la instancia: en ATT1 va por OpenRouter con `z-ai/glm-5.3-flash` (ejemplo), porque su Hermes no tiene credenciales de Anthropic y con esa configuración el divisor manda la respuesta entera;
   - `CHATWOOT_STALLED_MONITOR_ENABLED`, el monitor de conversaciones trabadas, con los valores por defecto salvo `CHATWOOT_STALLED_MAX_PAGES` (constante del repo). Si un barrido falla, `/ready` da 503 hasta el siguiente; el healthcheck mira `/health`, así que eso no reinicia el bridge;
   - `CHATWOOT_AUDIO_TRANSCRIPTION_ENABLED`, la transcripción de audios, con `OPENROUTER_API_KEY` en `bridge.env` (la misma de `hermes.env`, copiada sin mostrarla). Sin el flag, un audio de un lead queda sin respuesta, sin derivación y sin una línea en el log.
4. Con Slack, los cuatro flags del bridge del paso 14.
5. El redeploy.
6. La prueba, desde un teléfono de prueba: que conteste con el conocimiento aprobado, que mande el enlace cuando se lo piden y que derive cuando se le pide una persona. Una nota de voz. Al final, y con **otro** teléfono, «No más mensajes».

**Verificación:**
- `/ready` da 200 y el log no muestra errores.
- El link emitido aparece en la base con su oferta (el que no tiene intención sale con la oferta por defecto, `default_no_intent`).
- La derivación aparece en el team de derivaciones, con la etiqueta `automation_paused` y una nota interna con el motivo. Con Slack, una tarjeta en el canal con el link a la conversación.
- El audio se transcribe y se contesta (o deriva con su motivo si no se pudo transcribir).
- El opt-out deja la etiqueta `automation_opted_out`, el agente no vuelve a escribir y `contact_opt_out_events` queda en `applied`. Ese teléfono queda frenado para todo flujo de salida: es el freno funcionando, y no sirve para las pruebas que siguen.
- En ATT1, el E2E pasó entero el 2026-10-05 entre las 11:37Z y las 11:54Z (setter-instancia-att1#13).

**Devolverle una conversación derivada al agente** exige tres cosas, y la reanudación se evalúa recién con el próximo mensaje del lead: que alguien del equipo le haya contestado al lead desde Chatwoot (eso marca la derivación como atendida), que la conversación no tenga agente asignado, y sacar la etiqueta `automation_paused` (a mano o con el macro «Reanudar»). Si no se saca, también vuelve sola cuando el lead escribe y el equipo lleva 8 horas sin escribir.

### La entrada del formulario de GHL (si los formularios son de GHL)

No es un flujo: nadie recibe un mensaje por esto. Es la entrada que deja una intención con consentimiento, y el carrito la necesita (ver abajo). El envío entra como `intencion` por el adaptador (`docs/contracts/ghl-precheckout-adapter-v1.md`), sin tocar la landing. Se puede prender el día 0: deja intenciones y no manda nada. En ATT1 se prendió el 2026-10-01 (setter-instancia-att1#7 y #8).

1. **Antes:** la base tiene la migración `20260930000200` y la fila del binding con `additional_offer_landings` (en una instancia desde la plantilla, ya las trae). Sin eso, solo entra el formulario de la landing por defecto y el resto da `503` (que GHL reintenta).
2. **Manifiesto** (PR en el repo de la instancia): `"intencion"` en `eventos` y `[adaptadores.ghl]` con el id de cada formulario (el `attributionSource.mediumId` del webhook, 20 letras o números). Un formulario se lista solo después de verificar que muestra la aclaración de `consentimiento.copy_version`: listarlo afirma `whatsapp_contact = true` para cada envío. `validate` tiene que dar verde.
3. **Secretos:** `GHL_PRECHECKOUT_ADAPTER_TOKEN`, aleatorio, de 32 caracteres o más y distinto de todo otro secreto del bridge (el arranque lo compara contra toda la configuración y nombra el que repite). Lo genera `generar.py` (paso 7). No rota en cada despliegue: GHL lo tiene copiado.
4. **Bridge:** `GHL_PRECHECKOUT_ADAPTER_ENABLED=true` y redespliegue. El proxy tiene que enrutar `/webhooks/adapters/ghl/lead-precheckout` al bridge.
   **Verificación:** `/ready` da 200 con `ghl_precheckout_adapter: "enabled:<n>-forms"`, con `n` igual a los formularios del manifiesto. Si el bridge no arranca, el error nombra lo que falta (`runtime flags exceed the instance manifest flows: ghl_precheckout_adapter_enabled->intencion` o `->adaptadores.ghl`, o el token), o dice que `[adaptadores.ghl] cannot run with flujos.precheckout` (o `flujos.pago_fallido`) `on without the written risk acceptance`: con la sección en el manifiesto, esos dos flujos exigen la aceptación escrita del riesgo (sección *Riesgos* del contrato).
5. **GHL:** en el workflow con disparador *Form submitted* filtrado por esos formularios, una acción *Webhook* `POST` a `https://<dominio del bridge>/webhooks/adapters/ghl/lead-precheckout`, con el token en *Custom Data* como `setter_token` (o en el header `X-Setter-Adapter-Token`, si la acción admite headers). Nunca en la URL. Tiene que ser la acción que manda el envío completo, con `attributionSource`: en ATT1 una acción nueva mandó el cuerpo sin él (`422 ghl_not_a_form_submission`) y se resolvió cambiando la URL de la acción original. Un formulario se suma al disparador **después** de entrar a la lista y del redeploy: antes, sus envíos reciben `422 ghl_form_not_allowed` y no dejan intención.
6. **Prueba controlada:** un envío real del formulario desde la URL de la landing, en una ventana de incógnito y directo a la landing. GHL pone en `attributionSource.url` la página donde empezó la visita: una prueba que pasó antes por otra página da `ghl_landing_unknown`.
   **Verificación:** la ejecución del workflow en GHL registra `200`; en la base hay una fila nueva en `precheckout_submissions` y la intención en `purchase_intents` con la `landing_ref` y la `offer_ref` de esa landing y `whatsapp_contact_authorized = true`. Un `422` dice por qué en el log del bridge (`ghl_form_not_allowed`, `ghl_landing_unknown`, `ghl_phone_unusable`) y no toca la base.

**La aceptación del riesgo, antes de cualquier flujo de salida.** Antes de prender `[flujos].precheckout`, `[flujos].pago_fallido` o una audiencia `consented_intent` o `consented_intent_in_cohort` (el carrito de abajo), el token es la única barrera (el id del formulario y las URL son públicos), así que hace falta una verificación fuera de banda de cada envío, que todavía no existe, o la aceptación del riesgo por escrito del responsable de la instancia (contrato, sección Riesgos). Desde el bridge 1.3.0 la aceptación son tres claves en `[adaptadores.ghl]`:

```toml
riesgo_aceptado_por = "<COMPLETAR: nombre de quien decide>"
riesgo_aceptado_el = 2026-10-01
riesgo_contrato = "ghl-precheckout-adapter-v1"
```

Las escribe a mano quien decide, por PR en el repo de la instancia: nadie más las completa, y mergear ese PR es la firma. El marcador entre `<` y `>` no carga: se reemplaza por el nombre. La fecha es la del día en que se acepta y no puede ser posterior a hoy. **Verificación:** `validate` informa `riesgo aceptado por … el … (contrato …)`, y después del redespliegue `/ready` da `ghl_adapter_risk: "accepted:<fecha>:<contrato>"`. En ATT1, firmada el 2026-10-04 (setter-instancia-att1#12, `1cee5fc`).

Sin ellas, con la sección en el manifiesto, el bridge no arranca con esos flujos prendidos ni con un scope del piloto de audiencia con consentimiento (`/ready` responde `503 ghl_adapter_risk_not_accepted`). Con `LANCEMOS_PILOT_BOUNDARY_ENABLED=true` y sin la aceptación, el arranque lee el modo de audiencia del scope antes de arrancar ningún worker: si la base no responde, falta la migración `20261001000300`, el scope no está publicado o es de audiencia con consentimiento, el bridge sale y el orquestador lo reinicia, y mientras tanto no recibe entrantes, bajas, formularios de GHL ni el webhook de Hotmart. Por eso la aceptación (o un scope `manual_cohort` publicado) y la `000300` van antes de prender la frontera. **La sección no se quita del manifiesto para esquivar la firma,** ni mientras haya en la base intenciones admitidas por el adaptador: quitarla saca la guarda, no las intenciones. Si el adaptador se apaga, se apaga con `GHL_PRECHECKOUT_ADAPTER_ENABLED=false` y la sección se queda.

### `carrito`

Le escribe una plantilla a quien abandona el checkout de Hotmart. **A quién le escribe lo decide el modo de audiencia del scope de recuperación:**
- `consented_intent_in_cohort`: solo a un contacto inscripto a mano en la cohorte **y** con una intención con consentimiento (un formulario con la aclaración de WhatsApp). Es el de la prueba.
- `consented_intent`: a todo carrito correlacionado con un formulario con permiso de WhatsApp, sin cohorte. Es el de la apertura.
- `manual_cohort`: a los contactos inscriptos a mano, sin exigir la intención del formulario.

Un carrito sin formulario, sin el permiso o de alguien que pidió «No más mensajes» no se planifica en los dos modos con consentimiento: queda `failed` con `pilot_scope_rejected:pilot_audience_intent_unresolved`, `…_consented_intent_not_authorized` o `…_consented_intent_prior_opt_out`, y no le llega nada. **El permiso para escribirle sale del formulario, no del checkout:** una aliada sin formularios con la aclaración no tiene carritos que el setter pueda cubrir, y la apertura a todos no le escribe a nadie (paso 0, «Las landings, los formularios y el consentimiento»). El `manual_cohort` sirve para las pruebas con los teléfonos de quien instala, no para leads.

El carrito y el pago fallido **comparten el scope de recuperación**: los dos salen con la versión que está armada y gastan los mismos topes. Por eso el carrito se abre con el pago fallido apagado (abajo).

**Antes de empezar:**
- `inbound` prendido y probado: la baja y la derivación son los frenos del flujo;
- Hotmart entrega al bridge, con el freno por compra prendido (paso 10);
- la plantilla del carrito aprobada, en `[plantillas].carrito`, y **el medio de pago de Meta cargado**. Sin él, Meta rechaza la plantilla con `131042`, y el envío gasta igual el tope del scope, porque el presupuesto cuenta arranques y no entregas;
- con `[adaptadores.ghl]`, la aceptación del riesgo (arriba);
- el teléfono de prueba con el agente: sin «No más mensajes», sin una compra y sin una conversación derivada.

**1. El repo al servidor**, desde `origin/main` recién traído, con `[flujos].carrito = true` mergeado. Solo, no manda nada.

**2. La versión de la prueba del scope de recuperación**, en `consented_intent_in_cohort` y con topes chicos, publicada con `despliegue/base/publicar-recuperacion.sql` y activada. Activar no arma: el control queda `inactive`.

```sh
ssh <servidor> 'sh /opt/<stack>/instancia/despliegue/base/piloto.sh activar <scope de recuperación> <versión de la prueba> <quien> "prueba del carrito; queda inactive"'
```

En ATT1 fue la v2 de `att1-recuperacion`, con 2 personas y 4 envíos (ejemplo). `LANCEMOS_PILOT_SCOPE_VERSION` (constante del repo) tiene que ser esa versión.

**3. Los nueve flags del carrito**, con respaldo: los seis comunes a toda salida y los tres del carrito.
- Comunes a toda salida: `LANCEMOS_PILOT_BOUNDARY_ENABLED`, `DURABLE_DISPATCHER_ENABLED`, `DURABLE_OUTBOUND_ENABLED`, `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`, `LEAD_FIRST_NAME_GREETING_ENABLED` (sin él la plantilla sale con el nombre completo tal como se escribió) y, al final, `META_FINAL_EFFECT_ENABLED`. El séptimo común, `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED`, ya está desde el paso 10.
- Del carrito: `RESOLUTION_WORKER_ENABLED`, `HOTMART_PURCHASE_WORKER_ENABLED` y `PORTABLE_HOTMART_RECOVERY_ENABLED`.
- Las variables que esos flags leen ya las escribió `generar.py`: `LANCEMOS_PILOT_SCOPE_KEY` y `_VERSION`, `LANCEMOS_PILOT_TENANT_KEY`, `LANCEMOS_PILOT_CHANNEL_PROVIDER`, `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF`, `FOLLOWUP_POLICY_KEY` y `_VERSION`, `WABA_FIRST_TOUCH_TEMPLATE_NAME`, `WABA_TEMPLATE_LANGUAGE`, `WABA_TEMPLATE_CATEGORY`, `DURABLE_DISPATCHER_WORKER_ID` y `ALLOWED_WHATSAPP_JID` vacío.

Después, el redeploy.

**Se verifica:**
- se reinicia solo el bridge, con la misma imagen, y el log no muestra errores;
- la cuenta de flags en `true` sube en nueve;
- `/ready` da 200 con `pilot_boundary: configured` y `automation_state: inactive`.

**Lo que el worker encuentra al prenderse.** Las compras aprobadas que el freno por compra guardó desde el paso 10 están en `received`, y el worker las toma apenas arranca. Sin casos de recuperación, las deja `failed` con `purchase_correlation_contact_not_found`, `_case_not_found` o `_contact_ambiguous`. Es lo esperado: no crean contactos, no tocan Chatwoot ni mandan nada. Desde acá, el carrito de un lead real queda `failed` con `pilot_scope_rejected:pilot_runtime_not_armed`, sin envío, y no queda en cola: el worker solo toma los eventos en `received`.

**4. La prueba controlada**, en el horario de envío de la instancia, con la identidad de envío:
1. El formulario de una landing con el teléfono de prueba, un email propio y los permisos.
2. `piloto.sh intenciones`: el prefijo de esa intención (la última, con la forma del teléfono de prueba).
3. `piloto.sh sembrar <scope de recuperación> <versión de la prueba> <prefijo de 8> <quien>`.
4. `piloto.sh armar <scope de recuperación> <versión de la prueba> <quien> "prueba del carrito"`.
5. Desde la misma landing, el checkout de Hotmart con el mismo email y el mismo teléfono, y cerrarlo sin pagar.

**Se verifica:**
- la base muestra el caso y un intento aceptado;
- llega la plantilla del carrito al teléfono;
- con el intento en `accepted_by_chatwoot`, se aprietan los tres botones (arriba): «Envíame el enlace» obtiene el link en la misma conversación, «Necesito ayuda» la respuesta o la derivación, y «No más mensajes» el opt-out.

Al terminar, `piloto.sh pausar <scope de recuperación> <versión de la prueba> <quien> "fin de la prueba del carrito"`.

**La corrida de compra**, con la **otra** identidad, sembrada e inscripta igual: formulario, checkout y compra antes del carrito. **Se verifica:** el caso queda `cancelled` y no hay ningún intento de envío. La identidad que compra queda frenada sin ventana (`purchase_by_identity`), por eso esta corrida va después de la de envío.

**Tres reglas de la prueba:**
- **La intención de una landing sirve 24 horas** para el carrito y el pago fallido, contadas desde el **primer** envío del formulario. Reenviarlo no la renueva. Para repetir pasado ese plazo: `piloto.sh cerrar-intencion` sobre la intención de prueba (primero `mirar`) y un formulario nuevo, que crea una intención nueva con el mismo contacto y la misma cohorte.
- **Mismo teléfono con otro email, no.** La admisión le saca los permisos a la intención.
- **Lo que también va a pasar:** si otro sistema todavía le manda su recuperación al mismo teléfono (por ejemplo, un workflow de GHL que se dispara con el abandono de Hotmart), llegan las dos. Se anota desde qué número llega: es lo que se corta en el punto 6.

En ATT1, el E2E del carrito fue el 2026-10-06 (setter-instancia-att1#23), y quedó registrado «Envíame el enlace» con respuesta en la misma conversación.

**5. Abrir a todos.** Una versión publicada no se edita: abrir es otra versión. Antes: la prueba pasó, el OK comercial para abrir el flujo, los topes decididos y quien corta el otro sistema disponible para el punto 6.
1. `piloto.sh pausar` la versión de la prueba. Sin pausar no se puede activar otra versión (`pilot_scope_version_change_requires_pause`).
2. Publicar la versión siguiente en `consented_intent`, igual a la de la prueba salvo el modo y los topes, con los topes aprobados como parámetros. El SQL no corre sin números: sin ellos aborta, no toca nada y dice cuántos arranques lleva el scope.
   - `tope_total` cuenta los arranques de **todas** las versiones del scope, también los de la prueba y uno que Meta haya rechazado con `131042`. Si no supera los ya gastados, la versión nace agotada.
   - `tope_por_dia` es por día en la zona del manifiesto, y también cuenta los arranques de la versión anterior ese mismo día.
   - **Se verifica:** una línea `publicacion OK: … publicada en consented_intent (total N, por dia M, …)` que dice que el control sigue en la versión anterior, `paused`.
3. `piloto.sh activar` la versión nueva. Queda `inactive`.
4. En un PR del repo de la instancia, `LANCEMOS_PILOT_SCOPE_VERSION` pasa a la versión nueva en las constantes de `generar.py`. Se mergea recién en este paso, porque `generar.py` reescribe sus constantes en cada corrida: con ese PR en el servidor antes de activar, cualquier corrida y redeploy dejarían el carrito sin planificar. Después, el repo al servidor, `generar.py` y el redeploy.
   **Se verifica:** `generar.py` dice `cambiaron en esta corrida: LANCEMOS_PILOT_SCOPE_VERSION`, con los mismos flags en `true` que antes (si nombra otra constante, mirar antes de redesplegar); se reinicia solo el bridge; `/ready` da 200 con `pilot_boundary: configured` y `automation_state: inactive`. Mientras la versión del bridge y la activa no coincidan, `/ready` da 503 `pilot_active_scope_mismatch` y un carrito queda `failed` con `pilot_scope_rejected:pilot_scope_version_mismatch` sin reintento: los puntos 3 y 4 van seguidos.
5. **Armar, con el pago fallido apagado.** Se mira antes que `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED` no esté en `true` en `bridge.env` y que `pago_fallido = false` en el manifiesto: con el scope compartido, la versión abierta lo abriría a todos. Después, `piloto.sh armar` la versión nueva.
6. **Enseguida, el corte de lo que ya le escribe a los leads** (paso 0): quien tiene el acceso corta la entrada de la recuperación del otro sistema, mirado en su panel (en GHL, borrando el disparador del workflow). Las personas que ya están adentro terminan su secuencia. Si se corta antes del punto 5, los carritos de ese rato no reciben nada; si se demora, un lead con permiso recibe los dos mensajes. ⚠ Cortar la entrada deja sin mensaje también a los carritos que el setter no cubre (sin formulario, sin permiso o con baja): es una decisión de cobertura, que se toma mirando qué parte de los carritos y de las ventas tiene una intención nuestra.

**Se verifica:** el próximo carrito real de un lead con permiso queda planificado, sin cohorte, y sale:
- la base muestra un caso nuevo de fuente `hotmart` y un intento aceptado, con la versión abierta en `armed`;
- en el inbox sale la plantilla del carrito, en el horario de envío (fuera de él, a la mañana siguiente);
- el otro sistema no le escribe: ese contacto no entra a su workflow, mirado en su panel.

En ATT1, el carrito se abrió el 2026-10-08 (setter-instancia-att1#27 y #29).

**Gastado un tope, no sale ningún carrito.** Los carritos cubiertos se siguen planificando, pero el arranque se rechaza (`pilot_total_budget_exhausted` o `pilot_daily_budget_exhausted`): el intento queda `reserved` y la acción vence al día. `/ready` sigue en 200 con `armed`, y el motivo queda en el log del bridge (`durable_request_start_rejected … reason=…`) y en la señal `tope` de las alertas (paso 12). Con el otro sistema cortado, esos carritos no reciben nada de nadie. Cuánto queda se mira con el SQL de publicación y `N` y `M` sin reemplazar: aborta sin tocar nada y dice los arranques gastados, en total y hoy. Antes de llegar al 80 % de `tope_total` se publica otra versión con más tope.

**Subir el tope es otra versión, y va sin carritos esperando, de día.** Cada caso queda atado a la versión con la que se planificó: uno de la versión anterior que todavía no arrancó queda rechazado con la nueva activa (`pilot_request_start_rejected:pilot_scope_version_mismatch`) y no se reintenta. De noche los carritos esperan al horario de envío atados a la versión anterior: cambiar de noche los perdería a todos. El orden: publicar la versión nueva con la anterior todavía armada (publicar no toca el control); pausar la anterior **solo si** no hay ninguna acción del scope en `pending`, `deferred`, `retryable_failed` o `delivery_unknown`; activar la nueva; la constante por PR, `generar.py` y el redeploy (frenando si cambia otra constante); y armar. Entre la pausa y el armado no sale ningún carrito: los pasos van seguidos, en unos minutos.

**Volver atrás:** `piloto.sh pausar` frena los envíos en el acto, y quien tiene el acceso reabre la entrada del otro sistema. **Nunca al revés:** con la versión armada, el tope del día se repone al día siguiente y los dos sistemas le escribirían a la misma persona. Para sacar los flags, el respaldo de `bridge.env` y el mismo `stack deploy`. El manifiesto puede quedar en `true`, porque sin el flag no hace nada.

### `pago_fallido`

Le escribe a quien tuvo un pago rechazado. Comparte el scope de recuperación con el carrito: sale con la versión armada y gasta sus mismos topes. Con el carrito ya abierto, prenderlo **lo abre a todos de una, sin cohorte**. Una prueba en cohorte necesitaría otra versión del scope, y mientras dure el carrito volvería a la cohorte, porque el bridge usa una sola versión para los dos flujos.

Qué cuenta como pago fallido: el `PURCHASE_CANCELED` 2.0.0 con status `CANCELED`, que el webhook ya recibe como «Compra cancelada». Se correlaciona con una intención viva de la misma oferta, dentro de las 24 h desde el primer envío del formulario. Sin formulario, el evento queda `failed` con `payment_failure_correlation_unresolved` y no sale nada. Otros casos de Hotmart, como una ficha de pago que vence sin pagar (`PURCHASE_EXPIRED`), no los atiende este flujo.

1. PR al manifiesto con `pago_fallido = true` y su plantilla en `[plantillas].pago_fallido`; con `[adaptadores.ghl]`, la aceptación del riesgo ya está. El repo al servidor.
2. En `bridge.env`, con respaldo, `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED=true`. Los comunes a toda salida y `RESOLUTION_WORKER_ENABLED` y `HOTMART_PURCHASE_WORKER_ENABLED` ya están desde el carrito. `WABA_PAYMENT_FAILURE_TEMPLATE_NAME` (constante del repo) tiene que ser la plantilla del manifiesto. Antes, se corta en el otro sistema lo que mande lo mismo (paso 0). El redeploy.

**Verificación:**
- la cuenta de flags en `true` sube en uno; se reinicia solo el bridge, con la misma imagen, y el log queda sin errores;
- `/ready` da 200 igual que antes. No tiene ninguna clave del pago fallido: solo prueba que el bridge aceptó el manifiesto con el flag;
- la prueba de verdad es el primer `PURCHASE_CANCELED` real en la base: el evento `processed`, su correlación, un caso con ancla `payment_failure` en la versión armada y su arranque, y la plantilla en el inbox. Si no tenía formulario, el evento queda `failed` con su motivo.

En ATT1 se prendió el 2026-10-09, abierto a leads reales sin un E2E propio (setter-instancia-att1#33).

**Lo que se acepta al prenderlo sin tope por persona:** a quien le falla el pago y después abandona el carrito le llegan las dos plantillas; la política no tiene demora, así que quien reintenta con otra tarjeta y compra la recibe igual; cada reintento de tarjeta posterior al envío abre otro caso. El tope de mensajes proactivos por persona entre flujos existe desde el producto 1.4.0 (migración `20261007000100`), viene apagado, y prenderlo hoy rompe la adopción de la conversación: un mensaje frenado por el tope queda pendiente, y mientras tanto la respuesta de esa persona a una plantilla anterior no se adopta.

**Volver atrás:** volver la línea del flag a `false` (queda el respaldo) y el mismo `stack deploy`. El manifiesto puede quedar en `true`: con el flag apagado, el webhook vuelve a contestar `200 ignored`. Lo que ya estaba planificado sale igual mientras la versión siga armada, porque el despachador no mira el flag. En el acto, solo si hay un daño en curso, `piloto.sh pausar`, que frena también el carrito.

### El resto

#### El primer contacto tras el formulario

Es la secuencia del flujo `precheckout` (`docs/contracts/portable-precheckout-first-contact-v1.md`): quien deja el formulario con consentimiento y no llega al checkout recibe un único mensaje, demorado, con la plantilla de `[plantillas].precheckout`. En ATT1 se abrió el 2026-10-09 (setter-instancia-att1#31).

1. **Antes:** la entrada del formulario ya admite intenciones (arriba, o `/webhooks/lead`), `inbound` está prendido y probado (el opt-out y la derivación son los frenos del flujo), y Hotmart ya entrega la compra aprobada al bridge con `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED=true` y el hottok cargado.
2. **Base:** la política del flujo (propósito `cart_recovery`, un paso `first_contact`, la demora en `grace_period`) y un scope del piloto **propio**, distinto del de recuperación: fuente `landing`, evento `PRECHECKOUT_FORM_SUBMITTED`, audiencia `consented_intent_in_cohort` para la primera prueba, con el control en `inactive`. El aprovisionamiento del paso 8 ya lo siembra.
   **Verificación:** `pilot_scope_versions` tiene el scope publicado y `pilot_runtime_controls` lo tiene en `inactive` (`piloto.sh estado`).
3. **Manifiesto** (PR en el repo de la instancia): `precheckout = true` en `[flujos]`, con su plantilla en `[plantillas]` y sus `parametros` iguales a los del cuerpo aprobado. Con `[adaptadores.ghl]`, la aceptación del riesgo. `validate` tiene que dar verde.
4. **Servicio:** `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED=true`, `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY`, `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION` y `WABA_PRECHECKOUT_TEMPLATE_NAME` (igual a `plantillas.precheckout.nombre`), además de la frontera del piloto, el dispatcher, la salida durable y el modo directo con sus `WABA_*`. Si se prende antes que el carrito, `META_FINAL_EFFECT_ENABLED` sigue en `false` hasta el paso 7.
   **Verificación:** `/ready` da 200 con `portable_precheckout_first_contact: "inactive"`. Un `503 portable_precheckout_pilot_scope_config_mismatch` quiere decir que el scope del paso 2 no está publicado para este flujo; el healthcheck del contenedor usa `/health`, así que el bridge no se reinicia por eso. Si el bridge no arranca, el error nombra la variable que falta.
5. **Formulario con el scope desarmado.** Un envío real del formulario con un teléfono de prueba.
   **Verificación:** hay un renglón en `portable_precheckout_first_contact_plans` con `outcome = 'not_planned'` y `reason_code = 'pilot_runtime_not_armed'`, y ninguna fila nueva en `contacts`. Ese envío no se planifica después.
6. **Armar.** Sembrar el contacto de prueba, inscribirlo en la cohorte del scope y armar el runtime del scope.
   **Verificación:** `/ready` da `portable_precheckout_first_contact: "armed"`.
7. **Corrida de envío.** Con `META_FINAL_EFFECT_ENABLED=true`, reenviar el formulario (armar no planifica el envío que llegó desarmado: lo planifica el reenvío) y esperar la demora.
   **Verificación:** el renglón nuevo dice `planned` / `first_contact_scheduled`; la acción tiene `due_at` igual al envío más la demora; llega la plantilla al teléfono, se aprietan los tres botones y la respuesta cae en la misma conversación de Chatwoot.
8. **Corrida de compra antes de la demora**, con **otro** teléfono y otro email de prueba, sembrados e inscriptos igual.
   **Verificación:** el caso queda `cancelled` con `intent_purchased` y no hay ningún intento de envío. El orden importa: la identidad que compra queda frenada sin ventana (`purchase_by_identity`), así que la corrida de envío va primero y la de compra usa otra identidad.
9. **Abrir.** Un scope publicado no se edita: se pausa, se publica la versión siguiente con `audience_mode = 'consented_intent'` y los topes aprobados, se activa, se apunta `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION` a esa versión por PR, se redespliega y se arma. **Con el primer contacto y el carrito abiertos a la vez:** el carrito reemplaza al primer contacto si llega antes, pero no al revés, y quien recibe el primer contacto y abandona el carrito más tarde recibe los dos. Antes de tenerlos abiertos a la vez hay que decidir el tope de mensajes proactivos por persona: hoy el producto no lo aplica entre flujos (arriba).

#### El seguimiento con cupón y la reactivación

Van recién con el carrito andando, cada uno con su flag:
- **El seguimiento con cupón** (`[flujos].descuento`, desde el producto 1.5.0; `docs/contracts/conversation-followup-discount-v1.md`): `CONVERSATION_FOLLOWUP_ENABLED`, con las siete `CONVERSATION_FOLLOWUP_*` que escribe `generar.py`. No pasa por la frontera del piloto, sus topes ni `META_FINAL_EFFECT_ENABLED`: se frena volviendo su flag a `false` y redesplegando. Para la prueba, `CONVERSATION_FOLLOWUP_ONLY_PHONE` con el teléfono de prueba (en `bridge.env`, nunca en el repo) y `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS=900`, que se sacan al abrirlo. Vacía, `CONVERSATION_FOLLOWUP_ONLY_PHONE` es lo mismo que no tenerla: le escribe a todo el inbox.
- **La reactivación** (`[flujos].reactivacion`; `docs/contracts/conversation-reactivation-v1.md`): `CONVERSATION_REACTIVATION_ENABLED` con `WABA_REACTIVATION_TEMPLATE_NAME` y `_LANGUAGE`. Su envío no pasa por `META_FINAL_EFFECT_ENABLED`.

**Verificación:** la de cada contrato, con el teléfono de prueba, antes de abrirlo.

#### La revisión diaria

Es otro servicio del stack (`setter-daily-feedback`, de la misma versión del producto), con su propio archivo de secretos (`revision.env`, que escribe `despliegue/revision-diaria/generar-revision.py`). Pide cuatro revisores con usuario de Slack, la app de Slack para entrar (con la URL de vuelta `https://<dominio del bridge>/daily-feedback/auth/slack/callback`), y la aceptación escrita del disco sin cifrar (ADR 0024). **Nace apagada en la plantilla:** su servicio comentado en `stack.yaml`, `[revision_diaria]` comentada en `instancia.toml`, y `verificar.py` exige que se prenda todo junto, en un PR. Las conversaciones son de los leads de la aliada, y se prende cuando el responsable de la instancia firme. Su scheduler nace en `false`, y sin él no corre la purga de las 72 h. Los pasos están en «La revisión diaria» del README de la instancia; el contrato, en `docs/contracts/daily-feedback-production-v1.md`.

**Verificación:** `/ready` del servicio da 200 con `"daily_feedback":"operational"` y sus excepciones declaradas, y `chequear.sh` lo ve en el punto 3.
