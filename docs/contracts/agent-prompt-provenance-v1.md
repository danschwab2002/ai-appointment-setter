# Contrato — procedencia del prompt del agente (v1)

Vigente desde la migración `20260928000100_agent_prompt_provenance_v1.sql`.
Decisión: [ADR-0020](../decisions/0020-agent-prompt-provenance.md).

Responde una sola pregunta: **con qué prompt contestó el agente en este turno.**

## Las dos tablas

### `agent_prompt_releases`

Una fila por composición distinta del prompt, **observada donde vive** — dentro
de `infra_hermes`, en `/opt/data/profiles/<perfil>/` — y no derivada de Git.

| Columna | Qué guarda |
|---|---|
| `release_digest` | sha256 del manifiesto canónico: `{ruta: sha256}` ordenado. Es la identidad del release |
| `release_ordinal` | Contador por `(tenant_ref, scope_ref)`, desde 1. Es lo que viaja como `release_version` al paquete de revisión |
| `artifacts` | `{ruta: {sha256, bytes, modified_at}}` de cada artefacto observado |
| `artifacts_modified_at` | El mtime más nuevo de todos. Es con esto que se calcula la confianza |
| `soul_text` | El **texto completo** del SOUL, hasta 200.000 caracteres |
| `observed_at` | Cuándo lo miró el registrador, no cuándo se escribió la fila |

Los artefactos observados por defecto son `SOUL.md`, `config.yaml` y
`.skills_prompt_snapshot.json` — ese último es el snapshot que Hermes escribe con
las descripciones de las skills, y una `description` es prompt.

**Del SOUL se guarda el texto; del resto, solo el hash.** `config.yaml` puede
llevar credenciales del perfil.

Un release es un **hecho observado**: un trigger bloquea `update` y `delete` con
`agent_prompt_release_is_immutable`. No se corrige, se observa de nuevo.

**El digest no depende de los mtimes.** Tocar un archivo sin cambiarlo no inventa
un release nuevo, que es lo que permite que el registrador corra por cron sin
llenar la tabla de ruido.

### `agent_turn_provenance`

Una fila por turno del agente.

| Columna | Qué guarda |
|---|---|
| `turn_digest` | sha256 de `{delivery_id, attempt, context_digest}`, **unique**. Dos intentos son dos turnos; el mismo intento no se cuenta dos veces |
| `occurred_at`, `outcome` | Cuándo, y si el turno salió `completed` o `failed` |
| `release_id`, `release_digest`, `release_ordinal` | El release vigente, **desnormalizado**: registrar un release nuevo no reescribe lo que ya se dijo de un turno viejo. Los tres van juntos o los tres van en `null` |
| `model_requested`, `model_answered` | Lo que el bridge pidió y lo que Hermes dijo que contestó |
| `bridge_release` | El `GIT_SHA` del bridge que armó el turno |
| `context_builder_version` | Se sube cuando cambia la **forma** del contexto. Un turno con otra versión no es comparable |
| `context_digest` | sha256 del JSON exacto que se le mandó al agente |
| `context_added` | Lo que el bridge **agregó**: `known_fields`, cuáles venían con valor, la bandera de derivación, y la forma del historial |

**`context_added` nunca lleva el texto de los mensajes.** La transcripción vive
en Chatwoot; duplicarla acá copiaría datos personales sin agregar nada. El
`context_digest` es lo que permite probar después si una reproducción coincide
con lo que el agente realmente vio.

## Los tres entrypoints

| RPC | Quién la llama | Qué devuelve |
|---|---|---|
| `register_agent_prompt_release_v1` | `scripts/register_agent_prompt_release.py`, dentro de `infra_hermes` | `registered` o `unchanged` |
| `record_agent_turn_provenance_v1` | El bridge, al cerrar cada turno contra Hermes | `recorded`, `recorded_without_release` o `replayed` |
| `get_agent_turn_provenance_v1` | El servicio de revisión diaria, al armar el lote | Por conversación, la procedencia del **último** turno de la ventana |

Las tres son `security definer` y solo las ejecuta `service_role`. El guard del
trigger y `agent_turn_release_confidence_v1` se le **revocan** a mano: en
Supabase los privilegios por defecto del esquema le darían `execute` sobre toda
función nueva, y aparecerían como puertas de servicio que nadie declaró.

## La confianza de la atribución

El registrador corre por cron, no por turno. Entre dos observaciones el SOUL
puede haber cambiado — y se edita a mano en el VPS. Así que **la atribución no se
afirma**: se calcula comparando el mtime de los artefactos del release
**siguiente** contra el momento del turno.

| Veredicto | Cuándo |
|---|---|
| `verified` | El release siguiente cambió **después** del turno: el turno corrió lo atribuido |
| `misattributed` | Los artefactos ya habían cambiado **antes** del turno: corrió algo más nuevo |
| `open` | Todavía no hay observación posterior con la que comparar |
| `no_release` | El registrador no había corrido cuando el agente contestó |

Ese veredicto llega hasta el revisor en
`context['agent_release']['confidence']` del paquete de revisión diaria.

## Qué cambió en la revisión diaria

`release_id` y `release_version` existían desde `20260910000100` y se escribían
como `'release_lineage_unavailable'` y `0`, literales. Ahora llevan el digest y
el ordinal del release.

El guard del exportador pasó de
`review_package_release_lineage_unavailable_required` — que **exigía** el
literal — a `review_package_release_lineage_invalid`, que admite dos formas y
nada más: el marcador de "no se sabe", o un digest de 64 hex con versión ≥ 1. Un
string arbitrario sigue siendo un linaje inventado, y eso es peor que no tenerlo.

`daily_feedback_item_context_valid` admite una clave más, `agent_release`, con el
digest, el ordinal, los dos modelos, el sha del bridge, el digest del contexto y
la confianza. **No** lleva el texto del SOUL: son 19 KB por release, y vive en
`agent_prompt_releases`.

La lectura **falla blando**: si la migración no está aplicada, el informe sale
igual con el marcador de "no se sabe".

## Cómo se registra un release

Adentro del contenedor, como usuario `hermes`:

```sh
python3 scripts/register_agent_prompt_release.py --dry-run   # calcula y no escribe
python3 scripts/register_agent_prompt_release.py
```

Credenciales: primero PostgREST (`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`);
si no están, la Management API (`SUPABASE_PROJECT_REF` + `SUPABASE_ACCESS_TOKEN`),
que es la que el contenedor ya tiene. Se leen del entorno y no se imprimen.

Por la Management API la carga viaja como **un literal dollar-quoted** con una
etiqueta aleatoria verificada contra el contenido: interpolar el texto del prompt
en SQL a mano sería pedir un escape roto.

**Se cuelga del cron del perfil** (`/opt/data/profiles/agente-comercial/cron`)
para que no dependa de que alguien se acuerde. Es idempotente.

## Qué mirar cuando algo no cuadra

| Síntoma | Dónde |
|---|---|
| El feedback sigue sin versión | `select count(*) from agent_turn_provenance where release_id is null` — y el log del bridge, `agent_turn_provenance_without_release` |
| La versión que muestra el informe no es de fiar | `context['agent_release']['confidence']` dice `misattributed` |
| El registrador dejó de correr | El release más nuevo: `select max(observed_at) from agent_prompt_releases` |
| Qué prompt exactamente | `select soul_text from agent_prompt_releases where release_digest = '<el del informe>'` |

## Los límites, declarados

- La ventana de incertidumbre es el intervalo del cron, no cero. Un cambio de
  SOUL seguido de un turno dentro del mismo intervalo se atribuye al release
  viejo; se detecta como `misattributed` en la observación siguiente, no se
  previene. Cerrarlo del todo exige montar el perfil read-only en el bridge
  (ADR-0020, alternativas).
- Los turnos anteriores a la primera corrida del registrador quedan sin release
  para siempre. No se puede reconstruir: el SOUL de ayer no se guardó en ninguna
  parte.
- Esto **registra**, no cambia el comportamiento del agente. Que el feedback
  llegue al prompt es un feature aparte.
