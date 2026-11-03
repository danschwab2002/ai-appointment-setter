# La etiqueta de un segmento antes del primer turno del agente

- **Estado:** escrito al construir E-316 (PR-C de los segmentos), el 2026-11-03, antes de cualquier merge o despliegue. Describe la interfaz; no dice que nada esté desplegado ni activado. Todo va detrás de dos flags apagados por defecto.
- **Contrato:** `segment-labels-v1`, versión `1.0.0`.
- **Diseño:** `documentacion/2026-11-03-e310-diseno-segmentos-conocimiento-por-etiqueta.md` (E-310, fuera del repo), §2.7 y la fila PR-C del plan.
- **Depende de:** `segment-knowledge-v1` (PR-A, E-311): la declaración `[[agente.segmentos]]` del manifiesto (con `macro_etiqueta`) y la vigencia de cada segmento. Mientras PR-A no esté en `main`, este contrato la lee por atributo y sin PR-A no hay segmentos (ver «La interfaz con PR-A»).
- **Toca:** `flow-definition-v1` (1.5), `flow-engine-v2` (`2.16.0`, sección 28), `flow-change-cycle-v1` (dos códigos) y `automation-expert-read-v1` (`1.5.0`).

## Para qué

Un segmento le suma al agente un conocimiento (por ejemplo, la campaña de Black November de ATT1) mientras la conversación de Chatwoot tiene su etiqueta: el bridge la lee de `conversation.labels` del webhook en cada turno (PR-A). Dan, el 2026-11-03: *«un inicio de conversación sin esa etiqueta puede ensuciar mucho la memoria del agente, por más de que luego se le sume la etiqueta»*. Que el equipo la ponga a mano después no alcanza: la etiqueta tiene que estar **desde el primer turno del agente**.

Este contrato la pone antes por dos caminos, y deja diseñado un tercero.

## En corto

| Camino | Flag | Cuándo corre | Llamadas nuevas |
|---|---|---|---|
| El motor nativo la pone antes del primer envío | `FLOW_ENGINE_SEGMENT_LABELS_ENABLED` | en cada envío de un flujo nativo cuya versión fijada tiene `etiquetar: <segmento>` | la cola del paso (`flow_engine_native_step_tail_v1`) y el macro, solo en esos envíos |
| El bridge la hereda de otra conversación del contacto | `SEGMENT_LABEL_INHERITANCE_ENABLED` | la primera vez que el proceso ve una conversación sin ninguna etiqueta de segmento | una lectura de `GET /contacts/{id}/conversations`, una vez por conversación |
| La palabra clave del primer mensaje | (sin construir) | — | — |

## 1. El motor nativo pone la etiqueta antes del primer envío

### El paso

El paso `etiquetar` de un flujo nativo acepta, además de `cliente`, la etiqueta de cada `[[agente.segmentos]]` **con `macro_etiqueta`** (sin macro nadie la puede poner). Con cualquier disparador (`carrito`, `pago_fallido`, `intencion`, `compra`).

```yaml
dispara: {evento: carrito}
entra: {permiso: formulario}
pasos:
  - etiquetar: black-november     # la pone el motor antes de la plantilla
  - plantilla: att1_bn_01
termina: {si: [compra], vence: 1d}
```

- **Dónde va:** arriba de todo (fuera de las condiciones) y antes de la plantilla; una sola etiqueta por camino. Si no, `etiquetar_segment_not_before_template` (regla 7, forma). Al revés que `cliente`, que va después de la plantilla (`etiquetar_not_after_template`).
- **La vigencia:** si el segmento ya venció, `segment_label_outside_window` (regla 5, protección). Un segmento por empezar se puede publicar (para preparar la campaña), pero el motor no pone su etiqueta hasta que empiece (D-E316-5).
- **La regla 5 de la compra** (`etiquetar_requires_purchase_trigger`) queda solo para `cliente`.
- **Sin el flag**, el validador acepta solo `cliente`, como antes: una etiqueta de segmento es `invalid_value` (regla 1).
- Los códigos van en la 5 y la 7 porque la base acepta reglas de 1 a 17.

El validador no mira el reloj: quien arma la vista (`InstanceView.segment_labels`, pares `(etiqueta, estado)`) calcula el estado al validar. El estado sale de la vigencia del archivo del segmento (PR-A): `por_empezar`, `vigente`, `vencido`, o `sin_vigencia` si el bridge no tiene el archivo cargado (`AGENT_SEGMENTS_ENABLED` apagado). Las tres entradas que validan (`propose` y `validate` de la API de cambios, y la revalidación al publicar desde la página de aprobación) le pasan las etiquetas; la entrada del operador (`flow_change_operator`) acepta `instance.segment_labels: [{etiqueta, estado}]` opcional.

### La base (`20261103001300_flow_engine_native_segment_label_v1.sql`)

Un solo reemplazo contado en `_flow_engine_native_unsupported` (la definición vigente). Con un disparador que no es la compra, `etiquetar` sigue siendo `motor_nativo:forma` solo si la etiqueta es `cliente`, de la automatización (`automation_*`) o no es un texto. Que sea de un segmento declarado lo mira el validador: la base no tiene el manifiesto. Nada más cambia: el compilador ya no ve `etiquetar` (la admisión nativa le saca esos pasos antes de compilar, `20261024000200`) y la cola del paso ya devuelve el primer `etiquetar` de la versión fijada.

### El envío

Con el flag, para la acción de un recorrido nativo, el despachador lee la cola del paso antes del envío. Si su `etiquetar` es la etiqueta de un segmento con macro:

1. **Antes de la plantilla**, en la conversación donde va a salir:
   - un primer contacto: el remitente (`send_first_touch(before_send=…)`) la pone con la conversación ya elegida o creada, justo antes de `send_first_message`;
   - un seguimiento: con la conversación del seguimiento, antes de `send_followup`.
2. Con el envío aceptado, si antes no salió (`failed`, `disabled` o no corrió), **se reintenta una vez**.

La etiqueta se pone con el macro del segmento, que solo la suma (`ProductLabeler`: se lee el macro antes de la primera vez y, si hace algo más, queda apagado hasta el próximo arranque). Es idempotente: si la conversación ya la tiene, no se ejecuta. El macro lo aplica Chatwoot en un job, en el orden de segundos; la persona contesta después.

**Falla blanda (D-E316-3):** una etiqueta que no sale no frena el envío. Una cola del paso que no responde tampoco: el envío sale sin etiqueta. Un segmento por empezar o vencido no se pone y el envío sale. La herencia (sección 2) no cubre este caso: si las dos veces falló, la conversación queda sin la etiqueta y se ve en el log.

La etiqueta de un segmento **no** deja nota privada (la de `cliente` sí): el registro es el log y la etiqueta misma (D-E316-4).

### Logs

`flow_engine_native_segment_label when=before_send|after_send action_id conversation segment label outcome`, `flow_engine_native_segment_label_skipped … reason=por_empezar|vencido`, `flow_engine_native_segment_label_unavailable … error`, `first_touch_before_send_failed conversation` y los de `product_label` del macro. Ninguno lleva teléfono, mail ni nombre.

## 2. La etiqueta del contacto: la herencia

El webhook trae solo `conversation.labels`. Si la persona abre una conversación nueva (o el remitente crea otra), la etiqueta de la vieja no pasa sola.

Con `SEGMENT_LABEL_INHERITANCE_ENABLED`, en el turno del agente, antes de que se lean las etiquetas del turno (`customer_status` y los segmentos de PR-A):

- **La conversación ya tiene una etiqueta de segmento** en el webhook: nada, sin llamadas.
- **El proceso ya la miró:** se suma lo que heredó, sin llamadas. Lo heredado se recuerda por conversación (hasta 4.096, de la menos usada a la más), así que mientras el macro no se aplicó el turno siguiente igual lleva la etiqueta.
- **Si no, una llamada:** `list_contact_conversations` (el contacto del webhook, `conversation.meta.sender` de tipo `contact`, en el inbox de la instancia). Las etiquetas de segmento que tengan las otras conversaciones y que no vencieron se le ponen a esta por el macro y se suman a `conversation.labels` del turno. Sin macro, se suman al turno igual. Una lectura que falla no se recuerda: el turno siguiente vuelve a intentar.

**«El primer turno» (D-E316-6):** el bridge no sabe, antes de leer la historia, si la conversación tuvo turnos del agente. La regla es «la primera vez que este proceso ve la conversación sin una etiqueta de segmento»: una llamada por conversación y nunca por turno. Después de un reinicio, una conversación vieja sin etiqueta paga una lectura en su primer turno. Si hereda la etiqueta en un turno que no es el primero, PR-A lo anota (`segment_after_agent_replies` y `agent_replies_before` en la procedencia).

Logs: `segment_label_inherited conversation contact segment label from outcome`, `segment_label_inheritance outcome=nada|sin_contacto|lectura_fallida`.

## 3. La palabra clave del primer mensaje (diseñado, sin construir)

Un segmento podría declarar frases de entrada (la palabra de un anuncio, «BLACK») y el bridge ponerle la etiqueta antes del primer turno si el primer mensaje de la persona la contiene. No entra en este PR (D-E316-7) porque:

- la declaración vive en `[[agente.segmentos]]` (PR-A, esquema cerrado): sumar `frases_entrada` es tocar el parser y el contrato de PR-A, que todavía no está en `main`;
- «el primer mensaje» pide saber que es el primero, y eso hoy solo lo da la historia (la lectura de `run_shadow_with_canonical_history`), que corre después de armar el contexto.

Lo que haría, sobre este contrato: `frases_entrada = ["black", "black november"]` (normalizadas sin acentos ni mayúsculas, palabra entera, hasta 8 por segmento); en el mismo punto que la herencia, si la conversación no tiene etiqueta de segmento y no está en la memoria, y el texto del entrante contiene una frase, se pone la etiqueta por el macro y se suma al turno, con el log `segment_label_by_keyword`. Mientras tanto, una regla de automatización de Chatwoot por palabra clave hace lo mismo (D10 del diseño).

## 4. Lo que ve el Automation Expert (`automation-expert-read-v1` 1.5.0)

Con `FLOW_ENGINE_SEGMENT_LABELS_ENABLED`, `list_flows` (y `list_flows(flow_key)`) suma a `data`:

- `segments`: cada segmento con `key`, `label`, `labelable` (si tiene macro), `state` (ahora), `valid_from`, `valid_until` y `labeled_by` (las claves de los flujos cuya versión activa pone su etiqueta, de las definiciones que ya trae la lectura);
- `allowed_labels`: lo que acepta `etiquetar` en la instancia (`cliente` y las de los segmentos con macro).

Apagado, la respuesta es la de siempre. La página `chatwoot-whatsapp` de la biblioteca dice cómo se escribe el paso. El SOUL no cambia (está al tope).

## 5. La interfaz con PR-A

`segment_labels.declared_segment_labels(manifest, catalog)` lee:

- `manifest.agent_segments`: los `AgentSegment` de PR-A (`key`, `label`, `label_macro_id`);
- `catalog.segments`: los `LoadedSegment` de PR-A (`key`, `knowledge.valid_from`, `knowledge.valid_until`), de `settings.agent_segment_catalog`.

Sin el atributo (un `main` sin PR-A, o sin manifiesto) no hay segmentos. Con los flags prendidos y sin segmentos, el bridge no arranca (`… requires [[agente.segmentos]] in the manifest`). La herencia pide además `AGENT_SEGMENTS_ENABLED` (PR-A): heredar una etiqueta que el agente no usa no sirve.

**Al mergear los dos:** cambiar los `getattr` por el acceso tipado (`InstanceManifest.agent_segments`, `Settings.agent_segment_catalog`) y borrar el doble de las pruebas.

## 6. El arranque (falla cerrado)

`FLOW_ENGINE_SEGMENT_LABELS_ENABLED` pide manifiesto, el cliente de control de Chatwoot, `FLOW_ENGINE_ENABLED` y al menos un segmento con `macro_etiqueta`. `SEGMENT_LABEL_INHERITANCE_ENABLED` pide manifiesto, el cliente de control, `CHATWOOT_INBOX_ID`, al menos un segmento y `AGENT_SEGMENTS_ENABLED`. El macro de un segmento no puede ser el de la pausa, la reanudación, la baja ni el de `cliente`. Los dos flags son capacidades portables (por instancia, solo prenderlos).

## 7. Cómo se prende en ATT1 (después de PR-A y de este PR)

1. En Chatwoot (cuenta 2), la etiqueta del segmento y un macro que solo la suma; su id va en `macro_etiqueta` del segmento (PR-I1).
2. La migración `20261103001300` aplicada y la imagen desplegada.
3. `FLOW_ENGINE_SEGMENT_LABELS_ENABLED=true` (y, si se quiere la herencia, `SEGMENT_LABEL_INHERITANCE_ENABLED=true`, con `AGENT_SEGMENTS_ENABLED=true`).
4. El AE propone el flujo (por ejemplo, el carrito de la oferta de Black November con `etiquetar` primero), arranca en cohorte y se prueba con el teléfono de un aprobador: en el log, `flow_engine_native_segment_label when=before_send … outcome=added` antes del envío, y en el primer turno de la respuesta, `segment_context key=black-november` (PR-A) con `agent_replies_before=0`.

## 8. Decisiones (con su default)

- **D-E316-1.** La etiqueta de un segmento va antes de la plantilla en la definición (arriba de todo), porque es lo que hace el motor. Un `etiquetar` de segmento después del envío es un error de forma, no se corre solo.
- **D-E316-2.** Una sola etiqueta por camino, para `cliente` y para un segmento (la cola del paso devuelve un solo `etiquetar`).
- **D-E316-3.** Falla blanda: sin etiqueta, la plantilla sale igual y se reintenta una vez después del envío. Perder el envío por una etiqueta es peor.
- **D-E316-4.** Sin nota privada para un segmento.
- **D-E316-5.** Se publica un flujo con un segmento por empezar; vencido, no. El motor la pone solo mientras el segmento está vigente (o sin vigencia conocida).
- **D-E316-6.** «Primer turno» = la primera vez que el proceso ve la conversación sin etiqueta de segmento.
- **D-E316-7.** La palabra clave queda diseñada, sin construir.
- **D-E316-8.** Un segmento sin `macro_etiqueta` no se puede poner con `etiquetar` ni se hereda por macro (sí se suma al turno).
- **D-E316-9.** Una conversación que ya tiene una etiqueta de segmento no hereda otras.

## 9. Las pruebas

- `tests/test_segment_labels.py`: la declaración por atributo y sin PR-A, la vigencia en los bordes, el validador (el carrito con un segmento, `cliente` con el carrito, la condición, la entrada del operador, la vista que rechaza una etiqueta repetida o reservada), el despacho nativo con los dobles de `test_flow_engine_native_dispatch.py` (la etiqueta antes de la plantilla en la misma conversación, la falla reintentada después, fuera de la vigencia, la cola que no responde, sin el flag), el remitente con un `before_send` que lanza, la herencia contra la lista capturada del contacto 767 de Maribel y el webhook capturado de la 536 (hereda, recuerda, no lee con la etiqueta, vencido, la lectura que falla, sin macro), el armado y lo que ve el AE.
- `tests/test_segment_labels_wiring.py`: el turno sigue con el payload de la herencia, sin los flags nada cambia, los flags portables y apagados, el arranque sin manifiesto o sin segmentos, y `/flows` con y sin el flag.
- `tests/test_flow_validator.py`: los dos códigos con su caso negativo y su vecino positivo (`validator_results_cases.json` regenerado).
- `tests/sql/followup_engine/validate_flow_engine_native_segment_label.mjs` (PGlite, la cadena entera antes y después) y `tests/test_flow_engine_native_segment_label_migration.py`.
- **Deuda:** no hay captura de un webhook con una etiqueta de campaña puesta por un macro, ni de un contacto con una etiqueta de segmento en otra conversación: la etiqueta `black-november` es un ejemplo sobre las capturas.
