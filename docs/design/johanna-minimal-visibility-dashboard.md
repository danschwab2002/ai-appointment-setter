# Visibilidad mínima y práctica del piloto Johanna

- **Estado:** Base de diseño aprobada; implementación preparada
- **Fecha:** 2026-08-31
- **Disparador:** dirección macro confirmada por Juan: el equipo debe poder consultar,
  cuando lo necesite, volumen, conversaciones, resultados y funnel
- **Alcance:** lectura y visualización; no modifica Chatwoot, Supabase Cloud ni el
  comportamiento del agente
- **Implementación:** CLI, RPC sanitario, HTML y pruebas implementados y verificados;
  pendientes de merge y despliegue

## 1. Decisión aceptada

No construir un producto de analytics ni un dashboard con backend propio.

Mantener dos superficies complementarias:

1. **Chatwoot como superficie operativa de conversación:** ver y abrir
   conversaciones, leer historial, responder, asignar, resolver, filtrar y usar
   sus reportes nativos.
2. **Un artifact HTML on-demand como vista de negocio y sistema:** unir las
   conversaciones canónicas de Chatwoot con el estado durable de Supabase Cloud
   para mostrar funnels, resultados, bloqueos y casos que Chatwoot no puede
   interpretar por sí solo.

El artifact no copia el inbox. Cada fila que tenga conversación ofrece un enlace
al registro canónico de Chatwoot. El detalle humano continúa allí.

## 2. Preguntas que la vista debe responder

La visibilidad mínima se reparte entre dos superficies:

**Chatwoot responde sobre el universo conversacional:**

1. ¿Cuántas conversaciones hubo?
2. ¿Cuáles fueron, qué mensajes contienen y cuál es su estado operativo?
3. ¿Quién respondió, cuánto demoró y quién las atiende?

**El artifact responde sobre el universo de casos comerciales durables:**

4. ¿Cuántos casos se generaron y por qué trigger?
5. ¿Hasta qué etapa durable llegó cada caso?
6. ¿Qué resultado comercial durable tiene cada caso?
7. ¿Qué casos necesitan atención ahora?

El artifact no promete cubrir conversaciones que no tengan un caso durable
vinculado. Debe mostrar la cobertura del join y remitir a Chatwoot para el
universo completo de conversaciones.

No intenta explicar automáticamente por qué una conversación fue buena o mala.
Para la revisión cualitativa, el operador abre la conversación en Chatwoot.

## 3. Qué ya aporta Chatwoot

### 3.1 Vista operativa

Chatwoot ya posee la conversación, su historial y la identidad de los actores.
El contrato observado por el proyecto permite obtener:

- cuenta, inbox y conversación;
- estado y posibilidad de responder;
- equipo y assignee;
- etiquetas y atributos;
- timestamps de actividad;
- mensajes entrantes, salientes humanos y salientes del AgentBot;
- privacidad y dirección de cada mensaje;
- estado de lectura/interfaz.

La API lista conversaciones con paginación y filtros y devuelve metadatos como
inbox, estado, mensajes recientes, espera, unread y asignación.[5] También ofrece
conteos de conversaciones totales, asignadas y sin asignar.[6]

Esto alcanza para que el equipo pueda:

- ver exactamente qué conversaciones existen;
- abrir una conversación;
- saber si está abierta, pending o resuelta;
- saber quién la atiende;
- detectar si respondió un humano o el AgentBot;
- continuar la operación desde el mismo inbox.

### 3.2 Reportes nativos

Chatwoot ya ofrece reportes de:

- volumen de conversaciones;
- tiempo de primera respuesta;
- tiempo de resolución;
- cantidad de resoluciones;
- rangos de fechas y agrupación diaria, semanal o mensual.[1]

Su vista en vivo cubre conversaciones abiertas, desatendidas y sin asignar,
estado de agentes y mapas de calor de tráfico y resolución.[2]

La API oficial de reportes define además:

- conversaciones;
- mensajes entrantes y salientes;
- primera respuesta promedio;
- resolución promedio;
- resoluciones totales;[3]
- agrupación por inbox con volumen, primera respuesta, reply time y resolución.[4]

Chatwoot también documenta reportes por agente, equipo, inbox, canal, etiqueta y
CSAT. Estas capacidades no deben duplicarse en el HTML mientras el equipo pueda
resolver la misma pregunta directamente en Chatwoot.

### 3.3 Límite efectivo observado en producción

Un probe read-only del 2026-08-31 confirmó:

```text
GET /api/v1/profile                                      → 200
GET /api/v1/accounts/{account}/conversations/meta        → 200
GET /api/v2/accounts/{account}/reports/summary            → 401
GET /api/v2/accounts/{account}/reports/conversations      → 401
GET /api/v2/accounts/{account}/summary_reports/inbox      → 401
```

La credencial técnica actual puede consultar conversaciones, pero no reutilizar
los reportes V2. Esto no demuestra que los reportes no estén disponibles en la
UI humana; demuestra que **no son una fuente disponible para el generador con la
credencial actual**.

La V1 no debe pedir una credencial más amplia sólo para copiar métricas que el
operador ya puede mirar en Chatwoot. Si más adelante se desea integrar esos
reportes, se tratará como una decisión separada de privilegios.

## 4. Qué Chatwoot no puede responder solo

Chatwoot conoce conversaciones, no el workflow comercial durable del bridge.
Por sí solo no puede afirmar:

- si una conversación nació por inbound, carrito abandonado, pago fallido o
  preformulario;
- si un formulario tenía autorización comercial vigente;
- si Hotmart correlacionó una compra, abandono o pago fallido con la intención
  correcta;
- si un timer de 60 minutos fue programado, quedó due o fue reevaluado;
- si el bridge reservó un comando pero bloqueó el POST final;
- si existe `request_started` o `delivery_unknown`;
- si una compra cerró durablemente el caso;
- si una identidad quedó `unmatched`, `ambiguous` o en conflicto;
- si un opt-out o takeover bloqueó el efecto antes del sender;
- si la fila proviene de un cliente real, una prueba controlada o un simulador.

Tampoco debe interpretarse `conversation.status = resolved` como compra o éxito
comercial. Es un estado operativo de Chatwoot, no el resultado del caso.

## 5. Qué aporta Supabase Cloud y el bridge

La vista HTML puede reconstruir el funnel con hechos ya existentes, sin crear una
base analítica nueva.

| Pregunta | Fuente durable principal |
|---|---|
| ¿Llegó el evento Hotmart? | `webhook_events` |
| ¿Llegó el preformulario? | `precheckout_submissions` |
| ¿Existe intención y consentimiento? | `purchase_intents` |
| ¿Cómo se correlacionó Hotmart? | `hotmart_purchase_intent_correlations` |
| ¿Se programó y reevaluó el timer? | `hotmart_abandonment_reevaluations` |
| ¿Se reservó o finalizó el outbound? | `johanna_abandonment_one_shot_commands` |
| ¿Qué ocurrió con un pago fallido? | `johanna_payment_failure_cases` |
| ¿Existe caso comercial inbound? | `commercial_cases` e `inbound_commercial_case_admissions` |
| ¿Hubo handoff? | `human_handoff_requests` |
| ¿Hubo aceptación, falla o ambigüedad? | `followup_delivery_attempts` y comandos del caso |
| ¿Cuál es la conversación canónica? | referencias Chatwoot persistidas en el caso |

Supabase Cloud debe aportar la clasificación y el resultado; Chatwoot aporta la
conversación y su operación actual.

## 6. Métricas mínimas del HTML

### 6.1 Encabezado

- fecha y hora de corte;
- ventana de cohorte `[cutoff - 7 días, cutoff)`, expresada en UTC;
- última llegada por fuente;
- disponibilidad de Supabase Cloud y del enriquecimiento Chatwoot;
- estado de cada bloque: `complete`, `partial` o `unavailable`;
- advertencia cuando la procedencia `real/test` sea desconocida.

El período se aplica en la consulta agregada, no como filtro client-side. Una
nueva ventana genera un nuevo artifact.

### 6.2 Cobertura del join con Chatwoot

- casos de la cohorte;
- casos con conversación canónica vinculada;
- casos sin conversación;
- conversaciones vinculadas únicas;
- conversaciones compartidas por más de un caso;
- casos cuyo enriquecimiento Chatwoot no estuvo disponible.

Para conversaciones ya vinculadas, el HTML puede leer sólo metadata operativa:
`status`, inbox, team/assignee, timestamps y enlace. No consulta el endpoint de
mensajes ni clasifica actores. Los conteos se rotulan **conversaciones vinculadas
a casos**, nunca “conversaciones del período”. El volumen, los mensajes, los
actores y los tiempos continúan en los reportes de Chatwoot.

### 6.3 Funnel inbound

```text
admisión durable
→ caso comercial activo
→ automatización activa | pausada | deshabilitada
→ handoff | opt-out | blocked | failed | unknown
```

Métricas:

- admisiones y casos únicos creados en la cohorte;
- casos activos, pausados, completados y bloqueados;
- handoffs por estado;
- opt-outs durables;
- errores, conflictos y resultados desconocidos;
- cobertura de conversación canónica.

`chatwoot_status` se muestra como snapshot operativo separado. `resolved` no es
etapa ni resultado comercial. Evidencia de respuesta o calidad conversacional no
se calcula en el artifact V1; se inspecciona en Chatwoot.

### 6.4 Funnel recuperación de carrito

Este bloque incluye abandono Hotmart y precheckout diferido. Cada intención se
clasifica en una categoría disjunta: `precheckout_only`, `hotmart_only` o `both`.
`both` evita contar dos veces una intención observada por ambos orígenes.

```text
trigger Hotmart | preformulario autorizado
→ intención durable
→ correlación
→ timer/reevaluación cuando corresponda
→ comando reservado
→ request_started
→ aceptado por Chatwoot
→ compra durable | handoff | opt-out | blocked | failed | unknown
```

Métricas:

- intenciones únicas por origen disjunto;
- correlación `resolved`, `unmatched`, `ambiguous`, `conflict`;
- timers scheduled, due, completed y bloqueados;
- comandos reservados;
- requests iniciados;
- aceptados por Chatwoot;
- compras inequívocamente vinculadas;
- handoffs y opt-outs;
- `delivery_unknown`;
- casos sin conversación canónica.

No se calcula tasa de respuesta, conversión atribuida ni “sin respuesta” en V1:
esas métricas requieren una fuente conversacional uniforme y un horizonte de
madurez todavía no aceptado. Los casos no terminales se muestran por antigüedad
`<1 h`, `1–24 h` y `>24 h`.

Mientras la plantilla precheckout siga pendiente, la etapa debe mostrar
claramente:

```text
reserved → outbound bloqueado por configuración
```

No debe contarse como falla ni como mensaje enviado.

### 6.5 Funnel pago fallido

```text
webhook de pago fallido
→ caso admitido
→ correlación
→ comando reservado
→ aceptado por Chatwoot
→ compra durable | handoff | opt-out | blocked | failed | unknown
```

Métricas:

- eventos y casos únicos;
- correlación por outcome;
- mensajes aceptados;
- compras inequívocamente vinculadas;
- handoffs;
- bloqueos y errores;
- resultados ambiguos;
- casos sin conversación canónica.

No se presenta “compra recuperada” ni una tasa de conversión hasta aceptar una
ventana de atribución y una relación causal ejecutable.

### 6.6 Salud y atención requerida

Un bloque separado, siempre visible:

- `unmatched`, `ambiguous` y conflictos;
- `delivery_unknown`;
- comandos failed o bloqueados inesperadamente;
- handoffs pendientes o con error;
- timers vencidos sin terminalización;
- discrepancias entre estado durable y Chatwoot;
- filas de procedencia desconocida;
- casos no terminales por bucket de antigüedad.

Los problemas no se esconden dentro de una tasa promedio.

## 7. Vista de casos

La tabla central debe tener una fila por **caso**, no por mensaje.

Columnas V1:

| Columna | Propósito |
|---|---|
| Caso | ID sanitario y enlace al detalle |
| Tipo | inbound, abandono, precheckout o pago fallido |
| Procedencia | cliente, prueba, simulador o desconocida |
| Entrada | timestamp y trigger |
| Conversación | enlace a Chatwoot |
| Estado Chatwoot | snapshot open, pending o resolved; opcional |
| Etapa | etapa durable del funnel |
| Resultado comercial | `purchased` o `unknown`; nunca inferir `not_purchased` por ausencia |
| Resultado de control | handoff, opt-out, blocked, failed, delivery_unknown o none |
| Evidencia conversacional | `not_collected` en V1; el detalle vive en Chatwoot |
| Última actividad durable | timestamp y antigüedad |
| Atención | sí/no y reason code sanitario |

El HTML no muestra teléfono, email, nombre, payload, texto de mensajes, token ni
IDs externos completos. Para leer la conversación se abre Chatwoot.

## 8. Definiciones métricas

### 8.1 Unidades distintas

La vista debe mantener separados:

- **mensajes**: unidades de comunicación;
- **conversaciones**: threads de Chatwoot;
- **casos**: objetivos comerciales durables;
- **personas**: identidades canónicas.

No son intercambiables. Una conversación puede contener más de un caso y un caso
puede acumular varios mensajes.

### 8.2 Cohorte y conteos

La cohorte base contiene casos cuyo root durable (`commercial_case`, intención o
caso de pago fallido, según el funnel) fue creado dentro de
`[cutoff - 7 días, cutoff)`. Todos los timestamps se comparan en UTC.

Los estados Chatwoot y los estados mutables del caso son snapshots al `cutoff`;
no se presentan como eventos ocurridos dentro de la ventana.

El V1 muestra conteos, no tasas comerciales. Para cada etapa se informa:

- `eligible_total`: casos de la cohorte;
- `stage_count`: casos que alcanzaron la etapa durable;
- `ambiguous_or_unknown`: casos no clasificables;
- `excluded_by_provenance`: test, simulador o procedencia desconocida.

Los ambiguos y desconocidos permanecen visibles y nunca desaparecen del total.
Una futura tasa deberá declarar numerador, denominador, timestamp de inclusión,
horizonte de madurez y ventana de atribución antes de implementarse.

### 8.3 Resultados independientes

El V1 no comprime autoridades distintas en un único estado terminal:

- `commercial_outcome = purchased` sólo si `purchase_intents.lifecycle_state`,
  `recovery_cases.purchase_event_id` o `recovery_cases.won_at` aporta evidencia
  inequívoca vinculada al caso; de lo contrario usa `unknown`;
- `opt_out` se deriva de `contact_opt_out_events` correlacionado con el contacto o
  conversación del caso y posterior a su creación;
- `handoff` se deriva de `human_handoff_requests` y conserva su status;
- `blocked`, `failed` y `delivery_unknown` se derivan del comando o intento
  durable correspondiente.

Los resultados de control son aditivos. Un caso puede haber comprado y además
haber hecho opt-out o requerido handoff; el HTML muestra ambos hechos y no elige
uno para borrar al otro.

### 8.4 Evidencia de entrega

- `reserved` no es envío;
- `request_started` es ambiguo;
- `accepted_by_chatwoot` prueba aceptación CRM, no entrega física;
- `sent`, `delivered` y `read` sólo se muestran cuando provienen de evidencia
  canónica disponible;
- ausencia de error no se etiqueta como éxito.

## 9. Brecha mínima de datos: procedencia

La inspección actual confirmó que algunas entidades tienen `test_only`, pero no
existe una dimensión uniforme que distinga:

```text
customer_production | controlled_test | simulator | unknown
```

La clasificación se calcula en SQL por caso y se propaga a todos sus agregados:

1. `controlled_test` sólo con marcador durable explícito en la cadena causal;
2. `simulator` cuando todos los roots causales conocidos son de simulador y no
   existe un marcador conflictivo;
3. `customer_production` sólo con evidencia durable explícita de producción;
4. `unknown` cuando falta evidencia, hay mezcla conflictiva o sólo existen
   señales como `provider_observed`, `provisional` o `activation_authorized`.

Esas últimas señales no prueban test ni producción. Ante conflicto prevalece
`unknown`, nunca una inferencia positiva.

Los agregados se consultan ya agrupados por procedencia en Supabase Cloud. Los
filtros client-side no recalculan tarjetas. Por defecto, toda métrica denominada
“real” excluye `controlled_test`, `simulator` y `unknown`; si no existe evidencia
durable de producción, el resultado real es cero.

Como mejora pequeña posterior puede agregarse una procedencia durable común o un
registro de runs de prueba. No es requisito para generar el primer HTML, pero sí
para métricas comerciales confiables.

## 10. Arquitectura mínima

```text
pedido del usuario
→ skill read-only
→ consultas agregadas a Supabase Cloud
→ lista acotada de IDs de conversaciones ya vinculadas
→ lecturas Chatwoot V1 de metadata, sin mensajes
→ join de cobertura y snapshot operativo opcional
→ HTML estático sanitario
→ artifact temporal
```

Características:

- sin backend nuevo;
- sin servidor permanente;
- sin cron obligatorio;
- sin base analítica;
- sin librería frontend;
- sin escrituras;
- sin cache compartido en V1;
- máximo 100 casos detallados por consulta;
- agregados completos para la ventana pedida;
- período aplicado en SQL durante la generación;
- filtros client-side sólo sobre la tabla sanitaria, sin recalcular agregados.

## 11. Diseño visual mínimo

Una sola página con tres niveles:

1. **Ahora:** salud, cobertura del join, atención requerida y casos principales.
2. **Funnels:** inbound, recuperación y pago fallido.
3. **Casos:** tabla filtrable con enlace a Chatwoot.

Filtros:

- tipo de caso;
- etapa;
- resultado;
- procedencia;
- necesita atención.

El período se elige antes de generar el artifact; no es un filtro visual que
recalcule tarjetas.

No incluir gráficos complejos. Tarjetas, barras horizontales proporcionales y una
tabla son suficientes.

## 12. Qué queda fuera

- dashboard alojado permanentemente;
- login o gestión de usuarios;
- escritura en Chatwoot o Supabase Cloud;
- edición de labels, assignees o estados;
- réplica de historiales;
- buscador de contenido de mensajes;
- análisis automático de sentimiento;
- scoring de calidad con LLM;
- atribución de revenue sofisticada;
- análisis comparativo de cohortes o experimentación;
- reemplazo de reportes nativos de Chatwoot;
- CSAT automático nuevo;
- alertas o cron en la primera versión.

## 13. Secuencia recomendada

1. **Artifact manual V1:** generar el HTML actual bajo demanda con conteos y
   tabla sanitaria.
2. **Validación operativa:** Juan y el equipo lo usan para responder preguntas
   reales durante el piloto.
3. **Corregir definiciones:** ajustar sólo métricas que resulten ambiguas o
   inútiles.
4. **Procedencia durable:** agregarla únicamente si `unknown` impide medir el
   piloto.
5. **Automatización opcional:** programar snapshots o una URL estable sólo si la
   frecuencia real de consulta lo justifica.

## 14. Criterios de aceptación del V1

- Chatwoot responde las preguntas conversacionales 1–3 y el artifact responde
  las preguntas comerciales 4–7;
- cada total se deriva programáticamente;
- diferencia mensajes, conversaciones, casos y personas;
- separa Chatwoot operativo de resultado comercial durable;
- muestra cobertura del join sin afirmar que contiene toda la cuenta Chatwoot;
- no consulta ni serializa contenido de mensajes;
- no publica tasas de respuesta o conversión sin definición aceptada;
- no mezcla prueba, simulador y producción sin rotular;
- ofrece enlace a Chatwoot sin copiar el historial;
- muestra errores y ambigüedades;
- no contiene PII ni credenciales;
- no realiza escrituras;
- se genera en una sola ejecución on-demand;
- abre como artifact HTML sin infraestructura adicional.

## 15. Decisiones pendientes

Sólo quedan tres decisiones de producto antes de construir el V1:

1. **Ventana inicial predeterminada:** recomendación siete días móviles en UTC,
   aplicada al root durable del caso.
2. **Acceso al detalle:** recomendación enlace a Chatwoot, sin contenido copiado.
3. **Procedencia desconocida:** recomendación mostrarla y excluirla de métricas
   denominadas “reales”.

No hace falta decidir ahora sobre hosting, cron, CSAT, análisis con IA ni una UI
permanente.

## Sources

[1] https://www.chatwoot.com/es/features/conversation-reports
[2] https://www.chatwoot.com/es/features/live-view-of-reports
[3] https://developers.chatwoot.com/api-reference/reports/get-account-reports-summary
[4] https://developers.chatwoot.com/api-reference/reports/get-conversation-statistics-grouped-by-inbox
[5] https://developers.chatwoot.com/api-reference/conversations/conversations-list
[6] https://developers.chatwoot.com/api-reference/conversations/get-conversation-counts
