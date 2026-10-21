# Contrato: la definición de un flujo (v1)

- **Estado:** aceptado como contrato conceptual el 2026-10-15; sin implementación (lo construye la Etapa 1b). El esquema no describe nada desplegado ni activado. Lo que dice de ATT1 es la línea de base de la migración, con su fecha y su evidencia. Enmendado el mismo día, antes de construir nada: cada flujo es de un producto de la instancia (ver «El producto de un flujo»). **Versión 1.1 del 2026-10-21 (la T2.1 del motor):** varios envíos por camino, `cupon: personal` y las bolsas separadas del carrito y el pago fallido (ver «Historial», al final).
- **Fecha:** 2026-10-15
- **ADR:** [ADR-0007](../decisions/0007-durable-next-action-engine.md) y [ADR-0025](../decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md) §7
- **Esquema:** `setter-flujo/v1`
- **Alcance:** la forma fija con la que se escribe un flujo de una instancia como dato: sus cinco piezas, la prioridad frente a los otros flujos, la puerta común que revisa cada envío y las reglas del validador. El ciclo de una versión, el contrato de lectura y el dibujo de un flujo van en sus propias piezas (ver «Lo que no entra en la v1, y dónde va»).
- **Etapas:** las del roadmap macro del producto, que lleva Dan fuera de este repo. No son las etapas del onboarding de [first-infoproducer-onboarding-v1.md](../design/first-infoproducer-onboarding-v1.md). La Etapa 1 se parte en tres (decisión de Dan del 2026-10-13): la 1a, el Automation Expert que lee y explica los flujos; la 1b, los flujos como datos, con su motor y su ciclo de versiones; y la 1c, los flujos nuevos, primero la ficha sin pagar y después la activación. La Etapa 2 es el producto instalable, con varios productos por instancia, y la Etapa 4, el catálogo de Lancemos, que trae los lanzamientos.
- **Citas:** las citas de código son de `origin/main` en `1b83191` (1.8.0). El 2026-10-14, ATT1 corría la v1.6.1 (`5723806`; `instancia.toml:21`). Entre la v1.6.1 y `1b83191`, en `src/` y `supabase/` solo cambiaron tres archivos de la revisión diaria, y ninguno de los que cita este contrato. Las migraciones de `supabase/migrations/` se citan por su prefijo. `instancia.toml`, `aprovisionar-att1.sql`, `publicar-recuperacion-v5.sql`, `publicar-primer-contacto-v2.sql` y `generar.py` son de la instancia ATT1 (`setter-instancia-att1`), en `b1cdefe`. En este contrato, «hoy» es ese código y la instancia ATT1 al 2026-10-14. Lo que cita «El producto de un flujo» no cambió hasta `c3a61b2`.
- **Relacionados:** [followup-engine-v1.md](followup-engine-v1.md), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md), [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md), [conversation-followup-discount-v1.md](conversation-followup-discount-v1.md), [conversation-reactivation-v1.md](conversation-reactivation-v1.md), [resume-paused-conversation-v1.md](resume-paused-conversation-v1.md), [instance-runtime-v2.md](instance-runtime-v2.md), [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md), [executable-human-handoff-v1.md](executable-human-handoff-v1.md), [lead-precheckout-v1.md](lead-precheckout-v1.md), [hotmart-cart-abandonment-v1.md](hotmart-cart-abandonment-v1.md), [hotmart-purchase-approved-v1.md](hotmart-purchase-approved-v1.md), [referencia-manifiesto.md](../referencia-manifiesto.md) y el diseño [automation-expert-configuration-experience.md](../design/automation-expert-configuration-experience.md).

## Para qué

Este es el formato fijo con el que se escribe cualquier flujo como dato. No es un formulario ni una pantalla, y nadie lo llena a mano. Lo completa el Automation Expert conversando con quien opera la instancia (en ATT1, Brayan o Dan). El agente pregunta lo que falta, propone en voz alta un valor por defecto para lo que nadie nombró y discute con datos. La conversación termina en un borrador para aprobar. Ese resultado tiene tres usos:
- se guarda con versión en la base (1b);
- el lector de la 1a lo devuelve cuando alguien pregunta qué flujos hay y qué le llega a una persona, y por qué;
- la skill de la 1a lo dibuja.

Una versión publicada no se modifica. El YAML sirve solo para leerla y para comparar dos versiones. Este documento fija las piezas y sus reglas. Cómo cierra la conversación, cómo se dibuja un flujo y con qué motivos se explica por qué a alguien no le llegó nada van en sus propias piezas (ver «Lo que no entra en la v1, y dónde va»).

## En corto

- Un flujo se arma y se cambia solo conversando con el Automation Expert. No hay formulario.
- Tiene cinco piezas: qué lo dispara, quién entra, los pasos, cuándo termina, y el horario con los topes. También lleva su prioridad frente a los otros flujos y su versión.
- Cada flujo es de un producto de la instancia, y solo lo disparan los eventos de ese producto. Con un solo producto no hace falta escribirlo. La puerta y el tope por persona son de la instancia y suman todos los productos.
- Lo dispara un aviso de Hotmart, el formulario de la landing o una conversación que se calló. No hay otros disparadores.
- Hay cinco tipos de paso: esperar, mandar una plantilla, que el agente converse con un objetivo, una condición con dos ramas y pasarle la conversación a una persona. Mientras el motor no cambie, sale un solo mensaje por camino, y conversar y derivar se pueden escribir pero no publicar.
- El flujo termina si la persona compra, si contesta (ahí sigue el agente) o si vence. La baja y que una persona tome la conversación lo cortan siempre, desde el código.
- La prioridad se escribe con la frase de quien opera: «el carrito le gana al primer contacto».
- Todo envío de cualquier flujo pasa por una sola puerta en código, igual para todos, que revisa doce cosas en orden. El tope por persona cuenta los envíos de todos los flujos, cupón y reactivación incluidos.
- Conversando se cambia lo que es dato: las esperas, la plantilla, el horario, los topes, prender o pausar, el objetivo y la prioridad. Lo que protege a la persona y al número queda en código.
- Cuando algo no se puede, el agente dice si es una protección, que no va a cambiar, o algo que todavía no existe.
- Los cuatro flujos que corren hoy se escriben tal como corren. Lo que cambia al migrar queda en una lista, y se mide antes.
- Lo que todavía no se sabe queda «a medir», y una versión con un valor así no se publica.

## Las cinco piezas

El Automation Expert sigue los pasos de su diseño, aceptado el 2026-08-07. Interpreta el pedido, pregunta lo que falta, lo compara con los datos y discute si hace falta. Después propone y pide el sí. Recién entonces arma la definición y la pasa por el validador (`automation-expert-configuration-experience.md:15-28`).

Arranca desde la versión publicada, que le da el lector de la 1a, o desde una vacía. Completa las piezas en el orden en que la persona las cuenta, con una pregunta por vez, y siempre ofrece el valor por defecto.

Para cada pieza se explica qué es, qué admite en la v1, su valor por defecto, qué pregunta el agente y qué queda en código.

### 1. Qué lo dispara

**Qué es.** Es lo que tiene que pasar para que el flujo arranque para una persona. Cada flujo tiene un solo disparador, y un disparador distinto es otro flujo. Hoy el carrito y el pago fallido comparten un scope con dos disparadores (`publicar-recuperacion-v5.sql:23-28`). En el esquema son dos flujos que comparten la bolsa de topes. Desde el 2026-10-19 el pago fallido tiene su propio scope, `att1-pago-rechazado`, y ya no comparten bolsa (1.1).

El disparo es el momento del evento, o el del barrido que encontró el silencio. Desde ahí se cuentan las esperas y el plazo. En un evento de Hotmart es su `creation_date` (`resolution.py:283-286`), que llega de 30 a 40 minutos después de lo último que la persona hizo en el checkout (medido en ATT1 el 2026-10-06).

**Qué admite en la v1.** Siete disparadores, de dos tipos.
- Un evento (`evento`):
  - `carrito`: el carrito abandonado de Hotmart. Llega de 40 a 52 minutos después del formulario (medido en ATT1 el 2026-10-09).
  - `pago_fallido`: la compra cancelada, por cualquier motivo. Llega unos 10 segundos después del rechazo (registrado al prender el pago fallido en ATT1, el 2026-10-09).
  - `ficha_impresa`: la persona generó la ficha de pago. En ATT1 hubo de OXXO, Mercado Pago, Yape y Nequi. Hoy el evento se guarda sin correlacionarlo con nada, y el worker lo deja en `failed` (`20261012000100:24-31`; `worker.py:2393-2401`).
  - `ficha_vencida`: la ficha venció sin pagarse. Hasta el 2026-10-14 a las 22:51Z no había llegado ninguna a att1-db.
  - `compra`: la compra aprobada. Hoy solo cierra casos. Como disparador tiene que funcionar también cuando la venta no tiene un caso del recuperador.
  - `intencion`: el formulario de la landing, en el envío 1.1.0 y con los dos consentimientos.
- Un silencio de la conversación (`silencio`), que detecta un barrido de Chatwoot:
  - `lo_ultimo` puede ser `agente` o `persona`. Con `agente`, el último mensaje es del AgentBot y nadie del equipo escribió después del último mensaje de la persona. Con `persona`, la persona escribió último y nadie le contestó.
  - `desde` y `hasta` marcan la edad del último mensaje de la persona. El máximo es 30 días.
  - `conversaciones` puede ser solo las abiertas, o las abiertas y las resueltas.

**Qué datos trae cada uno para las plantillas.**
- Todos traen `nombre` y `producto`, que hoy son las únicas variables (`messaging.py:79-81`).
- El `nombre` sale de un lugar distinto según el disparador, y eso queda en código:
  - con un evento, sale del caso: el nombre del formulario o de Hotmart, con la cadena del saludo y su filtro (`worker.py:1023-1056`);
  - con un silencio, sale del contacto de Chatwoot, que es el nombre del perfil de WhatsApp. El cupón le aplica la misma cadena (`followup_discount.py:464-467` y `:901-906`), y la reactivación, su propia regla (`reactivation.py:363-365`);
  - unificar la fuente cambiaría el saludo del cupón, así que no se hace sin una versión aparte.
- El evento de compra trae el nombre (`data.buyer.name` y `first_name`, en el fixture `hotmart_cash_payment_approved_att1_20261012.json`), pero el bridge todavía no lo extrae (`HotmartPurchaseData`, `hotmart.py:64-77`). Extraerlo es código de la 1b.
- La ficha suma `medio_de_pago`. En la plantilla va el nombre del medio como lo conoce la persona en su país (OXXO, Mercado Pago, Yape, Nequi), traducido en código desde el código de Hotmart (`CASHPAYMENT`, `MERCADO_PAGO`…). Hoy el bridge pondría el código tal cual llega (`messaging.py:191-214`), y una decisión de Dan del 2026-10-12 pide no fijar los medios de pago de un país.
- Solo el pago en efectivo trae el link de la ficha (`billet_url`), que es el link del botón `ficha` (medido en att1-db el 2026-10-14, en las 6 fichas de ATT1).
- El silencio trae el link de checkout que emite la base con la oferta, el `sck` y el `fbclid` de la persona. Es el link del botón del cupón (`conversation-followup-discount-v1.md:62-74`).

**Por defecto.** No tiene. Es la única pieza sin valor por defecto.

**Qué pregunta el agente.** «¿Qué tiene que pasar para que arranque?» Traduce la respuesta a un disparador del catálogo y la confirma con su frase y con un número medido en la semana. Si es un silencio, pregunta además: «¿lo último lo dijimos nosotros o la persona?» y «¿desde cuántas horas, y hasta cuándo?». Si alguien sube el `hasta`, avisa que el primer barrido toma de una vez las conversaciones que ya habían pasado el límite viejo sin recibir nada: el techo de 72 h del cupón está para no escribirle a todo el histórico (`followup_discount.py:56-58`).

**Qué queda en código.**
- Los adaptadores de Hotmart y de la landing deciden qué llega, con qué datos y de qué producto es. Un evento de un producto o de una oferta que la instancia no declara no entra, y a cada flujo le llegan solo los de su producto (ver «El producto de un flujo»).
- Hoy los eventos de la ficha entran solo con el flag del pago fallido (`app.py:7655-7658` y `:7676-7682`). Con el esquema, cada flujo se prende con su control, y los adaptadores dejan de atar la ficha a ese flag.
- El barrido de los silencios: cada cuánto corre, cuántas conversaciones lee y cuántos envíos hace como máximo por barrido (por defecto, 10: `followup_discount.py:625` y `reactivation.py:454`). Una conversación silenciada o pospuesta no entra.
- Qué eventos recibe la instancia. Hoy es la lista cerrada del manifiesto (`EVENTS`, `instance_manifest.py:67`). Ver «El techo de la instancia», más abajo.

### 2. Quién entra

**Qué es.** Las condiciones que la persona tiene que cumplir para que el flujo arranque. La puerta las vuelve a revisar antes de cada envío.

**Qué admite en la v1.**
- `permiso` es la base con la que se le escribe a la persona. Tiene tres valores:
  - `formulario`: la persona dejó el formulario con el permiso de WhatsApp. Va con cualquier evento. Hoy es el modo `consented_intent` (`20260930000300:75-86`).
    - Con el carrito y el pago fallido, esa intención se cruzó con el evento en las 24 h anteriores (`aprovisionar-att1.sql:247-249`).
    - Con la ficha, ese cruce todavía no existe: la admisión guarda el evento «sin correlacionar ni planificar nada» (`20261012000100:24-31`). Construirlo es parte de la 1c. La ficha vencida no busca otra intención: usa la que se cruzó con la ficha impresa de la misma compra.
    - Con la compra, el cruce existe solo para frenar intenciones y cerrar casos, con 7 días hacia atrás (`aprovisionar-att1.sql:250-253`). Usarlo como permiso es parte de la 1c.
    - Un flujo que usa una intención como permiso entra en la guarda del riesgo de GHL, como hoy el primer contacto y el pago fallido (`GHL_RISK_GATED_FLOWS`, `instance_manifest.py:63-65`). La ficha tiene que sumarse ahí.
  - `conversacion`: la persona nos escribió al menos una vez en esa conversación. Va solo con un silencio. Es la base del cupón y de la reactivación.
  - `compra`: la persona compró el producto, aunque nunca haya dado permiso de WhatsApp. Va solo con el disparador `compra`, y solo para un mensaje de utilidad que confirma la compra, y uno solo: un envío por camino (regla 17). Nunca es el valor por defecto. Una instancia lo usa recién con el OK de su cliente, porque la base de contacto es responsabilidad del cliente: «el cliente es responsable de configurar una base válida para su operación y jurisdicción» (ADR-0007 §8). El mismo apartado dice que «la ausencia de opt-out no autoriza por sí sola un mensaje proactivo». Cómo se registra ese OK lo decide el contrato del ciclo. Sin ese OK, un flujo disparado por la compra entra con `permiso: formulario`: la bienvenida le llega solo a quien dejó el formulario con el permiso de WhatsApp.
- `contesto_a` es una lista de flujos. Con `permiso: conversacion`, el flujo entra solo donde la persona contestó una plantilla de esos flujos y el agente tomó la respuesta. Es la conversación adoptada que hoy exige el cupón (`20261010000100:22-32`).

«No haber comprado» se escribe con la salida `compra`, que se revisa al entrar y antes de cada envío. «Haber comprado» se escribe con el disparador `compra`.

**Por defecto.** `conversacion` con un silencio, y `formulario` con cualquier evento, la compra incluida: la decisión de Dan del 2026-10-14 pide también en la activación «tener teléfono y permiso de WhatsApp». `contesto_a` va vacío.

**Qué pregunta el agente.** Solo pregunta si hay que dejar a alguien afuera. Si el disparador es un silencio y lo último lo dijimos nosotros, pregunta: «¿a cualquiera que nos haya escrito, o solo a quien contestó una plantilla nuestra?». Confirma la respuesta con cuántas personas habrían entrado la semana pasada.

**Qué queda en código.**
- Que el teléfono se pueda leer.
- Que la persona esté resuelta en las dos formas del móvil.
- Que no esté dada de baja ni bloqueada.
- Los productos de la instancia y sus ofertas, que salen del manifiesto.
- Qué evidencia vale para cada permiso. Un permiso desconocido no habilita el envío (ADR-0007 §8).
- Con el formulario, que la intención siga viva y que el envío traiga los dos consentimientos (`20261001000200:211-229`).

### 3. Los pasos

**Qué es.** Una lista que se lee de arriba abajo, sin bucles ni saltos. El ADR-0007 §4 pide arrancar sin DAG ni DSL (`0007-durable-next-action-engine.md:96-97`).

**Qué admite en la v1.** Cinco tipos de paso:
- `esperar`: una duración. Se cuenta desde el paso anterior, y la primera espera se cuenta desde el disparo. El motor la guarda como hoy: la primera espera como gracia y las siguientes como plazos fijos desde el primer envío aceptado (`followup-engine-v1.md:68-81`). Si un envío sale tarde por el horario, el siguiente no se corre, así que puede quedar más cerca de lo pedido. Los máximos de frecuencia ponen el límite.
- `plantilla`: una plantilla aprobada, con su `idioma` y sus `variables` en el orden {{1}}, {{2}}… (`nombre`, `producto`, `medio_de_pago`, y desde la 1.1 `cuando`, `precio` y `vence`, ver la definición formal). Si la plantilla tiene un botón de URL dinámica, lleva también el `boton`: `checkout` (con `cupon` opcional: un código fijo, o `personal`, el código por persona que crea el worker del cupón en Hotmart y que vence; `personal` va a lo sumo dos veces por camino) o `ficha`. Mientras la plantilla no exista, se escribe `{a_crear: <nombre propuesto>}`, así al publicar se sabe cuál buscar en el catálogo.
- `conversar`: un objetivo de hasta 300 caracteres. El agente comercial escribe con toda la conversación, su SOUL, su conocimiento y ese objetivo. El bridge valida lo que el agente propone antes de mandarlo (ADR-0007 §3). Este paso va dentro de la rama de una condición con la ventana abierta.
- `condicion`: un `dato` y los valores que lo cumplen, con dos ramas, `entonces` y `si_no`. En la v1 hay dos datos posibles:
  - `ventana`: abierta o cerrada, como la informa Chatwoot;
  - `medio_de_pago`: solo con los disparadores de la ficha. Se compara con el código de Hotmart: `CASHPAYMENT` es el pago en efectivo, OXXO en México.

  Una condición no puede llevar otra adentro.
- `derivar`: una nota para el equipo de la instancia. Hace lo mismo que la derivación de hoy: pausa la conversación, le pone la etiqueta, la asigna al equipo, deja la nota y manda la tarjeta a Slack (ADR-0010). El flujo termina en ese paso.

**Lo que la v1 todavía no ejecuta.** El motor manda los envíos de su flujo del legado, en modo plantilla directa: hasta tres por camino, con la espera entre uno y otro (1.1; las capacidades del motor, `flow_engine.ENGINE_CAPABILITIES_V1`, y la forma de cada flujo, `flow_policy.STEPS_BY_LEGACY_KIND`). No le hace llegar un objetivo al agente, y no tiene cómo ejecutar `derivar` fuera de la conversación: hace falta una política de derivación aparte para el motor. Por eso, hasta que la 1b los construya, más envíos por camino de los que ejecuta el motor para ese flujo, `conversar` y `derivar` se pueden escribir en un borrador pero no se publican.

**Por defecto.**
- Un solo paso: una plantilla aprobada, sin espera.
- Con el formulario, la espera de hoy, de 60 minutos.
- Con la ficha impresa, la primera espera no tiene valor por defecto: mandar el recordatorio apenas se imprime la ficha no es un seguimiento, así que el agente la pregunta siempre.

**Qué pregunta el agente.** «¿Qué le llega, y cuándo?», mostrando las plantillas aprobadas con su texto, sus variables y su categoría. Si ninguna sirve, lo dice y explica lo que hace falta:
- redactar la plantilla;
- que el cliente ratifique el texto (en ATT1, Marce);
- que el cliente decida si va como utilidad o como marketing (en ATT1, Juan y Marce);
- que Meta la apruebe, lo que tarda de horas a semanas.

Mientras la plantilla no esté aprobada, el flujo queda en borrador. «¿Y si no contesta?» solo lo pregunta si la persona habla de insistir, y en ese caso explica que un segundo mensaje todavía no se puede.

**Qué queda en código.**
- El texto de cada plantilla, que aprueba Meta, y su categoría, que se lee del catálogo.
- La cantidad exacta de variables. Si no coincide, Meta rechaza el envío después de que Chatwoot lo aceptó (`conversation-followup-discount-v1.md:87`).
- El filtro del nombre: hasta 60 caracteres, sin URL, mail, dominio ni dígitos.
- La traducción del medio de pago a su nombre.
- Que `conversar` salga solo con la ventana de 24 h abierta.
- El efecto de `derivar`, y que la conversación derivada no se suelta sola.
- Los máximos de frecuencia. En la v1 son dos: los envíos que ejecuta el motor para ese flujo y el tope por persona de la puerta.

### 4. Cuándo termina

**Qué es.** Las salidas del flujo. Cuando se cumple una, el recorrido termina y no manda lo que le faltaba.

**Qué admite en la v1.** En `termina.si` hay tres salidas posibles:
- `compra`: una compra aprobada de la persona en el producto del flujo, o una compra que podría ser suya y no se pudo atribuir.
- `contesta`: la persona escribe en el inbox después del disparo y antes del primer envío. El recorrido se corta en el momento en que llega el mensaje, antes de que la admisión decida si el agente la toma, y la conversación sigue con el agente.
- `ficha_vencida`: llegó el vencimiento de la ficha. Solo vale si el disparador es `ficha_impresa`.

Además está `vence`, un plazo que se cuenta desde el disparo. Con un silencio no se escribe, porque el plazo lo marca `hasta`.

Después del primer envío aceptado, una respuesta de la persona corta siempre lo que falta, tenga o no el flujo `contesta`. Lo dice el ADR-0007 §6 y queda en código, como hoy (`prospect_replied`, `20260805000300:402-404`).

**Lo que `contesta` resuelve, y lo que no.** Hoy la respuesta a una plantilla no se adopta mientras la persona tiene otro paso pendiente, y el agente no le contesta. Es la regla de adopción de `20261001000400` (su «decisión 6», :43-47). Como `contesta` termina el recorrido antes de que esa regla lo mire, en los flujos que la llevan ese freno desaparece. La regla en sí no cambia. En los flujos sin `contesta`, un envío que espera, por una espera o por el horario, sigue frenando la respuesta hasta que sale o vence. Es un límite conocido y todavía sin decidir: la propuesta del 2026-10-09 que cancela la plantilla que supera el tope por persona lo deja para una tarea propia. Los tres flujos de hoy por evento no llevan `contesta`, para no cambiar nada. Sumársela es una versión aparte, con su propia aprobación.

**Por defecto.**
- Con un evento: `compra` y un día de plazo.
- Con un silencio: `compra`.
- Con la compra como disparador: ninguna salida.

**Qué pregunta el agente.** Pregunta el plazo. Si hay una espera antes del primer envío, pregunta si una respuesta en ese rato tiene que cortar el flujo, y explica qué pasa si no: «mientras espera, si la persona contesta otra plantilla nuestra, el agente no le responde hasta que este mensaje salga o venza». Si el disparador tiene un evento que es su par, como la ficha impresa y la vencida, pregunta si ese evento corta el flujo.

**Qué queda en código.** Estas salidas cortan siempre, sin escribirse:
- la baja;
- que una persona tome la conversación: el recorrido termina y no se reanuda solo; si después llega otro disparo, arranca un recorrido nuevo (ADR-0007 §6 y §7);
- una respuesta después del primer envío aceptado;
- que otro flujo le gane;
- que no quede ningún horario antes de vencer;
- que ya se hayan mandado todos los pasos.

También quedan en código cómo se detecta la compra y si el recorrido queda ganado o cancelado. El primer contacto, por ejemplo, queda cancelado y nunca ganado (`portable-precheckout-first-contact-v1.md:15`).

### 5. El horario y los topes

**Qué es.** Cuándo puede salir un envío, y cuántos pueden salir.

**Qué admite en la v1.**
- `horario` puede ser un horario o la palabra `siempre`. El horario tiene tres partes:
  - `zona`: una zona IANA. Se elige al crear el flujo y después no cambia, porque el cupo del día se cuenta en el día de esa zona. La base ya lo impide (`pilot_scope_timezone_must_remain_constant`, `20260810000100:87-100`), y el diseño del Automation Expert deja las zonas permitidas en los servicios determinísticos (`automation-expert-configuration-experience.md:110`). Ninguno de los siete flujos necesita cambiarla;
  - `desde`: la hora de inicio de la franja, que vale para todos los días;
  - `hasta`: la hora de fin, con el último minuto incluido, como en el motor de hoy (`20260803000100:1008-1011`).
- `topes` tiene cuatro campos:
  - `bolsa`: el nombre con el que se cuentan los envíos. Los flujos de una misma bolsa los cuentan juntos, sumando todas sus versiones. Hoy ese papel lo cumple el `scope_key`.
  - `por_dia` y `total`: el límite de este flujo sobre la cuenta de su bolsa, o `sin_tope`. Así, subir el diario del carrito es una sola versión, y el pago fallido sigue frenando en su propio número.
  - `repeticion`: cuántas veces le puede tocar este flujo a la misma persona. Siempre hay un solo recorrido en curso a la vez. `una_por` dice qué cuenta como «la misma»: la persona, la persona y la oferta, o la conversación. Como cada flujo es de un producto, la persona se cuenta dentro de ese producto. `otra_despues_de` dice cuándo puede arrancar otro recorrido después del último envío aceptado o incierto: una duración, o `nunca`. Así cuenta hoy el primer contacto (`20261001000200:836-862`). Un recorrido con un envío incierto sigue en curso hasta que se reconcilia o vence, y mientras tanto no arranca otro (ADR-0007, «Estados y resultados mínimos»).

Hay dos topes por persona, y son distintos. La repetición vale para un solo flujo. El tope por persona de la puerta cuenta los envíos de todos los flujos de todos los productos de la instancia, cupón y reactivación incluidos (decisión de Dan del 2026-10-14).

**Por defecto.**
- El horario: el de los otros flujos de la instancia. En ATT1, de 09 a 21 de CDMX.
- La bolsa: la del propio flujo.
- Los topes:
  - con un evento de recuperación (el carrito, el pago fallido, la ficha o el formulario), 10 por día y 150 en total, los números con los que se abrió el primer contacto v2 en ATT1, el 2026-10-09;
  - con un silencio o con la compra, `sin_tope`.
- La repetición depende del disparador:
  - con un evento, un recorrido en curso por persona y oferta, y otro posible después de cada envío;
  - con un silencio, una vez por conversación;
  - con la compra, una vez por persona.

**Qué pregunta el agente.** Solo pregunta si la persona habla de volumen, de la noche o de cuántas veces. Antes de proponer otro número, muestra lo gastado contra el tope actual.
- Si el flujo comparte la bolsa con otro, pregunta «¿solo este flujo o los dos?» y muestra lo gastado de la bolsa y de cada flujo. Si son los dos, la propuesta lleva una versión de cada uno, que se aprueban y se publican juntas.
- Avisa el efecto sobre los flujos a los que este les gana: a quien queda sin cupo no le llega este flujo, y tampoco el que perdió con él.
- Confirma explicando el efecto. Con un evento: «si llega de noche, espera a las 09; si se gastó el cupo del día, espera al día siguiente, salvo que haya vencido». Con un silencio: «fuera de horario no se mira nada; si la conversación sale de la franja de horas mientras tanto, no le llega» (`conversation-followup-discount-v1.md:121`).

**Qué queda en código.**
- La zona, que no cambia después de crear el flujo.
- El tope por persona de la puerta, salvo su número. El número va de 1 a 20 envíos, en una ventana de 1 h a 30 días (`20261007000100:82-86`).
- Los máximos de frecuencia.
- Que el diario no supere el total (`20260810000100:49-52`).
- Que la zona sea válida (`20260810000100:79-85`).

### El producto de un flujo

**Qué es.** El producto de la instancia al que pertenece el flujo. Una instancia es una aliada con un número de WhatsApp, y puede tener varios productos: ATT2 es el segundo producto de la aliada de ATT1 (decisión de Dan del 2026-10-11). Cada flujo es de uno solo. El carrito de ATT1 y el de ATT2 son dos flujos, aunque se escriban igual.

**Qué cambia en cada pieza.**
- **Qué lo dispara.** Con un evento, solo lo disparan los eventos de su producto. De qué producto es un evento lo deciden los adaptadores, por el producto y la oferta de Hotmart, o por el formulario y la landing. El producto de Hotmart solo no alcanza: hasta el 2026-09-04, ATT2 se vendía con ofertas del producto de ATT1 (medido en el catálogo de Hotmart el 2026-10-08). Con un silencio, entra la conversación de su producto.
- **Las variables.** `producto` es el nombre de su producto.
- **Cuándo termina.** La salida `compra` es una compra de su producto.
- **Los topes.** La bolsa puede ser de un producto o compartida entre varios. La repetición cuenta dentro del flujo, así que ya es por producto: no hace falta otro valor de `una_por`.
- **La prioridad.** `le_gana_a`, `no_arranca_mientras` y `contesto_a` nombran flujos de su mismo producto. Uno de otro producto se nombra `<producto>/<flujo>`.
- **La puerta.** Es una por instancia y no cambia. El tope por persona suma los envíos de todos los flujos de todos los productos, porque la persona y el número son los mismos.

**Por defecto.** Con un solo producto, ese. Con más de uno no hay valor por defecto, y el agente pregunta «¿de qué producto?».

**Qué queda en código.** Los productos de la instancia y sus ofertas, que salen del manifiesto. De qué producto es cada evento, y de qué producto es una conversación.

**Hoy.** Una instancia tiene un solo producto: el de `[hotmart]` en el manifiesto (`instance_manifest.py:185`), que se repite en cada scope (`external_product_id`, `20260810000100:20`). Los flujos de hoy son todos de ese producto, y esta parte del esquema no les cambia nada. El manifiesto con varios productos, de qué producto es una conversación y si la compra de un producto corta los flujos de otro van al diseño de varios productos, en la Etapa 2 (ver «Lo que no entra en la v1, y dónde va»).

### La versión, el control y el techo

- **La versión.** `version` la asigna el ciclo al publicar. `estado` puede ser `borrador` o `publicada`. Una versión publicada no se modifica, y para cambiar algo se publica la siguiente, como hoy (`20260803000100:48-69`; `20260810000100:57-77`).
- **El control.** Va fuera de la versión, uno por flujo. Guarda la versión activa y uno de tres estados:
  - prendido: el flujo corre.
  - pausado: no arranca recorridos nuevos. Un disparo que llega en ese rato no arranca nada y no vuelve cuando el flujo se prende, como hoy (`publicar-recuperacion-v5.sql:71-75`). Igual queda registrado, así que el flujo sigue ganándoles a los que le ceden el lugar. Los recorridos que ya estaban en curso esperan sin mandar: si el flujo se prende antes de que venzan, salen; si no, vencen (`worker.py:1540-1562`; visto en ATT1 el 2026-10-09, durante la pausa del primer contacto).
  - apagado: el flujo no corre, los recorridos en curso terminan y su disparo ni se registra, así que no le gana a nadie. Todo flujo nuevo nace apagado. Es lo que pasa hoy con el flag del carrito apagado: el evento vuelve `ignored`, la intención no se clasifica y el primer contacto sale (`app.py:7666-7688`; `20261001000200:297-299`).
- **Hoy el control es uno por scope, no por flujo** (`pilot_runtime_controls`, `20260810000100:127-128`). El carrito y el pago fallido comparten el suyo: pausar `att1-recuperacion` frena los dos (registrado al prender el pago fallido en ATT1, el 2026-10-09), y cambiar la versión de uno cambia la del otro (`publicar-recuperacion-v5.sql:65-70`). Con un control por flujo, pausar el carrito deja corriendo el pago fallido. Si al migrar se conserva un control común para la bolsa lo decide el contrato del ciclo. La pausa de grupo, para los lanzamientos, va a la Etapa 4.
- **Prender y pausar.** Se piden conversando, con aprobación y sin publicar otra versión. Hoy el cupón y la reactivación no tienen esa pausa: para frenarlos hay que sacar el flag y redesplegar (`conversation-followup-discount-v1.md:126`).
- **Qué avisa el agente al proponer una pausa.**
  - Que los disparos que lleguen en ese rato no vuelven cuando el flujo se prende.
  - Que lo que ese flujo le gana a otro sigue valiendo aunque esté pausado. Por ejemplo, con el carrito pausado, a quien abandona no le llega ni el carrito ni el primer contacto (`20261001000200:297-299`).
  - Mientras el carrito y el pago fallido compartan el control, que pausar uno frena los dos.
- **El techo de la instancia.** Hoy una instancia solo puede prender los flujos de una lista fija (`FLOWS` y `TEMPLATE_SLOTS`, `instance_manifest.py:67-69`). Con esa lista, la ficha y la activación pedirían un release cada una, y la 1c dejaría de ser un cambio de datos, contra el ADR-0025 §7, aceptado el 2026-10-13 (el Automation Expert cambia datos, nunca código). Por eso el techo pasa a ser la lista de eventos que recibe la instancia (`eventos`, `instancia.toml:23`; `EVENTS`, `instance_manifest.py:67`).
  - El release se pide una sola vez, para sumar `ficha_impresa` y `ficha_vencida`. La compra ya está en la lista.
  - Después, cada flujo se prende con su control.
  - El techo de un silencio es el evento `entrante`, como hoy el del cupón y el de la reactivación (`FLOW_REQUIREMENTS`, `instance_manifest.py:82-89`).
  - `[flujos]` y `[plantillas]` siguen valiendo para los flujos de hoy hasta la migración.

### Cómo explica el agente un bloqueo

El agente siempre distingue una recomendación de un bloqueo (`automation-expert-configuration-experience.md:44-63`).
- Una recomendación termina en una pregunta: «Te recomiendo no hacerlo por esto. ¿Lo hago igual?».
- Un bloqueo lo decide el validador. El agente nunca bloquea por su cuenta algo que el validador permite. Cada bloqueo dice de qué tipo es, el límite, la razón y la alternativa, y el agente arma su frase con eso, sin improvisar. Hay dos tipos, cada uno con su frase:
  - **Una protección** está en código a propósito y no se cambia conversando: «Esto no lo puedo cambiar: <límite> está en código a propósito, porque <razón>. Lo que sí puedo hacer es <alternativa>.»
  - **Algo que todavía no existe** en esta versión: «Esto todavía no se puede: <qué> no está en esta versión. Mientras tanto puedo <alternativa>.»

Algunas protecciones:
- «Escribile igual a quien pidió que no le escriban»: la baja no se afloja sin un contrato de reautorización (`followup-engine-v1.md:569-570`).
- «Mandale este texto a quien no escribe hace dos días»: Meta deja mandar texto libre solo dentro de las 24 h. La alternativa es una plantilla aprobada.
- «Mandale un mensaje por segundo hasta que compre»: lo frenan los máximos de frecuencia.
- «Que le llegue el carrito aunque Mariana lo esté atendiendo»: si una persona del equipo tiene la conversación, ningún flujo entra.
- «Reenviá el cupón a los que quedaron en duda»: un envío incierto no se repite (`conversation-followup-discount-v1.md:99`).

Algunas cosas que todavía no existen:
- «Mandale un segundo recordatorio si no paga la ficha»: la v1 manda un solo mensaje por camino. Mientras tanto, uno solo, con la espera que se elija.
- «Que no salga los domingos»: los días de la semana no están en esta versión. Mientras tanto, el mismo horario todos los días.
- «Cambiá el cupón por otro»: el código del cupón vive también en Hotmart y en el conocimiento del agente, y en la v1 no se cambia conversando. Mientras tanto, se pide como un cambio con PR y aprobación.

## La prioridad y la puerta común

### Cómo conviven los flujos

Hoy no hay ninguna columna de prioridad. La convivencia sale de reglas repartidas entre SQL y Python: la búsqueda de `priority` en las migraciones no encuentra nada. El esquema convierte esas reglas en dos relaciones que se escriben en cada flujo y se cambian conversando.

- **`le_gana_a`** se escribe en el flujo que gana. Es la frase de quien opera: «el carrito le gana al primer contacto».
  - Los flujos de la lista no arrancan, y el que estaba esperando termina con el motivo `le_gano:<flujo>`, si para la misma persona pasa una de dos cosas:
    - el disparo del que gana llegó para la misma intención de compra, aunque no mande nada (porque está pausado, sin cupo o fuera de horario);
    - el que gana tiene un recorrido en curso. Sin otra indicación, el que gana es del mismo producto (ver «El producto de un flujo»).
  - Un flujo apagado no le gana a nadie, porque su disparo ni se registra.
  - Es lo que hace hoy el primer contacto (`20261001000200:293-313`).
  - Se revisa al planificar, al reevaluar y al arrancar.
  - Va en un solo sentido: el flujo que pierde no mira al que gana.
- **`no_arranca_mientras`** se escribe en el flujo que no tiene que superponerse con otro.
  - Ese flujo no arranca si la persona tiene en curso un recorrido de alguno de los flujos de la lista, con la misma clave de repetición.
  - El disparo no queda esperando: se pierde, y queda registrado con su motivo.
  - Es lo que hace hoy el carrito cuando hay un pago fallido en curso (`pilot_case_binding_conflict`, `20260930000300:837-846`).
- Dos flujos sin relación conviven. El cupón, por ejemplo, convive con todos.

Dos campos de las piezas completan la convivencia. La `bolsa` suma en una sola cuenta los envíos de varios flujos. La `repeticion` regula cómo convive un flujo consigo mismo.

Dos palabras se usan siempre con el mismo sentido:
- **Persona:** el contacto más las dos formas de su móvil (52/521 y 54/549), como en el tope por persona (`20261007000100:32-36`).
- **Recorrido:** un flujo que arrancó para una persona. Está en curso mientras espera, mientras está por salir y mientras tiene un envío incierto sin reconciliar.

Hay dos cosas que no hace falta modelar:
- El cupón y la reactivación no pueden elegir la misma conversación. El cupón exige que lo último lo diga el agente, y la reactivación, que lo diga la persona (`conversation-followup-discount-v1.md:103-112`).
- La compra corta toda la recuperación de su producto, por la salida `compra` de cada flujo.

### Lo que pasa hoy en ATT1, y el campo que lo escribe

1. **El carrito y el pago fallido le ganan al primer contacto, y no al revés.**
   - Se escribe con `le_gana_a: [precheckout]` en los dos.
   - Cuenta aunque el carrito no salga, porque se mira el disparo de la misma intención (`20261001000200:297-299`).
   - Medido en att1-db el 2026-10-14: en 7 días se cancelaron 155 primeros contactos por el carrito, contra 13 enviados.
2. **A quien le falla el pago y después abandona le llegan las dos plantillas, con unos 40 minutos de diferencia.**
   - Dan lo aceptó el 2026-10-09, al prender el pago fallido en ATT1, mientras faltara el tope por persona.
   - De día, el pago fallido sale unos 10 segundos después del rechazo y su recorrido termina. Cuando llega el carrito, no hay nada en curso, así que arranca.
   - Con la puerta, lo frena el tope por persona cuando la instancia cargue su número.
3. **De noche sale una sola.** El pago fallido espera a las 09, y el carrito no arranca. Se escribe con `no_arranca_mientras: [pago_fallido]` en el carrito (la misma decisión del 2026-10-09).
4. **Al revés no pasa.**
   - Si llega un pago fallido mientras hay un carrito en curso, el pago fallido abre su propio recorrido (`20260904000100:87-98`), así que a las 09 saldrían las dos plantillas.
   - Esto se infiere del código y falta medirlo.
5. **Comparten los topes.** Se escribe con `bolsa: att1-recuperacion` en los dos (`publicar-recuperacion-v5.sql:23-28` y `:36-44`). Desde el 2026-10-19 ya no: el pago fallido cuenta en `att1-pago-rechazado` (1.1).
6. **Hay un solo primer contacto en curso por persona, y ninguno en las 24 h siguientes a un envío aceptado o incierto.** Se escribe con `repeticion: {una_por: persona, otra_despues_de: 24h}` (`20261001000200:836-862`).
7. **El cupón sale una vez por conversación, y solo en las adoptadas.** Se escribe con `otra_despues_de: nunca` y `contesto_a` (`conversation-followup-discount-v1.md:9`; `20261010000100:22-32`).
8. **La compra corta la recuperación.** Se escribe con `compra` en `termina.si`.
9. **Una conversación derivada o pausada frena distinto a cada flujo.**
   - El primer contacto frena si cualquier conversación del contacto está derivada, tomada, pausada, cerrada o bloqueada (`20261001000200:334-360`).
   - El carrito y el pago fallido miran solo la conversación de su caso, que no existe antes del primer envío (`20260805000300:297-306`).
   - El cupón mira su conversación y la derivación de su caso (`20260928000400:268-290`).
   - La reactivación mira la derivación de su caso y entra a propósito en las conversaciones pausadas (`20261001000500:284-296`; `conversation-reactivation-v1.md:96-98`).
   - Con la puerta es una sola regla para todos, en el chequeo 4.
10. **La respuesta a una plantilla no se adopta mientras la persona tenga otro paso pendiente.**
    - Es la regla de adopción (`20261001000400:43-47`), y queda en código.
    - La salida `contesta` saca ese freno en los flujos que la tienen (ver «Cuándo termina»).

**Lo nuevo no hereda nada en silencio.**
- Hoy el primer contacto cede ante cualquier caso de Hotmart abierto del producto (`20261001000200:300-310`).
- El carrito se junta con cualquier caso abierto de la misma oferta (`20260803000100:475-485`).
- Si la ficha se armara con esos planificadores, los dos flujos cambiarían de comportamiento sin que nadie lo decida.
- La adopción vale hoy solo para las plantillas del piloto con binding: el carrito, el pago fallido y el primer contacto (`20261001000400:28-33`). La reserva del cupón acepta cualquier adopción, sin mirar de qué flujo vino (`20261010000100:22-32`). Si la 1b hace adoptables las plantillas de la ficha o de la bienvenida, el cupón se extendería solo a esas respuestas. Por eso la reserva tiene que filtrar por el flujo de la adopción, para cumplir `contesto_a`.
- Con las relaciones explícitas, cada una de estas es una pregunta que hace el agente cuando se crea el flujo.

### La puerta, chequeo por chequeo

Una sola puerta, en código, revisa cada envío proactivo de cualquier flujo, venga del motor o de un barrido. Así, un flujo nuevo no trae barreras propias.

Una parte ya existe: la reevaluación del motor (`20260805000300:256-414`) y `authorize_lancemos_pilot_request_start`, que el producto ya llama «la puerta común» de los tres arranques (`20261007000100:22-25`). Ahora vale también para el cupón, para la reactivación y para lo nuevo.

La puerta corre dos veces:
- **Cuando llega el disparador.** Además de los chequeos, mira la repetición, `no_arranca_mientras` y si otro flujo le gana. Si no pasa, el recorrido no arranca y el disparo queda registrado con su motivo.
- **Antes de cada envío,** lo más cerca posible del envío. Ahí vuelve a revisar todo.

El orden de los chequeos decide qué motivo queda registrado. Una guarda que frena no la anula ninguna guarda posterior. Los cinco primeros siguen el orden de la reevaluación de hoy: el plazo, la baja, la compra, la toma humana y, después, la respuesta. Cada chequeo deja su motivo. La lista cerrada de motivos, con la que la 1a contesta «por qué no le llegó», va al contrato de lectura.

1. **El plazo.**
   - Qué frena: el recorrido venció.
   - Resultado: termina con `vencio`.
   - Hoy: es lo primero que mira la reevaluación (`20260805000300:256-283`).
   - Conversando: el plazo.
2. **La baja y los bloqueos.**
   - Qué frena: una baja en cualquiera de las dos formas del móvil, un contacto bloqueado o restringido, o un permiso denegado.
   - Resultado: termina con `baja`.
   - Hoy: `contact_blocked` (`20260805000300:285-288`), `precheckout_prior_opt_out` (`20261001000200:315-332`) y `blocked_contact` en el cupón y la reactivación.
   - Conversando: nada.
3. **La compra y las otras salidas que no dependen de la conversación.**
   - Qué frena: que la persona compró, que llegó el vencimiento de la ficha (si el flujo tiene esa salida) o que otro flujo le ganó.
   - Resultado: termina con `compro`, `ficha_vencida` o `le_gano:<flujo>`. Con la compra queda ganado, salvo en el primer contacto.
   - Hoy: `purchase_detected` (`20260805000300:289-291`) y `superseded_by_provider_event`.
   - Conversando: las salidas y la prioridad.
4. **Una persona tiene la conversación.**
   - Qué frena: en cualquier conversación de la persona, una derivación sin atender, una conversación tomada o pausada por el equipo, o una conversación con la automatización apagada.
   - La pausa que el equipo dejó no frena: la persona escribió último, el equipo lleva 8 h sin escribir y no hay una derivación sin atender. Son las condiciones con las que hoy la reactivación entra en una conversación pausada (`conversation-reactivation-v1.md:90-104`). De la reanudación no se copian dos condiciones: que no haya un assignee, que no es señal, y que la ventana esté abierta, porque la reactivación actúa con la ventana cerrada (`resume-paused-conversation-v1.md:18` y `:29-30`). Es una propuesta: cómo pasa lo decide el contrato del ciclo (ver «Lo que cambia respecto de hoy»).
   - Resolver una conversación no cuenta, porque es solo ordenar la bandeja (`followup_discount.py:63-69`).
   - Resultado: no sale, y el recorrido termina con `tomada_por_el_equipo`. No se reanuda solo: si después llega otro disparo, arranca uno nuevo, que vuelve a pasar por la puerta (ADR-0007 §7).
   - Hoy hay cuatro alcances distintos (el punto 9 de la lista de arriba).
   - Conversando: nada.
5. **La persona contestó.**
   - Qué frena: la persona escribió después del disparo, si el flujo tiene `contesta`, o después del primer envío aceptado, en cualquier flujo.
   - Resultado: termina con `contesto`.
   - Hoy: `prospect_replied` (`20260805000300:402-404`) y, en el cupón, `lead_wrote_meanwhile`. Hoy la toma humana se mira antes que la respuesta, y en la puerta también.
   - Conversando: la salida `contesta`.
6. **La identidad, el destino y el permiso.**
   - Qué revisa: que el teléfono se pueda leer, que corresponda a una sola persona, que la conversación sea del inbox de la instancia y que haya un permiso vigente para el `permiso` del flujo.
   - Resultado: si no se sabe, no sale y se escala (`followup-engine-v1.md:319-321`).
   - Conversando: el permiso, dentro de lo que admite el disparador.
7. **El flujo en marcha.**
   - Resultado: si el flujo está pausado, el recorrido espera; si está apagado, termina con `flujo_apagado`.
   - Si la versión del recorrido no es la activa, hoy el arranque se rechaza y se reintenta hasta vencer (`pilot_scope_version_mismatch`). Qué pasa con quien está a mitad de un flujo lo decide el contrato del ciclo.
   - Conversando: prender y pausar.
8. **Sigue entrando.**
   - Qué revisa: vuelve a mirar las condiciones que cambian con el tiempo: el silencio y su edad, si la conversación fue adoptada, si la intención sigue viva y el consentimiento.
   - Resultado: si ya no se cumplen, el recorrido termina con `ya_no_entra`.
   - Hoy: `precheckout_authorization_lost` y la relectura que hace el cupón antes de reservar.
   - Conversando: esas condiciones.
9. **El horario.**
   - Resultado: fuera de la franja, en la zona del flujo, el envío espera a la próxima apertura. Si no queda ninguna antes de vencer, el recorrido termina con `sin_horario`. Hoy, en ese caso, se escala (`20260805000300:348-354`).
   - En un barrido, fuera de horario no se lee nada (`outside_send_hours`).
   - Conversando: la franja. La zona se elige al crear el flujo.
10. **Las reglas de Meta.**
    - Qué frena: fuera de la ventana de 24 h, solo puede salir una plantilla APPROVED, en su idioma, con todas sus variables completas y un nombre que pase el filtro. `conversar` sale solo con la ventana abierta, y el bridge valida lo que propone el agente (ADR-0007 §3).
    - Resultado: no sale.
    - Va antes de los topes, para no gastar cupo en algo que no puede salir.
    - Hoy: `approved_template_mismatch` (`referencia-manifiesto.md:91`) y el chequeo final de Meta.
    - Conversando: qué plantilla aprobada sale.
11. **Los topes.**
    - Qué revisa: los máximos de frecuencia y el límite del flujo sobre su bolsa. En la v1, los máximos son el tope por persona de la instancia, que suma los envíos de todos los flujos de todos sus productos, cupón y reactivación incluidos (de 1 a 20 envíos en una ventana de 1 h a 30 días), y los envíos que ejecuta el motor para ese flujo, que asegura el validador.
    - Resultado: el envío espera sin gastar cupo y se reintenta hasta que se libere el tope o venza el plazo. Una propuesta del 2026-10-09, todavía sin decidir ni publicar, propone cancelar el envío que supera el tope por persona.
    - En este chequeo se gasta el cupo.
    - Hoy: `authorize_lancemos_pilot_request_start` (`20261007000100:22-30`; `20260930000300:620-639`).
    - Conversando: los números.
12. **No mandar dos veces.**
    - Qué revisa: cada recorrido y paso tiene una clave estable, una reserva viva no deja hacer otra, y un resultado incierto no se reintenta: se reconcilia o se escala.
    - Hoy: `followup-engine-v1.md:362-374`, `conversation-followup-discount-v1.md:99` y `conversation-reactivation-v1.md:148-152`.
    - Conversando: nada.

### Lo que cambia respecto de hoy

Al migrar, la puerta única y la separación del carrito y el pago fallido traen estas diferencias. Casi ninguna se nota del lado de la persona, pero algunas hay que medirlas.
- **La toma humana se mira por persona.**
  - El carrito y el pago fallido pasan a frenar también por una derivación sin atender, o por una conversación tomada, aunque sea otra conversación de la persona.
  - El cupón y la reactivación, también: hoy miran su conversación y la derivación de su caso (`20260928000400:268-290`; `20261001000500:284-296`).
  - Es uno de los huecos que motivaron la puerta única (decisión de Dan del 2026-10-14).
  - A medir: cuántos envíos de cada flujo salieron así.
- **La toma humana termina el recorrido.**
  - Hoy el carrito y el pago fallido cancelan su acción y dejan el caso `paused` (`20260805000300:425-438`). Ese caso sigue abierto: junta los carritos nuevos de la misma oferta y frena al primer contacto. El primer contacto, en cambio, cancela su caso (`20261001000200:1665-1677`).
  - Con la puerta, todos terminan, como pide el ADR-0007 §7 («cancela la planificación vigente»), y un disparo nuevo arranca otro recorrido que vuelve a pasar por la puerta.
  - A medir: cuántos casos `paused` hay hoy, y desde cuándo.
- **La pausa que el equipo dejó deja de frenar al primer contacto.**
  - Hoy lo frena cualquier pausa, y también los estados de conversación cerrada o bloqueada (`20261001000200:344-360`).
  - Recomendación: aceptarlo. Es una sola regla para todos, y con ella la reactivación se escribe como está construida.
  - A medir: en cuántos formularios pasó.
- **La compra se detecta de una sola forma.**
  - Hoy hay tres:
    - el caso ganado por correlación, en el carrito y el pago fallido;
    - la compra por identidad sin ventana, en el primer contacto (`portable-precheckout-first-contact-v1.md:144`);
    - el mail o los últimos 9 dígitos del teléfono, en el cupón.
  - A medir: los envíos a personas que ya habían comprado según la identidad.
- **El cupón y la reactivación quedan bajo los mismos controles que el resto.**
  - Pasan a tener su propia pausa, a pasar por el interruptor general de Meta y a contar en el tope por persona.
  - Hoy no pasan por ninguno (`conversation-followup-discount-v1.md:126`).
  - No cambia nada mientras ATT1 no tenga renglón del tope.
- **El nombre que no pasa el filtro.**
  - Hoy el cupón lo reintenta en cada barrido (`conversation-followup-discount-v1.md:122`), y el despachador lo cierra sin reintento (`portable-precheckout-first-contact-v1.md:171`).
  - La puerta usa una sola regla.
- **El disparo que no entra por la repetición o por `no_arranca_mientras`.**
  - Hoy el carrito lo deja en `failed` con `pilot_case_binding_conflict`, y el pago fallido lo suma como evidencia.
  - Con el esquema, en los dos casos queda registrado con su motivo.
  - La persona no nota la diferencia, pero el lector de la 1a sí.
- **La persona.** Hoy la repetición del primer contacto mira el contacto (`20261001000200:840-857`). La puerta junta las dos formas del móvil.
- **El carrito y el pago fallido se separan.**
  - Al 2026-10-14 compartían el scope, la política y el control (desde el 2026-10-19 el pago fallido tiene su scope, `att1-pago-rechazado`, su política y su control). La primera espera es la gracia de esa política, y el bridge la toma de una sola variable para los dos (`FOLLOWUP_POLICY_KEY`, `app.py:1083`; `resolution.py:324-337`).
  - Con dos flujos, cambiar la espera del carrito no toca el pago fallido, que hoy sale a los 10 segundos, y pausar el carrito deja corriendo el pago fallido.
  - Al migrar, cada uno arranca con su propia política, con los mismos valores de hoy.
- **El orden de los chequeos.** Hoy el primer contacto mira la compra y el carrito antes que la baja (`20261001000200:285-332`). Con la puerta, en el caso raro de las dos cosas a la vez, cambia solo el motivo registrado.
- **La ficha deja de colgar del flag del pago fallido** (`app.py:7655-7658`). Hoy no cambia nada, porque la ficha no dispara ningún flujo.

Cómo pasa cada diferencia lo decide el contrato del ciclo, como decidió Dan el 2026-10-14. Antes se mide en att1-db, solo leyendo, para que la migración siga siendo la línea de base de «sin que cambie nada».

## Los siete flujos de validación

Son ejemplos para validar el esquema, escritos con los valores de la instancia ATT1 al 2026-10-14. No son valores por defecto del producto: los valores por defecto son los de cada pieza, más arriba.

Cada bloque lleva además `esquema: setter-flujo/v1`. Ninguno escribe `producto`: ATT1 tiene uno solo, y es el valor por defecto.

Los cuatro flujos que corren hoy en ATT1 van tal como corren. La línea de base es la del 2026-10-14:
- el código del producto en `origin/main` (`1b83191`, 1.8.0), que en los flujos es igual a la v1.6.1 que corría ATT1 (ver «Citas», arriba);
- la instancia en `b1cdefe` (el commit siguiente, `fefdef8`, del 2026-10-15, solo pasó el conocimiento a la v6, sin tocar flujos ni plantillas);
- att1-db.

Si el 2026-10-20 la recuperación de ATT1 no llega a su piso de 3 ventas por semana con su link, se vuelve a prender el carrito de GHL (decisión de Dan del 2026-10-13), y esa línea de base cambia.

Lo que no se sabe dice `a_medir`, `a_decidir` o `a_crear`.

Los ocho bloques se probaron contra las reglas del validador de más abajo:
- los cuatro flujos de hoy se publican. El primer contacto lleva una advertencia: tiene una espera y no tiene `contesta`, y eso hoy es real;
- la reactivación como está construida se publica con dos advertencias: no tiene horario y usa una plantilla de marketing, y no tiene la salida `compra`;
- la reactivación dentro de la ventana, la ficha y la activación se guardan como borradores, y no se publican hasta tener lo que les falta.

### 1. El carrito abandonado (ATT1, hoy)

```yaml
flujo: carrito
nombre: Carrito abandonado
version: 1                    # hoy: scope att1-recuperacion v5 + política att1-recuperacion-un-toque v1
estado: publicada
dispara:
  evento: carrito             # PURCHASE_OUT_OF_SHOPPING_CART 2.0.0
entra:
  permiso: formulario
pasos:
  - plantilla: att1_carrito_abandonado_01
    idioma: es_MX
    variables: [nombre, producto]
termina:
  si: [compra]                # sin contesta: hoy sale aunque la persona haya escrito antes del envío
  vence: 1d
horario: {zona: America/Mexico_City, desde: "09:00", hasta: "21:00"}
topes:
  bolsa: att1-recuperacion    # la comparte con pago_fallido y arrastra lo gastado
  por_dia: 80
  total: 1000
  repeticion: {una_por: persona_y_oferta, otra_despues_de: 0m}
prioridad:
  le_gana_a: [precheckout]
  no_arranca_mientras: [pago_fallido]
# control: prendido (hoy, el de att1-recuperacion, armed con la v5 desde 2026-10-13 00:20:37Z)
```

- **Paridad:**
  - Gracia 0, vence al día, de 09 a 21 de CDMX con el minuto de las 21 incluido, y un solo envío (`aprovisionar-att1.sql:193-214`).
  - 80 por día y 1000 en total, los de la v5, publicada en ATT1 el 2026-10-13.
  - Hoy el paso comparte su lista de pasos con el pago fallido y dice `mode: freeform`, aunque sale como plantilla (`aprovisionar-att1.sql:197-212`). En el esquema cada flujo tiene su propio paso y su propia política, y el tipo dice lo que pasa de verdad.
- **Fuente hoy, para el lector de la 1a:** el scope `att1-recuperacion` v5, la política `att1-recuperacion-un-toque` v1 y su control en att1-db; `generar.py:146` y `:165`; `instancia.toml:96` y `:199`.
- **A medir:** lo gastado de la bolsa el día de la migración. Medido en att1-db el 2026-10-13 a las 00:21Z: 126 de 1000.

### 2. El pago fallido, la compra cancelada (ATT1, hoy)

```yaml
flujo: pago_fallido
nombre: Pago fallido
version: 1                    # hoy: el mismo scope y la misma política que el carrito
estado: publicada
dispara:
  evento: pago_fallido        # PURCHASE_CANCELED 2.0.0, cualquier motivo
entra:
  permiso: formulario         # lo da la intención con consentimiento, cruzada en las 24 h previas
pasos:
  - plantilla: att1_compra_fallida_01
    idioma: es_MX
    variables: [nombre, producto]
termina:
  si: [compra]
  vence: 1d
horario: {zona: America/Mexico_City, desde: "09:00", hasta: "21:00"}
topes:
  bolsa: att1-recuperacion
  por_dia: 80
  total: 1000
  repeticion: {una_por: persona_y_oferta, otra_despues_de: 0m}
prioridad:
  le_gana_a: [precheckout]
# control: prendido (hoy comparte con el carrito el control de att1-recuperacion)
```

- **Paridad:**
  - El evento no da el permiso. Lo da la intención con consentimiento que se cruzó con el evento (`20261001000100:1125-1132`).
  - Si llega otra falla mientras hay un recorrido en curso, se suma como evidencia.
  - Cada reintento de tarjeta después del envío abre otro recorrido, porque el caso ya quedó `sequence_exhausted` (`20260904000100:87-98`; `20260813000100:397-417`; aceptado por Dan al prender el pago fallido, el 2026-10-09).
- **Fuente hoy:** la misma del carrito, más `generar.py:166`, `instancia.toml:97` y `:200`.
- **A medir:**
  - cuántas personas recibieron las dos plantillas (medido en att1-db el 2026-10-14: en 7 días, 2 personas tuvieron dos arranques, sin desglose);
  - el caso 4 de la convivencia.

### 3. El primer contacto: formulario sin compra (ATT1, hoy)

```yaml
flujo: precheckout
nombre: Primer contacto tras el formulario
version: 1                    # hoy: scope att1-primer-contacto v2 + política att1-primer-contacto-formulario v1
estado: publicada
dispara:
  evento: intencion           # el formulario: envío 1.1.0 con los dos consentimientos
entra:
  permiso: formulario
pasos:
  - esperar: 60m
  - plantilla: att1_interes_precheckout_01
    idioma: es_MX
    variables: [nombre, producto]
termina:
  si: [compra]                # el recorrido queda cancelado, nunca ganado
  vence: 1d
horario: {zona: America/Mexico_City, desde: "09:00", hasta: "21:00"}
topes:
  bolsa: att1-primer-contacto # arrastra lo gastado de la v1 y la v2
  por_dia: 10
  total: 150
  repeticion: {una_por: persona, otra_despues_de: 24h}
# sin prioridad propia: le ganan el carrito y el pago fallido (lo dicen ellos)
# control: prendido (hoy, armed con la v2 desde el 2026-10-09)
```

- **Paridad:**
  - Espera 60 minutos y vence al día (`aprovisionar-att1.sql:219-233`).
  - 10 por día y 150 en total, los de la v2, publicada en ATT1 el 2026-10-09.
  - Si la persona escribe antes, no lo frena (`20260805000300:373-377`). Por eso no lleva `contesta`.
- **La advertencia:** mientras espera sus 60 minutos, una respuesta a otra plantilla nuestra no se adopta. Hoy es así. Sumarle `contesta` es una versión aparte.
- **Fuente hoy:** el scope `att1-primer-contacto` v2, la política `att1-primer-contacto-formulario` v1 y su control; `generar.py:160` y `:167`; `instancia.toml:95` y `:198`.

### 4. El seguimiento con cupón (ATT1, hoy)

```yaml
flujo: descuento
nombre: Seguimiento con cupón
version: 1                    # hoy: variables CONVERSATION_FOLLOWUP_* y [plantillas].descuento
estado: publicada
dispara:
  silencio:
    lo_ultimo: agente
    desde: 24h
    hasta: 72h
    conversaciones: [abierta, resuelta]
entra:
  permiso: conversacion
  contesto_a: [carrito, pago_fallido, precheckout]   # la conversación adoptada
pasos:
  - plantilla: att1_seguimiento_descuento_01
    idioma: es_MX
    variables: [nombre, producto]
    boton: {link: checkout, cupon: TIROIDES10}       # el cupón no se cambia conversando en la v1
termina:
  si: [compra, contesta]      # contesta: hoy es lead_wrote_meanwhile
horario: {zona: America/Mexico_City, desde: "09:00", hasta: "20:59"}   # 09-21 con el fin excluido
topes:
  por_dia: sin_tope
  total: sin_tope
  repeticion: {una_por: conversacion, otra_despues_de: nunca}
# control: prendido (hoy, CONVERSATION_FOLLOWUP_ENABLED y [flujos].descuento)
```

- **Paridad:**
  - Sale entre 24 y 72 h después del último mensaje de la persona, cuando el último mensaje es del AgentBot y nadie del equipo escribió después (`followup_discount.py:469-517`).
  - Toma conversaciones abiertas y resueltas (`followup_discount.py:63-69`), solo las adoptadas, y una vez por conversación (`conversation-followup-discount-v1.md:9`).
  - El horario se corta a las 20:59 porque hoy el cupón excluye la hora de fin (`followup_discount.py:855-860`), y el motor incluye ese minuto.
  - Medido en att1-db el 2026-10-14 a las 22:59Z: 16 enviados y 0 fallidos.
- **Fuente hoy:** `generar.py:183-198`, `instancia.toml:99` y `:202`. El intervalo (600 s), las páginas y el máximo por barrido quedan como parámetros del motor.
- **El código del cupón:** en la v1 se lee y no se cambia conversando. Vive también en Hotmart y en el conocimiento del agente, que desde la v4 lo explica solo a quien recibió la plantilla. Cambiarlo es un cambio con PR y aprobación.

### 5. La reactivación

Lo que existe es la reactivación **fuera** de la ventana de 24 h: saltea toda conversación con la ventana abierta (`reactivation.py:321-327`). La reactivación «dentro de la ventana» es la propuesta del 2026-09-23, que sigue como propuesta y no está construida. Por eso van dos versiones.

**5a. Como está construida (apagada en ATT1)**

```yaml
flujo: reactivacion
nombre: Reactivación de quien quedó esperando
version: 1
estado: publicada             # como está construida
dispara:
  silencio:
    lo_ultimo: persona
    desde: 24h                # con la ventana de Meta ya cerrada
    hasta: 30d
    conversaciones: [abierta]
entra:
  permiso: conversacion
pasos:
  - plantilla: att1_reactivacion_01
    idioma: es_MX
    variables: [nombre]
termina:
  si: []                      # no mira compras: el validador lo advierte
horario: siempre              # hoy no tiene horario
topes:
  por_dia: sin_tope
  total: sin_tope
  repeticion: {una_por: conversacion, otra_despues_de: nunca}
# control: apagado ([flujos].reactivacion = false; 0 envíos en ATT1)
```

- **Paridad:**
  - Entra en las conversaciones pausadas que el equipo dejó hace más de 8 h (`conversation-reactivation-v1.md:93-98`). La puerta lo reproduce con la regla del chequeo 4.
  - Sale una vez por conversación (`conversation-reactivation-v1.md:154-158`).
  - Está apagada (`instancia.toml:201`) y, medido en att1-db el 2026-10-14, nunca mandó nada en ATT1.
- **Antes de prenderla:** conviene publicar una versión con horario y con la salida `compra`, porque hoy puede salir de noche y llegarle a quien ya compró.
- **Lo que cambia al migrar, sin efecto mientras siga apagada:** hoy su plantilla sale de `WABA_REACTIVATION_TEMPLATE_NAME` y no de `[plantillas]` (`instancia.toml:100-102`), y tiene su propia regla de nombre. Las dos pasan a las comunes.

**5b. Dentro de la ventana, como se propuso el 2026-09-23 (sin construir)**

```yaml
flujo: reactivacion
nombre: Reactivación de quien quedó esperando
version: 2
estado: borrador              # la propuesta del 2026-09-23, sin construir
dispara:
  silencio:
    lo_ultimo: persona
    desde: 0m
    hasta: 30d
    conversaciones: [abierta]
entra:
  permiso: conversacion
pasos:
  - condicion: {dato: ventana, es: [abierta]}
    entonces:
      - conversar: "Retomar la conversación donde quedó y contestar lo último que escribió la persona."
    si_no:
      - plantilla: att1_reactivacion_01
        idioma: es_MX
        variables: [nombre]
termina:
  si: []
horario: siempre              # a decidir: la propuesta no lo fija
topes:
  por_dia: sin_tope
  total: sin_tope
  repeticion: {una_por: conversacion, otra_despues_de: nunca}
```

- **Como se propuso** (propuesta del 2026-09-23): con la ventana abierta, escribe el agente; fuera de la ventana, sale la plantilla.
- **La frontera con el monitor de trabadas:** la propuesta dice que, con la ventana abierta, la rama del agente entra solo en una conversación pausada que el equipo dejó. Sigue en propuesta, y pide mirar un E2E antes de construir esta rama. No es un límite en código, y la v1 no tiene un campo para escribirla: si la rama se confirma, se suma.
- **A medir o decidir:**
  - hoy el objetivo no le llega al agente: hacerlo llegar es código de la 1b, y hasta entonces `conversar` no se publica;
  - el horario;
  - si la respuesta del agente cuenta como la única reactivación.

### 6. La ficha de OXXO sin pagar (1c, pedida el 2026-10-12)

```yaml
flujo: ficha
nombre: Ficha impresa sin pagar
version: 1
estado: borrador
dispara:
  evento: ficha_impresa       # PURCHASE_BILLET_PRINTED
entra:
  permiso: formulario         # el cruce de la ficha con la intención todavía no existe: se construye en la 1c
pasos:
  - esperar: a_medir          # Hotmart no manda el vencimiento; las 2 compras viejas por OXXO se pagaron a ~4 días
  - condicion: {dato: medio_de_pago, es: [CASHPAYMENT]}   # el pago en efectivo: OXXO en México
    entonces:
      - plantilla: {a_crear: att1_ficha_oxxo_01}          # con botón al link de la ficha
        idioma: es_MX
        variables: [nombre, producto]
        boton: {link: ficha}
    si_no:
      - plantilla: {a_crear: att1_ficha_otros_medios_01}  # sin botón; nombra el medio
        idioma: es_MX
        variables: [nombre, medio_de_pago, producto]
termina:
  si: [compra, contesta, ficha_vencida]
  vence: a_medir              # mayor que la espera
horario: {zona: America/Mexico_City, desde: "09:00", hasta: "21:00"}
topes:
  bolsa: ficha
  por_dia: 10                 # el default de un flujo nuevo de recuperación
  total: 150
  repeticion: {una_por: persona_y_oferta, otra_despues_de: 0m}
prioridad:
  le_gana_a: [precheckout]    # propuesta, como el carrito
# control: apagado hasta que las dos plantillas estén aprobadas
```

- **Como se pidió** (decisión de Dan del 2026-10-12): un seguimiento a quien imprimió la ficha y no pagó, con el botón solo para OXXO. Termina con la compra o con la ficha vencida.
- **Medido en att1-db el 2026-10-14 a las 22:51Z:**
  - 6 fichas: 2 en efectivo (`CASHPAYMENT`) con link, 2 de Mercado Pago, 1 de Yape y 1 de Nequi;
  - 2 ya están pagadas y ninguna venció;
  - hoy quedan en `failed` (`worker.py:2393-2401`).
- **Ya medido:** la forma del pago en efectivo de ATT1 está en el fixture `hotmart_cash_payment_approved_att1_20261012.json`. `payment.type` es `CASHPAYMENT`, con `billet_url` y `billet_barcode`, y su nota de captura dice que las dos compras se pagaron en OXXO.
- **A medir:**
  - el sobre real de un `PURCHASE_BILLET_PRINTED`: hoy las pruebas lo derivan de la compra aprobada (`tests/test_hotmart_payment_slip_events.py:1-12`), y en att1-db ya hay 6;
  - la parte fija de `billet_url`, para el botón (el fixture la anonimizó);
  - la espera y el plazo.
- **A crear, en la 1c:**
  - el cruce de la ficha con la intención, que hoy no existe;
  - sumar la ficha a la guarda del riesgo de GHL;
  - las dos plantillas, con los nombres propuestos.
- **Los topes** son los de un flujo nuevo de recuperación. El volumen medido es de 6 fichas en unos 2 días.

### 7. La activación después de la compra (1c)

```yaml
flujo: activacion
nombre: Bienvenida después de la compra
version: 1
estado: borrador
dispara:
  evento: compra              # PURCHASE_APPROVED, con o sin caso del recuperador
entra:
  permiso: formulario         # el default; compra, recién con el OK del cliente (en ATT1, Juan o Marce)
pasos:
  - plantilla: {a_crear: att1_bienvenida_01}   # de utilidad; el contenido se define conversando
    idioma: es_MX
    variables: [nombre, producto]               # el evento trae el nombre; el bridge todavía no lo extrae
termina:
  si: []                      # sale aunque la persona haya escrito: a decidir con el contenido
  vence: a_decidir
horario: siempre              # propuesta: que la bienvenida no quede esperando (la regla de adopción)
topes:
  por_dia: sin_tope
  total: sin_tope
  repeticion: {una_por: persona, otra_despues_de: nunca}
# control: apagado; antes de prenderla se corta la bienvenida de GHL
```

- **Como se pidió** (decisión de Dan del 2026-10-07): cuando se aprueba una compra, sale una bienvenida, y el contenido se define conversando.
  - La bienvenida de ATT1 vive en GHL (relevado el 2026-10-07). El producto no la tiene: `PURCHASE_APPROVED` solo cierra casos.
  - No choca con la recuperación, porque la compra termina todo flujo que tenga `compra` en `termina.si`.
- **A medir, crear o decidir:**
  - la plantilla, de utilidad;
  - extraer el nombre del evento de compra, que lo trae (1b);
  - el permiso. Con `formulario`, la bienvenida le llega solo a quien dejó el formulario con permiso. Pasar a `compra` pide el OK del cliente (en ATT1, Juan o Marce). Antes conviene medir, de las ventas de una semana, cuántas tienen una intención con permiso;
  - el plazo;
  - que también la dispare una venta sin caso del recuperador.
- **El horario:** `siempre`, para que la bienvenida no quede esperando. Mientras espera, si quien acaba de comprar contesta otra plantilla nuestra, el agente no le responde (la regla de adopción). Si se le pone horario, conviene sumarle `contesta`.
- **El tope por persona** de la puerta también la cuenta, como a todo envío.
- **Lo que queda afuera:** el mail.
- **Ojo:** si se prende sin cortar la bienvenida de GHL, la persona recibe dos bienvenidas.

## La definición formal

```yaml
# ═══ setter-flujo/v1 · la definición de un flujo ═══
# Una definición es UNA versión de UN flujo de un producto de una instancia. La completa
# el Automation Expert conversando y la aprueba una persona (contrato del ciclo, 1b). Se
# guarda en la base; este YAML es la forma de leerla y de comparar dos versiones.
#
# Tipos
#   duración   entero + unidad: m, h o d ("0m", "60m", "24h", "1d", "30d"). Nunca negativa.
#   hora       "HH:MM", de "00:00" a "23:59". Quien escribe el YAML la pone entre comillas:
#              en YAML 1.1, 21:00 sin comillas se lee como un número.
#   flujo      la clave de otro flujo del mismo producto, o <producto>/<flujo> para uno de
#              otro producto de la instancia (att2/carrito).
#   producto   la clave con la que el manifiesto declara un producto de la instancia. Mientras
#              la instancia tenga uno solo, es el funnel_ref de [instancia] (en ATT1, att1).
#   a_medir · a_decidir · a_crear
#              un valor que todavía no se sabe. Se acepta en un borrador; una versión con uno
#              de estos no se publica. Una plantilla que no existe se escribe {a_crear: <nombre>}.
# Marcas
#   [existe: …]  hoy está en la base, en el manifiesto o en el código, con el mismo significado.
#   [NUEVO]      hace falta para escribir al menos uno de los siete flujos de validación.
# Una clave que no está en este esquema rechaza la definición entera.

esquema: setter-flujo/v1          # fijo

flujo: carrito                    # ^[a-z][a-z0-9_]{1,39}$. Única en su producto e igual en
                                  #   todas las versiones. Los flujos de hoy usan su nombre de
                                  #   [flujos] del manifiesto. [existe: FLOWS, instance_manifest.py:68]
                                  #   No es el scope_key: a ese se parece la bolsa.
producto: att1                    # producto. Default: el único de la instancia; con más de uno,
                                  #   obligatorio. Solo lo disparan los eventos de ese producto.
                                  #   [existe: un solo producto por instancia ([hotmart] del
                                  #   manifiesto) y external_product_id por scope] [NUEVO: el campo]
nombre: Carrito abandonado        # texto, hasta 60 caracteres, como lo llama la persona. [NUEVO]
version: 1                        # entero > 0. La pone el ciclo al publicar, nunca la conversación.
                                  #   [existe: version en pilot_scope_versions y followup_policy_versions]
estado: publicada                 # borrador | publicada. Default: borrador. Una publicada no se
                                  #   modifica: cambiarla es publicar la siguiente.
                                  #   [existe: status y los triggers de inmutabilidad]

# ── 1 · Qué lo dispara: exactamente uno, un evento o un silencio. Sin default ──
dispara:
  evento: carrito                 # carrito | pago_fallido | ficha_impresa | ficha_vencida |
                                  #   compra | intencion (el formulario de la landing).
                                  #   [existe: source + source_event_type, con 3 valores hoy]
                                  #   [NUEVO en ese CHECK: ficha_impresa, ficha_vencida, compra]
  # silencio:                     # en lugar de evento. [NUEVO: las condiciones del disparador]
  #   lo_ultimo: agente           # agente | persona. Obligatorio.
  #                               #   agente: el último mensaje es del AgentBot y nadie del
  #                               #   equipo escribió después del último de la persona.
  #                               #   persona: la persona escribió último y nadie le contestó.
  #   desde: 24h                  # edad mínima del último mensaje de la persona. Obligatorio.
  #   hasta: 72h                  # edad máxima: mayor que desde, hasta 30d. Hace de vencimiento.
  #   conversaciones: [abierta]   # abierta | resuelta. Default: [abierta].

# ── 2 · Quién entra ──
entra:
  permiso: formulario             # formulario (solo con un evento) | conversacion (solo con un
                                  #   silencio) | compra (solo con el evento compra).
                                  #   Default: conversacion con un silencio; formulario con un
                                  #   evento, la compra incluida.
                                  #   [existe: audience_mode consented_intent = formulario]
                                  #   [NUEVO: conversacion]
                                  #   compra: nuevo por la decisión del 2026-10-15; ningún flujo
                                  #   de validación lo usa todavía. Solo para un mensaje de
                                  #   utilidad que confirma la compra, y recién con el OK del
                                  #   cliente de la instancia (ADR-0007 §8).
  contesto_a: []                  # flujos. Default: []. Solo con permiso conversacion: entra solo
                                  #   donde la persona contestó una plantilla de esos flujos.
                                  #   [existe como regla fija: la conversación adoptada]
                                  #   [NUEVO como lista]

# ── 3 · Los pasos: una lista de arriba abajo, sin bucles ni saltos ──
pasos:                            # [existe: followup_policy_versions.steps] [NUEVO: el tipo de cada paso]
  - plantilla: att1_carrito_abandonado_01   # una plantilla APPROVED del catálogo del inbox, o
                                            #   {a_crear: <nombre propuesto>} mientras no exista
    idioma: es_MX                           # default: el de esa plantilla en el catálogo
    variables: [nombre, producto]           # en el orden {{1}}, {{2}}…: nombre | producto |
                                            #   medio_de_pago (solo con la ficha; va el nombre
                                            #   del medio) | cuando | precio | vence.
                                            #   Default: [nombre, producto].
                                            #   [existe: nombre y producto] [NUEVO: medio_de_pago]
                                            #   [1.1: cuando, precio y vence, los nombres con los
                                            #   que el bridge arma A1, Z y el seguimiento del pago
                                            #   rechazado: cart_first_touch.py:33,
                                            #   cart_followups.py:67-68,
                                            #   payment_failure_followups.py:65]
                                            #   cuando: el momento del intento (el abandono o el
                                            #   rechazo) en la zona del país del comprador (CDMX
                                            #   si Hotmart no la manda); solo con un disparador
                                            #   de evento.
                                            #   precio: el precio del producto que declara la
                                            #   instancia (RECOVERY_DISCOUNT_PRICE, «USD 47»),
                                            #   aunque el checkout muestre moneda local; solo con
                                            #   cupon: personal.
                                            #   vence: el vencimiento del cupón personal del paso,
                                            #   en la zona del país; solo con cupon: personal.
    # boton: {link: checkout, cupon: TIROIDES10}
    #                             # solo si la plantilla tiene un botón de URL dinámica.
    #                             #   link: checkout [existe: el del cupón] | ficha [NUEVO; solo
    #                             #   con la ficha]. cupon: opcional, solo con checkout: un código
    #                             #   fijo, o personal (el código por persona del worker del cupón;
    #                             #   a lo sumo dos por camino) [NUEVO en 1.1: personal]. En la v1
    #                             #   no se cambia conversando.
  # - esperar: 60m                # duración. Desde el paso anterior; la primera, desde el disparo.
  #                               #   [existe: grace_period y el plazo de cada paso]
  # - conversar: "objetivo"       # hasta 300 caracteres. Escribe el agente comercial. Solo en la
  #                               #   rama entonces de una condición con la ventana abierta.
  #                               #   [NUEVO; todavía no se publica]
  # - condicion: {dato: ventana, es: [abierta]}
  #                               # dato: ventana (abierta | cerrada) | medio_de_pago (solo con la
  #                               #   ficha; se compara con el código de Hotmart). [NUEVO]
  #   entonces: [ ...pasos ]
  #   si_no: [ ...pasos ]         # default: []
  # - derivar: "nota para el equipo"
  #                               # el equipo es [chatwoot].equipo_derivacion. Último de su rama.
  #                               #   [NUEVO como paso; todavía no se publica]
  # Default: un solo paso, una plantilla aprobada sin espera.
  # No se escriben: el máximo de mensajes (sale de los pasos), el ancla, el tipo de acción,
  # el modo del envío y el propósito. Los pone el motor.

# ── 4 · Cuándo termina ──
termina:                          # [NUEVO: las salidas como dato]
  si: [compra]                    # compra | contesta | ficha_vencida. Default: [compra]; [] con
                                  #   el evento compra.
                                  #   compra: compró, o hay una compra que podría ser suya.
                                  #   contesta: la persona escribe después del disparo y antes del
                                  #   primer envío; corta al llegar el mensaje, y la conversación
                                  #   sigue con el agente.
                                  #   ficha_vencida: solo con el disparador ficha_impresa.
  vence: 1d                       # duración desde el disparo. Default: 1d. Obligatorio con un
                                  #   evento; no va con un silencio (ahí vence el hasta).
                                  #   [existe: expires_after]
  # Cortan siempre, en código, sin escribirse: la baja, que una persona tome la conversación,
  # una respuesta después del primer envío aceptado (ADR-0007 §6), que le gane otro flujo, que
  # no quede horario antes de vencer y haber mandado todos los pasos.

# ── 5 · El horario y los topes ──
horario:                          # el objeto, o la palabra siempre.
                                  #   Default: el de los otros flujos de la instancia.
  zona: America/Mexico_City       # IANA. Default: [instancia].zona_horaria. Se elige al crear el
                                  #   flujo y después no cambia (código).
                                  #   [existe: timezone, constante por scope]
  desde: "09:00"                  # [existe: business_windows, con los días 1 a 7]
  hasta: "21:00"                  #   incluye ese minuto, como el motor de hoy
topes:
  bolsa: att1-recuperacion        # ^[a-z][a-z0-9_-]{1,39}$. Default: <producto>-<flujo>
                                  #   (att1-carrito). Los flujos de una bolsa cuentan juntos sus
                                  #   envíos, de todas sus versiones, y pueden ser de productos
                                  #   distintos. [existe: hoy lo hace el scope_key]
  por_dia: 80                     # entero > 0 | sin_tope. Día de la zona. Es el límite de ESTE
  total: 1000                     #   flujo sobre la cuenta de su bolsa; por_dia ≤ total.
                                  #   Default: 10 y 150 con un evento de recuperación; sin_tope
                                  #   con un silencio o con la compra.
                                  #   [existe: max_outbound_request_starts_per_day y _total]
                                  #   [NUEVO: sin_tope]
  repeticion:                     # cuántas veces le toca este flujo a la misma persona; siempre
                                  #   uno en curso a la vez. [NUEVO: hoy, en cada planificador]
    una_por: persona_y_oferta     # persona | persona_y_oferta | conversacion
    otra_despues_de: 0m           # duración | nunca: cuándo puede arrancar otro después del último
                                  #   envío aceptado o incierto. Un envío incierto deja el
                                  #   recorrido en curso hasta reconciliarse o vencer.
                                  #   Default: {persona_y_oferta, 0m} con un evento;
                                  #   {conversacion, nunca} con un silencio; {persona, nunca}
                                  #   con la compra.

# ── La prioridad frente a los otros flujos ── [NUEVO]
prioridad:                        # Default: sin relaciones; los flujos conviven.
  le_gana_a: [precheckout]        # flujos. Si a la persona le toca este flujo, esos ya no le
                                  #   escriben: no arrancan, y el que esperaba termina. Un flujo
                                  #   apagado no le gana a nadie.
  no_arranca_mientras: [pago_fallido]
                                  # flujos. Este no arranca si la persona tiene uno de esos en
                                  #   curso, con la misma clave de repetición. El disparo se pierde.

# ═══ Fuera de la versión ═══
# control:                        # uno por flujo. [existe: pilot_runtime_controls, hoy uno por scope]
#   version_activa: 1
#   estado: prendido              # prendido | pausado | apagado. Se pide conversando, con
#                                 #   aprobación, sin publicar otra versión. Un flujo nuevo
#                                 #   nace apagado.
# puerta:                         # uno por instancia.
#   tope_por_persona: sin_tope    # o {maximo: 2, cada: 24h}: de 1 a 20 envíos en una ventana de
#                                 #   1 h a 30 días, sumando los envíos de todos los flujos de
#                                 #   todos los productos de la instancia.
#                                 #   [existe: pilot_proactive_contact_caps, un renglón por tenant]
```

### Las reglas del validador

Son código y corren en dos momentos. Un bloqueo no se puede confirmar; una advertencia, sí.

**Al guardar un borrador.** Son reglas de forma. Los valores `a_medir`, `a_decidir` y `a_crear` se aceptan.
1. Una clave o un valor que no está en el esquema.
2. `dispara` que no tiene exactamente un evento o un silencio. Con un silencio, `desde` mayor o igual que `hasta`, o `hasta` de más de 30 días.
3. Un `permiso` que no corresponde al disparador: `formulario` va solo con un evento, `conversacion` solo con un silencio y `compra` solo con el disparador `compra`. `contesto_a` sin `permiso: conversacion`.
4. Un flujo disparado por un evento que no es la compra y que no tiene `compra` en `termina.si`, o la compra como salida de un flujo disparado por la compra. La baja no se escribe, porque corta siempre.
5. `ficha_vencida` como salida sin el disparador `ficha_impresa`. `medio_de_pago` o el link `ficha` sin un disparador de la ficha. Un `cupon` sin el link `checkout`. Desde la 1.1, como protección: `precio` o `vence` en un paso cuyo botón no lleva `cupon: personal` (`variable_needs_personal_coupon`), y `cuando` sin un disparador de evento (`variable_needs_event_trigger`). Van en la 5 y no en una regla nueva porque la base acepta reglas de 1 a 17 (`20261016000100:726`).
6. Un evento sin `vence`, o un silencio con `vence`.
7. Un `conversar` fuera de la rama `entonces` de una condición con la ventana abierta. Un `conversar` o un `derivar` que no es el último paso de su rama. Una condición dentro de otra.
8. Horas mal escritas, o un `desde` que no es menor que `hasta`. Una zona que no es IANA, o distinta de la de la versión anterior. Flujos de una misma bolsa con zonas distintas.
9. Topes que no son enteros positivos ni `sin_tope`. Un `por_dia` mayor que el `total`.
10. Un `le_gana_a`, `no_arranca_mientras` o `contesto_a` que nombra un flujo que no existe. Dos flujos que se ganan uno al otro. Un `producto` que la instancia no declara, o que falta cuando la instancia tiene más de uno. Dos flujos con la misma clave en el mismo producto.

**Al publicar.** Las anteriores, y además:
11. Un valor `a_medir`, `a_decidir` o `a_crear`.
12. Un `vence` que no supera la suma de las esperas.
13. Una plantilla que no está APPROVED en el catálogo del inbox, en ese idioma, con tantas variables como marcadores y con el botón que declara.
14. Algo que el motor de la instancia todavía no ejecuta o no trae. Hoy: más envíos por camino de los que ejecuta el motor para ese flujo, un paso `conversar` o `derivar`, los disparadores de la ficha y de la compra, `medio_de_pago`, y el `nombre` con la compra.
15. Un código de cupón distinto del de la versión publicada: en la v1 no se cambia conversando, y pasar de un código fijo a `personal`, o al revés, también es un cambio de código (`coupon_change_not_supported`). Más de dos `cupon: personal` en un camino (`coupon_personal_limit`): dos códigos por caso, la regla del 2026-10-17.
16. En los flujos de hoy, mientras `[flujos]` y `[plantillas]` del manifiesto sigan siendo el techo, una plantilla distinta de la que nombra `[plantillas]`. Prender uno de esos flujos exige, además, que esté en `true` en `[flujos]`.
17. Un flujo con `permiso: compra` que manda algo que no es una plantilla de utilidad, o más de un envío por camino. A diferencia de la regla 14, esta no se levanta cuando el motor mande más de un mensaje por camino.

Los bloqueos 14 y 15 son del tipo «todavía no». Los demás son protecciones, o errores de forma que el agente corrige antes de mostrar nada.

**Advertencias:**
- `horario: siempre` con una plantilla de marketing.
- Un flujo disparado por un evento de recuperación con `sin_tope`.
- Un flujo por silencio sin la salida `compra`: le puede llegar a quien ya compró.
- Dos flujos con el mismo disparador y sin relación entre sí.
- Una espera antes del primer envío en un flujo sin `contesta`: mientras espera, si la persona contesta otra plantilla nuestra, el agente no le responde hasta que este mensaje salga o venza (la regla de adopción). El agente ofrece sumar `contesta`.
- Un silencio cuya franja (`hasta` menos `desde`) es más corta que el tramo cerrado más largo del horario. Pierde conversaciones, porque fuera de horario el barrido no mira nada (`conversation-followup-discount-v1.md:121`).
- Una condición con una rama vacía en un flujo con `le_gana_a`: a quien cae en esa rama no le llega nada, y tampoco el flujo al que este le gana.

### Dónde vive hoy cada campo

Hoy un flujo del motor vive en dos filas que no se modifican y que están atadas entre sí:
- el scope (`pilot_scope_versions`): el disparador, el producto, la audiencia, las ofertas, los topes y la zona;
- la política (`followup_policy_versions`): los pasos, el horario, la gracia y el vencimiento.

Al 2026-10-14, el carrito y el pago fallido compartían las dos filas y el control; desde el 2026-10-19, cada uno tiene las suyas (`att1-recuperacion` y `att1-pago-rechazado`). El cupón y la reactivación no tienen filas: viven en variables y en el manifiesto. Para el lector de la 1a, cada flujo de hoy tiene arriba su línea «Fuente hoy».

Un cliente fuera de Lancemos no queda frenado por el tenant. El CHECK `tenant_key = 'lancemos'` de `20260810000100:11` ya se relajó en `20260903000300:3-7`. Quedan funciones con ese valor escrito, pero son del primer toque de Johanna (`20260829000300:294` y otras tres), no del camino de ATT1.

Las claves nuevas de los flujos rompen a los lectores que nombran los scopes de hoy:
- fuera de los repos, las consultas con las que se opera ATT1: la de los topes y el lector del estado del recuperador;
- en la instancia, `piloto.sh` y las alertas.

La migración los actualiza. Qué cambia en la base no se aprueba con este esquema: va como propuesta al contrato del ciclo (ver la sección siguiente).

## Lo que no entra en la v1, y dónde va

- **El ciclo:** proponer, aprobar, publicar y volver atrás, y quién aprueba. Va al contrato del ciclo, en la 1b. El esquema solo deja lugar para `version`, `estado` y el control. Ahí van también:
  - cómo cierra la conversación: la propuesta que el agente muestra antes del sí, al lado de la versión vigente y con lo que cambia resaltado (`automation-expert-configuration-experience.md:65-80`), y el canal por el que se aprueba;
  - que la propuesta diga qué pasa con los recorridos que están esperando. Hoy no salen y vencen (`pilot_scope_version_mismatch`);
  - que una propuesta pueda llevar versiones de varios flujos que se publican juntas, como el carrito y el pago fallido cuando se cambia su bolsa.
- **Qué pasa con quien está a mitad de un flujo cuando cambia la versión.** Hoy el piloto lo deja sin salir, y el ADR-0007 §5 dice que sigue con su versión. Va al contrato del ciclo.
- **El entorno de prueba:** la cohorte, el modo de un solo teléfono del cupón y la sombra. Va al contrato del ciclo.
- **Cómo pasa a la puerta cada diferencia de hoy.** Va al contrato del ciclo (decisión de Dan del 2026-10-14).
- **Dónde se guarda, y qué cambia en la base.** Va al contrato del ciclo, con estas propuestas:
  - cada flujo con su propia política, tomada de la definición y no de `FOLLOWUP_POLICY_KEY`. El carrito y el pago fallido arrancan con dos políticas de valores iguales;
  - un control por flujo en `pilot_runtime_controls`, que hoy tiene uno por scope, o uno común para la bolsa;
  - los topes contados por bolsa, que arrastran lo gastado: `att1-recuperacion`, de la v1 a la v5, y `att1-primer-contacto`, de la v1 y la v2 (`20260930000300:622-634`);
  - ampliar los CHECK de `source`, `source_event_type`, `audience_mode` y `purpose` en el scope, la política, las autorizaciones y las bajas (`20260829000300:10-20`; `20260930000300:78-86`; `20260810000100:22`; `20260803000100:23` y `:82`; `20260809000100:9`), y admitir «sin tope» en los topes, que hoy son obligatorios y mayores que 0 (`20260810000100:27-30`);
  - el lock por persona siempre, porque las relaciones entre flujos lo necesitan (hoy existe solo con renglón del tope, `20261007000100:49-56`), y `pilot_proactive_contact_caps` con versión;
  - si el scope y la política se publican juntos o pasan a una sola tabla.
- **Las conversaciones de prueba del Automation Expert.** Van a sus casos de prueba. Las cinco que se escribieron con la primera versión de este esquema quedan como insumo, con tres cambios:
  - «pausá el carrito»: hoy frena también el pago fallido;
  - faltan dos casos, uno con la bolsa compartida («bajá el tope por día») y otro que todavía no se puede («un segundo recordatorio»);
  - sale el de la palabra clave.
- **El dibujo de un flujo.** Va a la skill de la 1a que dibuja un flujo, y sale solo de la definición.
- **La lista cerrada de motivos,** con la que la 1a contesta «por qué no le llegó». Va al contrato de lectura de la 1a. La puerta deja acá el motivo de cada chequeo. Guardar los rechazos al arrancar, que hoy quedan solo en el log (`worker.py:1540-1562`), es una pieza aparte, con la 1b, como recomienda el análisis de la Etapa 1 del 2026-10-14.
- **La adopción de las plantillas nuevas.** Va a la 1b. La ficha y la activación necesitan que se adopte la respuesta a la plantilla de cualquier flujo, que la reserva del cupón filtre por el flujo de la adopción para cumplir `contesto_a`, y que la adopción aplique `contesta` antes de mirar si hay otro paso pendiente.
- **Las palabras clave en un mensaje entrante, como disparador.** No entran: «no es necesario» (decisión de Dan del 2026-10-14).
- **Los pedidos de Brayan del 2026-10-01:** las ramas según el botón que tocó la persona o según si no contestó, el encabezado con imagen o video y las variables por campos personalizados. Entran cuando algún flujo los use, como otro `dato` de la condición o como otro parámetro de la plantilla.
- **Más de un mensaje por camino, y `conversar` y `derivar` funcionando.** Van a la 1b, junto con los máximos de frecuencia en código.
- **El mail de bienvenida.** El producto no manda mails. Es una decisión aparte.
- **Las fechas fijas, las listas y los lanzamientos, con su pausa de grupo.** Van a la Etapa 4.
- **Una prioridad numérica, o un arbitraje global entre casos.** El ADR-0007 §12 lo deja para más adelante, y las dos relaciones cubren lo que existe hoy.
- **Un flujo para varios productos a la vez, o con más de un disparador.** Cada flujo es de un producto y tiene un disparador. Varios productos pueden compartir una bolsa.
- **Lo demás de varios productos por instancia.** Va al diseño de la Etapa 2: el manifiesto con varios productos, de qué producto es una conversación (de eso dependen el silencio y el entrante sin formulario), la elección de ofertas y si la compra de un producto corta los flujos de otro. Si hace falta lo último, se suma a `termina` sin cambiar el resto.
- **Los días de la semana y más de una franja por día.** La base ya los soporta (`business_windows`, `20260803000100:1000-1012`), pero ningún flujo los usa. Si alguien pide «que no salga los domingos», el agente contesta con la frase de «todavía no».
- **Filtrar quién entra por el medio de pago** («solo OXXO»). Ningún flujo lo usa: la ficha le escribe a todos los medios, con el botón solo para OXXO. Entra cuando alguien lo pida.
- **La familia como campo.** Se deduce del disparador: Hotmart y el formulario son recuperación, el silencio es seguimiento y la compra es activación.
- **El intervalo, las páginas y el máximo por barrido de los silencios.** Son parámetros del motor, por instancia.
- **Lo que no es un flujo:** el agente que contesta, el monitor de trabadas, la reanudación, la derivación del entrante y la inferencia del nombre. Quedan en código.
- **Lo que dice el agente** (el SOUL y el conocimiento), salvo el objetivo de `conversar`. Incluye el código del cupón, que vive también en el conocimiento. Depende de la respuesta de Juan sobre de quién es el flujo comercial.
- **La simulación previa de un cambio.** Es un tema abierto del diseño del Automation Expert (`automation-expert-configuration-experience.md`, §8).
- **El cupón escalonado** (Juan, 2026-07-23). Entra cuando alguien lo pida.
- **Los avisos de las alertas de la instancia.** Van a la Etapa 2.

## De dónde sale

**Cómo se armó.**
- Se escribieron tres diseños con enfoques distintos: la continuidad con lo que corre, la conversación y la convivencia entre flujos.
- Hubo dos revisiones. La de producto eligió el diseño de convivencia, y la de ingeniería, el de continuidad.
- La base es el de convivencia, porque es el que respeta sin excepciones lo que Dan confirmó el 2026-10-14: una sola puerta igual para todos, la prioridad como dato y un esquema pensado para conversar.
- Del diseño de continuidad vienen la bolsa, el mapa de las tablas, la paridad campo por campo y la fuente de cada flujo para la 1a.
- Del diseño de conversación vienen las conversaciones de prueba, que pasan a los casos de prueba del Automation Expert, y las advertencias del validador.
- Después lo revisaron tres verificadores: la paridad con el código, el respeto de lo decidido y una conversación de prueba. Esta versión aplica lo que trajeron con evidencia.
- Los ocho bloques de flujo se probaron con un validador de prueba que aplica las reglas de este documento.

**Las decisiones y las mediciones.** Salvo el ADR-0025, están registradas fuera de este repo, y se citan por su fecha:
- Decisión de Dan del 2026-10-15: aprueba este esquema, y la compra como permiso para la bienvenida, solo para un mensaje de utilidad y con el OK del cliente. El mismo día aclaró que ese mensaje es uno solo (la regla 17).
- Decisión de Dan del 2026-10-15, por la noche: el diseño de varios productos por instancia va en paralelo con la Etapa 1, y este esquema se enmienda antes de que la 1b construya el motor, para que cada flujo nazca sabiendo de qué producto es. Sale del relevamiento de ese día del código en `448a252`: el manifiesto, el binding, los scopes y la clave de un flujo asumen un solo producto.
- Decisión de Dan del 2026-10-14: las cinco piezas, lo que se cambia conversando y lo que queda en código, la puerta única con el tope por persona, que no hay palabras clave como disparador y que las diferencias de hoy se deciden en el contrato del ciclo.
- Decisiones de Dan del 2026-10-13: la Etapa 1 partida en tres, con los flujos como datos en el centro, y que cada instancia tiene su Hermes. Esa segunda está en este repo, como el [ADR-0025](../decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md), con su §7.
- El análisis de la Etapa 1 del 2026-10-14: el esquema, el contrato de lectura, los casos de prueba, el dibujo, por qué el esquema va antes que el lector de la 1a, y sus recomendaciones sobre la propuesta del tope por persona, los rechazos al arrancar, el alcance de la 1b y la puerta común.
- Decisión de Dan del 2026-10-09, al prender el pago fallido en ATT1: el doble mensaje de día, aceptado mientras falte el tope por persona; lo que pasa de noche; y que pausar `att1-recuperacion` frena los dos flujos. Del mismo día, la propuesta, todavía sin decidir, de cancelar la plantilla que supera el tope por persona.
- Decisión de Dan del 2026-10-12 sobre los pagos que no se completan: el seguimiento de la ficha sin pagar, y no fijar los medios de pago de un país.
- Decisión de Dan del 2026-10-07: la activación después de la compra pasa al producto. Ese día también fijó el objetivo del Automation Expert: que los flujos de un infoproductor no choquen entre sí en un solo número.
- La propuesta del 2026-09-23, sin construir: la reactivación dentro de la ventana.
- Las mediciones en ATT1: la demora de Hotmart (2026-10-06); el carrito a los 40-52 minutos del formulario, una respuesta a la plantilla del carrito que no se adoptó (la conversación 21), la apertura del primer contacto v2 y su pausa (2026-10-09); la v5 del carrito (2026-10-13); y, en att1-db, solo leyendo, el 2026-10-14 a las 22:51Z y a las 22:59Z, las fichas, los cupones, las adopciones, el tope por persona y la convivencia.

**El producto:**
- `docs/contracts/followup-engine-v1.md`: :24-81, :288-321, :362-374 y :548-570.
- `docs/decisions/0007-durable-next-action-engine.md`: §3 a §9, §11, «Estados y resultados mínimos» y §12.
- `docs/decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md`: :86-99 y :109.
- `docs/design/automation-expert-configuration-experience.md`: :15-80, :104-114 y :135-145.
- `docs/contracts/conversation-followup-discount-v1.md`: :9, :62-99 y :103-126.
- `docs/contracts/conversation-reactivation-v1.md`: :69-112 y :136-160.
- `docs/contracts/portable-precheckout-first-contact-v1.md`: :13-16, :135-171 y :248-252.
- `docs/contracts/resume-paused-conversation-v1.md`: :18-36.
- `docs/referencia-manifiesto.md`: :84-120.
- Las migraciones de `supabase/migrations/`:
  - `20260803000100`: :19-82, :475-485, :705-716 y :1000-1012;
  - `20260805000300`: :256-474;
  - `20260809000100`: :9;
  - `20260810000100`: :7-201;
  - `20260813000100`: :7-44 y :395-440;
  - `20260829000300`: :10-20;
  - `20260903000300`: :3-7;
  - `20260904000100`: :87-98;
  - `20260928000400`: :262-290;
  - `20260930000300`: :75-86, :620-640 y :835-846;
  - `20261001000100`: :1075-1217;
  - `20261001000200`: :193-364, :826-862 y :1665-1677;
  - `20261001000400`: :1-55;
  - `20261001000500`: :275-296;
  - `20261007000100`: :1-91;
  - `20261010000100`: :1-36;
  - `20261012000100`: :1-40.
- El código de `src/bridge/`:
  - `followup_discount.py`: :54-76, :464-517, :625, :855-860 y :901-906;
  - `reactivation.py`: :321-327, :363-365 y :454;
  - `messaging.py`: :79-81 y :186-214;
  - `instance_manifest.py`: :55-89 y :185;
  - `hotmart.py`: :32-77;
  - `resolution.py`: :277-345;
  - `app.py`: :1083, :3864 y :7645-7692;
  - `worker.py`: :990-1056, :1528-1562 y :2390-2402.
- Las pruebas: `tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json` (solo la nota de captura y los nombres de los campos) y `tests/test_hotmart_payment_slip_events.py:1-12`.

**La instancia ATT1:**
- `instancia.toml`: :21, :23, :33, :94-109 y :175-202.
- `despliegue/base/aprovisionar-att1.sql`: :184-253.
- `despliegue/base/publicar-recuperacion-v5.sql`: :1-78.
- `despliegue/base/publicar-primer-contacto-v2.sql`: :1-40.
- `despliegue/secretos/generar.py`: :146, :160, :165-169 y :183-198.
- `conocimiento/knowledge-v6.toml` (en `fefdef8`): también explica el cupón (:190-196).

## Historial

- **1.0, 2026-10-15.** El esquema, con su enmienda de producto del mismo día.
- **1.1, 2026-10-21 (la T2.1 del motor, decisión de Dan del 2026-10-21 de que el motor ejecuta solo el carrito en la Etapa 1).** Sin cambiar `setter-flujo/v1` ni ninguna definición escrita antes:
  - se levanta «un solo envío por camino»: el motor ejecuta los envíos de la forma de su flujo del legado (`flow_policy.STEPS_BY_LEGACY_KIND`: el carrito, A1, A2 y Z; el pago fallido, B1 y su seguimiento; el primer contacto, uno), hasta tres, con la espera entre envíos. Una definición con más envíos que esa forma, o con el cupón personal en un paso que no lo admite, no se compila (`flow_policy_not_compilable:<detalle>`): agregar o sacar un toque es un cambio de código;
  - `boton.cupon` acepta `personal`, el código por persona del worker del cupón, a lo sumo dos veces por camino (regla 15, `coupon_personal_limit`). Pasar de un código fijo a `personal`, o al revés, sigue siendo `coupon_change_not_supported`;
  - el carrito y el pago fallido de ATT1 ya no comparten bolsa desde el 2026-10-19 (`att1-recuperacion` y `att1-pago-rechazado`). Los bloques 1 y 2 de «Los siete flujos de validación» siguen siendo la línea de base del 2026-10-14, con la bolsa compartida;
  - la regla 17 no cambia: con `permiso: compra`, un solo envío por camino;
  - tres variables nuevas, con los nombres del bridge: `cuando` (el momento del intento en la zona del país), `precio` (el que declara la instancia) y `vence` (el vencimiento del cupón personal). La regla 5 suma dos protecciones: `precio` y `vence` solo en un paso con `cupon: personal`, y `cuando` solo con un disparador de evento.
