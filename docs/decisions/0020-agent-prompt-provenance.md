# ADR-0020 — La procedencia del prompt se observa donde vive, no se deriva de Git

- **Estado:** aceptada
- **Fecha:** 2026-09-28
- **Decide:** Dan
- **Contrato:** [agent-prompt-provenance-v1](../contracts/agent-prompt-provenance-v1.md)
- **Migración:** `20260928000100_agent_prompt_provenance_v1.sql`

## El problema, medido

El 2026-09-27 Dan completó el reporte de revisión diaria y preguntó dónde queda
esa información. Se midió en producción: las cuatro decisiones de ese día
quedaron en `public.daily_feedback_decisions` con
`release_id = 'release_lineage_unavailable'` y `release_version = 0`.

Esos dos campos existen en el esquema desde `20260910000100`. Nunca se llenaron:
`src/bridge/daily_feedback_export.py` los escribe como literales en el
constructor de `ReviewConversation`, y `materialize_daily_review_package`
tenía además un guard —
`review_package_release_lineage_unavailable_required` — que **exigía** que
fueran exactamente eso. El campo estaba, el reporte lo mostraba, y nada lo
llenaba.

La consecuencia concreta: del feedback de un revisor se sabía qué se dijo y de
qué día, **pero no sobre qué versión del agente**. Que es justo lo que hace
falta para decidir si un cambio de prompt mejoró algo.

Dos hechos más, medidos el mismo día:

| Qué | Cómo está |
|---|---|
| El cuerpo de la respuesta de Hermes | El bridge lo descartaba entero salvo `choices[0].message.content`. `model` venía y se tiraba: no quedaba rastro de **qué** contestó |
| El SOUL que el agente lee | `/opt/data/profiles/agente-comercial/SOUL.md` dentro de `infra_hermes`. Hoy es byte a byte idéntico a `origin/main` (`0d254a82…`), pero **se edita a mano ahí**: había tres copias fechadas de septiembre al lado del archivo vivo (`SOUL.md.2026-09-11`, `.2026-09-26`, `.2026-09-26-2350`) |
| El acceso del bridge al perfil | Ninguno. Su imagen no trae `profiles/` y su único volumen es `/app/data` |
| Un recibo de instalación del perfil | No tiene. Los perfiles `att1` y `client-copilot` sí (`profile-package-installation.json`, con sha256 por archivo) |

## La decisión

**La composición del prompt se observa donde vive, y cada turno del agente queda
registrado con la observación vigente.** Dos tablas nuevas:

1. `agent_prompt_releases` — una fila por composición distinta del prompt,
   observada dentro de `infra_hermes` por
   `scripts/register_agent_prompt_release.py`: el sha256 y el mtime de
   `SOUL.md`, `config.yaml` y `.skills_prompt_snapshot.json`, más el **texto
   completo del SOUL**.
2. `agent_turn_provenance` — una fila por turno: el modelo pedido y el que
   contestó, el sha del bridge, la versión del armador de contexto, el digest
   del contexto exacto que se mandó y las informaciones agregadas
   (`known_fields`), más el release vigente en ese momento.

El registrador se cuelga del cron que el perfil ya tiene. Es idempotente: si
nada cambió, la RPC devuelve `unchanged` y no escribe.

### Lo que no se afirma

El registrador corre por cron, no por turno. Entre dos observaciones el SOUL
puede haber cambiado, así que **en el momento de escribir el turno no se puede
garantizar que el release vigente sea el que el agente leyó**. No se afirma:
`agent_turn_release_confidence_v1` lo calcula después, comparando el mtime de
los artefactos del release **siguiente** contra el momento del turno.

| Veredicto | Qué significa |
|---|---|
| `verified` | El cambio que produjo el release siguiente ocurrió **después** del turno: el turno corrió lo atribuido |
| `misattributed` | Los artefactos ya habían cambiado **antes** del turno: corrió algo más nuevo que lo atribuido |
| `open` | Todavía no hay una observación posterior con la que comparar |
| `no_release` | El registrador no había corrido cuando el agente contestó |

Ese veredicto viaja hasta el revisor, en `context['agent_release']['confidence']`
del paquete de revisión diaria. Si dice `misattributed`, quien lee el feedback
tiene que saberlo en el momento y no tres semanas después.

## Las alternativas que se descartaron

**Derivar el hash del SOUL desde Git, con el `GIT_SHA` del bridge.** Era gratis
y no necesitaba nada nuevo. Se descartó porque sería mentira el día que alguien
edite el SOUL en el VPS sin commitear — y las tres copias fechadas prueban que
editarlo a mano es la práctica, no la excepción. Un campo que dice la verdad
hasta que alguien toca un archivo es peor que un campo vacío: el vacío se ve.

**Montar el perfil read-only en el contenedor del bridge**, para que hashee los
artefactos en cada turno. Sería correcto por construcción, sin ventana de drift
posible. Se descartó **por ahora**: exige tocar EasyPanel, y el directorio
completo no se puede montar porque ahí viven `.env` y `auth.json`. Queda como el
paso siguiente, y el esquema no cambia cuando se dé: el mismo
`agent_turn_provenance` pasa de *registrado con detección de drift* a *correcto
por construcción*.

**Pedirle al agente que declare su propia versión** en la propuesta. Un dato que
el modelo reporta sobre sí mismo no es evidencia: puede alucinarlo.

**Guardar la transcripción junto con la procedencia.** La conversación ya vive en
Chatwoot y el link la abre. Copiarla a la capa durable duplicaría datos
personales sin agregar nada. Del contexto se guarda el digest —que permite
probar después si una reproducción coincide— y lo que el bridge **agregó**:
`known_fields`, la bandera de derivación y la forma del historial. Ningún texto
de mensaje.

**Guardar el contenido de `config.yaml`.** Puede llevar credenciales del perfil.
Viaja su sha256, nunca su contenido; el único artefacto cuyo texto se guarda es
el SOUL.

**Poner el registro detrás de un flag nuevo, apagado por defecto.** Se descartó:
esto solo observa —escribe una fila y no cambia ninguna decisión— y un flag
apagado sería exactamente el modo de falla que este trabajo viene a arreglar. El
campo existía desde el 10/09 y nadie se acordó de llenarlo. Los defaults de
tenant y scope son los reales del inbox de Johanna, así que el registro arranca
con el redeploy sin cargar ninguna variable.

**Hacer obligatoria la lectura de la procedencia en la revisión diaria.** Falla
blando: si la migración no está aplicada, el informe sale igual con el marcador
de "no se sabe". Un informe que se cae entero porque falta un dato accesorio es
peor que un informe sin ese dato.

## Consecuencias

- `release_id` pasa a llevar el digest del release (64 hex) y `release_version`
  su ordinal por scope. El guard del exportador cambió de
  `review_package_release_lineage_unavailable_required` a
  `review_package_release_lineage_invalid`: admite el marcador o un digest real,
  y sigue rechazando un linaje que el llamador se inventó.
- `daily_feedback_item_context_valid` admite una clave más, `agent_release`. El
  validador se extrajo textualmente de `20260927000100` y se parcheó por ancla,
  para que las otras once reglas no puedan divergir.
- Tres entrypoints nuevos de `service_role`: el inventario de ACL pasa de 105 a
  108. El guard del trigger y la función de confianza se le **revocan** a mano,
  porque los privilegios por defecto del esquema de Supabase se los darían.
- Los turnos sin release quedan contables en
  `agent_turn_provenance_without_release_idx`, y el bridge loguea
  `agent_turn_provenance_without_release` cada vez que pasa. Si el registrador
  del perfil deja de correr, se ve.
- El texto del SOUL se guarda una vez por release (hoy 18.833 caracteres), no
  por turno ni por item de revisión.

## Lo que esto no resuelve

El registrador cierra la ventana al intervalo del cron, no a cero. Mientras no
haya montaje read-only, un cambio de SOUL seguido de un turno dentro del mismo
intervalo se atribuye al release viejo — detectable como `misattributed` en la
observación siguiente, no prevenido.

Y no existe todavía ningún mecanismo que lleve el feedback al prompt: esto
registra con qué versión se produjo cada cosa. Que el feedback **cambie** el
comportamiento del agente es un feature aparte, con su propia decisión detrás
(qué entra al prompt, quién lo aprueba, qué pasa si dos revisores se
contradicen).
