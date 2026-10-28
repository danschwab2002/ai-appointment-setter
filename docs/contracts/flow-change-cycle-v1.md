# Contrato: el ciclo de un cambio de flujo (v1)

- **Estado:** aceptado como contrato conceptual el 2026-10-15; sin implementación (lo construye la 1b). El contrato no describe nada desplegado ni activado. Lo que dice de ATT1 es lo que pasó, con su fecha y de dónde sale.
- **Fecha:** 2026-10-15
- **ADR:** [ADR-0025](../decisions/0025-cada-instancia-tiene-su-hermes-declarado-por-el-producto.md) §7, [ADR-0007](../decisions/0007-durable-next-action-engine.md) §5 y [ADR-0017](../decisions/0017-central-slack-operations-connector.md)
- **Contrato:** `flow-change-cycle-v1`, versión `1.9.0`. La 1.1.0 suma «Lo que fijó la implementación (1.1.0)»: lo que decidió la construcción del almacén (#243) y que este contrato dejaba abierto. No cambia ninguna decisión de Dan. La 1.2.0 suma «Lo que fija el comando del operador (1.2.0)», con un valor por defecto que revisa Dan: el operador también arma propuestas. La 1.3.0 suma «Lo que fijan las rutas del agente (1.3.0)»: la forma de las rutas y de las herramientas del Automation Expert, sin la pausa (otro valor por defecto que revisa Dan), y una ruta de lectura del almacén. La 1.4.0 suma «La simulación en la vista previa (1.4.0)»: validar arma la simulación con el motor, y aprobar registra la que quedó congelada. La 1.5.0 suma «Registrar un flujo y crear su ruta (1.5.0)»: dos subcomandos del operador para poner un flujo del legado en el almacén. La 1.6.0 suma «Lo que el agente necesita para no errarle (1.6.0)»: el motivo de un 422 de entrada, el ítem listo para copiar en `/flow-store` y cuándo sale cada mensaje antes y después del cambio. La 1.7.0 la construye el PR 4 del hito 4 (migración `20261024000600`), escrita antes del merge: el bridge contesta `1.7.0` (`CONTRACT_VERSION`, `flow_change_api.py`). Suma «Crear un flujo: el ítem `nuevo` y la cohorte (1.7.0)»: con el motor nativo del contrato `flow-engine-v2`, una propuesta puede crear un flujo, que se publica para la cohorte de prueba (decisiones D2 y D6 de Dan del 2026-10-24). La 1.8.0 la construye el PR 5 (migración `20261025000100`), escrita antes del merge: el bridge contesta `1.8.0`. Suma «Volver a la cohorte: `prender` con `a: cohorte` (1.8.0)». La 1.9.0 (2026-10-26) enmienda la decisión 2 por una decisión de Dan del mismo día: el sí se da en una página del bridge con «Sign in with Slack», a la que se llega por un botón de URL que el Automation Expert deja en su DM («El sí por un link en el chat del Automation Expert (1.9.0)»). Reemplaza el botón firmado del conector central; las demás decisiones del 2026-10-15 no cambian. El 2026-10-27 Dan aprobó los valores de ese canal que quedaban abiertos («Lo que decidió Dan el 2026-10-27 sobre el canal»), y la misma 1.9.0 los escribe como decididos: la lista de Maribel, los dos pasos para lo que llega a todos, el aviso de otra propuesta abierta, el aviso al canal de alertas y la carga de la cohorte desde la página. Es un contrato antes de construir: no hay nada de eso desplegado, y el bridge sigue contestando `1.8.0` hasta que lo construya su PR.
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
2. **El Automation Expert solo propone y valida.** No puede aprobar, publicar ni pausar (la pausa, desde la 1.3.0), porque no tiene la clave para hacerlo.
3. **Una persona aprueba, por un canal que el agente no toca:** una página del bridge, a la que entra con «Sign in with Slack» desde un botón de URL que el agente deja en su DM. La página verifica quién es contra una lista de aprobadores, y la clave para aprobar vive solo en el bridge. El aprobador queda registrado por su ID de Slack verificado, no por un texto (1.9.0; antes, un botón firmado del conector central).
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

- El agente le dice a la persona que la propuesta está lista y le muestra el resumen. El sí **no** se da escribiendo en la charla: se da en el canal de aprobación (decisión 2), que desde la 1.9.0 es la página del bridge a la que lleva el botón del DM.
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
| **Pausar** | Lo pide un aprobador, con un solo botón en la página de aprobación (1.9.0). No pasa por una propuesta. El agente no pausa (1.3.0) | No arranca recorridos nuevos. Los que estaban en curso esperan sin mandar: si el flujo se prende antes de que venzan, salen. |
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

El sí a cualquier propuesta entra por el **canal de aprobación**, con otra clave que el profile del Automation Expert no tiene. Sus rutas están en «El sí por un link en el chat del Automation Expert (1.9.0)».

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
  - aprobar y publicar solo los ejecuta el servicio del canal de aprobación: la página del bridge, con un rol de base propio (`flow_change_approver`, 1.9.0), y el comando del operador;
  - pausar lo hace un aprobador desde la página o el operador. El agente no pausa (1.3.0).
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
- La forma exacta de la página de aprobación, del mensaje con el botón y de sus códigos de error. Lo que el canal tiene que cumplir está en «El sí por un link en el chat del Automation Expert (1.9.0)».
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

> **Enmendada el 2026-10-26 (1.9.0).** Dan eligió que el sí se dé en una página del bridge con «Sign in with Slack», a la que se llega por un botón de URL en el DM del Automation Expert: *«Dale vamos con A′. Con el botón-link en el chat del AE»*. Reemplaza el botón firmado del conector central de abajo. Lo de «Mientras el botón no exista» y la alternativa descartada siguen valiendo. El canal nuevo está en «El sí por un link en el chat del Automation Expert (1.9.0)».

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
- **La pausa que pide el agente** queda registrada con el agente y con el usuario de Slack que le da Hermes, que no está verificado como un botón. *Desde la 1.3.0 el agente no pausa, y desde la 1.9.0 un aprobador pausa desde la página con su ID verificado; este punto queda para el día en que el agente vuelva a pausar.*
  - Por eso los usuarios que el profile acepta (`SLACK_ALLOWED_USERS`, contrato de lectura, «El flag») tienen que estar todos en la lista de aprobadores.
  - Si alguno no está, el agente no pausa: le pide a esa persona que lo haga un aprobador.
- **El OK del cliente para `permiso: compra`,** que el esquema le dejó a este contrato. Va en la propuesta que publica por primera vez un flujo con ese permiso: quién del cliente lo dio, por qué medio y la fecha. La vista previa lo muestra, y sin ese dato la validación bloquea.
  - En ATT1, el OK de Juan o Marce ya está dado (decisión de Dan del 2026-10-15).
  - Con ese permiso sale un solo envío por camino (esquema, regla 17; decisión de Dan del 2026-10-15).
- **Las herramientas de cambio van en su propio plugin y toolset** (`automation-expert-change` y `automation_expert_change`), apartados de los de lectura. Las de lectura siguen sin escribir nada.
  - El plugin tiene su clave en una variable propia del profile, distinta de la de lectura y de todos los secretos del bridge, con las mismas reglas que la de lectura (contrato de lectura, «Clave propia»).
  - La prueba del profile que hoy exige solo `automation_expert_read` se actualiza con esa pieza.

## Lo que fijó la implementación (1.1.0)

El almacén es la primera pieza de la 1b: `supabase/migrations/20261016000100_flow_change_store_v1.sql`, `src/bridge/flow_definition.py` y `src/bridge/flow_validator.py` (#243). Esta sección dice qué quedó fijo. Escrita el 2026-10-16, antes del merge; no afirma que nada esté desplegado ni activado.

**Qué trae y qué no.**
- **Las cuatro tablas:**
  - `flow_controls`: un control por flujo, con el estado `apagado`, `prendido` o `pausado` y una generación;
  - `flow_definitions`: las versiones publicadas, que no se pueden cambiar;
  - `flow_change_proposals`: la propuesta, en `borrador`, `validada`, `aprobada`, `publicada`, `descartada` o `vencida`;
  - `flow_change_events`: el registro, que solo suma.
- **Siete funciones que ejecuta solo el dueño de la base:** `operator_register_flow_v1`, `operator_save_flow_change_draft_v1`, `operator_record_flow_change_validation_v1`, `operator_approve_flow_change_v1`, `operator_publish_flow_change_v1`, `operator_pause_flow_v1` y `operator_discard_flow_change_v1`.
  - Son `security invoker`.
  - Ningún rol de la API (`anon`, `authenticated`, `service_role`) las ejecuta ni tiene permisos sobre las tablas, así que un `grant execute` equivocado tampoco alcanza para escribir.
  - Las garantías de inmutabilidad y de registro valen contra todos menos contra el dueño.
- **Lo que queda para otras piezas:**
  - el rol propio del Automation Expert, sus rutas y su clave van con las herramientas del Automation Expert;
  - el comando del operador y la vista previa van con el canal de aprobación;
  - las reglas 14 y 16, la simulación y aplicar `cancelar` van con el motor.
  - Nada lee el almacén para mandar un mensaje: publicar o pausar en el almacén no cambia ningún envío hasta que exista el motor.

**Las clases de quien actúa.**
- Proponen `automation_expert` u `operador`.
- Aprueban `operador` o `slack`, este último con el formato de un ID de Slack. Las tablas ya aceptan `slack`, así que el sí de la página (1.9.0) se suma sin cambiar la tabla de versiones.
- Rehacer, congelar y descartar exigen que actúe quien pidió la propuesta. Descartar lo puede hacer también un aprobador.
- El Automation Expert no puede descartar lo que pidió el operador.

**Las dos bases.**
- La del borrador (`draft_base`) es la versión de cada flujo que leyó quien propone. Si al guardar ya no es la activa, da `flow_change_stale_base`.
- La de la validación la arma la base, con las filas bloqueadas: la versión activa de **todos** los flujos de la instancia, y la generación de los flujos de la propuesta. Se compara con igualdad de `jsonb`. Las reglas 8 (la bolsa) y 10 (las relaciones) miran otros flujos, y por eso la base cubre la instancia entera.
- **Consecuencias:**
  - publicar una versión de cualquier flujo de la instancia invalida las otras propuestas validadas de esa instancia;
  - una pausa del mismo flujo entre la validación y la publicación también la invalida;
  - una pausa de otro flujo no la invalida.

**La huella.** La calcula solo la base: un sha256 sobre el `jsonb` canónico de la propuesta, que incluye el contenido, las dos bases, el «para qué» y el id de la propuesta. Una validación de otra propuesta no sirve para esta.

**El plazo y la revalidación.**
- Una propuesta validada vence a las 24 horas. Se marca vencida cuando alguien la toca, sin barrido.
- Publicar bloquea la propuesta y después todos los controles de la instancia, en orden. Recién entonces mira:
  - el plazo;
  - la base;
  - una revalidación de la misma huella hecha en los últimos 10 minutos.
- La revalidación tiene que ser de fase `publish` y del actor que pidió la propuesta.
- `validated_at` va en ISO 8601 con zona.
- Las funciones que esperan locks llevan `lock_timeout`.

**El validador** (`p_validator`) lleva seis claves exactas: `contract`, `rules_version`, `phase`, `actor`, `product_version` y `git_sha`. El resultado:
- separa `blocks` de `warnings` en forma positiva: una advertencia nunca bloquea y un bloqueo nunca entra como advertencia;
- tiene un tope de 200 hallazgos y de 60 KiB, y si se pasa, se cortan primero las advertencias;
- con la vista previa, tiene que entrar en la columna de 192 KiB.

**Lo que está en curso.** Hasta el motor, una propuesta acepta solo `en_curso: seguir`. `cancelar` da `en_curso_not_supported`.

**Aprobar** registra `simulation: not_built` mientras no exista la simulación.

**Lo que fijó el validador sobre el esquema:**
- **La identidad del disparador** es la clase más el evento (`dispara.evento`), o la clase más `dispara.silencio.lo_ultimo`.
  - Cambiar eso es otro flujo: `trigger_change_is_another_flow`.
  - `desde` y `hasta` de un silencio son una versión nueva del mismo flujo. El Automation Expert los puede cambiar, porque son «las esperas y el plazo» de la decisión 4 (esquema, «Qué lo dispara», y `hasta` que «hace de vencimiento»).
  - `conversaciones` cambia quién entra: para el Automation Expert es `change_outside_1b_scope`.
- **La regla 8 y la zona.** La zona se elige al crear el flujo y no cambia nunca, ni después de una versión con `horario: siempre`. Así se leyó la redacción de la regla 8 («distinta de la de la versión anterior»), siguiendo «Cuándo puede escribir» del esquema. Con `horario: siempre` no hay zona que mirar, y la base no la exige.
- **La regla 17, en forma positiva.** Con `permiso: compra`, todo paso con plantilla bloquea salvo que su categoría en el catálogo sea exactamente `UTILITY`, incluida una categoría faltante o vacía. Al prender se vuelve a mirar.
- **Los textos** de la definición y de la propuesta rechazan los caracteres de control, de formato, los bidi, los separadores de línea y los sustitutos sueltos.

**Riesgos que este contrato todavía no resuelve:**
- **La bolsa por defecto.** `<producto>-<flujo>` puede no entrar en la forma de una bolsa, que llega hasta 40 caracteres. Si no entra, el validador bloquea con `default_pool_invalid` y pide una bolsa explícita.
- **La ruta de pausa del Automation Expert** (`POST /flows/{flow_key}/pause`, en «Las rutas y las herramientas») no lleva el producto. Desde que la clave de un flujo es única solo dentro de su producto, la va a tener que llevar.
- **La identidad del flujo no lleva la aliada.** La clave única es (`tenant_ref`, `product_ref`, `flow_key`). Si un producto pasa de una aliada a otra, la nueva no puede registrar sus flujos. Queda para la Etapa 2.

## Lo que fija el comando del operador (1.2.0)

El comando del operador es la primera parte de las herramientas (la T4): `scripts/flow_change_operator.py`, que corre en el VPS como `piloto.sh`, y `src/bridge/flow_change_operator.py` con `src/bridge/flow_change_preview.py`, que corren en un contenedor nuevo de la imagen del bridge. Escrita el 2026-10-21, antes del merge; no afirma que nada esté desplegado ni activado, y mientras ningún flujo tenga su ruta en el motor, el almacén no gobierna ningún envío.

- **El comando también arma propuestas.** `volver`, `prender` y `apagar` arman su ítem, lo guardan como propuesta del operador, lo validan y siguen al sí y a la publicación en un solo comando, y `proponer` guarda y valida una propuesta del operador escrita en un archivo, como la v1 de un flujo. Así, el operador vuelve atrás sin depender del agente. El agente sigue armando las suyas, y el canal las aprueba igual. **Es el valor por defecto de Andy (21/10) para el desvío V1#16 del diseño del almacén, y lo revisa Dan:** donde arriba dice que «las cuatro clases de ítem las arma el agente» y que el canal «solo las aprueba y las ejecuta», el operador también puede armarlas.
- **El sí lo da una persona en una terminal.** Toda escritura muestra un código y lo lee de `/dev/tty`; sin terminal, el comando no escribe. La garantía real es ser root en el VPS. `--quien` tiene que estar en `APROBADORES` (`dan` en ATT1).
- **La validación corre aparte del bridge vivo,** en un contenedor nuevo de la imagen fijada por digest, sin red y de solo lectura, con el almacén y el catálogo que el comando leyó con `psql`. El catálogo sale de la base de Chatwoot (`channel_whatsapp.message_templates` de la bandeja), con la misma forma que `GET /inboxes/{id}`. El diff que se muestra antes del sí lo calcula el comando con la biblioteca estándar, y el «para qué» de la propuesta se muestra escapado y cercado como texto no verificado.
- **Sin simulación no se publica salvo que se pida.** Mientras la vista previa traiga `simulation_not_built`, publicar exige `--sin-simulacion`. La base sigue registrando `simulation: not_built` al aprobar hasta la migración de las herramientas del agente.
- **Validar** registra solo las propuestas del operador; las del agente las valida su ruta del bridge.
- **Mover la ruta de un flujo** (`operator_route_flow_v1`, contrato `flow-engine-v1`) va en este comando: es `mover`, más abajo («Mover la ruta de un flujo»).

## Lo que fijan las rutas del agente (1.3.0)

La segunda parte de las herramientas (la T4): `src/bridge/flow_change_api.py`, enchufado en `app.py` detrás de `AUTOMATION_EXPERT_CHANGE_ENABLED` (apagado por defecto), y el plugin `profiles/automation-expert/plugins/automation-expert-change/`. Escrita el 2026-10-21, antes del merge; no afirma que nada esté desplegado ni activado.

- **Dos claves propias, separadas de todo lo demás.**
  - El plugin entra al bridge con `AUTOMATION_EXPERT_CHANGE_TOKEN` (en el profile, `AUTOMATION_EXPERT_CHANGE_API_TOKEN`): 32 caracteres o más y distinta de todo otro valor, también de la de lectura.
  - El bridge entra a PostgREST con `AUTOMATION_EXPERT_CHANGE_DB_KEY`, el JWT del rol `automation_expert_change` de la migración `20261021000100`, que alcanza solo sus cuatro envoltorios. El bridge no arranca si esa clave no dice ese rol o si es igual a la de `service_role`; estas rutas nunca usan el cliente de `service_role`.
- **Las rutas,** todas bajo `/internal/automation-expert/v1/` y con el sobre `{contract: "flow-change-cycle-v1", contract_version, data}`:
  - `GET /flow-store`: los flujos del almacén de la instancia, cada uno con `flow_id`, `product_ref`, `flow_key`, `state`, `active_version`, `generation`, `changed_at`, los números de sus `versions` y su `active_definition` completa. Es de donde el agente copia lo que cambia: la lectura de la 1a no trae la definición ni el `flow_id` mientras siga leyendo la proyección.
  - `POST /flow-changes`: `{items, base, purpose, requested_by}`, y `proposal_id` con `revision` para rehacer. Corre el validador en fase `draft`; con bloqueos contesta 422 `flow_change_draft_blocked` con `blocks` y `warnings` y no guarda; si no, 201 con el borrador guardado.
  - `POST /flow-changes/{id}/validate`: `{revision, requested_by}`. Lee el catálogo de la bandeja de la instancia, valida en fase `publish`, arma la vista previa y la registra. Sin catálogo contesta 503 `template_catalog_unavailable` y no registra nada.
  - `GET /flow-changes/{id}`: la propuesta, con su `preview` y sus últimos eventos.
- **Los errores son códigos cerrados.** De la base: 22023 es 422, P0002 es 404 (PostgREST 14 lo contesta como 500) y un resultado `ok: false` con código conocido es 409. Todo lo demás, incluido un permiso negado, es 503 `automation_expert_change_unavailable`, sin más detalle. Dos pedidos a la vez como máximo (`automation_expert_change_busy`) y cuerpos de hasta 256 KiB (`body_too_large`).
- **Quién pide el cambio lo pone el plugin, nunca el modelo.** Sale del contexto de la sesión de Hermes (`HERMES_SESSION_PLATFORM` y `HERMES_SESSION_USER_ID`), y solo en Slack; fuera de Slack el plugin no propone ni valida. La base lo guarda como declarado: el bridge no lo puede verificar.
- **El «para qué»** se rechaza con un mail o siete dígitos seguidos (`identifier_not_allowed`), en el plugin y en el bridge.
- **Las herramientas** son cuatro, en el toolset `automation_expert_change`: `get_flow_definitions`, `propose_flow_change`, `validate_flow_change` y `get_flow_change`. El profile las tiene en Slack y en la terminal, junto a las de lectura (`platform_toolsets`, y `herramientas` en `hermes/declaracion.toml`).
- **El agente no pausa.** Donde arriba dice que hay una ruta `POST /flows/{flow_key}/pause` y una herramienta `pause_flow`, no existen: la base no puede verificar a quien pide la pausa, y la ruta tampoco llevaba el producto. Pausar es del comando del operador. **Es el valor por defecto de Andy (21/10), y lo revisa Dan.** Desde la 1.9.0, un aprobador pausa también desde la página de aprobación, con su ID de Slack verificado; el agente sigue sin pausar.
- **Aprobar registra la simulación que quedó congelada en la vista previa** (`validation.preview.simulacion`): `built` con `summary_sha256`, `failed` con `reason_code`, o `not_built` si no hay. Lo hace `_flow_change_approve` desde la migración `20261021000100`. Hasta que la vista previa arme la simulación con el motor (contrato `flow-engine-v1`), sigue saliendo `not_built`.

## Lo que el agente necesita para no errarle (1.6.0)

Salió del E2E de la T4 en ATT1, el 2026-10-22. El agente pidió el A2 a 36 h y el Z a 72 h, y `POST /flow-changes` le contestó 422 `flow_change_items_invalid` ocho veces, sin decir por qué. El agente probó seis variantes del ítem a ciegas. Además, en su cuenta dejó la espera antes del Z en 48 h: con el A2 a 36 h, el Z quedaba a las 84 h y no a las 72. Escrito antes del merge: no afirma que nada esté desplegado.

- **Un 422 de entrada dice su motivo.** `invalid_body`, `identifier_not_allowed` y `flow_change_items_invalid` contestan `{"detail": code, "reason": {...}}`. El motivo dice el campo (`field`) y, según el caso, el código de los ítems (`code`, por ejemplo `keys_invalid`), el número de ítem (`item`), las claves que faltan (`missing`) y las que sobran (`extra`), o qué no cierra (`problem`). Lleva nombres de campos y de claves, nunca un valor: una clave que no tiene forma de clave sale como `<otra>`. El bridge anota la misma línea en el log (`route=propose status=422 code=… reason=…`). El plugin se lo pasa al agente solo si tiene esa forma cerrada.
- **`/flow-store` trae el ítem listo para copiar.** Cada flujo con versión activa trae `draft_item`: el ítem de una versión nueva con las claves que pide `POST /flow-changes` (`producto`, `flujo`, `flow_id`, `tipo: version`, `definicion` como borrador y `en_curso: seguir`). Trae también `draft_base`, la `base` de esa versión. El agente los copia y cambia solo los pasos. Un flujo sin versión activa trae `draft_item: null`.
- **Cuándo sale cada mensaje.** `/flow-store` trae, por flujo, `toques`: cada plantilla de los pasos de arriba con `horas_desde_el_primero`. El 201 de `POST /flow-changes` trae `toques` de cada versión nueva, `antes` y `despues`. Desde una `condicion`, una conversación o una derivación, lo que sigue depende del lead, y la lista termina con `sigue: "depende_del_lead"`. El SOUL le pide al agente comparar `despues` con la hora que dijo la persona antes de contar el cambio.

## La simulación en la vista previa (1.4.0)

La tercera parte de las herramientas (la T4c): `src/bridge/flow_change_simulation.py`, que sigue los pasos de la sección 4 del contrato `flow-engine-v1`. Escrita el 2026-10-21, antes del merge; no afirma que nada esté desplegado ni activado.

- **Quién la arma.** Validar, por los dos caminos: la ruta `POST /flow-changes/{id}/validate` del bridge, con la clave del rol del agente, y el comando del operador, con `psql`. Los dos leen `read_flow_engine_v1` (todas sus páginas) y `read_flow_engine_history_v1` del producto de la propuesta, con los últimos 8 días menos 5 minutos. El comando pide la historia con el reloj de la base.
- **La vigente** de cada flujo es la `active_version` de la lectura del motor, si el flujo está prendido. **La propuesta** sale de `proposed_versions`. Las dos corren sobre la misma historia con las capacidades del motor de la instancia (`ENGINE_CAPABILITIES_V1`).
- **La forma, en `validation.preview.simulacion`:**
  - `{"status": "built", "summary": {...}, "summary_sha256": "<64 hex>", "cambios": [...]}`. `summary` es el de `flow_replay.simulate`: los disparos, cuántos cambian, y el resumen de la vigente y de la propuesta. `summary_sha256` es el sha256 del resumen en JSON canónico (claves ordenadas, sin espacios, UTF-8). `cambios` trae hasta 20 disparos, con el fin antes y después.
  - `{"status": "failed", "reason_code": "..."}` cuando no se pudo armar. Los motivos:
    - `flow_engine_unavailable`: la base no tiene las funciones del motor, o no contestaron;
    - `flow_engine_history_too_large`: la historia pasa de 4 MB (4.000.000 bytes, migración `20261021000300`; antes era 1 MB);
    - `flow_engine_history_invalid`: la historia no tiene la forma cerrada de la captura v3, que es la prueba de privacidad;
    - `flow_engine_read_invalid` y `flow_engine_capabilities_unknown`: la lectura del motor no es la del contrato;
    - `simulation_multi_product`: la propuesta toca más de un producto;
    - el código de `not_supported` cuando la propuesta trae algo que el motor no ejecuta, como `engine_not_supported:…`.
- **La vista previa.** Con la simulación, el hueco `simulation_not_built` se va. Si la simulación falló, queda `simulation_failed` con su motivo. **Validar no se frena por la simulación:** sin ella, la propuesta se valida igual y la vista previa dice por qué no está.
- **Aprobar** registra la simulación de la validación guardada (migración `20261021000100`): `built` con el `summary_sha256`, o `failed` con el motivo. **El comando del operador** publica sin `--sin-simulacion` solo si esa simulación guardada está `built`; si no, frena y dice el motivo.
- **Lo que no cambia:** el conteo de lo que está en curso (`in_flight_not_counted`) y el cambio de audiencia (`audience_change_not_computed`) siguen siendo huecos. Mover la ruta de un flujo (`operator_route_flow_v1`) es `mover` («Mover la ruta de un flujo»).

## Registrar un flujo y crear su ruta (1.5.0)

La cuarta parte de las herramientas (la T4d): dos subcomandos de `scripts/flow_change_operator.py`, con el mismo código de confirmación que el resto. Escrita el 2026-10-21, antes del merge; no afirma que nada esté desplegado ni activado.

- **`registrar <producto>/<flujo>`** llama a `operator_register_flow_v1`. El flujo queda `apagado` y sin versión, y el almacén no lo ejecuta ni frena nada. El producto tiene que tener un binding activo, y un flujo ya registrado se rechaza antes del código. Después va su v1 con `proponer`: una `version` y un `prender` en la misma propuesta.
- **`ruta <producto>/<flujo> --tipo carrito|pago_fallido|ficha --scope <scope del legado>`** llama a `operator_create_flow_route_v1` (contrato `flow-engine-v1`, sección 3). La ruta va en `legado`. Desde ahí, la guarda del legado lee el estado del flujo en el almacén: **pausarlo o apagarlo en el almacén frena lo que manda el legado.** Una versión nueva no cambia ningún envío mientras la ruta siga en `legado`. Por eso el comando exige el flujo prendido y con versión: con la ruta sobre un flujo apagado, el legado se frenaría al instante.
- **`estado`** muestra la ruta de cada flujo y si pausarlo frena el legado.
- **La simulación necesita la ruta:** `read_flow_engine_history_v1` lee la historia por la ruta del carrito del producto. Sin ella, la simulación queda `failed` con `flow_engine_unavailable`.
- **El orden para un flujo del legado:** `registrar`, `proponer` con su v1 y `prender`, `publicar --sin-simulacion` (todavía no hay historia que leer), y `ruta`. Desde ahí, cada propuesta se valida con su simulación.

## Mover la ruta de un flujo (`mover`)

El paso 4 de la T5: `scripts/flow_change_operator.py mover <producto>/<flujo> motor|legado --quien <q> --motivo "..."`, con el mismo código de confirmación que el resto, y `src/bridge/flow_route_revalidation.py`, que corre en un contenedor nuevo de la imagen fijada, como el validador de `validar`. Escrita el 2026-10-22, antes del merge; no afirma que nada esté desplegado ni activado.

- **Lee en una consulta** la ruta (`executor`, `generation`, `legacy_kind`, `legacy_scope_key`), la huella de la versión activa (`flow_definitions.definition_sha256`), el estado del scope del legado, si la instancia tiene tope por persona, y lo en curso de cada lado (`_flow_engine_legacy_in_flight` y los recorridos abiertos del motor). Si la versión activa cambió entre esa lectura y la del almacén, no sigue.
- **A `motor`** arma la revalidación de `flow-engine-v1` §3.2 con las seis claves exactas. El módulo valida la versión activa, tal cual está publicada, en fase `publish` con `operador`, con el `legacy_kind` de la ruta (la regla 14 de la forma del legado) y con `flow_engine.not_supported` con `ENGINE_CAPABILITIES_V1` (la regla 14 del motor). `rules_version` es 2: las reglas del validador más las capacidades v1 del motor, el `engine.rules_version` de `read_flow_engine_v1`. Sin catálogo de plantillas no se arma (la regla 13 no se puede mirar). La regla 16 (el manifiesto como techo) no la mira, igual que el validador al publicar. Con bloqueos, con el flujo apagado o con tope por persona, frena antes del código.
- **A `legado`** no lleva revalidación (`p_revalidation` nulo). Frena antes del código si el scope del legado no está `armed`. Es la vuelta atrás de la T5: apagar `FLOW_ENGINE_ENABLED` no saca la ruta de `motor`.
- **Pasa la generación que leyó** como `p_expected_generation`: si otro la movió en el medio, la base contesta `flow_route_generation_stale` y el comando sale con 3. Con la ruta ya en el destino, no escribe ni pide el código.
- **Lo en curso no se mueve**, y el comando lo dice antes del código, con los dos conteos.
- **El carrito a `motor` pide A1 y A2/Z en el bridge** (E-165, `flow-engine-v2` 2.8.0, sección 20). El motor ya arranca sin ellos, y un bridge sin ellos manda cada carrito de una ruta en `motor` al legado. La base no conoce los flags del bridge, así que el comando lo dice antes del código: `/ready` tiene que decir `flow_engine_cart_touches: enabled`.
- **Andy no lo corre.** Lo corre una persona en una terminal (`ssh -t`).

## Crear un flujo: el ítem `nuevo` y la cohorte (1.7.0)

Lo que pide el hito 4 para que el Automation Expert cree un flujo (decisiones D2 y D6 de Dan del 2026-10-24). La forma y el detalle están en el contrato [flow-engine-v2](flow-engine-v2.md), secciones 6 y 7. Escrita el 2026-10-24, antes de construir nada: no afirma que nada esté desplegado ni activado.

- **Al construirse, deja sin efecto** el «No entra: crear flujos nuevos» de «Qué puede cambiar el Automation Expert en la 1b», solo para flujos nativos (los que nacen en el motor, sin legado). Un flujo del legado sigue registrándose con `registrar` del operador («Registrar un flujo y crear su ruta (1.5.0)»).
- **Una quinta clase de ítem, `nuevo`,** sin `flow_id`: lleva `producto`, `flujo`, la definición completa como borrador, `prender: cohorte` y, con `permiso: compra`, `ok_del_cliente`. La arman el agente con `propose_flow_change` y el operador con `proponer`.
- **Registrar pasa adentro de la publicación.** En la misma transacción, o nada: registra el control, publica la v1, crea la bolsa si no existe, crea la ruta nativa y deja el control en `cohorte`. Así un flujo nuevo nunca queda a medias.
- **Un flujo nuevo siempre se publica primero para la cohorte de prueba** (D6), con el teléfono que elige Dan. Es el estado `cohorte` del control, y responde a «Cómo corre un flujo solo para la cohorte lo fija la pieza del almacén» de «Cómo se prueba un cambio». Pasar a todos es un ítem `prender`, con su propuesta y su sí.
- **La regla 16 no aplica** a un flujo nativo, y la 14 se mira con las capacidades del motor nativo (`flow-engine-v2` §9).
- **La simulación de un flujo nuevo** cuenta los eventos de su disparador de los últimos 7 días (`read_flow_engine_native_trigger_counts_v1`, `flow-engine-v2` §14) y queda `built`: el operador publica sin `--sin-simulacion`. La simulación del motor (`build_simulation`) no simula un `nuevo`: con uno adentro da `simulation_new_flow_not_built`.

### Lo que fija el PR 4 (1.7.0)

Escrito al construirlo, antes del merge (migración `20261024000600_flow_change_new_flow_v1.sql`). Describe la interfaz; no dice que nada esté desplegado ni activado.

- **El ítem en la base.** `_flow_change_items_are_valid` acepta la quinta clase, sin `flow_id`, con las claves `producto`, `flujo`, `tipo`, `definicion`, `prender` y, opcional, `ok_del_cliente`. La definición y el OK del cliente tienen la forma de una `version`. A lo sumo un `nuevo` por producto y flujo. Un `nuevo` no entra en la base de la propuesta: solo, la `base` es `{}`.
- **Guardar.** `prender` distinto de `cohorte` es `22023 new_flow_must_start_in_cohort`. Una clave que ya existe en el producto devuelve `{"ok": false, "reason_code": "flow_already_registered"}`, que la ruta del agente contesta 409. `flow_creation_not_enabled` sigue para una `version` sobre un flujo registrado sin versión.
- **El validador** (`flow_validator.py`) arma, para un `nuevo`, un flujo nativo apagado que no está en la foto, completa la clave del ítem en la definición y le pone los valores por defecto de un flujo nuevo. La ruta de proponer del agente lee el catálogo de la bandeja solo cuando hay un `nuevo`, para el idioma de la plantilla, y sin catálogo el borrador lo pide como hoy. La regla 14 de un flujo nativo sale de `flow_engine.native_not_supported`: lo que la base no abre ni admite (`motor_nativo:disparador`, `:permiso`, `:repeticion` y `:prioridad`) y, con un código cerrado, `motor_nativo:forma`, lo que el compilador no ejecuta con la forma `nativo:compra`: un solo envío, las variables exactas `[nombre, producto]`, sin botón y sin espera después del envío. Lo que no ejecuta va en la razón. Un flujo del legado con `etiquetar` es `engine_not_supported:etiquetar`. `etiquetar` sin el disparador `compra` es la regla 5 (`etiquetar_requires_purchase_trigger`), y antes de una plantilla o dos veces en un camino, la 7 (`etiquetar_not_after_template`).
- **Publicar.** Bajo el lock de los controles y antes de escribir, lo que impide abrir el flujo vuelve como resultado `{"ok": false, "reason_code"}`, con su evento `flow_change_publish_rejected`. Los motivos son `flow_already_registered`, `new_flow_must_start_in_cohort`, `flow_definition_unresolved_value` (una `{a_crear: …}`: se guarda y no se publica), los `motor_nativo:*`, `flow_route_person_cap_present`, `flow_engine_native_zone_missing`, `flow_engine_pool_zone_invalid`, `flow_engine_pool_is_legacy_scope`, `flow_engine_pool_other_instance` y `flow_engine_pool_zone_mismatch`. Después de pasar la propuesta a `publicada`, en la misma transacción, registra el control (atribuido a quien aprobó, con el para qué), publica la v1, lo abre con `_flow_engine_open_native_flow` (la bolsa, la ruta nativa y el control en `cohorte`) y deja `flow_created`. `published` trae `flow_id`, `pool` y `state: cohorte` del flujo nuevo.
- **La zona de la bolsa.** Con `horario: siempre` es la de la instancia, que el comando pasa en `p_instance.time_zone` de `operator_publish_flow_change_v1` (`ZONA`). Sin ella, `flow_engine_native_zone_missing`.
- **El almacén** (`read_flow_store_v1`) dice de cada flujo si es nativo (`native`), y `/flow-store` lo pasa.
- **La cohorte.** `cohorte <producto>/<flujo> agregar|sacar --quien <q> --motivo "..."` del comando del operador llama a `operator_flow_cohort_v1`. El teléfono se lee de la terminal sin eco, va a la base por `_args` y se resuelve a su contacto por la forma canónica de WhatsApp. La respuesta trae `{ok, action, changed, members, max_members}`, y el evento `flow_cohort_changed`, la acción y el conteo: ninguno trae el teléfono ni el contacto. Hasta 5 por flujo (`flow_cohort_full`), solo en un flujo nativo (`flow_cohort_needs_native_flow`). Además: `flow_cohort_contact_not_found`, `flow_cohort_contact_ambiguous`, `flow_cohort_member_not_found` y `22023 flow_cohort_phone_invalid`.
- **Lo que queda para otro PR.**
  - Volver de `pausado` a `cohorte` (`prender` con `a: cohorte`): de `pausado`, `prender` lleva a `prendido` para todos. **Lo trae la 1.8.0.**
  - Las lecturas del agente (`list_flows` y `lead_history`) todavía no muestran los recorridos nativos. Su lista de flujos es cerrada (`FLOW_KEYS`, `automation_expert_read.py`) y su historia sale de los scopes del legado. Mostrarlos cambia el contrato `automation-expert-read-v1`. El flujo nativo sí se ve en `get_flow_definitions`, con su estado y `native`. **Lo trae el contrato de lectura 1.2.0 (PR 5).**
  - El validador no mira la regla 8 de la 1.2 (una bolsa nativa con el nombre de un scope): la foto del almacén no trae los scopes. La guarda que cuenta es la de la base.

## Volver a la cohorte: `prender` con `a: cohorte` (1.8.0)

Escrito al construir el PR 5 del hito 4 (2026-10-25, migración `20261025000100`), antes del merge. Describe la interfaz; no dice que nada esté desplegado ni activado.

- **La forma.** Un ítem `prender` puede llevar una clave más, `a`, con un solo valor: `{"producto", "flujo", "flow_id", "tipo": "prender", "a": "cohorte"}`. Sin `a`, `prender` lleva a `prendido`, como siempre. `a` en otro tipo de ítem, o con otro valor, no se guarda: en la base `flow_change_items_invalid` (`_flow_change_items_are_valid`), y en el bridge `ProposalItemsInvalid("a_invalid")`.
- **Cuándo vale.** Solo para un flujo nativo en `pausado`: la transición `pausado → cohorte` es la que deja `validate_flow_control_change` (flow-engine-v2, sección 6), que además frena `cohorte` sobre una ruta del legado. El validador lo mira antes, con un código nuevo del ciclo, `prender_a_cohorte_needs_paused_native`.
- **Publicar** lleva el control a `cohorte` en lugar de a `prendido` (`_flow_change_publish`, los dos lugares donde traduce el ítem a un estado). Los recorridos en curso no cambian.
- **La vista previa** dice `estado: {antes: pausado, despues: cohorte}` y `a_quien: cohorte`.
- **Quién lo pide.** El agente con `propose_flow_change` y el operador con `proponer`, con la aprobación de siempre. Pausar sigue sin pasar por una propuesta.

## El sí por un link en el chat del Automation Expert (1.9.0)

Decisión de Dan del 2026-10-26 (≈13:10Z), que enmienda la decisión 2: *«Dale vamos con A′. Con el botón-link en el chat del AE»*. Corregida el 2026-10-27 con las decisiones de Dan de ese día (≈17:08Z, *todas las recomendaciones*; ver «Lo que decidió Dan el 2026-10-27 sobre el canal»). Escrita antes de construir nada: no afirma que nada esté desplegado ni activado. Mientras no exista, el sí lo sigue dando el comando del operador, que corre Dan («Lo que fija el comando del operador (1.2.0)»).

**Por qué hace falta.** Sin esto, cada cambio que pide Brayan pasa por Dan en la terminal del VPS (`APROBADORES=dan`), y Brayan no puede operar el Automation Expert solo.

**Lo que sigue igual del 2026-10-15:** una lista de aprobadores por instancia que se cambia con un PR (en ATT1, Dan y Brayan; en Maribel, solo Dan: decisión 1 del 2026-10-27); Brayan puede aprobar lo que pidió él; pausar lo pide un aprobador y es inmediato; lo que está en curso sigue con su versión; publicar revalida contra la base, con el plazo de 24 horas y `flow_change_stale_base`.

### El recorrido

1. El agente valida la propuesta con `validate_flow_change`. Si la ruta contesta bien y la propuesta quedó `validada`, **el código del plugin**, no el modelo, deja en el mismo DM un mensaje fijo con un botón de URL «Revisar y aprobar». El botón es un link: no es un clic que atienda Hermes.
2. El link es `https://<dominio del bridge>/flow-approval/p/<proposal_id>`. Lleva solo el `proposal_id`, que es un UUID y no da acceso. No lleva texto del modelo ni ningún secreto. El mensaje sale con el unfurl apagado.
3. La página pide entrar con «Sign in with Slack». Después muestra la vista previa guardada en la validación (el diff, el para qué escapado como texto no verificado, a quién llega, lo que está en curso, la simulación, las advertencias y cómo se vuelve atrás) y los botones que correspondan al estado de la propuesta.
4. La persona aprieta «Aprobar y publicar». El bridge hace lo mismo que `_publish` del comando del operador (`scripts/flow_change_operator.py:655-712`), con el aprobador `slack` en lugar de `operador` (ver «Publicar desde la página»).
5. La página muestra el resultado: publicada, o aprobada sin publicar con su motivo.

### Separación por construcción

- **El modelo no tiene cómo aprobar.** El profile del Automation Expert no recibe la clave de aprobar ni una herramienta que apruebe, publique, pause o descarte. El plugin que postea el link no suma herramientas.
- **La clave vive solo en el bridge:** `FLOW_CHANGE_APPROVER_DB_KEY`, el JWT de un rol de base nuevo, `flow_change_approver`, que ejecuta solo sus envoltorios (ver «Lo que pide a la base»). El bridge no arranca con el canal prendido si esa clave no dice ese rol, o si es igual a la de `service_role` o a `AUTOMATION_EXPERT_CHANGE_DB_KEY`. Estas rutas nunca usan el cliente de `service_role`.
- **Quién aprueba lo dice Slack, no el agente:** el ID sale del OIDC, verificado en el bridge, nunca del mensaje ni de la sesión de Hermes.

### Entrar: OIDC y sesión

El patrón es el de la página de la revisión diaria (`daily-feedback-production-v1.md`, «Autenticación HTTPS» y «Headers y privacidad»), con su propio prefijo de rutas y su propia tabla de sesiones:

- `GET /flow-approval/auth/slack/start?p=<proposal_id>` crea un `state` y un `nonce` aleatorios, guarda solo sus hashes con un vencimiento de 10 minutos y redirige al OIDC de Slack con el scope `openid`. La vuelta va a una URL fija, configurada por instancia.
- `GET /flow-approval/auth/slack/callback` consume el `state` una sola vez, cambia el `code` por el token por HTTPS y valida el `id_token`: el issuer fijo de Slack, la audiencia (el client ID de la app), el vencimiento, el `nonce`, y que `https://slack.com/team_id` sea el workspace de la instancia. El `sub` tiene que corresponder exacto con `https://slack.com/user_id`.
- Recién ahí mira la lista de aprobadores. Quien no está ve «No estás en la lista de aprobadores de esta instancia», y no se crea sesión.
- **La sesión** es un secreto aleatorio de 256 bits en una cookie `__Host-` con `Secure`, `HttpOnly`, `SameSite=Lax` y `Path=/`; la base guarda solo su sha256, con el ID de Slack y el vencimiento. Dura como máximo 8 horas, como la revisión diaria (decisión 4 del 2026-10-27). Salir la revoca.
- **Cada pedido, GET o POST, vuelve a validar** la sesión en la base y que el ID siga en la lista de aprobadores de ese momento. Sacar a alguien de la lista le corta el acceso en el próximo clic, aunque su cookie no haya vencido.
- **CSRF:** todo lo que escribe es un POST con un token derivado de la sesión con un HMAC (separación de dominio), en un campo oculto, independiente de la cookie. El bridge exige además que el `Origin` sea el de la página. Después de un POST, redirige (PRG).
- **Los headers** son los de la revisión diaria: `no-store`, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` y una CSP sin scripts de terceros.

### La lista de aprobadores

- **Vive en el repo de la instancia,** declarada con los IDs de Slack del workspace de la instancia, y llega al bridge como configuración: `FLOW_APPROVAL_SLACK_TEAM_ID` y `FLOW_APPROVAL_SLACK_APPROVERS`. **Se cambia con un PR al repo de la instancia** y la subida de su configuración (decisión 1).
- **Quiénes, por instancia (decisión 1 del 2026-10-27):**
  - **ATT1:** Dan y Brayan (decidido el 2026-10-15).
  - **Maribel:** solo Dan, hasta que Dan nombre a quien opere sus flujos. Esa persona se suma con un PR al repo de Maribel. Como los usuarios que el profile acepta tienen que estar en la lista (abajo), el `SLACK_ALLOWED_USERS` del Automation Expert de Maribel queda también en Dan solo.
- El bridge no arranca con el canal prendido si la lista está vacía o si un ID no tiene la forma `^[UW][A-Z0-9]{8,20}$`.
- Los usuarios que el profile acepta (`SLACK_ALLOWED_USERS`) siguen teniendo que estar en esta lista («Elecciones técnicas, con su valor por defecto»).
- **La base no guarda la lista;** la verifica el bridge en cada pedido, desde la configuración de la instancia (decisión 7 del 2026-10-27).

### Qué se puede hacer en la página

| Acción | Cuándo aparece | Qué hace |
|---|---|---|
| **Aprobar y publicar** | La propuesta está `validada` o `aprobada`, no venció, y su simulación guardada está `built` | Aprueba y publica en un solo POST (ver abajo). |
| **Descartar** | La propuesta está en `borrador`, `validada` o `aprobada` | `_flow_change_discard` con el aprobador `slack`. Pide un motivo, de una línea. |
| **Pausar** | En cada flujo de la propuesta que esté `prendido` o en `cohorte` | `_flow_change_pause` con el aprobador `slack` y la generación que mostró la página. Es inmediato y no pasa por una propuesta. Con otra generación, la página se recarga y no pausa. |
| **Sumar un teléfono a la cohorte** | En cada flujo nativo en `cohorte`, en la lista de flujos y en el resultado de publicar un flujo nuevo | Lo mismo que `cohorte … agregar` del comando del operador, con el aprobador `slack` (ver «Cargar la cohorte desde la página»). |
| **Volver atrás** | Siempre, como explicación | La página dice a qué versión se volvería y que se pide en el DM: «volvé a la versión anterior». Es una propuesta más (§6), que el agente arma y valida, y que llega con su propio botón. La página no arma propuestas. |

Además, `GET /flow-approval/` (con sesión) lista los flujos de la instancia con su estado y su versión, con el botón de pausar, y las propuestas `validada` o `aprobada` que no vencieron. Sirve para pausar sin esperar un mensaje del agente.

**Sin simulación construida, la página no publica:** dice el motivo de la vista previa y que la publica el operador con `--sin-simulacion`, como hoy. El botón no tiene ese atajo (decisión 10 del 2026-10-27).

### Un clic o dos pasos

Decisión 2 del 2026-10-27. Lo que solo toca a la cohorte de prueba, o frena algo, se hace con un clic; lo que llega a todos o corta lo que está en curso pide un segundo paso.

- **Un clic:** pausar, descartar, sumar un teléfono a la cohorte, y aprobar y publicar una propuesta cuyos ítems llegan todos solo a la cohorte: un `nuevo`, un `prender` con `a: cohorte`, y una `version` o un `volver` de un flujo que está en `cohorte`.
- **Dos pasos:** aprobar y publicar una propuesta con al menos un ítem que:
  - pasa un flujo a todos (`prender` sin `a`);
  - cambia un flujo que ya llega a todos (una `version` o un `volver` de un flujo `prendido`);
  - apaga un flujo (`apagar`);
  - cancela lo que espera (`en_curso: cancelar`, cuando el motor lo acepte).
- **El segundo paso** es una pantalla aparte, después del primer clic, que dice en una línea qué hace: «llega a todos» o «corta lo que está en curso», con el flujo y la cuenta de lo que está en curso de la vista previa. Recién su botón aprueba y publica.
- **Lo decide el bridge, no la página:** el bridge clasifica la propuesta con la vista previa guardada y el estado del control que relee (`a_quien`, `estado` y `en_curso` de cada ítem). Un POST de una propuesta de dos pasos sin el campo del segundo paso no aprueba ni publica, y vuelve a la pantalla del segundo paso. El campo va atado a la sesión, a la propuesta y a su `fingerprint`, con el mismo HMAC que el token de CSRF.
- **Ante la duda, dos pasos:** un ítem que el bridge no sabe clasificar (por ejemplo, una `version` de un flujo `pausado`, que no llega a nadie hasta un `prender`) pide el segundo paso. Así un ítem nuevo del ciclo nace con dos pasos hasta que una versión de este contrato diga otra cosa.

### Si hay otra propuesta abierta del mismo flujo

Decisión 5 del 2026-10-27. Lo de antes sigue: una propuesta validada no se edita, y si cambió la base, no publica (`flow_change_stale_base`). Además:

- La página de una propuesta muestra un aviso arriba si hay **otra propuesta abierta** (`borrador`, `validada` o `aprobada`, sin vencer) que toca alguno de sus flujos: el flujo, el estado de la otra y cuándo se validó, sin su para qué.
- El aviso informa, no bloquea: si las dos se publican, la segunda frena sola por `flow_change_stale_base`. El texto lo dice: «si publicás esta, la otra va a tener que validarse de nuevo».
- Lo lee el bridge con su propia función de la base (ver «Lo que pide a la base»), al cargar la página y otra vez en el primer clic de publicar.

### Cargar la cohorte desde la página

Decisión 11 del 2026-10-27, en la v1 del canal. Sin esto, quien crea un flujo con el agente no lo puede probar sin Dan.

- **Es lo mismo que la terminal, por el mismo camino de la base:** la página llama a la función de la cohorte del almacén (la de `operator_flow_cohort_v1`, «Lo que fija el PR 4 (1.7.0)») por un envoltorio del rol `flow_change_approver`, con la acción `agregar` y el actor `slack` con el ID verificado de quien lo cargó. Mismos límites: hasta 5 por flujo (`flow_cohort_full`) y solo en un flujo nativo (`flow_cohort_needs_native_flow`), con los mismos motivos de error.
- **El teléfono se escribe una vez y nunca se muestra de vuelta.** Va en un POST, en un campo sin autocompletar; el bridge lo pasa a la base y no lo guarda, no lo registra en sus logs ni lo pone en la tabla de sesiones. La página muestra solo lo que devuelve la base: `{ok, action, changed, members, max_members}`. El evento `flow_cohort_changed` queda como hoy, sin teléfono ni contacto.
- **Sacar a alguien de la cohorte** sigue en la terminal (`cohorte … sacar`).
- **El agente no lo carga:** el teléfono no pasa por el modelo.

### Publicar desde la página

Del lado del bridge, en este orden:

1. Relee la propuesta por el `proposal_id` del link. Exige que el `fingerprint` del POST, que la página mostró, sea el de la propuesta. Si no es, no hace nada y recarga.
2. Lee el catálogo de plantillas de la bandeja de la instancia. Sin catálogo, 503 `template_catalog_unavailable`, y no aprueba.
3. Valida de nuevo en fase `publish`, **con la clase de quien pidió la propuesta** (`automation_expert` u `operador`), que es lo que la base exige a la revalidación. Con bloqueos, no aprueba.
4. Si la propuesta está `validada`, la aprueba con `('slack', <user_id verificado>)`. La base vuelve a mirar el plazo y la base de la propuesta (`flow_change_stale_base`).
5. Si pasaron más de 8 minutos desde el paso 3, valida otra vez, como el comando del operador (`REVALIDATE_AFTER`, `flow_change_operator.py:87`).
6. Publica con esa revalidación, el `fingerprint` y la zona de la instancia. La base revalida bajo el lock: el plazo de 24 horas, la base, la huella y que la revalidación tenga menos de 10 minutos.

### Un clic viejo o repetido no publica otra cosa

- El link nombra una sola propuesta, y el POST lleva su `fingerprint`. Una propuesta validada está congelada, así que lo que se ve es lo que se publica.
- **Repetido:** aprobar una propuesta ya aprobada con la misma huella y publicar una ya publicada devuelven `replayed`, sin escribir de nuevo. La página muestra «ya estaba publicada».
- **Viejo:** pasadas las 24 horas desde la validación, la propuesta vence y la página lo dice; para seguir, el agente valida de nuevo y deja otro link. Si mientras tanto se publicó otra cosa en la instancia, o se pausó el mismo flujo, la base contesta `flow_change_stale_base` y no publica.
- Dos POST a la vez sobre la misma propuesta los ordena el lock de la base: el segundo ve el primero.

### Lo que queda registrado

- La aprobación, con `approved_by_kind = slack` y `approved_by_ref` = el ID de Slack verificado, en la propuesta y en `flow_definitions`, y el evento `flow_change_approved`.
- La pausa y el descarte, con el actor `slack` y su ID, en `flow_change_events`.
- La carga de la cohorte, con el actor `slack` y su ID, en `flow_cohort_changed`, sin teléfono.
- **Un aviso de una línea en el canal de alertas de la instancia** cada vez que se publica o se pausa algo desde la página (decisión 6 del 2026-10-27):
  - dice el flujo, qué pasó («publicada la versión N para la cohorte», «publicada para todos», «pausado») y quién, como mención a su ID de Slack, con el link a la página;
  - sale por el mismo productor del conector de Slack que usan los avisos del motor (MOT-001 y MOT-002, `flow_engine_runtime.py`), después de que la base confirmó;
  - no lleva teléfonos, ni el para qué, ni texto del modelo;
  - si el aviso no sale, la publicación o la pausa no se deshacen; la página dice que el aviso no salió. Sin conector en la instancia, no avisa, y la página lo dice.
- El ingreso y la salida, en la tabla de sesiones, con el ID y las horas y sin nada más (no van a `flow_change_events`, cuya lista de eventos es cerrada). La base no guarda el token de Slack, ni el `code`, ni el `id_token`.

### Si falla después del sí

- Si la publicación falla después de aprobar (por ejemplo, `flow_change_stale_base`, un bloqueo nuevo en la revalidación o el catálogo que no contesta), la propuesta queda `aprobada` y sin publicar, como dice «Elecciones técnicas, con su valor por defecto».
- La página lo muestra en el mismo momento, con el motivo, y ofrece «Reintentar publicar» mientras no venza. Reintentar hace el recorrido de arriba desde el paso 1; la aprobación ya registrada no se repite.
- Si la base de la propuesta cambió, no hay reintento que sirva: la página dice que hay que pedirle al agente una propuesta nueva.
- La página no le avisa al agente: el aviso por el DM queda afuera de la v1 del canal (decisión 9 del 2026-10-27).

### El flag

- `FLOW_APPROVAL_ENABLED`, **apagado por defecto.** Apagado, las rutas `/flow-approval/` contestan 404 y el plugin no postea el link: dice en el DM que el sí lo da el operador, como hoy.
- **La app de Slack del «Sign in with Slack» es la del Automation Expert de cada instancia** (decisión 12 del 2026-10-27): se le suma el permiso de entrar (`openid`) y la URL de vuelta, y se reinstala. No es la app de la revisión diaria.
- Prendido, el bridge exige al arrancar: la clave del rol, el client ID y el secreto de la app de Slack, la URL de vuelta, el workspace, la lista de aprobadores y el secreto de la sesión, cada uno distinto de los demás secretos del bridge.
- El plugin postea el link solo si la configuración del profile dice que el canal está prendido. Apagar el flag del bridge sin apagar el del profile deja links que dan 404; no publica nada.
- **Del lado del profile** (el plugin `automation-expert-change`, `approval_button.py`), tres variables del `.env` del profile: `FLOW_APPROVAL_ENABLED` (`true` lo prende; cualquier otro valor, o ninguno, lo deja apagado), `FLOW_APPROVAL_BASE_URL` (https y solo el dominio público del bridge, sin ruta, sin query y sin usuario) y el `SLACK_BOT_TOKEN` de la app del agente, el mismo con el que Hermes contesta en ese DM. El DM y el hilo salen de la sesión de Hermes (`HERMES_SESSION_CHAT_ID`, que tiene que ser un `D…`, y `HERMES_SESSION_THREAD_ID`).
- **El botón** es un `button` de Block Kit con `url` y `action_id` `flow_approval_open`, en un bloque `flow_approval`, con texto fijo y el unfurl apagado. Slack igual le avisa a la app cuando alguien lo aprieta: el plugin registra para ese `action_id` un manejador que solo hace `ack` (`register_slack_action_handler`, Hermes 0.21.0).
- **Lo que vuelve al agente** en `data.approval_button` de `validate_flow_change`: `{"status": "posted"}` o `{"status": "not_posted"}`, con una nota fija. Nunca el link ni el error de Slack. Con el canal apagado, la respuesta queda igual que antes.

### Lo que pide a la base

Una migración propia (el número lo asigna la coordinadora), con el molde de `20261021000100`:

- el rol `flow_change_approver`, `NOLOGIN NOINHERIT`, concedido solo a `authenticator`;
- envoltorios `SECURITY DEFINER` con `search_path` fijo, que solo ejecuta ese rol, sobre los helpers del almacén con la clase `slack`: releer una propuesta con su vista previa, leer el almacén de la instancia, aprobar, publicar, pausar y descartar;
- una lectura de las otras propuestas abiertas que tocan los mismos flujos que una propuesta, con su estado y su hora de validación, sin su para qué (decisión 5);
- un envoltorio de la cohorte con la acción `agregar` y el actor `slack` (decisión 11), sobre la misma función que usa el comando del operador;
- la tabla de sesiones y la de `state` del OIDC, con solo hashes, y sus funciones de crear, consumir, validar y revocar;
- sin permisos sobre tablas para el rol, ni para la API.

### Alternativas descartadas

- **A, un clic dentro del chat del agente:** en Socket Mode el clic no viene firmado, la clave queda en el mismo proceso que el modelo separada solo por configuración, y Hermes no controla quién aprieta el botón de un plugin.
- **B, el botón firmado del conector central (decisión 2 original):** el botón le aparece a Brayan fuera del chat del agente, y obliga a tocar el conector compartido con Johanna o a armar otro; 6 a 10 días contra 4 a 6.

### Lo que decidió Dan el 2026-10-27 sobre el canal

Dan aprobó el 2026-10-27 (≈17:08Z) todas las recomendaciones de la lista de valores del canal (filas 0 a 12). Están escritas arriba, donde corresponde; acá va la lista. Se cambian con una versión menor de este contrato.

0. **«Un botón en el chat de Slack» es este canal.** El botón está en el DM del Automation Expert y aprobar es un clic más en la página que abre. Aprobar sin salir de Slack es la opción A, descartada.
1. **Quién aprueba:** en ATT1, Dan y Brayan; en Maribel, solo Dan hasta que nombre a quien opere sus flujos, que se suma con un PR («La lista de aprobadores»).
2. **Un clic o dos pasos:** un clic para la cohorte, pausar y descartar; dos pasos para pasar a todos, cambiar un flujo que llega a todos, apagar y cancelar lo que espera («Un clic o dos pasos»).
3. **El botón vale 24 horas** desde que el agente valida. Vencido, la página lo dice y el agente valida de nuevo.
4. **La sesión dura 8 horas,** como la revisión diaria.
5. **Si algo cambió:** una propuesta validada no se edita y una base cambiada no publica (`flow_change_stale_base`); además, la página avisa si hay otra propuesta abierta del mismo flujo («Si hay otra propuesta abierta del mismo flujo»).
6. **Qué queda registrado:** lo de «Lo que queda registrado», más una línea en el canal de alertas de la instancia al publicar o pausar.
7. **La lista de aprobadores** se verifica en el bridge, desde la configuración de la instancia, y se cambia con un PR.
8. **Volver atrás no se pide desde la página:** se pide en el DM y llega con su propio botón.
9. **Sin aviso al agente** si la publicación falla en la v1: lo muestra la página.
10. **Sin publicar sin simulación** desde la página: queda para el comando del operador.
11. **La cohorte se carga desde la página,** en la v1 («Cargar la cohorte desde la página»).
12. **El «Sign in with Slack» lo hace la app del Automation Expert de cada instancia** («El flag»).

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
