# Registro operativo: recuperación de carrito Hotmart E2E

- **Fecha:** 2026-08-02
- **Estado:** validado mediante ejecución real
- **Alcance:** abandono de carrito Hotmart → primer contacto por WhatsApp → respuesta atendida por el mismo agente comercial

## Resumen

Quedó implementado y validado en producción el flujo integral de recuperación de carritos abandonados. El bridge recibe y autentica un webhook compatible con Hotmart v2.0.0, persiste el evento, resuelve identidad y contexto comercial, determina de forma autoritativa si corresponde iniciar el contacto y solicita a Hermes únicamente la redacción del mensaje.

Cuando la decisión requerida es `send_first_touch / first_touch`, el bridge busca y reutiliza de forma segura el contacto de Chatwoot o lo crea si no existe, abre una conversación y publica el primer mensaje mediante el inbox conectado a Evolution. La persona puede responder desde el WhatsApp autorizado y la conversación continúa mediante el mismo profile `agente-comercial`.

La prueba E2E real confirmó la recepción del primer mensaje y una respuesta posterior del agente. Los artefactos de prueba fueron eliminados de Supabase y Chatwoot al finalizar.

## Flujo validado

```text
Hotmart compatible v2.0.0
→ POST /webhooks/hotmart
→ autenticación por X-HOTMART-HOTTOK
→ validación, deduplicación y persistencia en Supabase
→ worker diferido
→ resolución determinística de identidad y contexto
→ SituationReport
→ decisión autoritativa del bridge
→ Hermes redacta bajo una política cerrada
→ validación independiente de la propuesta
→ autorización final del worker
→ búsqueda o creación del contacto en Chatwoot
→ creación de conversación
→ mensaje saliente por Chatwoot/Evolution
→ WhatsApp autorizado
→ respuesta entrante por Chatwoot
→ bridge
→ Hermes / agente-comercial
→ respuesta en el mismo WhatsApp
```

## Frontera de razonamiento

Se aplicó la separación definida en [ADR-0003](../decisions/0003-deterministic-reasoning-boundary.md):

- Supabase y el bridge poseen los hechos y las escrituras.
- El bridge calcula las guardas y la decisión comercial requerida.
- Hermes recibe un `SituationReport` estructurado y reglas de redacción.
- Hermes no accede directamente a Supabase.
- La propuesta sólo es válida si coincide exactamente con la acción y el `reason_code` requeridos.
- El worker recalcula la decisión antes de invocar la mensajería.
- Contexto ausente, fallido, incompleto o malformado falla de forma cerrada.

## Frontera de mensajería

Se aplicó la abstracción definida en [ADR-0004](../decisions/0004-messaging-layer-abstraction.md):

- Chatwoot es el gateway del canal.
- Evolution es el transporte actual.
- El bridge no llama directamente a Evolution ni a Meta.
- La migración futura a WABA conservará esta frontera.
- Los inicios proactivos por WABA deberán seleccionar templates aprobados cuando corresponda.

## Invariantes de seguridad confirmados

- Supabase usa exclusivamente `SUPABASE_SERVICE_ROLE_KEY` en el backend; no existe fallback silencioso a una clave anon.
- Tokens, firmas, claves, payloads completos, teléfonos, emails y contenido de mensajes no se escriben en logs de aplicación.
- La decisión de enviar es determinística y no puede ser relajada por una salida del modelo.
- Human takeover, contacto bloqueado, `do_not_contact`, conversación activa, caso abierto o contexto insuficiente impiden el primer contacto según la política cerrada.
- El caso actual no se cuenta como un caso previo abierto.
- Casos cancelados, cerrados o perdidos no bloquean un intento nuevo.
- Lookups HTTP 200 con estructura o campos inválidos fallan cerrados.
- El parser de contactos de Chatwoot acepta únicamente IDs enteros positivos y no booleanos.
- La búsqueda de contactos exige teléfono normalizado exacto, vínculo con el inbox objetivo y ausencia de bloqueo.
- Múltiples contactos exactos distintos se consideran ambiguos; no se elige uno arbitrariamente.
- Un contacto existente se reutiliza antes de intentar `POST /contacts`.
- Los errores HTTP de Chatwoot se convierten en códigos estables sin URL, query ni PII.
- `ALLOWED_WHATSAPP_JID` se aplica tanto en el worker como en el sender Evolution.
- Sólo se acepta un JID individual canónico con forma `dígitos@s.whatsapp.net`.
- Un teléfono diferente al JID autorizado se bloquea antes de cualquier llamada a Chatwoot.
- La entrada Hotmart rechaza letras, `@`, sufijos JID u otro contenido ajeno a un formato telefónico permitido antes de normalizar.

## Verificación realizada

### Pruebas automatizadas

Al cierre de la implementación:

```text
182 passed
```

También se verificó:

- `git diff --check` exitoso;
- pruebas unitarias de autenticación, anti-replay y deduplicación;
- validación estructural fail-closed de Supabase;
- equivalencia entre política declarativa y decisión ejecutable;
- guardia final del worker;
- búsqueda, reutilización y creación de contactos Chatwoot;
- bloqueo saliente del teléfono no autorizado;
- rechazo de JIDs y teléfonos crudos malformados;
- E2E automatizado con transportes HTTP controlados;
- revisiones independientes de las fronteras de decisión, contacto y autorización saliente.

### Verificación HTTP y E2E real

La ejecución real confirmó:

- `/health` del bridge respondió HTTP 200 después de cada redeploy relevante;
- un hottok incorrecto devolvió HTTP 401;
- el webhook final fue aceptado con HTTP 202;
- el evento final terminó `processed` sin error;
- el caso de recuperación fue creado;
- Hermes produjo una propuesta compatible con la decisión requerida;
- Chatwoot aceptó la conversación y el mensaje saliente;
- el primer mensaje llegó al WhatsApp autorizado;
- una respuesta enviada desde ese WhatsApp activó el webhook entrante;
- el mismo agente comercial respondió correctamente en la conversación.

El evento técnico usado para la validación exitosa fue `evt-e2e-authorized-1785697248954`. No se preservan en este documento el número autorizado, el contenido de los mensajes, nombres, emails, credenciales ni payloads completos.

## Incidentes encontrados y correcciones

### RLS bloqueaba la persistencia

El primer intento real devolvió `webhook_persist_unavailable`. Se confirmó que RLS impedía escribir con anon. El backend se migró a service role exclusivamente server-side, sin crear una policy abierta ni exponer credenciales.

### Identidad marcada como resuelta prematuramente

El worker intentaba crear un caso con identidad `resolved` sin identidad de canal seleccionada. La resolución fue corregida para permanecer `pending` hasta satisfacer el contrato de datos.

### Propuestas incompatibles de Hermes

Hermes podía producir acciones permitidas con `reason_code` no autorizado porque no recibía una política cerrada. El bridge pasó a calcular `required_recovery_decision`, enviar esa decisión como requisito y rechazar cualquier contradicción. La skill y el contrato del profile quedaron alineados, pero la autoridad permanece en código.

### Contexto autoritativo incompleto

Errores o respuestas malformadas de Supabase podían parecer resultados vacíos válidos. Los lookups ahora validan documento, filas, tipos, campos y enums; cualquier incertidumbre relevante exige `handoff / insufficient_context`.

### Respuesta real de creación de contacto

Chatwoot puede devolver el contacto creado dentro de una lista `payload`. El parser esperaba un objeto y reportaba `invalid_contact_id`. Ahora acepta las formas observadas y conserva validación estricta de IDs.

### Creación duplicada de contacto

Después de que un intento anterior creara el contacto, un reintento recibió HTTP 422 al ejecutar nuevamente `POST /contacts`. Se agregó búsqueda previa, comparación local exacta, vínculo obligatorio con el inbox y manejo explícito de ambigüedad y contactos bloqueados.

### Prueba enviada a un número ficticio

Una prueba manual inicial usó un teléfono ficticio. Chatwoot creó la conversación y Evolution informó que el destino no era un WhatsApp válido. Esto reveló que la restricción del JID estaba aplicada al flujo entrante pero no como invariante independiente del primer envío.

Se corrigió con validación en dos capas: el worker bloquea cualquier destino no autorizado incluso con un sender inyectado, y `EvolutionMessageSender` repite la comprobación antes de Chatwoot. La prueba final derivó internamente el teléfono desde `ALLOWED_WHATSAPP_JID` sin imprimirlo y fue exitosa.

## Limpieza posterior

Después de la validación se eliminaron:

- eventos manuales, diagnósticos y E2E de Supabase;
- casos de recuperación asociados;
- contactos internos y puntos de contacto creados por las pruebas;
- conversaciones de prueba en Chatwoot;
- el contacto Chatwoot asociado al número ficticio.

La consulta final confirmó:

```text
eventos de prueba restantes: 0
casos de prueba restantes: 0
contactos internos de prueba restantes: 0
```

El contacto real autorizado no fue eliminado cuando Chatwoot lo reutilizó.

## Cambios publicados durante la validación

Los principales commits que llevaron al flujo validado fueron:

```text
522635c  pipeline inicial Hotmart → Supabase → worker → Hermes → Chatwoot
6d7f9fa  service role exclusivamente server-side
3e9442c  identidad pendiente hasta seleccionar canal
ec2520c  diagnóstico seguro del pipeline
20afddf  exclusión del caso actual en guardas
f47d54b  diagnóstico seguro de propuestas inválidas
bcffb16  decisiones determinísticas y contexto fail-closed
38e530a  parser de respuesta real de contactos Chatwoot
c31b6c3  búsqueda y reutilización de contactos existentes
11a303d  autorización saliente por JID en dos capas
```

## Deuda conocida no bloqueante

La validación E2E está completa, pero permanecen mejoras operativas y de producto:

- idempotencia fuerte del primer mensaje ante carreras o reintentos después de aceptación remota;
- modelado explícito de propuesta, creación de contacto, conversación, mensaje aceptado y entrega efectiva;
- actualización completa del estado de `recovery_case` durante hold, abort, respuesta y cierre;
- watchdog y alertas para eventos procesados sin avance posterior;
- reconciliación segura de conflictos de creación concurrente en Chatwoot;
- selección real de templates aprobados al migrar a WABA;
- políticas de retención y limpieza automatizada de datos de prueba.

## Documentación relacionada

- [Arquitectura](../architecture.md)
- [Webhook de Chatwoot](../chatwoot-webhook.md)
- [ADR-0001: frontera del profile comercial](../decisions/0001-commercial-profile-boundary.md)
- [ADR-0002: detección de human takeover](../decisions/0002-human-takeover-detection.md)
- [ADR-0003: frontera determinística](../decisions/0003-deterministic-reasoning-boundary.md)
- [ADR-0004: abstracción de mensajería](../decisions/0004-messaging-layer-abstraction.md)
- [Contrato observado de Chatwoot](../research/chatwoot-observed-contract.md)
- [Registro operativo anterior](./2026-07-31-production-readiness.md)
