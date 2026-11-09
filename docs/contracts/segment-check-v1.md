# Contrato: chequeo del alta de un segmento (`segment-check-v1`, reporte `segment-check-v2`)

Diseño: E-310, «Segmentos: una etiqueta le abre al agente un conocimiento extra», §3, con la
corrección de Dan del 2026-11-03 (E-312b): **el chequeo lo hace un modelo, no reglas fijas**, y lo
que no está claro se pregunta igual que las contradicciones y los huecos. El diseño vive en el vault
del OS (`productos/soporte-infoproductores/documentacion/2026-11-03-e310-diseno-segmentos-conocimiento-por-etiqueta.md`).
Código: `src/bridge/segment_check.py`. El esquema del archivo del segmento (`segment-knowledge-v1`)
y su cargador son de `docs/contracts/segment-knowledge-v1.md` (PR-A del mismo diseño).

## Para qué

Un segmento (una campaña) no entra en producción con una contradicción, un hueco o una duda abierta.
Antes de aprobarlo, un juez de modelo lo lee junto con todo lo que el agente ya sabe y lista cada
cosa que hay que preguntarle al equipo. Con cero items en todas las corridas, el chequeo da el
`chequeo_sha` que va en `[cabecera]`, y el cargador del bridge rechaza un segmento `aprobado` sin ese
sha o con uno que no es el del contenido actual.

## Uso

Es `setter segmento chequear`. Mientras `instance_cli.py` (el `setter` de `validate`) no lo enganche
(lo toca PR-A, y se suma en ese PR o en el siguiente), se corre como módulo:

```
OPENROUTER_API_KEY=… (en el entorno, nunca como argumento)
python -m bridge.segment_check chequear <instancia>/conocimiento/segmentos/<clave>-v<N>.toml
    [--instancia DIR] [--soul SOUL.md] [--catalogo catalogo.json] [--casos casos.json] [--flujos DIR]
    [--modelo M] [--corridas N] [--grabar DIR] [--grabadas r1.json r2.json …]
    [--ahora 2026-11-20T00:00:00-06:00] [--json] [--salida DIR]
```

- `--instancia`: por defecto, la carpeta que contiene `conocimiento/segmentos/`. De ahí salen el
  manifiesto (`agente.conocimiento`, `agente.segmentos`), el conocimiento base, los otros segmentos
  declarados, `flujos/publicados/` (con su `indice.json`), el catálogo de plantillas más nuevo de
  `evidencia/*catalogo-plantillas*.json` y `conocimiento/prueba/casos.json`.
- `--soul`: por defecto, `profiles/agente-comercial-comun/SOUL.md` de este repo.
- `--modelo`: por defecto `moonshotai/kimi-k2.6` (o `SEGMENT_CHECK_MODEL`). Es el respaldo que eligió
  Dan el 2026-10-14 para los profiles de ATT1. Es de otra familia que el agente comercial
  (`z-ai/glm-5.3-flash`), así que no comparte sus puntos ciegos, y ya funcionó con la cuenta de
  OpenRouter con retención cero.
- `--corridas`: por defecto 3.
- `--grabar DIR` guarda la respuesta cruda de cada corrida; `--grabadas` las reproduce sin red.
- Salida: `0` limpio (y `LIMPIO chequeo_sha=<sha>` en stderr), `1` con items abiertos o con alguna
  corrida inválida, `2` si el segmento o una fuente no se pueden leer, `3` si el juez no pudo correr
  (sin clave, o ninguna corrida válida).

## Compuerta (E-364a, solo para `campaign_check`)

`chequear` y `check_files` aceptan `compuerta=` (`Callable[[Item, GateContext], str | None]`). Sin ella, todo sigue igual: cada item abre y el alta por archivo no cambia. Con ella, `Report.items` son los que frenan (`Item.stop_reason` dice por qué) y lo demás va a `Report.notices`. `Report.clean` y `chequeo_sha` salen con la misma fórmula, y `CHECK_RULES_VERSION` no se toca. `check_files` acepta además `label=`, la etiqueta de Chatwoot de un segmento que no está en el manifiesto, para buscar los flujos que la ponen: el `chequeo_sha` no depende de los flujos.

## Qué recibe el juez

Un mensaje `system` con sus instrucciones (`JUDGE_INSTRUCTIONS`) y un mensaje `user` con las
fuentes enteras, cada una entre `<fuente id="…">`:

| id | Qué |
|---|---|
| `segmento` | el archivo TOML del segmento, tal cual, con sus `[[preguntas_del_alta]]` |
| `respuestas` | (reglas v2, solo si hay) las entradas de `[[preguntas_del_alta]]` cuya `pregunta` empieza con `[clave]`, una por bloque: `[clave] Pregunta / Respuesta / Fuente`, presentadas como resueltas |
| `soul` | el SOUL común |
| `base` | el render del conocimiento base, tal como lo recibe el agente |
| `segmento:<clave>` | cada otro segmento declarado cuya vigencia se pisa con este |
| `contexto` | la fecha del chequeo, la vigencia, los flujos que ponen la etiqueta (prendidos o no y sus plantillas), las plantillas que no están en el catálogo y si hay casos |
| `plantilla:<nombre>` | el texto de cada plantilla de esos flujos (sin los ejemplos del catálogo) |
| `caso:<id>` | cada caso del arnés con `segmentos = ["<clave>"]`, entero |
| `preguntas_fijas` | las 12 preguntas del alta (`alta-01` … `alta-12`, E-310 §3.3) |

Al final va un bloque «Datos que encontró el extractor»: montos, porcentajes, fechas, horas, cupones,
garantía, meses sin intereses y medios de cada fuente, sacados con expresiones regulares. Es una
ayuda para que no se le pase un número. No es una fuente citable y no decide nada.

## Qué devuelve el juez

Solo un objeto JSON: `{"items": [{"tipo", "clave", "titulo", "citas": [{"fuente", "texto"}], "pregunta", "campo", "reabre"}]}`
(`reabre` es de las reglas v2; una salida sin ese campo se sigue leyendo).

- `tipo` es `contradiccion`, `hueco` o `duda`. Lo que el juez no tiene claro va como duda, nunca
  como «está bien».
- Una respuesta en `[[preguntas_del_alta]]` deja la cuestión resuelta: el juez no la vuelve a
  preguntar con otras palabras, desde otro campo ni como otro tipo de item. Solo la reabre si la
  respuesta no la resuelve, es ambigua o un texto dice otra cosa; entonces pone en `reabre` la clave
  de la respuesta y cita las dos cosas. Sin reabrir nada, `reabre` va vacío.
- `oferta.cupon` es el cupón de la campaña: no va en `oferta.reemplaza` (que solo admite
  `REPLACEABLE_FIELDS`), y que exista o sea distinto de un cupón del base no es un item. Sí lo es que
  el segmento ofrezca otro cupón o diga que se suma con otro cuando se dice que no.

**Reabrir una respuesta (reglas v2, en código, `gate_reopened`):** un item reabre la respuesta
`[clave]` si lo dice en `reabre` o si el juez le pone el mismo slug que tenía la pregunta contestada
(la clave sin el tipo ni la huella). Queda abierto solo si cita, verificado, un fragmento de esa
respuesta **y** un texto que no está en ninguna entrada de `[[preguntas_del_alta]]`. Si no, sale en
los avisos («el juez volvió a preguntar [clave]… sin citar…») y no cuenta como abierto. La
equivalencia con otras palabras la decide el juez; el código solo exige la evidencia.

**La validación en código:**
- Una salida que no tiene exactamente esa forma es inválida. Hay un reintento por corrida y, si
  vuelve a fallar, la corrida queda inválida.
- Cada cita se busca literalmente en el texto de la fuente que nombra, con NFC, comillas y guiones
  unificados y espacios colapsados. La que no aparece se descarta y queda en `citas_descartadas` y en
  los avisos.
- Un item sin ninguna cita válida no se tira: queda abierto, con `sin_cita_verificable: true`.

## Varias corridas

El juez corre N veces sobre los mismos mensajes. Los items se juntan: dos de corridas distintas son
el mismo si son del mismo tipo y comparten una cita válida. Ninguno se pierde: uno que salió en una
sola corrida se pregunta igual, y el reporte dice en cuáles salió (`corridas`).

Cada item queda numerado (`C1…`, `H1…`, `D1…`) y lleva una clave: el slug del juez más la huella de
sus citas, estable mientras el juez cite lo mismo. También lleva un `mensaje_slack` listo para
mandar.

El chequeo es limpio solo si **las N corridas fueron válidas y no quedó ningún item abierto** (un
item que reabre una respuesta sin la evidencia de arriba va a los avisos y no cuenta).

## Las respuestas del equipo

```toml
[[preguntas_del_alta]]
pregunta = "[c-precio-prohibido-en-el-base-e4a1177d] ¿Para quien está en la campaña vale USD 37?"
respuesta = "Sí: para quien está en la campaña vale USD 37."
fuente = "Slack, <quién>, <fecha>"
```

La corrida siguiente las lee como parte del segmento y, con su clave, en la fuente `respuestas`. Si
la respuesta alcanza lo sigue juzgando el modelo; lo que el código exige (reglas v2) es que, para
reabrirla, el item cite la respuesta y el texto que la contradice.

## El `chequeo_sha`

```
contenido = el TOML del segmento sin cabecera.{chequeo_sha, estado, aprobado_por, aprobado_el}
chequeo_sha = sha256(JSON canónico de {
  esquema: "segment-check-v2", reglas: CHECK_RULES_VERSION,
  segmento: sha256(JSON canónico de contenido),
  base: sha256 del render del conocimiento base,
  otros: [[clave, sha256 del contenido]] de los otros segmentos declarados cuya vigencia se pisa, ordenados,
  abiertos: 0 })
```

- JSON canónico: claves ordenadas, sin espacios, UTF-8 y fechas en ISO 8601.
- No lo cambian: aprobar y escribir el propio sha.
- Lo cambian: el contenido del segmento (también una respuesta nueva), el base, un segmento que se
  pisa y la versión del chequeo.
- El SOUL, las plantillas, los flujos, los casos y el modelo son entradas del juez pero no de la
  huella, porque el bridge no los tiene al arrancar. El reporte guarda sus sha y el modelo de cada
  corrida.

## El enchufe del cargador

```python
from bridge.segment_check import verificar_chequeo, ChequeoError

verificar_chequeo(raw_del_segmento, base=conocimiento_base, otros=[raw de los otros segmentos declarados])
```

- `raw` es el TOML del segmento tal como lo devuelve `tomllib`.
- Un `borrador` pasa.
- Un `aprobado` sin `chequeo_sha`, o con uno distinto del que da `calcular_chequeo_sha`, lanza
  `ChequeoError`.
- El cargador no corre el juez: verifica la huella.

## Límites

- La huella prueba que un chequeo con cero items corrió sobre este contenido solo si nadie calcula el
  sha a mano con `calcular_chequeo_sha`. Es un gate de proceso, no una firma.
- El juez puede no ver algo. Las N corridas y la regla de que lo dudoso se pregunta bajan el riesgo,
  pero no lo eliminan.
- Una cita literal válida prueba que el texto existe, no que el juez lo haya entendido.
