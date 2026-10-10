# Primer nombre del lead en la primera plantilla — contrato v1

## Para qué

La variable `{{1}}` de toda plantilla de primer contacto de Johanna lleva el
nombre con el que se saluda al lead, no el nombre completo que escribió en el
formulario. Aplica a los cuatro caminos que hoy leen
`precheckout_submissions.canonical_payload #>> '{lead,full_name}'`:
precheckout diferido (`johanna_interes_precheckout_01`), carrito abandonado
(`johanna_carrito_abandonado_01`), pago rechazado (`johanna_compra_fallida_01`)
y el primer toque de prueba del precheckout.

En una instancia con manifiesto, la variable `nombre` de las plantillas del
primer contacto, del carrito y del pago fallido usa la misma cadena en el modo
directo del dispatcher desde la 1.1.0, con `LEAD_FIRST_NAME_GREETING_ENABLED`
([approved-template-direct-dispatch-v1](approved-template-direct-dispatch-v1.md)).
Desde la 1.5.0 la instancia también puede llenar el primer nivel, con
`LEAD_FIRST_NAME_INFERENCE_ENABLED`. Ahí el envío busca la inferencia por el nombre del contacto
(`contacts.full_name`), que es el texto del formulario cuando el contacto lo
creó la planificación del primer contacto. Si el contacto nació con otro texto
(el nombre del comprador de Hotmart, un contacto anterior, o un formulario cuyo
primer contacto no se planificó), la clave no coincide y el saludo cae al nivel
`deterministic`, como sin inferencia.

## La cadena de tres niveles

| Nivel | Fuente | Cuándo se usa |
|---|---|---|
| `inferred` | fila `confident` en `lead_first_name_inferences` | el modelo respondió que está seguro |
| `deterministic` | `reactivation_first_name(full_name)` | no hay fila, la fila es `uncertain` o la lectura falló |
| `full_name` | el nombre tal cual, sin espacios de borde | la regla determinística no da nada usable (emoji, una letra) |

La cadena nunca levanta y nunca devuelve vacío: la variable de Meta no puede
ir vacía. El contacto de Chatwoot se sigue creando con el nombre completo; solo
cambia la variable de la plantilla y el `content` que se registra.

## Cuándo corre el modelo

Una vez por nombre, después de responder el `POST /webhooks/lead` o el
`POST /webhooks/adapters/ghl/lead-precheckout` que admitió un formulario nuevo
(`outcome = inserted`), como tarea en segundo plano. El envío nunca espera al
modelo: si la inferencia no llegó, cae al nivel `deterministic`.

El modelo es el profile de `LEAD_FIRST_NAME_MODEL_NAME` (por defecto
`HERMES_MODEL_NAME`, el del agente) por el API server de Hermes de la
instalación. Hermes le apila el SOUL del profile antes del prompt, así que cada
inferencia cuesta del orden de un turno del agente. Medido con el profile de
ATT1 (`z-ai/glm-5.3-flash` y el SOUL común): ~4.800 tokens de entrada y
USD 0,0006 por nombre, y el SOUL no saca al modelo del contrato
([evidencia del 2026-10-10](../operations/2026-10-10-nombre-de-pila-con-el-profile-de-att1.md)).

Ese api_server es el mismo de los turnos del agente, con un tope de corridas en
vuelo que contesta 429 al pasarlo. El cliente manda de a dos
(`MAX_CONCURRENT_INFERENCES`): una ráfaga de formularios espera en el bridge y
no le quita lugares al agente.

Con el flag, el nombre de todo formulario nuevo admitido sale al proveedor del
modelo del profile, tenga o no permiso de WhatsApp. En una instancia el saludo
casi nunca usa la inferencia de quien no dio permiso, porque el contacto con el
texto del formulario lo crea la planificación del primer contacto, que lo exige.
En ATT1 la diferencia es mínima: el adaptador de GHL marca el permiso en todo
formulario con un teléfono válido.

Entrada única al modelo, como mensaje `user`:

```json
{"full_name": "Andres Felipe Pérez García"}
```

Criterio del prompt (`PROMPT_VERSION = lead-first-name-v2`):

- Por defecto, **un solo nombre de pila**, el primero: la mayoría tiene dos y usa uno.
- Dos palabras solo si forman un compuesto que se usa siempre junto (*Juan Pablo*, *María José*); ante la duda, uno.
- Si la primera palabra no es un nombre por sí sola (*Santa Lucía*) va el compuesto; si es un apellido y el nombre viene después, va el nombre.
- Nunca un apellido.
- Los ejemplos del prompt no son nombres de la muestra con la que se mide (lo verifica `tests/test_lead_first_name.py`).

Cambiar el criterio es cambiar `PROMPT_VERSION`: eso cambia la `Idempotency-Key` y Hermes no replica respuestas de la versión anterior. Las filas ya guardadas no se recalculan.

Salida aceptada, sin texto alrededor (se toleran cercos de código):

```json
{"result": "confident", "first_name": "Andres Felipe"}
{"result": "uncertain", "first_name": null}
```

El bridge acepta `first_name` solo si:

- son palabras **seguidas** del nombre original (sin distinguir mayúsculas), todas letras;
- son de 1 a 3 palabras, contando las partículas (`de`, `del`, `la`, `las`, `los`): «María de los Ángeles» queda `uncertain` y saluda la regla, hasta medir esos compuestos con el modelo;
- no terminan con una partícula, y empiezan con una solo si el nombre original empieza así («Del Carmen López»): «Juan de» o «de Dios» son un compuesto cortado;
- tienen al menos 2 caracteres y pasan `template_greeting_name_is_safe`, el filtro del modo directo: hasta 60 caracteres y sin caracteres numéricos como «²». Un nombre que el filtro rechaza dejaría a la persona sin plantilla, donde la regla determinística sí saluda. La tabla acepta hasta 80.

Las palabras se toman del original y se les arreglan las mayúsculas; las
partículas que no van al principio quedan en minúscula: «María del Carmen»,
«José de Jesús». Cualquier otra respuesta (otra clave, otro nombre, texto libre)
se guarda como `uncertain`. Estas reglas descartan las formas medidas que
saludan peor que la regla; no garantizan el saludo correcto (un apellido suelto,
por ejemplo, pasa si el modelo lo devuelve). Un error de transporte o un HTTP distinto de 200 **no**
se guarda: el próximo formulario con ese nombre vuelve a intentar.

## Almacenamiento

`public.lead_first_name_inferences`, migración `20260928000300`:

- `name_key`: sha256 en hex del nombre con los espacios colapsados y
  `casefold()`. El nombre completo no se copia.
- `result` (`confident` | `uncertain`), `first_name`, `model_name`,
  `prompt_version`, `created_at`.
- La primera respuesta manda: `record_lead_first_name_inference_v1` inserta con
  `on conflict do nothing` y devuelve `inserted` o `existing`.
- `get_lead_first_name_inference_v1(name_key)` devuelve cero o una fila.
- Las dos RPC son `security definer` y solo las ejecuta `service_role`; nadie
  lee la tabla directo.

## Flags

| Variable | Default | Efecto |
|---|---|---|
| `LEAD_FIRST_NAME_GREETING_ENABLED` | `false` | aplica la cadena al enviar. Apagada, el envío es idéntico al anterior |
| `LEAD_FIRST_NAME_INFERENCE_ENABLED` | `false` | llama al modelo al admitir el formulario. Exige la anterior, Hermes y Supabase. Con manifiesto se admite desde la 1.5.0; antes el bridge no arrancaba |
| `LEAD_FIRST_NAME_MODEL_NAME` | `HERMES_MODEL_NAME` | modelo del API server de Hermes |

Vuelta atrás en una instancia con manifiesto (Johanna no corre la compuerta de
portabilidad y no lo necesita):

- Para volver a una imagen anterior a la 1.5.0, primero hay que sacar el flag:
  esa imagen, con el flag prendido, no arranca.
- Apagar la inferencia no devuelve el saludo a la regla: el saludo lee las filas
  guardadas en cada envío. Para eso hay que borrar en la base de la instancia las
  filas creadas desde la activación (no hay RPC: psql, por `created_at`).
- Con la inferencia prendida, el bridge exige una entrada de formularios
  (`LEAD_PRECHECKOUT_ENABLED` o `GHL_PRECHECKOUT_ADAPTER_ENABLED`). Cortar el
  adaptador de GHL obliga a sacar en el mismo redeploy los flags del adaptador,
  del primer contacto y de la inferencia.

## Observabilidad

Los logs llevan solo el nivel usado (`source=inferred|deterministic|full_name`)
y el reason code de la inferencia. Nunca el nombre.

Las líneas de éxito (`lead_first_name_inference_recorded`, `first_touch_greeting`
y `durable_first_touch_greeting`) son INFO, y el bridge hoy no publica INFO: no
configura logging, así que en el log del contenedor solo salen los WARNING de
falla. La inferencia se verifica por presencia, con las filas de
`lead_first_name_inferences` y el `{{1}}` de las plantillas enviadas.
