# Primer nombre del lead en la primera plantilla — contrato v1

## Para qué

La variable `{{1}}` de toda plantilla de primer contacto de Johanna lleva el
nombre con el que se saluda al lead, no el nombre completo que escribió en el
formulario. Aplica a los cuatro caminos que hoy leen
`precheckout_submissions.canonical_payload #>> '{lead,full_name}'`:
precheckout diferido (`johanna_interes_precheckout_01`), carrito abandonado
(`johanna_carrito_abandonado_01`), pago rechazado (`johanna_compra_fallida_01`)
y el primer toque de prueba del precheckout.

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

Una vez por nombre, después de responder el `POST /webhooks/lead` que admitió
un formulario nuevo (`outcome = inserted`), como tarea en segundo plano. El
envío nunca espera al modelo: si la inferencia no llegó, cae al nivel
`deterministic`.

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

El bridge acepta `first_name` solo si son de 1 a 3 palabras **seguidas** del
nombre original (sin distinguir mayúsculas), todas letras, y hasta 80
caracteres. Las palabras se toman del original y se les arreglan las
mayúsculas. Cualquier otra respuesta (otra clave, otro nombre, texto libre) se
guarda como `uncertain`. Un error de transporte o un HTTP distinto de 200 **no**
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
| `LEAD_FIRST_NAME_INFERENCE_ENABLED` | `false` | llama al modelo al admitir el formulario. Exige la anterior, Hermes y Supabase |
| `LEAD_FIRST_NAME_MODEL_NAME` | `HERMES_MODEL_NAME` | modelo del API server de Hermes |

## Observabilidad

Los logs llevan solo el nivel usado (`source=inferred|deterministic|full_name`)
y el reason code de la inferencia. Nunca el nombre.
