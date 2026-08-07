# Compilación durable de mensajes entrantes

- **Estado:** Implementado localmente; pendiente de despliegue y E2E productivo
- **Alcance:** Primer MVP limitado al ingreso Chatwoot → Hermes
- **Fuera de alcance:** División de respuestas salientes, indicadores de escritura, seguimiento comercial y cambios de UX

## Problema

El flujo implementado procesa cada webhook entrante como un turno independiente:

```text
un webhook → una lectura canónica → una invocación Hermes → una respuesta
```

En WhatsApp una persona suele expresar una sola idea mediante varios mensajes consecutivos. Procesarlos por separado provoca respuestas prematuras y una dinámica rígida de una burbuja por turno.

## Comportamiento aprobado

Una conversación usa un período de silencio de **30 segundos**:

```text
primer mensaje público entrante
→ admisión durable y HTTP 202
→ esperar 30 segundos

nuevo mensaje de la misma conversación antes del vencimiento
→ admitirlo durablemente
→ reiniciar el período de 30 segundos

30 segundos sin nuevos mensajes
→ cerrar un turno lógico
→ invocar Hermes una sola vez con el historial canónico hasta el último mensaje
```

El tiempo se mide desde la admisión durable más reciente de esa conversación, no mediante un contador en memoria.

## Diseño técnico del primer corte

### Fuente de verdad

Chatwoot continúa siendo la fuente canónica del contenido y orden de los mensajes. Los archivos privados de `CAPTURE_DIR/.work` conservan la admisión y el momento de llegada necesario para el debounce; no compilan ni duplican contenido conversacional en una nueva base.

### Clave de agrupación

Sólo los eventos públicos entrantes aceptados se agrupan por la conversación Chatwoot canónica. Intervenciones humanas y otros controles determinísticos no esperan el período de gracia.

### Selección del trigger

La admisión local más reciente fija el vencimiento de la ventana, pero no el
orden conversacional. Al vencer, lidera el delivery con el mayor `message_id`
canónico de Chatwoot. Esto tolera webhooks entregados fuera de orden. La lectura
canónica se trunca en ese trigger y debe contener todos los IDs admitidos del
grupo; si falta alguno, el turno falla de forma reintentable en vez de completar
con contexto parcial. El cliente pagina hacia atrás con `before` en páginas de 20;
mantiene una ventana reciente y sigue paginando hasta encontrar todos los IDs
obligatorios, el inicio del historial o el límite operacional de 100 páginas. Un
inbound sin ID canónico válido se
ignora antes de admitirlo; un envelope legacy ya admitido con ese defecto falla
cerrado y nunca se completa como si hubiera integrado el turno.

### Durabilidad y replay

- Cada webhook recibe `202 Accepted` sólo después de su admisión durable existente.
- Un reinicio conserva el momento de admisión y retoma la espera restante.
- Los deliveries anteriores del mismo turno no invocan Hermes por separado.
- El procesamiento se serializa mediante un lock privado cuyo nombre contiene
  sólo el hash de la conversación; dos workers no ejecutan handlers concurrentes
  para el mismo caso. Bajo ese lock se vuelve a escanear la conversación y se
  revalidan miembros, leader y deadline antes de invocar Hermes.
- Si aparece un mensaje nuevo durante el razonamiento, la autorización final existente bloquea una respuesta basada en un trigger anterior; el delivery nuevo formará el turno siguiente.
- Los fallos reintentables mantienen todos los miembros `admitted` y unido el
  mismo grupo. Sólo un éxito completa los miembros anteriores; si el líder agota
  sus intentos, todo el grupo termina `failed` para reconciliación. Un journal
  durable registra primero esa intención grupal y permite completar la transición
  idempotentemente después de un crash entre escrituras de miembros.

## Configuración

La ventana se expone como:

```text
CHATWOOT_INBOUND_DEBOUNCE_SECONDS=30
```

El valor `0` mantiene el comportamiento inmediato para pruebas controladas o rollback. Los valores negativos son inválidos.

## Invariantes

1. El endpoint no espera 30 segundos; el ACK durable no cambia.
2. Dos mensajes de una conversación dentro de la ventana producen como máximo una invocación Hermes para ese turno.
3. Conversaciones diferentes tienen ventanas independientes.
4. Un control de intervención humana nunca queda demorado por el debounce.
5. El contenido completo no se registra en logs ni en nuevos nombres de archivo.
6. La respuesta pública continúa siendo una sola burbuja en este MVP.

## Validación prevista

- prueba RED→GREEN del worker con reloj controlado;
- prueba de reinicio durante la ventana;
- prueba de conversaciones independientes;
- prueba integrada de dos webhooks → una invocación Hermes con ambos mensajes canónicos;
- prueba de orden canónico con webhooks fuera de orden;
- prueba de backoff, dead-letter y exclusión concurrente por conversación;
- prueba de un turno de más de 20 mensajes sin truncamiento silencioso;
- suite completa y `compileall`;
- verificación HTTP real del ACK inmediato;
- E2E controlado enviando varios mensajes dentro de 30 segundos.

## Temas posteriores

- división durable de una respuesta lógica en varias burbujas;
- límite máximo de espera cuando llegan mensajes continuamente;
- política configurable por cliente;
- migración a coordinación compartida si el bridge deja de operar como un único servicio sobre un volumen persistente.
