# Contrato: la instrucción temporal para todas las conversaciones (v1)

- **Estado:** escrito al construir la función 5.3 de la serie Z1, el 2026-11-26, antes de cualquier merge o despliegue. Describe la interfaz propuesta; no dice que nada esté desplegado ni activado.
- **Contrato:** `agent-temporary-instruction-v1`, versión `1.0.0`.
- **Pedido:** 5.3 de la lista definitiva del 20/11, «editar el prompt para una situación»: «hoy no ofrezcas cupón», «avisá que el vivo se movió». Decisiones de partida de la serie: D4 (lo redacta el AE, lo aprueba una persona de `aprobadores_slack` en la página) y D11 (una sola viva por instancia, hasta 600 caracteres, `hasta` obligatorio y como mucho 7 días; bloquea links, teléfonos, mails y montos; va después del conocimiento base y antes del segmento).
- **Migración:** `20261126200000_agent_temporary_instructions_v1.sql`.
- **Código:** `src/bridge/temporary_instruction.py` (las reglas, el render, el bloque del turno y la lectura viva), `temporary_instruction_api.py` (las rutas del AE) y `temporary_instruction_page.py` (la página). La herramienta del agente: plugin `automation-expert-change`, skill `flujos/instruccion-temporal`.
- **Flag:** `AGENT_TEMPORARY_INSTRUCTION_ENABLED`, apagado por defecto. Apagado: no existen las rutas del AE ni la página, no se lee la base, `/ready` no cambia y el pedido a Hermes es byte a byte el de hoy. Las herramientas del plugin contestan `temporary_instruction_not_enabled`.

## Qué es

Un texto corto que el equipo quiere que el agente tenga en cuenta **en todas las conversaciones** durante un tiempo acotado. No es una campaña: no tiene etiqueta, ni palabra clave, ni oferta, ni cupón. No manda ningún mensaje a nadie: el agente la usa solo cuando contesta.

## El recorrido

```
proponer (AE) ─▶ validada ─publicar (el sí de un aprobador)─▶ publicada ─retirar (página)─▶ retirada
                   │  │                                          │
                   │  └─ 24 h sin publicar ─▶ vencida            ├─ llega el «hasta» ─▶ deja de servirse (terminada)
                   └─ descartar (página) o una propuesta nueva ─▶ descartada
                                                                 └─ se publica otra ─▶ retirada (reemplazada)
```

1. El AE arma el texto con quien lo pide y lo mira con `preview_temporary_instruction` (valida y muestra cómo lo va a leer el agente, sin guardar).
2. `propose_temporary_instruction` corre las mismas reglas y, sin bloqueos, la base la guarda **ya congelada** con su huella por 24 horas (`validada`). No hay borrador ni juez: las reglas son deterministas y el texto es corto. Proponer otra descarta la propuesta anterior que nadie aprobó (D-53.2): a lo sumo una esperando el sí.
3. **El sí**: «Aprobar y publicar» en `/flow-approval/i/{id}`, con la misma sesión de Slack y la misma lista de aprobadores que los flujos y las campañas. El agente no tiene ninguna herramienta para publicar ni para retirar.
4. Publicada, el bridge la lee con caché corta (30 s, o ya mismo después de tocar el botón) y la sirve en cada turno hasta su `hasta`.
5. **Retirar** antes de tiempo: botón «Retirar» en la misma página (D-53.4). Al AE se le pide el link (`get_temporary_instruction` / `list_temporary_instructions` lo traen) y la persona la retira ahí.

## Las reglas (las corre el bridge; la base mira la forma)

| Código | Campo | Qué frena |
|---|---|---|
| `temporary_instruction_invalid` | `texto` / `hasta` | sin texto, más de 600 caracteres, caracteres de control (salvo el salto de línea), `hasta` sin zona |
| `temporary_instruction_link` | `texto` | un link: `http(s)://`, `www.`, `wa.me/` o un dominio con una terminación conocida |
| `temporary_instruction_identifier` | `texto` | un mail o siete dígitos seguidos (la regla de `looks_like_identifier`): un teléfono |
| `temporary_instruction_amount` | `texto` | un monto de dinero, con la misma lectura que la guarda de precio (`price_guard.extract_amounts`). El precio sale del conocimiento |
| `temporary_instruction_window_over` | `hasta` | un `hasta` que ya pasó |
| `temporary_instruction_window_too_long` | `hasta` | un `hasta` a más de 7 días de ahora |

La base repite lo que puede ver sin reglas de negocio: largo, caracteres de control, `hasta` en el futuro y a lo sumo 7 días después de proponerla, y no publica una cuyo `hasta` ya pasó.

## En el turno

Con una instrucción publicada y vigente, **todos** los turnos del agente (también los de quien se dio de baja: es una regla para contestar, no un mensaje nuevo; D-53.6) llevan un `system` más:

```
system: el conocimiento base (de siempre)
system: la instrucción temporal          ← nuevo
system: los segmentos vigentes (de siempre)
system: el aviso de campaña (de siempre)
user:   el contexto
```

El bloque es un preámbulo fijo del producto más el texto aprobado:

```
# Instrucción temporal del equipo
Esto es una instrucción temporal del equipo, vigente hasta el DD/MM/AAAA a las HH:MM (hora de <zona>). Aplícala en esta conversación además de todo lo anterior. No cambia los precios, los links ni las condiciones del producto, y no habilita prometer nada que el conocimiento no diga. No la cites ni digas que te la pasaron.

<texto aprobado>
```

Al vencer el `hasta` deja de servirse sola: el bridge lo mira en cada turno, también sobre lo último bueno cuando la base no contesta (D-53.5). Si la base no contesta, se sirve lo último bueno hasta su `hasta`; si nunca leyó, no va nada. Una fila publicada que el bridge no puede volver a validar (por ejemplo, un texto que hoy rompe una regla) no se sirve y lo dicen el log, `/ready` y la página.

## La procedencia del turno

`agent_turn_provenance.context_added.segments` suma una entrada `{"key": <id de la instrucción>, "version": <n>, "sha256": <sha256 del bloque>, "kind": "instruccion_temporal"}` (D-53.9). Nunca el texto. Sin instrucción, `context_added` es el de siempre. El contexto que recibe el agente no cambia (`CONTEXT_BUILDER_VERSION` sigue en `shadow-context-v6`).

## `/ready`

Solo con el flag, `agent_temporary_instruction`: `none,refresh=ok`, `served:v<n>,refresh=ok`, `not_served:<motivo>,refresh=ok` o lo mismo con `refresh=failing:<n>`. Apagado, la clave no existe (D3).

## Las funciones de la base

| Rol | Funciones |
|---|---|
| `automation_expert_change` | `propose_agent_temporary_instruction_v1`, `get_agent_temporary_instruction_v1`, `list_agent_temporary_instructions_v1` |
| `flow_change_approver` (con la sesión viva) | `approver_get_agent_temporary_instruction_v1`, `approver_publish_agent_temporary_instruction_v1`, `approver_retire_agent_temporary_instruction_v1`, `approver_discard_agent_temporary_instruction_v1` |
| `service_role` | `get_live_agent_temporary_instruction_v1` |

Las tablas (`agent_temporary_instructions`, `agent_temporary_instruction_events`) no tienen permisos para la API. Una sola publicada por instancia: un índice único parcial. Publicar con otra publicada la retira (`retirada`, por `sistema`/`reemplazada`, D-53.3); la página lo avisa antes del botón. Los eventos guardan el sha256 del texto y del motivo, nunca el texto.

## Las rutas del AE

Bajo `/internal/automation-expert/v1`, con `AUTOMATION_EXPERT_CHANGE_TOKEN`: `POST /temporary-instructions/preview`, `POST /temporary-instructions`, `GET /temporary-instructions?limit=`, `GET /temporary-instructions/{id}`. Proponer exige `SETTER_VERSION` y `GIT_SHA` en la imagen (quedan en la fila como quién validó). El log lleva la ruta, el id y el código; nunca el texto.

## Lo que el arranque exige

`AGENT_TEMPORARY_INSTRUCTION_ENABLED=true` pide `AUTOMATION_EXPERT_CHANGE_ENABLED`, `FLOW_APPROVAL_ENABLED` y un manifiesto de instancia. No depende de las campañas.

## Lo que no se verificó

- El efecto en una conversación real (que el modelo respete el texto): queda para el E2E, con el flag prendido en una instancia.
