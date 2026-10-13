# ADR-0025: cada instancia tiene su propio Hermes, declarado por el producto, con tres profiles

- Estado: aceptada
- Fecha: 2026-10-13
- Decide: Dan
- Afina: [ADR-0005](0005-reproducible-client-deployments.md) §1 y §5, y [ADR-0021](0021-setter-producto-instalable.md) §7
- Concreta: [ADR-0006](0006-three-agent-product-surface.md) §1 y §3
- Nombres: [`docs/referencia-nombres.md`](../referencia-nombres.md)

## Contexto

El ADR-0005 pidió en julio un Hermes en el stack de cada cliente, con versión fija y separado del Hermes de desarrollo. El ADR-0021 §7 dejó a cada instancia con «el profile de Hermes» propio, y no con un Hermes propio. El producto nunca declaró nada de Hermes:

- el bridge conoce solo su dirección (`HERMES_API_BASE_URL`) y el nombre del profile (`HERMES_MODEL_NAME`, igual a `[agente].modelo` del manifiesto);
- Hermes se configura a mano, afuera del repo.

Medido el 2026-10-13 sobre el Hermes compartido del VPS (servicio `infra_hermes`, imagen `nousresearch/hermes-agent:v2026.8.31`):

- Tiene cinco profiles: `default`, `agente-comercial` (Johanna), `att1-agente-comercial`, `client-copilot` y `client-copilot-correlation-review`. El `client-copilot` difiere de su versión de Git, y al de ATT1 le faltan el Automation Expert y el copiloto.
- Cada profile corre su propio proceso, de 300 a 420 MB, y el panel de control ocupa 218 MB: 1,45 GB en total. Un Hermes con N profiles pesa casi lo mismo que N Hermes con uno cada uno, así que compartirlo no ahorra memoria.

Lo que el bridge usa de Hermes en la `v1.6.1` (#229, merge `5723806`) son cuatro pedidos, los cuatro a `POST /chat/completions`, sin sesión y con `stream: false`:

- la respuesta del agente comercial, con el nombre del profile como `model` (`src/bridge/hermes.py`);
- el nombre de pila y la ayuda para correlacionar, también con el nombre de un profile. En ATT1, el del comercial;
- partir la respuesta en mensajes, que pide un modelo por nombre y proveedor (`model` y `provider`, `src/bridge/reply_splitter.py`). En ATT1, `z-ai/glm-5.3-flash` por OpenRouter, por la misma dirección que el comercial.

El profile comercial tiene todas las herramientas apagadas. Lo que Hermes trae y sería caro de rehacer es para los agentes con herramientas, el Automation Expert y el copiloto: el ciclo de herramientas, Slack y Telegram con permisos, las sesiones, la memoria y las skills.

El Automation Expert del ADR-0006 §3 «diseña y explica; la aplicación valida y ejecuta» ([diseño](../design/automation-expert-configuration-experience.md)). Pero hoy los flujos son código y variables de entorno, así que no tendría sobre qué operar sin tocar código:

- el motor de seguimiento acepta solo `purpose = 'cart_recovery'` (`supabase/migrations/20260803000100_followup_engine_v1.sql`);
- cambiar una espera, una plantilla o un tope pide una versión nueva por SQL, variables y redesplegar;
- el cupón y la reactivación son código propio (`src/bridge/followup_discount.py`, `src/bridge/reactivation.py`).

`profiles/att1/` y `scripts/install_att1_product_profiles.py` (commit `340fb2b`) son un paquete candidato inerte de un intento anterior. El agente de ATT1 no lo usa: corre con el SOUL común (`profiles/agente-comercial-comun/SOUL.md`) y el conocimiento que manda el bridge (ADR-0021 §6).

## Decisión

1. **Hermes es parte del producto.**
   - El producto publica una imagen propia de Hermes con cada release, junto al bridge y la revisión diaria: `ghcr.io/danschwab2002/setter-hermes:<versión del producto>`.
   - La imagen parte de la oficial con la versión fija y le suma lo nuestro: plugins y moldes de profile. No se copia el código de Hermes.
   - Hasta que haga falta lo nuestro (el primer plugin), una instancia usa la imagen oficial con la versión que el producto declara.
   - Subir Hermes de versión es un PR en el producto, con una prueba en el CI que instale los profiles y les hable.
   - Como el bridge, una versión nueva llega a cada instancia por un PR en su repo, primero a la de menor facturación (ADR-0021 §3).

2. **El producto declara cómo queda el Hermes de una instancia,** en un archivo versionado. El formato se define en el PR que escribe el instalador.
   - **Qué declara:**
     - qué profiles existen, y nada más;
     - el molde de cada uno: SOUL, configuración, herramientas, plugins y skills;
     - quién le habla: el bridge por la red interna, o Slack con su lista de usuarios.
   - **La declaración de cada versión lista los profiles que esa versión trae.** Primero el agente comercial; el Automation Expert y el copiloto se suman con la versión que los construye.
   - **Qué queda apagado:** el panel de control, la terminal, los archivos y el profile `default`. Si Hermes no deja borrar el `default` porque es su carpeta raíz, queda sin canal, sin modelo y sin herramientas.
   - **Lo que pone la instancia:** el modelo de cada profile, en el manifiesto, y sus keys, en los secretos.
   - **Un instalador** deja el Hermes de la instancia igual a la declaración, y correrlo dos veces da lo mismo. Verifica los archivos por hash antes de escribir, como el instalador candidato de `profiles/att1/`.
   - **Un chequeo de deriva** compara lo que corre con lo declarado: lo que sobra, lo que falta y lo que difiere de Git. Sale con error ante cualquier diferencia.

3. **Un Hermes por instancia, adentro del stack de la instancia.**
   - El bridge le habla por la red interna, con el servicio llamado `hermes`. El bridge ya acepta http solo hacia ese nombre o hacia localhost (`src/bridge/app.py`).
   - El Hermes compartido (`infra_hermes`) queda como transición: Johanna, hasta que se mude al manifiesto, y el agente comercial de ATT1, hasta que pase al Hermes propio de su instancia.
   - Después, ese Hermes queda solo para desarrollo, separado de los clientes, como pide el ADR-0005 §1.

   ```text
   stack de la instancia
   ├── db · rest · gateway
   ├── appointment-bridge ──(http interno)──▶ hermes
   ├── daily-feedback                         ├── agente-comercial
   └── en el servidor: alertas · respaldo     ├── automation-expert
                                              └── client-copilot
   ```

4. **Tres profiles por instancia, los del ADR-0006:** `agente-comercial`, `automation-expert` y `client-copilot`.
   - El agente comercial se queda en Hermes y se aliviana, en vez de salir. Así el diseño es más predecible y más simple de entender, y lo que se ahorraría sacándolo se gastaría en construirlo en el bridge.
   - Los pedidos auxiliares del bridge no suman profiles. El nombre de pila y la ayuda para correlacionar usan el profile del comercial. Partir la respuesta llega por la misma dirección con un modelo propio: antes de apagar el `default`, la prueba en seco confirma que lo atiende el profile del comercial.
   - Adentro del Hermes de una instancia, los profiles van sin prefijo. La declaración es la misma en todas, y lo que cambia es el modelo de cada uno en el manifiesto.

5. **El Hermes de una instancia se mantiene liviano:**
   - sin panel de control ni `default`;
   - cada profile solo con los canales que usa (el comercial, solo el que le habla al bridge);
   - un tope de memoria para el contenedor, como el que ya tienen el bridge y la base. Se fija después de medir un Hermes con un solo profile;
   - una imagen sin las dependencias que no usamos (navegador, voz, imágenes). Antes de contar con eso, se mide si baja la memoria además del disco.

6. **Los nombres siguen la referencia de [`docs/referencia-nombres.md`](../referencia-nombres.md).** Todo lo que se despliega para una instancia arranca con su nombre corto y sigue con el mismo nombre para el mismo servicio. Nada que esté corriendo se renombra solo por el nombre: cada pieza toma el nombre nuevo cuando se mude.

7. **El Automation Expert cambia datos, nunca código.**
   - Los flujos pasan a ser definiciones con versión, guardadas en la base: disparador, condiciones de entrada, pasos y condiciones de salida.
   - Un motor del bridge las ejecuta sin redesplegar.
   - El Automation Expert las lee y propone cambios por una API acotada del bridge. Una persona aprueba antes de publicar, y se puede volver a la versión anterior.
   - El código cambia solo por el producto: PR, CI, release y subida.
   - Quedan en código, a propósito, los límites:
     - los adaptadores de Hotmart y de las landings;
     - la baja;
     - los máximos de frecuencia;
     - las reglas de Meta: la ventana de 24 h y las plantillas aprobadas;
     - que una persona pueda tomar la conversación;
     - no mandar dos veces lo mismo.

   El modelo de flujos, su API y su motor se definen en su propio contrato, antes de construirlos.

## Consecuencias

- **Capacidad estimada.** Con tres profiles, el Hermes de una instancia ronda 1 GB y la instancia entera 1,5 GB (el stack de ATT1 sin Hermes ocupaba 0,5 GB el 2026-10-13). En un VPS con 18 GB libres entran unas diez instancias con margen.
- **Hermes se sube de a una instancia,** igual que el bridge, y una caída de Hermes afecta a una sola instancia.
- **El Automation Expert nace con las herramientas al lado de las keys de su propia instancia,** no de las de otros clientes.
- **`profiles/att1/` y su instalador salen del producto.** De ahí pasan al molde genérico la configuración con todo apagado y la verificación por hash. El claim `att1-product-hermes-runtime`, que reserva esos archivos desde el 2026-09-07, se cierra antes de sacarlos.
- **`profiles/agente-comercial/SOUL.md`, el SOUL de Johanna, queda** hasta que Johanna se mude al manifiesto.
- **`profiles/client-copilot/` entra a la declaración cuando se construya el copiloto.** Su plugin de revisión de correlación sigue congelado.
- **Hasta que existan los flujos como definiciones, el Automation Expert solo lee y explica.**
- **Costos:**
  - un Hermes más que actualizar por instancia;
  - la imagen derivada que mantener;
  - el instalador y el chequeo, que hay que sostener cuando Hermes cambie su formato de profiles.

## Alternativas descartadas

- **Un Hermes compartido entre instancias.**
  - No ahorra memoria.
  - Los profiles comparten el disco, y el Automation Expert, que tiene herramientas, quedaría al lado de las keys de otros clientes.
  - Si Hermes se cae o se satura, se caen todas las instancias.
  - No se puede subir de versión primero la que menos factura.
  - No sirve para clientes de otras empresas (ADR-0005 §2).
- **Copiar el código de Hermes al producto.** Para frenar las actualizaciones automáticas alcanza con fijar la versión. Una copia se desactualiza enseguida, porque Hermes publica cada dos semanas, y los arreglos de seguridad habría que traerlos a mano.
- **Sacar el agente comercial de Hermes y llamar al modelo desde el bridge.** Ahorra un proceso por instancia, pero obliga a rehacer en el bridge lo que Hermes ya resuelve (el SOUL delante, la elección del modelo) y deja dos caminos distintos para los agentes.
- **Un Automation Expert que edite el código del bridge, sus variables o las tablas activas.** Saltea la validación determinística y la aprobación que piden el ADR-0003 y el ADR-0006 §3, y un error suyo llega a producción sin PR ni CI.

## Implementación

Se escribe en futuro: ninguna parte está construida al escribir este ADR.

1. Un PR del producto con la declaración, el instalador, el chequeo de deriva y la salida de `profiles/att1/`, con el instalador probado en el CI.
2. El Hermes propio de ATT1 con el agente comercial adentro:
   - una prueba en seco antes de pasar el bridge;
   - la vuelta atrás al Hermes compartido;
   - `att1-agente-comercial` sale del compartido después de una semana sin problemas.
3. El respaldo y las alertas de la instancia cubren ese Hermes.
4. El Automation Expert se suma a ese Hermes como un profile más, con la imagen derivada que trae el plugin que lee los flujos.
