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

### 3.3 Un rechazo de contrato se reporta como indisponibilidad

Esto es lo central, y esta demostrado, no inferido. `admit_and_correlate_hotmart_cart_abandonment` valida el contrato v2.0.0 del payload y **lanza** si no lo cumple: version distinta de `2.0.0`, `offer.code` ausente, `product.id` como texto, `creation_date` como texto, o ningun dato de contacto. La llamada es atomica, asi que la excepcion revierte la admision entera. En el bridge esa excepcion es un `SupabaseError` y sale como `503 webhook_persist_unavailable`.

O sea: **el 503 no dice lo que pasa y ademas invita a lo peor**. Lo que pasa es que el evento no cumple el contrato y nunca lo va a cumplir; lo que el codigo 503 le dice al emisor es que el servicio esta caido y que vuelva a intentar. Hotmart reintenta, falla, y cuenta cada falla para desactivar la configuracion del webhook.

Que la base no es el problema tambien esta medido: el endpoint del funnel de Johanna persiste en la misma Supabase y acumulo 1753 admisiones sin un solo 503 en el mismo periodo.

La reproduccion completa, con la tabla de que condicion rompe cual, esta en la seccion 4 de `../operations/2026-09-19-claude-production-ingress-audit-v1.md`.

### 3.4 Un fallo de persistencia, si alguna vez ocurre, se delega entero al emisor

Ante `SupabaseError` el bridge devuelve 503 y se olvida del evento. La recuperacion queda a cargo del reintento de Hotmart, que son como maximo cinco y que ademas van a chocar contra la guarda de frescura. El bridge ya resuelve este problema para Chatwoot con admision durable en disco (`CAPTURE_DIR`, escritura privada y atomica, retomada al reiniciar): responde despues de persistir una admision recuperable, no despues de completar el trabajo. Hotmart no tiene ese tratamiento.

## 4. Propuesta

En orden de valor sobre riesgo.

**A. Que los rechazos deterministas dejen de parecer caidas.** Dos cambios de codigo de respuesta, ninguno cambia que evento se procesa:

- el payload que no cumple el contrato pasa de `503` a `200 {"status": "ignored", "reason": "payload_contract_rejected"}`, con el detalle de que condicion fallo;
- `stale_webhook` pasa de `401` a `200 {"status": "ignored", "reason": "stale_webhook"}`.

El handler ya tiene ese patron para los eventos que el clasificador no acepta. Es el cambio mas chico y el que quita el riesgo mayor, que es que Hotmart apague el canal.

**B. Calibrar la ventana contra el emisor real.** Subir `HOTMART_MAX_AGE_SECONDS` a un valor coherente con la entrega por lotes de Hotmart y con su politica de reintentos, y medirlo en vez de estimarlo: hace falta el `creation_date` real de los eventos entregados, que se lee en el panel de Hotmart. Mientras no se mida, el valor por defecto de 300 segundos es una decision sin dato.

**C. Admision durable para Hotmart.** Escribir la admision en disco antes de responder, con el mismo patron que el ingreso de Chatwoot, y reconciliar contra Supabase en un worker. Convierte un fallo de la base en un retraso en vez de una perdida. Es el cambio mas grande de los tres y el unico que toca el modelo de procesamiento. **Con lo que ahora se sabe, no es prioritario:** el 503 medido no viene de la base sino del contrato, y para eso A alcanza. Queda anotado para cuando aparezca un fallo real de disponibilidad.

**C-bis. Separar los dos errores.** Un `SupabaseError` en la admision y uno en la correlacion son problemas distintos: el primero pide reintento del emisor, el segundo pide reproceso interno de un evento que ya esta guardado. Deben tener codigos y nombres distintos antes de decidir nada mas, porque hoy el diagnostico no se puede hacer.

**D. Exponer el motivo del rechazo en `/ready`.** Hoy la unica forma de saber que se esta perdiendo el noventa por ciento del ingreso es leer el log del proxy y medir el tamano de los cuerpos de error para adivinar el motivo. Contadores por causa (`rejected_stale`, `rejected_token`, `persist_unavailable`) hacen visible el problema sin auditoria forense.

## 5. Lo que hay que averiguar antes de implementar

1. **Que campo concreto viene mal en los eventos reales de Hotmart.** La causa general esta demostrada; cual de las cinco condiciones se incumple en produccion se lee de un payload real, en el historial de envios del panel de Hotmart, que guarda 60 dias. De eso depende si ademas del cambio de respuesta hace falta relajar o corregir alguna validacion: si Hotmart empezo a mandar `version` `2.1.0`, por ejemplo, el arreglo no es de codigos de respuesta sino de contrato.
2. **Si la configuracion del webhook en Hotmart sigue activa** y cuantos reintentos consumio. Lo mira una persona con acceso a esa cuenta.
3. **Con que `creation_date` llegan los eventos de abandono**, que es el dato que calibra B.

## 6. Alcance y limites

- Afecta solo al endpoint `POST /webhooks/hotmart` y a su configuracion.
- A y B no cambian que eventos se procesan, solo que se responde y con que ventana. C si cambia el modelo de procesamiento y necesita su propio contrato y sus pruebas.
- Nada de esto autoriza activar flags, desplegar ni contactar personas.
