# Contrato: el lanzamiento por fechas, fases de una campaña del AE (v1)

- **Estado:** propuesto. Escrito al construir el 4.8 de la lista definitiva, el 2026-11-26, antes de cualquier merge o despliegue. Describe la interfaz; no dice que nada esté desplegado ni activado.
- **Contrato:** `agent-campaign-phases-v1`, versión `1.0.0`. Extiende [agent-campaigns-v1](agent-campaigns-v1.md) sin cambiar nada de una campaña sin fases.
- **Migración:** `20261126800000_agent_campaign_phases_v1.sql`.
- **Código:** `src/bridge/agent_campaigns.py` (la forma, las piezas y el catálogo vivo), `agent_campaign_api.py` (la vista previa por piezas), `campaign_approval.py` (la página), `campaign_check.py` (el juez por pieza) y `supabase.py` (la lectura del reposo).
- **Flag:** `AGENT_CAMPAIGN_PHASES_ENABLED`, apagado por defecto; exige `AGENT_CAMPAIGNS_ENABLED`. Apagado: el parser del bridge no acepta `fases` ni `bloque_minimo` (una campaña con fases no se puede guardar ni previsualizar: `campaign_invalid` con las claves de más), una fila guardada con fases no se sirve (`contenido_invalido`), el bridge no llama a `get_agent_campaign_rests_v1` y una campaña sin fases se arma, se juzga y se sirve byte a byte igual. `/ready` suma solo la clave `agent_campaign_phases` con `disabled`, y solo donde ya está `agent_campaigns`.
- **No manda mensajes.** Es conocimiento del agente por fecha: cambia qué bloque lee el agente en cada turno. No dispara flujos, plantillas ni envíos.

## Por qué

Un lanzamiento cambia de fase a horas fijas (preventa, carrito abierto, último día, cierre). En GHL lo programan los workflows; con las campañas del AE lo hace una persona: pausa una versión y publica la siguiente. El dato real (`tests/fixtures/agent_campaign_phases_maribel_att1_20261126.json`, leído en solo lectura el 26/11): `forever-young-2026` de Maribel tuvo cinco versiones; la v1 (preventa) terminó el 19/11 a las 16:00Z y la v3 (carrito) se publicó a mano a las 16:21Z; la v3 se pausó a mano a las 22:38Z con el tráfico prendido y la v5 entró el 21/11 a las 18:20Z. En ese hueco de casi 44 horas cayó la ola del 20/11: 15 derivaciones en 90 minutos, porque el agente no tenía ningún bloque (lección 1 del Black: «pausar una campaña también cambia lo que dice el agente»). ATT1 (`black-november-2026`) tuvo una sola versión.

## La forma

Una campaña puede traer, además de lo de siempre:

- **`fases`** (1 a 6, en orden, sin pisarse, adentro de `desde`/`hasta` de la campaña; puede haber huecos entre fases): cada una con `clave` (1 a 24, minúsculas, números y guiones, sin `--` ni guion en las puntas, y nunca `minimo`), `nombre`, `desde`, `hasta` y, opcional, cualquiera de `instruccion`, `oferta`, `asesoramiento`, `se_puede_decir`, `no_confirmado`, `promesas_prohibidas` y `faq`. **Una sección que la fase trae reemplaza entera la de la campaña; la que no trae, la hereda** (D-48.2). Para que una fase no tenga oferta, la oferta va en las fases y no arriba.
- **`bloque_minimo`** (obligatorio con `fases`; se puede traer solo, sin fases): `asesoramiento` (obligatorio) y, opcional, `instruccion`, `oferta`, `se_puede_decir`, `no_confirmado`, `promesas_prohibidas`, `faq`. **Su oferta no puede traer `cupon`, `codigo_oferta`, `precio_antes` ni `frenar_descuento_carrito`** (D-48.3): es lo durable del producto (qué es, el precio de siempre, que es digital, los medios), nunca el lanzamiento. No hereda nada de la campaña. Sin `instruccion`, el bridge le pone una fija que queda en la fila (se juzga y se aprueba con ella): que no hay ninguna fase abierta, que no ofrezca precio de lanzamiento, cupón, bonos ni fechas del lanzamiento, y que las novedades se informan por el mismo chat.
- La campaña entera sigue cabiendo en los 32768 bytes de la fila y la clave de la campaña, en 102 caracteres.

La base (`_agent_campaign_draft_is_valid`, reescrita por la migración) acepta las dos claves con su forma: `fases` de 1 a 6 objetos con clave, nombre y fechas con zona, nunca sin `bloque_minimo`; `bloque_minimo` con su `asesoramiento`. Las reglas finas las corre el bridge, como el resto del borrador.

## Las piezas

Cada fase y el bloque mínimo son una **pieza**: un segmento de `segment-knowledge-v1` con su clave, su vigencia y su render.

| Pieza | Clave | Vigencia | Prioridad |
|---|---|---|---|
| fase | `<clave>--<fase>` | la de la fase | 1000 (la de una campaña) |
| la campaña sin fases, con bloque mínimo | `<clave>` (la de siempre) | la de la campaña | 1000 |
| bloque mínimo | `<clave>--minimo` | desde el inicio de la campaña hasta 14 días después de su `hasta` (D-48.4) | 1001 |
| bloque mínimo de una campaña en reposo | `<clave>--minimo` | la misma | 1002 en adelante |

La etiqueta y el macro son los de la campaña en todas las piezas. El bloque de una fase se titula «<nombre de la campaña> · <nombre de la fase>»; el bloque mínimo, con el nombre de la campaña. La `oferta` de arriba de una campaña con fases pasa por las reglas de oferta (E-410, E-411) aunque ninguna fase la herede, porque es la de la entrada por oferta. La procedencia del turno nombra la pieza (`black-november-2026--carrito`, versión de la fila): quien cuente turnos por campaña tiene que mirar el prefijo `<clave>--` además de la clave. Lo mismo el `campaign_ref` del link con la oferta de una fase (`AGENT_CAMPAIGN_OFFER_LINK_ENABLED`, E-410): lleva la clave de la pieza (cabe en el slug de 128 de esa columna), y VENTAS POR CAMPAÑA del lector del OS tiene que agruparla por el prefijo.

## Qué se sirve en cada turno (D-48.4)

A quien tiene la etiqueta, a lo sumo una pieza de la campaña:

1. La fase cuya ventana contiene ahora.
2. Fuera de toda fase (entre fases, después del cierre y hasta 14 días más), el bloque mínimo. El bloque mínimo **nunca** va junto con una fase de la misma campaña.
3. Una campaña **pausada**, o **terminada** hace menos de 14 días, que trae `bloque_minimo` (la campaña «en reposo»): se sirve solo su bloque mínimo. La lee `get_agent_campaign_rests_v1` (service_role, hasta 20 filas, sin IDs de Slack), con la misma caché de 30 segundos. Se sirve solo si **ninguna publicada tiene su etiqueta ni su clave** (una versión nueva publicada gana; D-48.6), una por clave (la versión aprobada más nueva) y hasta 5 por instancia. Su `chequeo_sha` se verifica igual que el de una publicada. Si la lectura del reposo falla, se sirve lo último bueno del reposo y las publicadas siguen como siempre.

Sin `bloque_minimo`, una pausada no se sirve, como hoy.

### La entrada

- **Sin piezas**, la palabra clave y la pregunta corren en la vigencia, como hoy.
- **Con piezas**, la palabra clave sigue etiquetando hasta 14 días después del cierre, para que quien llega por la pauta reciba el bloque mínimo; la pregunta de entrada, solo en la vigencia.
- **En reposo**, solo la palabra clave (la pregunta no se hace). Si la palabra clave ya es de otra campaña servida, el reposo se queda sin entrada y el bloque mínimo sigue para quien ya tenía la etiqueta.
- **Una campaña en reposo no entra por la herencia, el `etiquetar` de un flujo ni la oferta** (`segment-labels-v1`, `campaign-entry-v1`). La entrada por oferta de una campaña con piezas usa el `oferta.codigo_oferta` de arriba de la campaña, no el de las fases.

Poner la etiqueta no manda nada; sí suma a esa persona a cualquier audiencia que filtre por la etiqueta (los envíos por audiencia y el masivo siguen detrás de sus propios flags y frenos).

## El control de calidad (D-48.5)

El chequeo cubre la campaña entera. `python -m bridge.campaign_check` corre el juez sobre cada pieza (3 corridas por pieza: una campaña con 3 fases y bloque mínimo son 12 corridas) y anota **limpio solo si todas dan limpio**. Cada ítem lleva adelante del número la pieza de la que sale (`carrito:H1`, `minimo:C2`). El `chequeo_sha` de la fila es la huella de los `chequeo_sha` de cada pieza, en orden:

```
sha256(canónico({"esquema": "agent-campaign-phases-v1", "piezas": [[clave_de_la_pieza, chequeo_sha], ...]}))
```

El bridge lo recalcula al publicar y en cada recarga; si no calza, la campaña no se sirve (`chequeo_no_calza`), como una sin fases. Cambiar cualquier pieza cambia la huella de la campaña. `CHECK_RULES_VERSION` no cambia: el `chequeo_sha` de una campaña sin piezas es el mismo de hoy.

## La vista previa y la página

Con piezas, la vista previa (`preview_campaign`, `propose_campaign`, `validate_campaign`) suma `piezas`: por cada una `clave`, `tipo` (`fase`, `campana` o `minimo`), `nombre`, `desde`, `hasta` y `render`. Una vista previa de más de 60000 bytes bloquea con `campaign_preview_too_large` (la fila admite 65536). Las reglas de la instancia corren sobre cada pieza; un bloqueo de una fase lleva su campo (`fases[1]`, `fases[1].oferta.codigo_oferta`, `bloque_minimo`).

La página `/flow-approval/c/{id}` muestra cada pieza con su ventana y lo que el agente va a saber, y dice que fuera de toda fase (entre fases, después del cierre y mientras esté pausada) el agente usa el bloque mínimo. Se aprueban todas juntas con el mismo botón. Pausar y reanudar no cambian: con bloque mínimo, pausar deja al agente con el bloque mínimo en vez de con nada.

## Una campaña viva por producto

No cambia: un lanzamiento por fases es **una** fila, así que ocupa un solo lugar de `live_scope` (`product-card-v1` §7). Pausar un producto pausa sus campañas; con bloque mínimo, esas campañas pasan a servir su bloque mínimo.

## `/ready`

`agent_campaign_phases`: `disabled` sin el flag; con el flag, `pieced=N,resting=N,rest_not_served=N,rests_refresh=ok|failing:N`.

## Con las otras piezas del bridge

- **El juez por el Automation Expert** (`agent-campaign-judge-v1`, 4.9): `judge_campaign` corre la misma función que el comando (`campaign_check.run_check`), así que con una campaña por piezas juzga cada pieza y anota la huella de la campaña entera. Las preguntas que lee el AE llevan la pieza adelante del número (`minimo:H1`).
- **Las reglas de palabra clave** (`keyword-rules-v1`, 3.9; D-48.8): la palabra clave de una campaña en reposo que sirve su bloque mínimo cuenta como la de una publicada: una regla con esa palabra no se guarda ni se sirve (`choca_con_una_campana`). Si el reposo se quedó sin entrada porque su palabra ya es de otra campaña, cuenta la de esa otra.
- **El conocimiento vivo** (`live-knowledge-v1`, 5.5; D-48.9): una campaña en reposo que sirve su bloque mínimo cuenta como un segmento en vigencia (D-55.4): mientras se sirve, el agente usa el conocimiento del archivo, contra el que se juzgó el bloque mínimo.

## Lo que no se verificó

- Ninguna corrida real del juez sobre una campaña por piezas (los tests van con el juez grabado).
- El comportamiento del agente con el bloque mínimo en una conversación real: se mide en el E2E, con el flag prendido en una instancia.
