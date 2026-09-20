# Durabilidad de la entrega de webhooks de Hotmart

- **Estado:** propuesta. No es una decision aceptada ni una implementacion.
- **Fecha:** 2026-09-19.
- **Origen:** hallazgo 1 de `../operations/2026-09-19-claude-production-ingress-audit-v1.md`.
- **Decide:** Dan. La implementacion toca `src/bridge/app.py`, hoy reservado por el claim `codex-appointment-operations-v1` (PR #160), asi que no puede empezar hasta que ese claim se cierre.

## 1. El problema, en una linea

Un evento de Hotmart que falla una vez no puede volver a entrar nunca, y el emisor interpreta la racha de fallas como una URL caida.

## 2. Lo observado

En 3,05 dias, de 103 entregas de Hotmart entraron 10. La secuencia que se repite es:

```text
primer intento   -> 503 webhook_persist_unavailable   (fallo al persistir en Supabase)
reintento        -> 401                               (el evento ya tiene mas de 300 s)
reintento        -> 401
reintento        -> 401
```

El handler valida en este orden: token, tamano, JSON, clasificacion, **frescura**, persistencia. La frescura compara `creation_date` del evento contra `HOTMART_MAX_AGE_SECONDS`, hoy en su valor por defecto de 300 segundos, y rechaza con `401 stale_webhook`.

## 3. Los tres defectos que esto expone

### 3.1 Un evento viejo no es un problema de autenticacion

`stale_webhook` responde **401**, el mismo codigo que un token invalido. Para el emisor son indistinguibles, y los dos cuentan como fallo de entrega. La consecuencia esta documentada en el relevamiento del producto: **Hotmart desactiva la configuracion del webhook cuando la URL falla de forma sostenida**, sin avisar y sin endpoint REST para recuperar el historial de abandono de carrito. El canal entero se puede apagar en silencio por una racha de rechazos que, desde el punto de vista del sistema, eran descartes deliberados.

El handler ya tiene el patron correcto para un descarte deliberado: cuando `classify_hotmart_event` no acepta el evento, responde **200 con `{"status": "ignored", "reason": ...}`**. Un evento fuera de ventana es exactamente eso, un descarte, y deberia responder igual.

### 3.2 La ventana de frescura no es la defensa que parece

La idempotencia real la da la base: `webhook_events` tiene `unique (source, external_event_id)`, y el flujo de abandono devuelve `duplicate` sobre esa unicidad. La ventana de 300 segundos **no** es lo que impide procesar dos veces el mismo evento; es una guarda contra replay de un tercero que capture una entrega valida.

Eso importa porque significa que la ventana se puede ampliar sin perder la garantia de procesamiento unico. Y hace falta ampliarla: la verificacion de abandono de carrito de Hotmart es por lotes, con demoras tipicas de decenas de minutos entre el abandono y la entrega, y el reintento de un evento legitimo llega, por definicion, tarde. Una ventana de cinco minutos medida contra `creation_date` esta calibrada para un emisor en tiempo real que Hotmart no es, al menos no para este evento.

### 3.3 Un fallo de persistencia se delega entero al emisor

Ante `SupabaseError` el bridge devuelve 503 y se olvida del evento. La recuperacion queda a cargo del reintento de Hotmart, que son como maximo cinco y que ademas van a chocar contra la guarda de frescura. El bridge ya resuelve este problema para Chatwoot con admision durable en disco (`CAPTURE_DIR`, escritura privada y atomica, retomada al reiniciar): responde despues de persistir una admision recuperable, no despues de completar el trabajo. Hotmart no tiene ese tratamiento.

## 4. Propuesta

En orden de valor sobre riesgo.

**A. Cambiar el codigo de respuesta del descarte por antiguedad.** `stale_webhook` pasa de `401` a `200 {"status": "ignored", "reason": "stale_webhook"}`. No cambia que el evento se descarte; cambia que el emisor deje de contarlo como caida. Es el cambio mas chico y el que quita el riesgo mayor.

**B. Calibrar la ventana contra el emisor real.** Subir `HOTMART_MAX_AGE_SECONDS` a un valor coherente con la entrega por lotes de Hotmart y con su politica de reintentos, y medirlo en vez de estimarlo: hace falta el `creation_date` real de los eventos entregados, que se lee en el panel de Hotmart. Mientras no se mida, el valor por defecto de 300 segundos es una decision sin dato.

**C. Admision durable para Hotmart.** Escribir la admision en disco antes de responder, con el mismo patron que el ingreso de Chatwoot, y reconciliar contra Supabase en un worker. Convierte un fallo de la base en un retraso en vez de una perdida. Es el cambio mas grande de los tres y el unico que toca el modelo de procesamiento; entra despues de A y B, y solo si el 503 resulta recurrente y no un sintoma de otra cosa.

**D. Exponer el motivo del rechazo en `/ready`.** Hoy la unica forma de saber que se esta perdiendo el noventa por ciento del ingreso es leer el log del proxy y medir el tamano de los cuerpos de error para adivinar el motivo. Contadores por causa (`rejected_stale`, `rejected_token`, `persist_unavailable`) hacen visible el problema sin auditoria forense.

## 5. Lo que hay que averiguar antes de implementar

1. **La causa del `SupabaseError`.** Si el 503 se debe a un error sistematico (una restriccion, un permiso, un esquema desincronizado) y no a indisponibilidad, la propuesta C no corresponde y lo que hay que arreglar es otra cosa. Sin esto, A y B tapan el sintoma mas visible pero no el origen.
2. **Si la configuracion del webhook en Hotmart sigue activa** y cuantos reintentos consumio. Lo mira una persona con acceso a esa cuenta.
3. **Con que `creation_date` llegan los eventos de abandono**, que es el dato que calibra B.

## 6. Alcance y limites

- Afecta solo al endpoint `POST /webhooks/hotmart` y a su configuracion.
- A y B no cambian que eventos se procesan, solo que se responde y con que ventana. C si cambia el modelo de procesamiento y necesita su propio contrato y sus pruebas.
- Nada de esto autoriza activar flags, desplegar ni contactar personas.
