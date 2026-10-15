# Contrato: el ciclo de un cambio de flujo (v1)

- **Estado:** aceptado como contrato conceptual el 2026-10-15; sin implementación (lo construye la 1b). El contrato no describe nada desplegado ni activado. Lo que dice de ATT1 es lo que pasó, con su fecha y de dónde sale.
- **Fecha:** 2026-10-15
- **ADR:** [ADR-0025](../decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md) §7, [ADR-0007](../decisions/0007-durable-next-action-engine.md) §5 y [ADR-0017](../decisions/0017-central-slack-operations-connector.md)
- **Contrato:** `flow-change-cycle-v1`, versión `1.0.0`
- **Alcance:** cómo se cambia un flujo que ya está escrito como dato. Fija:
  - quién lo propone;
  - qué lleva la propuesta y cómo se valida;
  - quién la aprueba y por qué canal;
  - cómo se publica;
  - qué pasa con los recorridos que están en curso;
  - cómo se prende, se pausa, se apaga y se vuelve atrás;
  - cómo se prueba.

  Deja propuestas para la base. La forma exacta de los pedidos y las respuestas queda para cada pieza.
- **Piezas:** la 1b se construye en partes separadas, que este contrato llama «piezas»: el almacén de las definiciones en la base, el motor que las ejecuta, el canal de aprobación, las herramientas del Automation Expert y la migración de ATT1. Cada una tiene su PR.
- **Etapas:** las del roadmap macro del producto, como en [flow-definition-v1.md](flow-definition-v1.md). La 1a es el Automation Expert que lee y explica los flujos; la 1b, los flujos como datos, con su motor y este ciclo; y la 1c, los flujos nuevos.
- **Citas:** las citas de código son de `origin/main` en `a0e3d1c`. Entre `1b83191` (1.8.0) y `a0e3d1c` no cambió nada en `src/`, `supabase/` ni `scripts/`, así que valen igual que las de los otros dos contratos. Las migraciones de `supabase/migrations/` se citan por su prefijo. `piloto.sh`, `publicar-recuperacion-v5.sql` y `camino-a-produccion.md` son de la instancia ATT1 (`setter-instancia-att1`), en `48d77f4`. Lo que pasó en la operación de ATT1 y no está en un repo se dice con su fecha, como en el esquema. En este contrato, «el esquema» es [flow-definition-v1.md](flow-definition-v1.md) y «el contrato de lectura» es [automation-expert-read-v1.md](automation-expert-read-v1.md), y sus secciones se citan por su título. «Hoy» es ese código y la instancia ATT1 al 2026-10-15.
- **Relacionados:** [flow-definition-v1.md](flow-definition-v1.md), [automation-expert-read-v1.md](automation-expert-read-v1.md), [lancemos-pilot-boundary-v1.md](lancemos-pilot-boundary-v1.md), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [operator-correlation-resolution-v1.md](operator-correlation-resolution-v1.md), [slack-correlation-resolution-v1.md](slack-correlation-resolution-v1.md), [slack-operations-connector-v1.md](slack-operations-connector-v1.md) y el diseño [automation-expert-configuration-experience.md](../design/automation-expert-configuration-experience.md).

## Para qué

Hoy, cambiar un flujo de ATT1 pide escribir SQL, cambiar una variable del bridge, mergear un PR de la instancia y redesplegar. Los comandos los corre una persona en su terminal (`camino-a-produccion.md:457-494`).
- Del carrito se publicaron así cinco versiones, y se armaron cuatro: la v1 nunca se armó (`publicar-recuperacion-v5.sql:6-7`).
- El 2026-10-13 se pasó de la v4 a la v5 con 5 carritos esperando, y ninguno salió. Cada uno hizo 144 intentos, sin una autorización ni un mensaje. El motivo que se deduce es la versión, sin el log que lo confirme (contrato de lectura, «Las reglas de la historia» y «Lo que se confirma al implementar»).
- Nunca se volvió a una versión anterior: solo se pausó.

El ADR-0025 §7 fija el destino. Los flujos son datos con versión en la base, y el Automation Expert propone cambios por una API acotada. **Una persona aprueba antes de publicar**, y se puede volver a la versión anterior **sin redesplegar**. Este contrato dice cómo.

Resuelve además lo que el esquema le dejó (esquema, «Lo que no entra en la v1, y dónde va»), salvo lo que vuelve a delegar con nombre. Y resuelve cinco de los temas abiertos del diseño del 2026-08-07 (`automation-expert-configuration-experience.md:135-145`):
- el contenido de la propuesta;
- los roles de aprobación;
- la advertencia y el bloqueo;
- la explicación previa;
- la simulación.

## En corto

1. **Todo cambio es una propuesta.** Puede llevar versiones nuevas de uno o varios flujos, volver a una versión ya publicada, o prender o apagar un flujo. Se publica entera o no se publica.
2. **El Automation Expert solo propone, valida y pausa.** No puede aprobar ni publicar, porque no tiene la clave para hacerlo.
3. **Una persona aprueba, por un canal que el agente no toca:** un botón firmado de Slack, que verifica quién lo apretó contra una lista de aprobadores. El aprobador queda registrado por su ID de Slack verificado, no por un texto.
4. **Publicar es un solo paso atómico en la base.** Guarda las versiones nuevas como inmutables, mueve el control de cada flujo a su versión y deja el registro. El motor lee la versión activa de la base: no hay variable que cambiar ni nada que redesplegar.
5. **Lo que está en curso sigue con la versión que lo empezó,** salvo que la propuesta pida cancelar los que esperan y el aprobador lo vea antes del sí. Así un cambio de versión ya no deja carritos sin salir, y se puede hacer a cualquier hora.
6. **Volver atrás es activar la versión anterior,** que ya estuvo publicada, con un solo sí. **Pausar no pasa por una propuesta:** la pide un aprobador y es inmediata.
7. **Antes del sí, la propuesta muestra todo:**
   - qué cambia, comparado con la versión vigente;
   - a quién afecta;
   - qué pasa con lo que está esperando;
   - qué habría hecho la versión nueva con los eventos reales de los últimos 7 días;
   - cómo se vuelve atrás.

## El recorrido de un cambio

```
conversación ─▶ borrador ─▶ validada ─▶ aprobada ─▶ publicada
                   │            │            │
                   └────────────┴────────────┴─▶ descartada · vencida
```

### Qué lleva una propuesta

Una propuesta lleva uno o varios ítems, cada uno sobre un flujo:
- **una versión nueva:** la definición completa, en el formato del esquema;
- **volver a una versión publicada:** el número de esa versión;
- **prender** o **apagar** el flujo.

Pausar no es un ítem: va por su propio camino (ver «Prender, pausar y apagar»). Las cuatro clases de ítem las arma el agente con `propose_flow_change`, y el canal de aprobación solo las aprueba y las ejecuta.

### 1. La conversación arma el borrador

- La persona que opera la instancia pide el cambio por mensaje directo. En ATT1 son Brayan o Dan. Por ejemplo: «que el mensaje del carrito salga a las 2 horas del abandono».
- El Automation Expert lee el flujo vigente (`list_flows`, del contrato de lectura) y arma la definición nueva en el formato del esquema. La guarda como **borrador** con `propose_flow_change`.
  - En el borrador, cada definición va con `estado: borrador` y sin `version`: el número lo asigna el ciclo al publicar (esquema, «La versión, el control y el techo»).
- Un borrador se puede rehacer cuantas veces haga falta. Guarda la **versión base** de cada flujo que toca, que es contra lo que se compara y lo que se revalida al publicar.
- Al guardarlo corren las reglas 1 a 10 del validador, las de forma (esquema, «Las reglas del validador»). Se aceptan `a_medir`, `a_decidir` y `a_crear`.

### 2. La validación

- `validate_flow_change` corre todas las reglas, de la 1 a la 17. Las propias de publicar son de la 11 a la 17, y algunas miran el estado vivo de la instancia:
  - el catálogo de plantillas de la bandeja (regla 13);
  - la versión publicada (regla 15);
  - el manifiesto, mientras siga siendo el techo de los flujos de hoy (regla 16).
- Devuelve **bloqueos**, con los que no se puede pedir el sí, y **advertencias**, que el aprobador ve y no impiden el sí.
- Arma la **vista previa**, que es lo que el aprobador ve. El diseño pide mostrar cómo está configurado ahora, qué cambio se propone, qué límites siguen valiendo y cuándo empieza a usarse (`automation-expert-configuration-experience.md:34-42` y `:65-80`). La vista previa lleva:
  - el cambio al lado de la versión vigente, con lo que cambia resaltado;
  - para qué lo pidió la persona, con sus palabras;
  - a quién empieza o deja de llegarle;
  - **qué pasa con lo que está esperando:** cuántos recorridos hay en curso y qué va a pasar con ellos (ver «Lo que está en curso cuando cambia la versión»);
  - **la simulación:** qué habría hecho la versión nueva con los eventos reales de los últimos 7 días, contra lo que hizo la vigente. Tiene esta forma: «con la espera de 2 horas, N de los M carritos de la semana habrían recibido el mensaje; de los otros, tantos ya habían comprado y tantos habían contestado»;
  - las advertencias;
  - que empieza a valer en el momento de publicar;
  - cómo se vuelve atrás.
- Una propuesta validada queda **congelada**: su contenido ya no cambia. Si hace falta tocarla, se hace otra propuesta.
- Una propuesta validada **vale 24 horas**. Pasado ese plazo, aprobada o no, ya no se publica: vence, y se valida de nuevo, porque la simulación y el estado vivo envejecen.

### 3. La aprobación

- El agente le dice a la persona que la propuesta está lista y le muestra el resumen. El sí **no** se da escribiendo en la charla: se da en el canal de aprobación (decisión 2).
- Se aprueba exactamente la propuesta validada. Su huella viaja con el sí: un hash del contenido y de las versiones base.
- Quien aprueba tiene que estar en la lista de aprobadores de la instancia (decisión 1).

### 4. La publicación

- Es una función de la base, atómica, que solo se puede llamar con una aprobación válida.
- Justo antes, el bridge relee el catálogo de plantillas de la bandeja y le pasa el resultado. La base no tiene ese catálogo: hoy lo lee el bridge de Chatwoot (`approved_templates.py:112`).
- Bajo un lock, la función hace esto:
  1. Revalida que la versión base de cada flujo siga siendo la activa. Si no, devuelve `flow_change_stale_base`, y hay que rehacer la propuesta. Revalida también la huella y que las plantillas sigan aprobadas.
  2. Para cada versión nueva, la guarda como `publicada` e inmutable, con su número: el siguiente de ese flujo.
  3. Mueve el control de cada flujo a su versión. Es la nueva, o la publicada a la que se vuelve. Para prender o apagar, cambia el estado.

     Cada cambio del control se hace con un CAS sobre la generación, como hoy: el control cambia solo si su generación sigue siendo la que se leyó (`20260810000100:377-381`).
  4. Aplica lo que la propuesta dice de los recorridos en curso.
  5. Deja los eventos de la propuesta, la validación, la aprobación con el aprobador, y la publicación.
- Si falla cualquier paso, no se publica nada. Una propuesta con varios flujos se publica entera o no se publica.
- **Publicar incluye activar y no toca el estado del control:** un flujo prendido sigue prendido con la versión nueva.
  - Hoy son dos pasos. Publicar no toca el control (`publicar-recuperacion-v5.sql:63`), y activar exige pausar antes y deja el control en `inactive`, así que hay que volver a armarlo (`20260810000100:178-186` y `:391-410`; `publicar-recuperacion-v5.sql:65-70`).
- **El motor lee la versión activa de la base.** El bridge deja de tener la versión en una variable (`LANCEMOS_PILOT_SCOPE_VERSION` y `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION`, `app.py:1247-1252`). Publicar no reinicia nada.

### 5. Prender, pausar y apagar

Es el **control** de cada flujo, que vive afuera de la versión, uno por flujo (esquema, «La versión, el control y el techo»; decisión 8):

| Pedido | Cómo se pide | Qué hace |
|---|---|---|
| **Pausar** | Lo pide un aprobador, con un solo botón, o el agente, cuando un aprobador se lo pide en la charla. No pasa por una propuesta | No arranca recorridos nuevos. Los que estaban en curso esperan sin mandar: si el flujo se prende antes de que venzan, salen. |
| **Prender**, o sacar la pausa | Con una propuesta y su sí | Arranca recorridos nuevos con la versión activa. |
| **Apagar** | Con una propuesta y su sí | Los recorridos en curso terminan, y un disparo ni se registra. |

- **Las transiciones:**
  - un flujo apagado solo se prende;
  - uno prendido se pausa o se apaga;
  - uno pausado se prende o se apaga.
- **Apagado no es terminal.** Un flujo nuevo nace apagado y se prende con un sí (esquema). Hoy hay cuatro estados, y `closed` es terminal (`20260810000100:127-136` y `:162-170`). Retirar un flujo para siempre queda afuera de este contrato.
- **Pausar no pasa por una propuesta** porque es la dirección segura y, ante un problema, tiene que ser inmediata. El esquema dice que prender y pausar se piden «con aprobación». Para pausar, el pedido del aprobador es esa aprobación: no hace falta además una propuesta con su botón. Hay un caso en el registro de ATT1: el primer contacto se pausó el 2026-10-09 (esquema, «La versión, el control y el techo»). Queda registrado quién pausó (ver «Elecciones técnicas, con su valor por defecto»).
- Al proponer una pausa, el agente avisa lo que el esquema le pide avisar. Los disparos que lleguen en ese rato no vuelven. Y lo que el flujo le gana a otro sigue valiendo aunque esté pausado.

### 6. Volver atrás

- **Es activar la versión anterior,** que ya estuvo publicada. No se edita nada, porque las versiones son inmutables.
- Va como una propuesta más, con su vista previa y su simulación. La diferencia es que el agente la arma sin conversar: «volvé a la versión anterior».
- Lo que está en curso con la versión de la que se vuelve sigue la misma regla que en cualquier cambio (decisión 3).
- **Los topes se arrastran:** cuentan lo gastado por la bolsa en todas sus versiones, como hoy (`20260930000300:622-636`). Volver atrás no devuelve cupo.

## Lo que está en curso cuando cambia la versión

Es el punto más delicado. Hoy el ADR y el código dicen cosas opuestas:
- **El ADR-0007 §5** dice que las secuencias siguen con la versión que las originó y que no migran en silencio cuando se publica otra.
- **El código del piloto no lo cumple.** Un caso atado a la versión vieja ya no arranca, porque la autorización lo rechaza con `pilot_scope_version_mismatch` (`20260930000300:490-494`).
  - El despachador deja el motivo solo en el log y lo vuelve a intentar en cada vuelta hasta que el caso vence, sin mandar nada (`worker.py:1540-1562`).
  - El runbook dice «no se reintenta». Para la persona da lo mismo, porque no sale nada.
- **Por eso el runbook de ATT1 pide cambiar de versión de día y «sin carritos esperando»** (`camino-a-produccion.md:457`). Los 5 carritos del 2026-10-13 se perdieron porque el cambio se hizo con ellos esperando.

La regla del contrato (decisión 3): **lo que está en curso sigue con su versión**. La propuesta puede pedir otra cosa, a la vista del aprobador. Por flujo hay tres opciones:
- **seguir**, la opción por defecto. Los recorridos que ya empezaron terminan con la versión que los empezó, con sus esperas y su plantilla. Los nuevos arrancan con la versión nueva. En la puerta, el chequeo 7, «El flujo en marcha» (esquema, «La puerta, chequeo por chequeo»), deja pasar el recorrido que sigue con su versión;
- **cancelar los que esperan:** los que todavía no mandaron nada terminan con el motivo `cambio_de_version`. Sirve si la versión vieja tenía un error;
- **migrar:** los que esperan pasan a la versión nueva y se vuelven a planificar. Queda para más adelante, porque es la operación más compleja y hoy ningún caso la pide.

La vista previa dice cuántos recorridos hay en curso y cuál de las opciones se va a aplicar.

El contrato de lectura le dejó a este contrato qué pasa con el chequeo 7, y hoy lo muestra con `reason` en `null`. Con este ciclo, en su próxima versión menor, suma `cambio_de_version` a su lista cerrada de motivos.

## Qué puede cambiar el Automation Expert en la 1b

Lo que hoy pide SQL y redesplegar (decisión 4):
- **las esperas y el plazo** (`esperar` y `vence`);
- **la plantilla** de un paso, solo por otra **ya aprobada** en Meta y en la bandeja (regla 13).
  - En los flujos de hoy, recién después de su migración. Hasta entonces, la regla 16 frena toda plantilla distinta de la que nombra el manifiesto;
- **los topes de la bolsa,** por día y total;
- **el horario**;
- **prender, pausar y apagar** un flujo.

No entra:
- **crear flujos nuevos:** es la 1c;
- **el código del cupón:** vive también en Hotmart y en el conocimiento, y la regla 15 lo bloquea;
- **el tope por persona:** es de la puerta, uno por instancia y afuera de toda versión (esquema, «La definición formal», `puerta`). Va con la puerta común;
- **el link de la clase de MRT,** que vive en el conocimiento del agente comercial de ATT1, no en un flujo. Sigue cambiándose por derivación a una persona (decisión 7).

Los pasos `conversar` y `derivar`, y más de un mensaje por camino, se pueden escribir, pero no se publican hasta que el motor de la 1b los ejecute (regla 14).

## Cómo se prueba un cambio

Hoy un cambio se prueba de cuatro maneras:
- copias de la base en local;
- el teléfono de prueba en una cohorte;
- el modo de un solo teléfono del cupón;
- y, la mayoría de las veces, el primer caso real hace de E2E.

La regla del contrato (decisión 5):
- **Todo cambio pasa por la simulación** de la vista previa: la versión nueva contra los eventos reales de los últimos 7 días. Es solo lectura y no necesita un entorno aparte.
- **Un flujo nuevo** (1c) pasa por la **cohorte de prueba**, con el teléfono que elige Dan, antes de prenderse para todos. En ATT1 se hizo así con `piloto.sh` (`piloto.sh:4-5`, «sembrar a la persona de prueba»).
  - Cómo corre un flujo solo para la cohorte lo fija la pieza del almacén, con la 1c: un estado del control o un campo. Hoy la cohorte vive en el scope.
- **El motor de la 1b** se prueba con la **paridad** antes de reemplazar al de hoy. Los flujos de hoy, escritos como definiciones, tienen que hacer exactamente lo mismo que el código contra los mismos eventos.
- El ciclo no usa la sombra ni el modo de un solo teléfono del cupón, que el esquema también le dejó (esquema, «Lo que no entra en la v1, y dónde va»). Si siguen o no se decide con la migración de cada flujo.

## Cómo pasan los flujos de hoy

- **Tal como corren** (decisión 6). Los flujos de ATT1 se escriben como definiciones que hacen exactamente lo de hoy, y la paridad lo prueba.
- **Las diferencias, como excepciones.** El esquema lista las que trae la puerta única («Lo que cambia respecto de hoy»), y antes se miden en att1-db, solo leyendo, como pide el esquema.
  - El esquema es cerrado, así que una excepción no puede ser una clave de la definición. Vive en el motor, por flujo y con nombre.
  - La pieza del motor hace la lista diferencia por diferencia: cuáles se conservan como excepción y cuáles cambian igual al migrar, con su medición.
  - **La separación del carrito y el pago fallido no puede ser una excepción:** en el esquema son dos flujos, con un control cada uno (decisión 8). Arrancan con los valores de hoy.
  - La recomendación del esquema para la pausa que el equipo dejó en el primer contacto era aceptar la diferencia. Con esta decisión, también se conserva al migrar, y aceptarla es un arreglo de después.
- **Cada arreglo va después, como un cambio aparte** que aprueba Dan. Por ejemplo, que la toma humana termine el carrito, o que el cupón pase por el tope por persona.
  - Si el arreglo es sacar una excepción del motor, es código y va por release.
- **El orden:**
  1. el carrito y el pago fallido, que comparten la bolsa;
  2. el primer contacto;
  3. el cupón;
  4. la reactivación, que está apagada en ATT1.
- La migración actualiza también lo que nombra los scopes de hoy: `piloto.sh`, las alertas de la instancia y las consultas con las que se opera ATT1 (esquema, «Dónde vive hoy cada campo»).

## Las rutas y las herramientas

En el bridge van bajo `/internal/automation-expert/v1/`, como las de lectura. Están apagadas por defecto con su propio flag y tienen una clave propia, distinta de la de lectura:
- `POST /flow-changes` crea o rehace un borrador;
- `POST /flow-changes/{id}/validate` valida y devuelve la vista previa;
- `GET /flow-changes/{id}` la relee, para mostrarla de nuevo sin validarla otra vez;
- `POST /flows/{flow_key}/pause` pausa.

`flow_key` es el `flujo` del esquema.

En el profile `automation-expert` hay tres herramientas nuevas: `propose_flow_change`, `validate_flow_change` y `pause_flow`. Ninguna aprueba ni publica. Van en su propio plugin (ver «Elecciones técnicas, con su valor por defecto»).

El sí a cualquier propuesta entra por el **canal de aprobación**, con otra clave que el profile del Automation Expert no tiene. Sus rutas las fija la pieza del canal.

## Qué cambia en la base

Son propuestas para la pieza del almacén, que las decide con este contrato como guía:
- **`flow_definitions`:** una fila por versión publicada de un flujo. Lleva:
  - `flow_key` y `version`;
  - la definición en `jsonb`, con su `esquema`;
  - la propuesta de la que salió.

  Es inmutable por un trigger, como `pilot_scope_versions` hoy (`20260810000100:57-77`). Los borradores no van acá: viven en la propuesta, sin número.
- **`flow_controls`:** un renglón por flujo, con la versión activa, el estado (prendido, pausado o apagado), la generación y quién la cambió.
- **`flow_change_proposals`:** la propuesta. Guarda:
  - sus ítems y sus versiones base;
  - su estado y su huella;
  - quién la pidió y quién la aprobó, con el ID verificado;
  - cuándo vence;
  - qué hace con lo que está en curso.
- **`flow_change_events`:** el registro de cada propuesta, validación, aprobación, publicación, pausa y vuelta atrás. No se borra ni se modifica, como `pilot_control_events` hoy (`20260810000100:278-331`).
- **Los rechazos al arrancar se guardan** (decisión de Dan del 2026-10-15, en el contrato de lectura). Con eso desaparece el hueco `start_rejection_not_persisted`.
- **Los permisos:**
  - las rutas de cambio del Automation Expert usan un rol de base propio, que solo puede crear y validar propuestas y pausar. Lo previó el contrato de lectura en su decisión 6: el rol propio va con la 1b. Las rutas de lectura siguen con `service_role`;
  - aprobar y publicar solo los ejecuta el servicio del canal de aprobación;
  - pausar lo pueden hacer los dos.
- **El lector de la 1a pasa a leer de acá:** `source` pasa de `projection` a `stored_definition`, y vale `mixed` mientras los flujos se migran de a uno (contrato de lectura, «Las dos preguntas»).
- **Las demás propuestas del esquema para la base las resuelve la misma pieza:**
  - una política por flujo;
  - los CHECK ampliados;
  - el lock por persona siempre;
  - `pilot_proactive_contact_caps` con versión;
  - si el scope y la política pasan a una sola tabla.

## Fuera de este contrato

- El motor genérico, los disparadores nuevos y la plantilla con variables: son sus propias piezas de la 1b.
- La puerta común, con el tope por persona.
- La app de Slack del Automation Expert y su excepción al ADR-0017.
- El canal de aprobación en sí: sus rutas, el mensaje con el botón y el comando del operador mientras no exista.
- La forma exacta de los pedidos, las respuestas y los códigos de error de cada ruta.
- Retirar un flujo para siempre.
- La migración de ATT1: es la última pieza de la 1b.

## Lo que decidió Dan el 2026-10-15

Dan decidió las ocho preguntas a las ≈22:16Z, todas con la recomendación.

### 1. Quiénes aprueban

- Aprueban Dan y Brayan, cada uno por su ID de Slack. Brayan es quien va a pedir los cambios, y si aprueba lo suyo no hay cuello de botella.
- Es una lista por instancia, que se cambia con un PR. Hoy la lista de operadores de Slack es por tenant del conector y vive en su configuración (`slack-operations-connector-v1.md:43`). Dónde vive la de aprobadores lo fija la pieza del canal.
- **Alternativa descartada:** que apruebe solo Dan, al menos al principio.

### 2. Por qué canal se da el sí

- **Un botón firmado de Slack,** que manda el conector central (ADR-0017).
  - El conector verifica la firma de Slack (`slack-correlation-resolution-v1.md:67-72`) y que quien apretó esté en la lista, como hoy con los operadores (`slack-correlation-resolution-v1.md:20-23`). Recién ahí llama al bridge con su propia clave.
  - El aprobador queda registrado por su ID verificado.
  - El agente no puede aprobar porque no tiene esa clave: están separados por construcción.
- **Mientras el botón no exista,** el sí lo da un comando del operador en el VPS, como `piloto.sh`, que corre Dan. Es lo de hoy, pero con la propuesta validada y la vista previa adelante.
  - Mientras tanto aprueba solo Dan, y el registro guarda el nombre que se escribe en el comando, no un ID verificado.
- **Alternativa descartada: preparar y confirmar en la charla, como el copiloto** (el profile `client-copilot`; `operator-correlation-resolution-v1.md`).
  - El sí lo da el mismo humano, en la misma charla con el agente.
  - La clave de idempotencia la genera el modelo (`profiles/client-copilot/SOUL.md:20-21`).
  - En la charla, el plugin no manda actor, así que queda el fijo de la configuración y no la persona. El actor dinámico es solo para un servicio, como el conector (`operator-correlation-resolution-v1.md:52-64`).

  Para cambiar lo que le llega a los leads, no alcanza.

### 3. Qué pasa con lo que está en curso cuando cambia la versión

- Sigue con su versión, como dice el ADR-0007 §5. La propuesta puede pedir «cancelar los que esperan», a la vista del aprobador. «Migrar» queda para más adelante.
- **Qué cambia:** un cambio de versión ya no deja carritos sin salir, y los cambios se pueden hacer a cualquier hora.

### 4. Qué puede cambiar el Automation Expert en la 1b

- Puede cambiar las esperas, el plazo y la plantilla, por otra ya aprobada. También los topes y el horario, y puede prender, pausar y apagar.
- No crea flujos, que son de la 1c, ni cambia el código del cupón.

### 5. Cómo se prueba un cambio

- La simulación con los eventos reales de la semana, para todo cambio.
- La cohorte de prueba, solo para un flujo nuevo.
- La paridad, para el motor.
- Sin un entorno aparte.

### 6. Si las diferencias de hoy se unifican al migrar

- No al migrar. Los flujos pasan tal como corren, con sus diferencias escritas como excepciones, y cada arreglo va después como un cambio que aprueba Dan.
- Si se arreglaran al migrar, la paridad dejaría de servir para saber si el motor nuevo hace lo mismo.
- Las diferencias de hoy:
  - la reactivación no tiene horario ni mira compras;
  - el cupón no pasa por el tope ni por la pausa;
  - el carrito mira la derivación solo en su conversación;
  - la toma humana pausa el carrito en vez de terminarlo.

  Están en el esquema: la de la reactivación, en «Los siete flujos de validación»; las otras tres, en «Lo que cambia respecto de hoy». Cuáles pueden ser una excepción está en «Cómo pasan los flujos de hoy».

### 7. Si el Automation Expert cambia el link de la clase de MRT

- No en la 1b. Es del conocimiento, no de un flujo, y sigue por derivación.
- El ciclo se puede extender a los valores del conocimiento más adelante.

### 8. Un control por flujo o uno por bolsa

- **Un control por flujo,** como dice el esquema. Así se puede pausar el pago fallido sin pausar el carrito.
- Los topes siguen siendo de la bolsa, compartidos.
- Hoy, pausar el carrito pausa también el pago fallido, porque comparten el scope `att1-recuperacion` y su control (`publicar-recuperacion-v5.sql:23-28`; `camino-a-produccion.md:496`).

## Elecciones técnicas, con su valor por defecto

Dan no las decidió: son los valores por defecto con los que se construye. Se cambian con una versión menor de este contrato.
- **Publicar incluye activar y no toca el estado del control.** Se deduce de las decisiones 3 y 6: sin eso, cada cambio pediría pausar y volver a prender.
- **El plazo de 24 horas** cuenta desde la validación y cubre el sí y la publicación.
- **Quién descarta una propuesta:** quien la pidió, en la charla, o un aprobador. Un borrador sin validar no vence.
- **Si la publicación falla después del sí,** por ejemplo con `flow_change_stale_base`, la propuesta queda aprobada y sin publicar hasta que vence. El aprobador lo ve en el mismo mensaje del sí.
- **La pausa que pide el agente** queda registrada con el agente y con el usuario de Slack que le da Hermes, que no está verificado como un botón.
  - Por eso los usuarios que el profile acepta (`SLACK_ALLOWED_USERS`, contrato de lectura, «El flag») tienen que estar todos en la lista de aprobadores.
  - Si alguno no está, el agente no pausa: le pide a esa persona que lo haga un aprobador.
- **El OK del cliente para `permiso: compra`,** que el esquema le dejó a este contrato. Va en la propuesta que publica por primera vez un flujo con ese permiso: quién del cliente lo dio, por qué medio y la fecha. La vista previa lo muestra, y sin ese dato la validación bloquea.
  - En ATT1, el OK de Juan o Marce ya está dado (decisión de Dan del 2026-10-15).
- **Las herramientas de cambio van en su propio plugin y toolset** (`automation-expert-change` y `automation_expert_change`), apartados de los de lectura. Las de lectura siguen sin escribir nada.
  - El plugin tiene su clave en una variable propia del profile, distinta de la de lectura y de todos los secretos del bridge, con las mismas reglas que la de lectura (contrato de lectura, «Clave propia»).
  - La prueba del profile que hoy exige solo `automation_expert_read` se actualiza con esa pieza.

## Lo que queda abierto

- Del esquema: si «un mensaje de utilidad», en `permiso: compra`, quiere decir también un solo envío (regla 17). Mientras tanto, la regla 14 ya frena más de un envío por camino.

## Lo que se confirma al construir

- **Qué tan fiel es la simulación.** No sabe qué habría contestado alguien a un mensaje que no salió. Sabe quién compró, quién contestó otra cosa y quién se dio de baja en la semana. La vista previa la muestra como lo que es: una estimación con los eventos reales.
- **El conteo de lo que está en curso.** Sale de las acciones pendientes de cada flujo. Un recorrido rechazado al arrancar cuenta como en curso hasta que vence, como hoy lo cuenta la guarda del runbook (`camino-a-produccion.md:457`).

## De dónde sale

- **Lo que existe en el producto,** leído el 2026-10-15 en `a0e3d1c`:
  - el perímetro del piloto (`20260810000100`):
    - versiones inmutables con `draft` y `published` (`:7-30` y `:57-77`);
    - un control por `scope_key` con generación (`:127-200`);
    - `activate_lancemos_pilot_scope_version` (`:333`) y `set_lancemos_pilot_runtime_state` (`:434`);
    - `pilot_control_events`, que no se borra (`:278-331`);
  - `authorize_lancemos_pilot_request_start` (`20260930000300:328`), que rechaza lo que está atado a otra versión (`:490-494`), y `worker.py:1540-1562`, que lo vuelve a intentar hasta que vence;
  - el preparar y confirmar del copiloto (`operator-correlation-resolution-v1.md`; `20260824000200`): comandos que vencen a los 10 minutos (`:203`), con huella e idempotencia (`:9-10`);
  - la aprobación firmada de Slack (ADR-0017; `slack-correlation-resolution-v1.md`): la firma verificada y la lista de operadores por tenant del conector, y el actor que el conector deriva del ID verificado (`operator-correlation-resolution-v1.md:61-64`);
  - el catálogo de plantillas, que lee el bridge (`approved_templates.py:112`);
  - el diseño del 2026-08-07 (`automation-expert-configuration-experience.md`): el agente solo inicia borradores (§5, `:94-102`), y la publicación y la vuelta atrás quedan en servicios determinísticos (`:104-114`);
  - el ADR-0025 §7 y el ADR-0007 §5;
  - lo que el esquema le deja al ciclo: «La versión, el control y el techo», «Las reglas del validador», «Lo que cambia respecto de hoy», «Dónde vive hoy cada campo» y «Lo que no entra en la v1, y dónde va».
- **Lo que pasó en ATT1** entre el 2026-10-06 y el 2026-10-13:
  - en la instancia: el runbook (`camino-a-produccion.md`), `piloto.sh` y los SQL de publicación, con el carrito de la v1 a la v5;
  - en el registro de la operación, fuera de los repos: el cambio de la v4 a la v5 con 5 carritos esperando, el 2026-10-13.
- **Dos contradicciones que este contrato resuelve:**
  - lo que está en curso: el ADR contra el código;
  - la versión activa en dos lugares: la base y la variable del bridge, que obliga a redesplegar.
