# ADR-0007: Motor durable de próxima acción

- **Estado:** Aceptada
- **Fecha:** 2026-08-03
- **Supersede parcialmente:** ADR-0003 y ADR-0004

## Contexto

El flujo de recuperación de carrito validado recibe un abandono de Hotmart,
resuelve identidad y contexto, aplica guardas determinísticas, solicita una
propuesta a Hermes y publica por Chatwoot. El flujo inicial ejecuta el primer
contacto directamente desde el worker.

El producto necesita incorporar grace periods y seguimientos por falta de
respuesta sin crear un cronjob por persona ni convertir el bridge en un motor BPM
genérico. Una acción que vence tampoco puede significar «enviar obligatoriamente»:
entre su planificación y ejecución pueden ocurrir una compra, una respuesta, un
opt-out, un human takeover, un cambio de horario o una caída parcial.

El sistema cruza Postgres, Hermes, Chatwoot y proveedores externos. No existe una
transacción distribuida que garantice exactly-once entre todos ellos. El diseño
debe asumir procesamiento al menos una vez, conservar evidencia durable y tratar
explícitamente los resultados remotos inciertos.

## Decisión

### 1. Adoptar un motor durable de próxima acción

Supabase/Postgres conservará el estado operativo canónico del motor:

```text
Política publicada e inmutable
→ secuencia activa
→ una próxima acción materializada
→ dispatcher
→ reevaluación autoritativa
→ efecto, diferimiento, cancelación o escalamiento
→ auditoría
```

Una acción programada ordena reevaluar al llegar cierto momento. Nunca constituye
por sí sola autorización para enviar.

Se materializará únicamente la próxima acción comercial de cada caso. No se
crearán por adelantado todos los pasos futuros.

### 2. Unificar el pipeline proactivo empezando por el primer contacto

Primer contacto y follow-ups usarán finalmente el mismo pipeline durable. El
primer caso de uso del nuevo motor será migrar el primer contacto de recuperación
de carrito ya validado E2E. Después de verificar esa migración se incorporarán los
follow-ups por falta de respuesta.

El receptor, la resolución de identidad, `SituationReport`, las guardas, Hermes,
Chatwoot y la restricción `ALLOWED_WHATSAPP_JID` se reutilizan. Cambia la
orquestación: el webhook planifica una acción y un worker independiente la
reclama y ejecuta.

### 3. Separar autorización y juicio comercial

El bridge posee la autoridad final sobre la acción ejecutable. Evalúa hechos y
guardas como:

- identidad y destino autorizados;
- permiso o base de contacto;
- compra confirmada;
- respuesta posterior al ancla;
- opt-out;
- human takeover;
- horario, expiración y límites de política;
- integridad y vigencia del estado.

Hermes puede proponer estrategia comercial y redacción dentro de las alternativas
seguras. No puede debilitar una guarda ni convertir una abstención en envío.
Después de recibir su propuesta, el bridge vuelve a validar antes del efecto.

Esta sección supersede la autoridad comercial más amplia atribuida al agente en
ADR-0003. Se conserva de ese ADR la separación entre contexto determinístico y
razonamiento generativo.

### 4. Mantener el alcance inicial acotado

Las abstracciones principales —política, secuencia, acción, ancla y resultado— no
quedarán artificialmente ligadas a Hotmart. Sin embargo, la primera implementación
cubrirá únicamente:

```text
abandono de carrito
→ grace period
→ primer contacto durable
→ espera de respuesta
→ follow-ups por falta de respuesta
→ terminación
```

No se implementarán inicialmente tareas humanas, DAGs, DSLs, workflows genéricos,
Temporal, Celery/RQ ni cronjobs por lead.

### 5. Versionar y aprobar las políticas

El Automation Expert puede proponer políticas. La aplicación valida su estructura
y una persona debe aprobarlas antes de publicarlas. Una versión publicada es
inmutable; editarla crea una versión nueva.

Las secuencias existentes continúan con la versión que las originó. No migran
silenciosamente cuando se publica otra versión.

La primera política efímera de prueba será:

- grace period: 1 hora;
- follow-up 1: 24 horas después del contacto anterior aceptado por Chatwoot;
- follow-up 2: 72 horas después del contacto anterior aceptado por Chatwoot;
- máximo: 3 mensajes automáticos incluyendo el primero;
- horario: lunes a sábado, 09:00–19:00 en la zona horaria del negocio;
- expiración: 7 días desde el abandono.

Estos valores son configuración versionada, no constantes del motor.

### 6. Definir vigencia mediante anclas

Cada acción declara qué hecho la mantiene vigente. Un seguimiento por falta de
respuesta se ancla al mensaje saliente aceptado por Chatwoot y sólo sigue vigente
si no existe una respuesta posterior.

Una respuesta posterior al ancla cancela la acción pendiente y completa la
secuencia `no_reply`; no cierra el caso comercial. Una nueva intención explícita
puede originar otra secuencia.

Una compra confirmada cancela las acciones pendientes y gana el caso. Sólo un
evento Hotmart autenticado, deduplicado y correlacionado inequívocamente puede
confirmarla automáticamente. Un reembolso o disputa posterior no revive la
secuencia anterior.

### 7. Aplicar takeover sin resurrección

La intervención humana pausa la automatización y cancela la planificación
vigente. Quitar la señal de takeover no resucita acciones. Una reanudación
explícita debe reconstruir el contexto y crear planificación nueva. Esta decisión
aplica ADR-0002 al motor.

### 8. Tratar el permiso de contacto como estado explícito

La ausencia de opt-out no autoriza por sí sola un mensaje proactivo. Supabase
conservará autorización o base válida por canal y propósito, con fuente y tiempo
mínimos. Un estado bloqueado cancela; un estado desconocido falla cerrado y
deriva.

El cliente es responsable de configurar una base válida para su operación y
jurisdicción. La aprobación de un template por Meta no demuestra autorización
para contactar a una persona determinada.

### 9. Mantener Chatwoot como frontera de canal y preparar WABA

Chatwoot continúa siendo la frontera de mensajería para Evolution y futura WABA.
Antes de invocar Hermes, el bridge determina la modalidad permitida:

```text
ventana abierta  → Hermes puede proponer texto libre
ventana cerrada  → sólo template aprobado con placeholders permitidos
```

Si no existe una modalidad válida, no se envía. Esta sección supersede el
contrato único de template descrito en ADR-0004, pero conserva Chatwoot como
abstracción de canal. WABA no se implementará hasta disponer de inbox y templates
reales.

### 10. Usar claims recuperables y operaciones transaccionales

La creación de caso, secuencia y primera acción será una única operación
transaccional e idempotente en Postgres. Los workers reclamarán acciones vencidas
mediante claims con lease y token de generación. Un claim abandonado puede
recuperarse al expirar; un worker con token obsoleto no puede confirmar cambios
en Postgres.

Para el primer despliegue alcanza un worker y una operación transaccional de
claim. El lease se conserva desde el inicio para permitir recuperación ante
caídas y futura concurrencia sin cambiar el modelo.

### 11. Reconocer los límites de idempotencia externa

Cada efecto saliente tendrá una clave estable y un intento durable. La unicidad
interna impide crear dos intentos lógicos iguales en Postgres, pero Chatwoot no
ofrece una garantía transaccional compartida con Supabase.

Si Chatwoot acepta un mensaje, el estado interno será `accepted_by_chatwoot`; no
se afirmará todavía entrega o lectura. Si el request termina de manera ambigua, el
intento pasa a `delivery_unknown`. No se reenvía automáticamente: primero se
reconcilia mediante un marcador y una ventana acotada de búsqueda. Si no puede
probarse aplicado o no aplicado, se escala.

### 12. Diferir el arbitraje global entre casos

Cada caso tendrá una secuencia activa y una próxima acción. El MVP no incorporará
ranking, combinación ni límites globales sofisticados entre casos de la misma
persona. Se mantienen las guardas actuales ante otro caso abierto claramente
incompatible. El arbitraje global se reconsiderará sólo con evidencia operativa
de solapamientos.

Los abandonos repetidos del mismo producto y oferta se agrupan mientras el caso
permanezca abierto. Actualizan el contexto y fuerzan reevaluación sin iniciar una
cadencia paralela. Otro producto/oferta o un caso terminal pueden originar un caso
nuevo.

### 13. Auditar hechos sin registrar PII innecesaria

Las transiciones relevantes producirán eventos estructurados append-only con IDs
internos, tipo, actor, tiempo, código de razón, política y versiones. No se
registrarán cuerpos completos, teléfonos, emails, nombres, credenciales ni
contenido de mensajes en logs de aplicación.

## Estados y resultados mínimos

Una reevaluación puede producir:

```text
execute
defer
replace
cancel
skip
pause
expire
escalate
```

Las acciones terminales distinguen como mínimo:

```text
accepted_by_chatwoot
cancelled
skipped
expired
permanent_failed
superseded
```

Los resultados `deferred`, `retryable_failed` y `delivery_unknown` son
recuperables. Conservan la misma acción, intención e idempotency key.
`delivery_unknown` bloquea el retry automático y el avance de la secuencia hasta
que se reconcilie como aceptado, se demuestre no aplicado o se escale.

## Consecuencias

### Positivas

- Las esperas sobreviven a reinicios y despliegues.
- Primer contacto y follow-ups convergen en las mismas guardas.
- Las políticas son configurables, revisables y reproducibles.
- Cada acción se reevalúa lo más cerca posible del efecto para reducir el uso de
  contexto obsoleto.
- El motor tolera procesamiento al menos una vez sin prometer exactly-once.
- Hermes conserva valor comercial sin recibir autoridad operativa irrestricta.

### Costos

- El primer contacto validado deberá migrarse a una orquestación durable.
- Se necesitan operaciones transaccionales, dispatcher y reconciliación.
- La compra requiere investigar y validar el contrato real de Hotmart.
- El permiso de contacto requiere modelado y evidencia por cliente.
- La incertidumbre externa puede exigir intervención manual.

## Alternativas descartadas

### Mantener el primer contacto fuera del motor permanentemente

Se descarta como estado objetivo porque duplica guardas, idempotencia y auditoría.

### Materializar todos los pasos futuros

Se descarta porque aumenta cancelaciones, divergencia y mantenimiento.

### Cronjob por persona o seguimiento

Se descarta porque el scheduler no debe ser fuente de verdad ni contener PII o
lógica comercial.

### Exactly-once entre Postgres y Chatwoot

Se descarta como garantía imposible sin una transacción distribuida compartida.

### Workflow engine genérico desde el comienzo

Se descarta por sobreingeniería para el alcance single-tenant y los casos actuales.

## Aspectos diferidos que no bloquean el MVP

- arbitraje global entre casos;
- límites globales sofisticados por contacto;
- migración automática de secuencias entre versiones;
- WABA hasta disponer de infraestructura real;
- múltiples pollers hasta justificarlo por carga;
- otros casos de uso fuera de recuperación de carrito.

## Prerrequisitos antes de implementar

1. Incorporar al repositorio un baseline sanitizado del esquema Supabase actual.
2. Validar el contrato real de compra Hotmart, sus identificadores, correlación y
   eventos de reembolso, disputa o reversión.
3. Definir el modelo físico de autorización por canal y propósito.
4. Diseñar las operaciones RPC transaccionales y sus invariantes.
5. Definir reconciliación acotada contra las capacidades reales de Chatwoot.
6. Mantener `ALLOWED_WHATSAPP_JID` durante la validación E2E inicial.

## Documentos relacionados

- [Contrato V1 del motor](../contracts/followup-engine-v1.md)
- [Diseño detallado aprobado](../design/followup-engine.md)
- [ADR-0001: frontera del profile comercial](0001-commercial-profile-boundary.md)
- [ADR-0002: human takeover](0002-human-takeover-detection.md)
- [ADR-0003: frontera determinística](0003-deterministic-reasoning-boundary.md)
- [ADR-0004: mensajería](0004-messaging-layer-abstraction.md)
- [ADR-0005: empaquetado reproducible](0005-reproducible-client-deployments.md)
- [ADR-0006: tres agentes](0006-three-agent-product-surface.md)
