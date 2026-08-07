# División durable de respuestas salientes

- **Estado:** Implementado y validado localmente; feature flag apagado, pendiente de despliegue y E2E real
- **Alcance:** respuesta textual de Hermes → 1–4 burbujas públicas ordenadas en la misma conversación Chatwoot
- **Fuera de alcance:** multimedia, typing indicators, templates WABA y cambios al razonamiento comercial

## Objetivo

Convertir una respuesta lógica ya aprobada del agente comercial en una secuencia breve y natural de mensajes de WhatsApp sin permitir que el divisor cambie su significado, invente contenido ni tome decisiones comerciales.

> **Bloqueo resuelto localmente:** el lote ya no se identifica por el delivery del
> webhook. Se identifica por conversación + trigger canónico y se persiste como un
> manifiesto inmutable antes del primer POST. El feature continúa apagado hasta
> completar revisión, despliegue controlado y E2E real.

```text
respuesta validada del agente
→ divisor de formato con prompt fijo
→ JSON estricto
→ validación determinista de preservación
→ lote durable
→ envío secuencial reautorizado
```

## Frontera de responsabilidad

El agente comercial sigue siendo el único autor del contenido y la estrategia. El divisor sólo propone fronteras entre partes. El bridge conserva autoridad sobre:

- esquema y número de partes;
- preservación del texto original;
- identidad e idempotencia de cada parte;
- orden y delay;
- reautorización antes de cada POST;
- reconciliación de respuestas HTTP ambiguas;
- detención ante avance de conversación o intervención humana.

## Contrato propuesto del divisor

Entrada:

```json
{
  "reply": "texto validado del agente",
  "max_parts": 4
}
```

Salida:

```json
{
  "parts": ["primera parte", "segunda parte"]
}
```

Reglas:

- objeto JSON directo o fence Markdown completo ya soportado por el parser estricto;
- exactamente una clave `parts`;
- entre 1 y 4 strings no vacíos;
- ningún string puede superar el límite del canal;
- al normalizar únicamente el espacio de frontera, la concatenación debe ser idéntica al reply original;
- no puede agregar, eliminar, traducir, resumir, corregir ni reordenar palabras o puntuación;
- respuestas cortas pueden permanecer en una parte;
- el nombre del modelo será configurable para permitir un modelo menor sin cambiar el bridge.
- la llamada reutiliza el API server Hermes y envía `provider` y `model`
  explícitos; según el contrato oficial, esa forma no depende de
  `direct_model_requests` ni agrega una credencial del proveedor al bridge.

## Decisiones aprobadas

1. El resultado admite entre **1 y 4 partes**; una respuesta corta queda en una sola.
2. El delay inicial es **2 segundos fijos** entre partes y será configurable por entorno.

## Fallback aprobado

Si la llamada al modelo falla, devuelve JSON inválido o altera el texto, se
persiste un manifiesto fallback con la respuesta original validada como una sola
parte y recién entonces se autoriza el envío. Esto preserva el comportamiento
productivo actual sin omitir la barrera durable. Si el manifiesto no puede
persistirse o uno existente es inseguro, corrupto o incompatible, el trabajo
falla cerrado y no se publica ninguna geometría nueva.

## Ejecución durable propuesta

Cada respuesta lógica crea un lote estable derivado de la conversación y el trigger canónico. Cada parte tiene identidad propia:

```text
reply_batch_hash = hash(conversation_id + trigger_message_id)
part_key         = hash(reply_batch_hash + part_index + total_parts)
```

La división validada se persiste antes del primer envío en un manifiesto versión
1 que incluye `batch_hash`, hash de la respuesta lógica, cantidad total y, para
cada parte, índice, contenido, hash de contenido e identidad de parte. En replay
se reutiliza exactamente el mismo lote; nunca se vuelve a dividir una respuesta
parcialmente enviada. Si el mismo trigger intenta presentar otra respuesta lógica,
el conflicto falla cerrado y no autoriza una geometría nueva.
Antes del JSON se persiste y sincroniza una claim hash-only independiente en el
directorio padre. Si la claim existe pero el manifiesto desapareció, el replay
falla cerrado sin consultar al divisor: no puede recalcular otra geometría que
eluda journals o markers de un envío parcial.
La frontera de aplicación materializa atómicamente el manifiesto después de
validar cualquier implementación de `ReplySplitter`, incluida una implementación
inyectada o su fallback por excepción. Ninguna ruta habilita POST sólo con partes
en memoria.

El feature flag controla únicamente la creación de lotes nuevos. Antes de aplicar
su valor, el bridge siempre busca un manifiesto semántico existente. Por eso un
restart o rollback con el flag apagado continúa y reconcilia el lote ya iniciado
con las mismas partes e identidades.
En la dirección inversa, un journal legacy de una respuesta única bloquea la
creación de journals multipart hasta reconciliar su marker; esto evita que una
activación del flag saltee un POST anterior cuyo resultado todavía es incierto.

Para cada parte, en orden:

1. esperar el delay configurado antes de cada parte posterior a la primera;
2. releer conversación, labels y mensajes canónicos;
3. aceptar únicamente las partes anteriores del mismo lote entre trigger y parte actual;
4. detenerse ante inbound nuevo, intervención humana, pausa o mensaje ajeno;
5. reconciliar el marcador remoto de la parte antes de repetir POST;
6. enviar con marker de lote, índice y total;
7. validar ID, conversación, actor, contenido y marker retornados;
8. aceptar sólo una respuesta canónica validada antes de avanzar.

Una pérdida de response después de un POST nunca autoriza un retry ciego. El sender busca primero el marker exacto de esa parte.
En replay se reutiliza la división persistida y se vuelve a esperar antes de una
parte pendiente; un crash puede alargar la separación, pero nunca acortarla ni
duplicar una parte ya reconciliada.

## Cancelación y avance de conversación

Una admisión entrante posterior al punto de corte forma un turno nuevo. Si aparece antes de una parte pendiente, las partes restantes del lote anterior se cancelan. Una intervención pública humana o `automation_paused` también cancela las partes no enviadas.

Las partes ya aceptadas por Chatwoot no se eliminan ni se ocultan; se conserva evidencia del resultado parcial.
El trabajo del turno termina normalmente cuando una guarda bloquea partes
restantes; los markers remotos conservan la evidencia de las partes ya aceptadas.

## Configuración propuesta

```text
CHATWOOT_REPLY_SPLITTER_ENABLED=false
HERMES_REPLY_SPLITTER_MODEL_NAME=<configurable>
HERMES_REPLY_SPLITTER_PROVIDER=<configurable>
CHATWOOT_REPLY_PART_DELAY_SECONDS=2
```

Los tiempos deben ser finitos y no negativos. El máximo inicial se fija en 4 en
el contrato, no como configuración mutable. El feature flag permite desplegar el
código sin activarlo.

## Criterios de aceptación

- una respuesta corta produce una parte y conserva el sender actual;
- una respuesta larga válida produce 2–4 partes en orden;
- el texto normalizado completo coincide con la respuesta original;
- cada parte tiene marker e idempotencia independiente;
- replay después de parte 1 no duplica esa parte y continúa con la siguiente;
- response HTTP perdido se reconcilia antes de repetir;
- un inbound entre partes bloquea las restantes;
- una intervención humana entre partes bloquea las restantes;
- un divisor inválido aplica el fallback aprobado;
- exactamente dos segundos separan partes nuevas en el camino normal con reloj controlado;
- reinicio conserva división y orden, y vuelve a aplicar el delay antes de una
  parte pendiente;
- logs, nombres de archivo y estados no exponen contenido, JID ni delivery ID.

## Implementación local

- preservación permitida: coincidencia exacta de cada parte contra el original;
  sólo se consume whitespace que ya estaba situado entre dos fronteras;
- cache privado `0600` de la división bajo `REPLY_DIR/.splits`, con directorio,
  locks y archivos validados por tipo y owner mediante `O_NOFOLLOW`;
- manifiesto inmutable versión 1 identificado por el mismo hash semántico
  `conversation_id + trigger_message_id` que usa el sender;
- cada manifiesto persiste total, partes ordenadas, hashes de contenido e
  identidades exactas compatibles con los markers y journals del sender;
- un reply distinto para el mismo lote produce `reply_split_manifest_conflict`
  fail-closed, sin recalcular ni enviar;
- fallback de una parte persistido para no insistir con el modelo en replay;
- almacenamiento inseguro, corrupto, incompatible o inaccesible falla cerrado y
  nunca autoriza una parte sin manifiesto ni recalcula un lote potencialmente parcial;
- markers por parte y lock compartido por lote;
- historial canónico paginado hasta 2000 mensajes con trigger requerido;
- journal hash-only `posting` persistido y sincronizado antes de cada POST;
- un journal existente autoriza sólo reconciliación, nunca otro POST, y mantiene
  el trabajo admitido sin límite hasta que el marker sea visible;
- agotamiento de 100 páginas sin frontera o 2000 mensajes únicos es error
  fail-closed aunque el trigger haya aparecido;
- bloqueo de restantes ante avance de conversación;
- feature flag apagado por defecto.

La suite local completa quedó verde con **374 pruebas**. Esta evidencia no
equivale a despliegue ni a entrega real en WhatsApp.

### Verificación HTTP local

Un servidor ASGI real recibió un webhook firmado y produjo evidencia sanitizada:

```json
{
  "http_status": 202,
  "ack_under_one_second": true,
  "splitter_calls": 1,
  "manifest_count": 1,
  "manifest_seen_before_post": true,
  "reply_count": 2,
  "part_indices": [1, 2],
  "part_counts": [2, 2],
  "delays": [2]
}
```

La verificación final se repitió con un `ReplySplitter` inyectado: la frontera de
aplicación materializó el manifiesto antes de ambos POST simulados y confirmó
`part_indices=[1,2]`. Después de agregar la claim independiente, el HTTP real se
repitió y confirmó claim + manifiesto presentes antes de ambos POST simulados.

Esta verificación demuestra el pipeline local y no constituye evidencia de
despliegue ni de entrega real en WhatsApp.
