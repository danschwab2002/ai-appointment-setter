# Contrato: el conocimiento vivo, editado por el Automation Expert (live-knowledge-v1)

- **Fecha:** 2026-11-26. Propuesto: nada de esto corre en ninguna instancia hasta que una version que lo traiga se suba y se prenda el flag.
- **Pieza:** 5.5 de la lista definitiva («Editar el conocimiento base hablando con el AE»), mapa de las funciones faltantes de Z1, D4.
- **Codigo:** `src/bridge/live_knowledge.py` (reglas, servido), `src/bridge/live_knowledge_api.py` (rutas del AE), `src/bridge/knowledge_approval.py` (la pagina), migracion `20261126600000_agent_live_knowledge_v1.sql`.
- **Flag:** `AGENT_LIVE_KNOWLEDGE_ENABLED` (apagado por defecto). Apagado, las rutas no existen, el bridge no lee la base para esto, `/ready` no cambia y el pedido a Hermes es byte a byte el de hoy.

## Que es

Hoy cambiar el conocimiento de una aliada es un archivo nuevo en el repo de la instancia (`knowledge-v<N>.toml`), la firma de Dan y un redeploy ([commercial-knowledge-v1](commercial-knowledge-v1.md), «Versiones»). Con el flag, el AE redacta con el duenio **un cambio sobre lo vigente**, el bridge lo valida con el mismo cargador (`CommercialKnowledge.from_mapping`) y contra la instancia, y una persona de la lista de aprobadores lo aprueba en la pagina de aprobacion. Aprobado, el bridge lo sirve en vivo, sin subir nada. Se vuelve al archivo con un boton.

El archivo del manifiesto sigue siendo la referencia: es lo que se sirve si nunca hubo una version viva, si se volvio al archivo, si la base no contesta en el primer arranque y si el archivo cambio despues de aprobar la viva.

## El cambio (D-55.1)

El AE no manda el TOML entero: manda las secciones que cambian, enteras, como JSON.

```json
{"cambios": {"faq": [ {"pregunta": "...", "respuesta": "...", "fuente": "...", "vigencia": "..."} ],
             "voz": {"resumen": "...", "reglas": ["..."]}}}
```

- Las claves posibles son las tablas de primer nivel del contrato menos `[cabecera]`: `identidad`, `voz`, `oferta`, `contenido`, `no_confirmado`, `promesas_prohibidas`, `limites_sensibles`, `faq`, `pagos`, `se_puede_decir`. En la forma de varios productos, ademas `productos.<clave>` reemplaza la tabla de ese producto (con la misma `clave`).
- Cada valor reemplaza la seccion entera. `null` saca una seccion opcional (`faq`, `pagos`, `se_puede_decir`).
- La cabecera la arma el bridge: la version del archivo, la aliada del manifiesto y, al servir, quien aprobo y cuando. El render la usa solo para el titulo, asi que el hash de lo validado es el hash de lo servido.
- El texto que resulta se valida con el cargador del contrato: las mismas claves, el tope de 32.000 bytes del render y la red de secretos.

Por que por secciones y no el TOML entero: el TOML de una aliada lleva sus fuentes en comentarios (ATT1 v15: 69 KB de archivo para unos 30 KB de render) y el AE no las ve ni las puede mantener; un cambio por secciones deja lo demas byte a byte como estaba y la pagina muestra exactamente que se toca. Los comentarios no viajan: la version viva guarda el `purpose` del AE y el diff, y el archivo sigue siendo la historia con fuentes.

## Las reglas contra la instancia

Bloquean (no se guarda):

| Codigo | Que |
|---|---|
| `knowledge_change_invalid` | El cambio no tiene la forma de arriba (clave que no existe, cabecera, `null` en una obligatoria). |
| `knowledge_invalid` | El cargador lo rechaza (falta una clave, sobra otra, pasa el tope, forma de credencial). |
| `knowledge_change_empty` | El render queda igual que el vigente. |
| `knowledge_shape_changed` | Pasa de un producto a varios o al reves. Eso sigue siendo un archivo nuevo. |
| `knowledge_products_mismatch` | Los productos no son los mismos que el manifiesto ata (mismas reglas que al arrancar). |
| `knowledge_offer_name_changed` | Cambia el nombre de la oferta de un producto: es lo que el turno usa para nombrarlo en `products`. |
| `knowledge_price_not_in_catalog` | El precio de una oferta no es un precio del manifiesto (`[hotmart]`, `[[productos]]`, `[guarda_precio] montos_extra`). |
| `knowledge_amount_not_in_catalog` | Un monto nuevo del texto (fuera de «No confirmado» y «Promesas prohibidas») que no esta en el catalogo ni en el vigente, con la comparacion de `price_guard`. |

Avisa (no frena): `knowledge_harness_not_run` (el arnes F-055 no corre desde aca, D-55.8) y `knowledge_near_size_limit` (pasa el 90 % del tope).

## El ciclo

1. **Proponer** (`POST /internal/automation-expert/v1/knowledge`): valida y, sin bloqueos, la base la guarda **validada**, con su huella y 24 horas para aprobarla. Un solo paso: no hay macro ni juez que esperar (D-55.6). Como mucho 3 abiertas por instancia.
2. **Aprobar** (pagina, `POST /flow-approval/k/{id}/publish`): una persona de `aprobadores_slack`. El bridge rehace la version desde la fila, recalcula el hash y las reglas, y revisa que lo vigente sea aquello sobre lo que se armo y que no haya un segmento en vigencia (abajo). La base exige la huella, que no haya vencido y que la base de la propuesta siga siendo la vigente; retira la viva anterior. Una sola viva por instancia.
3. **Descartar** (pagina): solo una validada.
4. **Volver al archivo** (pagina, `POST /flow-approval/k/{id}/return-to-file`): retira la viva. En la proxima lectura el agente vuelve al archivo del manifiesto.

## El servido

`LiveKnowledgeSource` lee `get_live_agent_knowledge_v1` cada 30 segundos (y al aprobar o volver, en el momento). Lo que el turno recibe:

- la version viva, si hay una publicada y se puede servir;
- si la base no contesta, lo ultimo bueno; si nunca leyo, el archivo;
- el archivo, si no hay viva o si la viva no se puede servir. No se sirve (y lo dice `/ready` y la pagina) si el archivo del manifiesto cambio despues de aprobarla (`archivo_cambio`: el redeploy de un archivo nuevo manda sobre la viva), si el render de hoy no da el hash aprobado (`render_no_calza`), si ya no pasa las reglas (`contenido_invalido`) o mientras haya un segmento en vigencia (`segmentos_vigentes`).

El render es el del cargador: determinista. La procedencia del turno (`agent_turn_provenance.context_added.knowledge`) dice cual uso: `{"origen": "archivo", "version": 15, "sha256": "..."}` o `{"origen": "vivo", "knowledge_id": "...", "live_version": 2, "version": 15, "sha256": "..."}`. Sin el flag la clave no aparece.

## Los segmentos (D-55.4: nunca a la vez)

El `chequeo_sha` de un segmento (`segment_check`) se calcula contra el hash del conocimiento base. Un segmento firmado contra el archivo no se revisó contra una version viva. Por eso:

- **Aprobar se bloquea** (`knowledge_segments_in_force`) mientras haya un segmento del manifiesto vigente o por empezar, o una campana del AE servida.
- Si despues de aprobar aparece uno (se publica una campana, empieza un segmento), **la viva deja de servirse mientras dure** y el agente recibe el archivo, contra el que se firmo el segmento. Cuando no queda ninguno, vuelve sola. La pagina lo dice.

Asi el agente nunca recibe una combinacion de conocimiento y segmento que no se haya revisado junta. Lo urgente durante una campana lo cubre la instruccion temporal (5.3).

## Rutas del AE

Con la clave del plugin de cambios y el JWT de `automation_expert_change`:

| Ruta | Que hace |
|---|---|
| `GET .../knowledge` | Lo vigente: origen, version, hash, las secciones editables como JSON y el tope. |
| `POST .../knowledge/preview` | `{"cambios"}`: valida y devuelve bloqueos, avisos y el diff, sin guardar. |
| `POST .../knowledge` | `{"cambios", "purpose", "requested_by"}`: valida y guarda. 422 `knowledge_change_blocked` con los bloqueos. |
| `GET .../knowledge/versions?estado=abiertas\|todas` | Las versiones con su estado. |
| `GET .../knowledge/versions/{id}` | Una version, con el link de la pagina. |

## Lo que no hace la v1

- No corre el arnes F-055 ni el juez (D-55.8): queda para cuando el juez corra por el AE (4.9).
- No escribe el repo de la instancia: la version viva no se vuelve archivo sola. Pasarla al repo es un PR como hoy.
- No avisa por Slack al aprobar.
- La pagina no vuelve sola despues de entrar con Slack: hay que abrir el link otra vez (D-55.9).
