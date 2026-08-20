# Intención pre-checkout y correlación de compra de Joana — V1

- **Estado:** Parcialmente implementada; admisión observada en Cloud y correlación Hotmart implementada/verificada localmente, E2E oficial pendiente
- **Fecha:** 2026-08-14
- **Alcance:** formulario intermedio → intención durable → observación Hotmart → clasificación fail-closed
- **Oferta:** `Libre de Ansiedad`
- **No implica:** API de consulta disponible, eventos de fallo observados, E2E oficial de correlación ni contacto general autorizado
- **Evidencia visual:** captura suministrada por el usuario el 2026-08-14, preservada fuera de Git; SHA-256 `e3e263c32ff6ea3f5e114891bf0adc9e38ffa4063a810d15d5948427a807dab4`
- **Relacionado con:** [tracer del app setter](joana-app-setter-pilot-v1.md), [readiness WABA/Hotmart](lancemos-waba-hotmart-readiness.md), [compra aprobada implementada](../contracts/hotmart-purchase-approved-v1.md)

## 1. Cambio de modelo

El formulario intermedio no representa un carrito abandonado ni un error de pago. Representa
una **intención de compra identificable** anterior al checkout de Hotmart:

```text
landing/BCL
→ formulario intermedio
→ purchase_intent durable
→ checkout Hotmart
→ observación y correlación
→ compra | fallo explícito | abandono candidato | pendiente/desconocido
```

El efecto externo nunca se dispara directamente al enviar el formulario. El adapter observado
ya admite una intención durable y la correlación local consume sólo eventos autoritativos
Hotmart. Ninguno de esos cortes programa reevaluación ni outbound. Un corte posterior podrá
programar una reevaluación después de un plazo `X` aprobado.

Abandono y fallo de pago comparten esta intención de origen, pero conservan mensajes,
políticas y gates diferentes. Una misma intención no puede abrir simultáneamente ambos casos.

## 2. Responsabilidades

### Formulario/landing

Debe capturar el mínimo necesario para:

- identificar la submission de forma estable;
- conocer producto/oferta y origen;
- correlacionar posteriormente con Hotmart;
- conocer el teléfono de WhatsApp;
- preservar evidencia de opt-in específico para contacto proactivo;
- propagar, si el checkout/proveedor lo permite y queda demostrado, un identificador opaco de
  correlación que no dependa de PII.

El formulario no decide abandono, fallo, permiso final ni copy.

### Bridge y Postgres

Poseen:

- autenticación y admisión durable;
- idempotencia de submission y eventos posteriores;
- normalización e identidad;
- correlación determinística;
- estado de la intención;
- deadline de reevaluación;
- clasificación fail-closed;
- exclusión mutua entre outcomes;
- autorización y supresión pre-efecto;
- auditoría sanitizada.

### Hotmart

Es fuente de hechos de checkout que realmente exponga y que hayan sido observados/contratados.
`PURCHASE_APPROVED` puede confirmar compra. Un fallo de pago sólo se clasifica si existe un
evento/estado oficial con causa soportada. La mera ausencia de un evento no demuestra una
causa.

### Agente comercial

Recibe un caso ya clasificado y autorizado. No correlaciona personas, no decide si hubo
compra, no infiere fallo desde silencio y no convierte incertidumbre en permiso para escribir.

## 3. Agregado durable propuesto

La unidad de trazabilidad es `purchase_intent`, no el webhook aislado ni el contacto:

```text
purchase_intent_id
submission_ref
customer_scope
website_ref
product_ref
intended_offer_ref
submitted_at
observation_due_at
identity_ref
whatsapp_identity_ref
consent_ref
status
classification_reason
matched_purchase_event_ref
matched_failure_event_ref
correlation_outcome
policy_ref/version
```

Los nombres anteriores conservan valor conceptual. El schema físico de admisión y
correlación ya está implementado en `purchase_intents`,
`hotmart_purchase_intent_scopes`, `hotmart_purchase_intent_event_identities`,
`hotmart_purchase_intent_correlations` y
`hotmart_purchase_intent_correlation_candidates`; se documenta en el
[contrato de correlación V1](../contracts/hotmart-purchase-intent-correlation-v1.md).

### Lifecycle y clasificación conceptual

```text
lifecycle_status:
admitted → observing → recovery_eligible → outbound_started
                         ↘ purchased | cancelled | paused_unknown

current_classification:
none | payment_failure_supported | abandonment_candidate
| identity_conflict | tracking_incomplete | expired_unknown
```

La clasificación vigente es mutuamente excluyente y su historial es append-only. No es el
estado terminal de autoridad: `PURCHASE_APPROVED` puede superseder monotonamente
`payment_failure_supported` o `abandonment_candidate` y mover el lifecycle a `purchased`.
Nunca se borra la clasificación anterior ni se reescribe un efecto empezado como si no hubiera
ocurrido. `purchased` y `cancelled` son terminales para nuevos efectos comerciales.

## 4. Datos mínimos a confirmar

### Observación visual sanitizada del formulario

La captura confirma solamente esta superficie visible:

- encabezados `ÚLTIMO PASO`, `¡Estás a un paso de empezar!` y
  `Completa tu información`;
- campos marcados con asterisco `Nombre Completo`, `Teléfono` y `Correo`;
- teléfono con selector visual de país/prefijo;
- placeholder de correo `your@email.com`;
- checkbox con el texto: “Acepto los Términos y Condiciones y autorizo el tratamiento de mis
  datos personales conforme a la Política de Privacidad de El Protocolo.”;
- CTA `Enviar`;
- leyenda visual “Tu información está segura y protegida”.

La captura contiene valores personales y permanece fuera de Git. Esos valores no son hechos
de producto ni se copian en logs o fixtures.

### Hallazgos y límites de esa evidencia

1. El formulario ofrece nombre, teléfono y correo como candidatos de identidad.
2. El texto visible autoriza tratamiento general de datos, pero no menciona contacto
   comercial, WhatsApp, finalidad de recuperación, vigencia ni revocación. La captura no
   demuestra opt-in suficiente para B/C; debe revisarse el texto y las políticas enlazadas.
3. La captura muestra una combinación visualmente inconsistente entre bandera y prefijo del
   teléfono. El backend no debe confiar en la presentación del widget: debe recibir país y
   número de forma coherente, normalizar canónicamente y bloquear mismatch/ambigüedad.
4. No se observa submission ID, website/product/offer ref, correlation ID, timestamp, policy
   version ni hidden input. Podrían existir, pero la imagen no lo demuestra.
5. No se puede inferir si el checkbox es obligatorio, si los links son navegables, qué valida
   cliente/servidor, qué persiste el backend ni qué datos se propagan a Hotmart.
6. La leyenda de seguridad es copy de interfaz; no prueba cifrado, custodia, retención,
   controles de acceso ni cumplimiento.

### Submission

- ID estable generado por el servidor, no por el navegador solamente;
- timestamp del servidor;
- website/landing/BCL de origen;
- product/offer refs canónicos;
- nombre sólo si el negocio lo necesita;
- email normalizable;
- teléfono con país y evidencia de que es WhatsApp contactable;
- texto/versión de consentimiento, finalidad, canal y timestamp;
- policy/version aplicable;
- identificador de sesión/click opaco si puede propagarse con seguridad;
- prueba de autenticidad del submit o del backend que lo reenvía.

Además de inspeccionar el payload, el E2E del formulario debe probar que país + teléfono
producen un valor canónico único o un rechazo explícito; nunca corregir silenciosamente una
combinación incompatible.

### Hotmart

- `PURCHASE_APPROVED` V2 observado/oficial;
- identidad que Hotmart entrega realmente —email y teléfono pueden ser parciales—;
- product ID y offer code;
- timestamp de aprobación y transaction ref;
- eventos/estados reales de fallo o rechazo;
- mecanismo autoritativo para consultar compra si existe;
- comportamiento ante webhook desactivado, atrasado, repetido o ausente.

No se usarán nombre aproximado, IP, orden de llegada ni similitud textual como evidencia de
identidad.

## 5. Correlación determinística

### Preferencia

La correlación más fuerte sería un ID opaco de la intención propagado hasta el checkout y
recibido de vuelta por Hotmart. No se asume que Hotmart lo soporte: debe demostrarse con el
flujo real.

Si no existe ese ID, la correlación candidata se construye con:

```text
customer_scope
+ product_ref
+ offer_ref compatible
+ identidad exacta normalizada
+ ventana temporal
```

La identidad exacta puede usar email y/o teléfono sólo conforme al contrato observado.
Reglas mínimas:

1. email y teléfono se normalizan por separado;
2. si ambos existen y apuntan a contactos diferentes, outcome `identity_conflict`;
3. no se elige email sobre teléfono ni viceversa por precedencia silenciosa;
4. una compra se atribuye automáticamente sólo a una intención candidata inequívoca;
5. dos intenciones candidatas, dos contactos posibles o una oferta incompatible bloquean la
   atribución y pausan todos los efectos pendientes de los candidatos;
6. teléfono capturado no equivale a JID/canal seleccionado;
7. no se crea un destinatario outbound mediante fuzzy matching;
8. toda decisión conserva referencias a la submission y evidencia Hotmart utilizada.

## 6. Reevaluación después de `X`

`observation_due_at` significa “volver a decidir”, no “enviar”. En ese momento el resolver
vuelve a leer hechos actuales y aplica esta precedencia:

| Evidencia | Outcome | Efecto comercial |
|---|---|---|
| compra aprobada correlacionada | `purchased` | cancelar/cerrar; cero contacto de recuperación |
| evento de fallo soportado correlacionado | `payment_failure_supported` | caso C sólo si opt-in, template y demás gates pasan |
| no compra con observación completa y política aprobada | `abandonment_candidate` | caso B sólo si opt-in, template y demás gates pasan |
| identidad conflictiva o varias intenciones candidatas | `identity_conflict` | cero contacto; revisión humana |
| fuente caída, backlog, webhook desactivado o cobertura incierta | `tracking_incomplete` | cero contacto; incidente operativo |
| no existe forma autoritativa de probar la negativa | `expired_unknown` | cero contacto; medir como bloqueado |

Un evento tardío de compra supersede la clasificación y gana sobre una recuperación todavía
no iniciada. Inmediatamente antes de `request_started`, el sistema vuelve a comprobar compra,
opt-out, takeover, consentimiento, identidad y lifecycle de la intención. Si el request ya
comenzó, se conserva la evidencia como efecto externo incierto/reconciliable y se suprimen
sucesores; no se declara que el mensaje no fue enviado.

## 7. Qué significa “no compró”

No observar `PURCHASE_APPROVED` no basta. La salud del ingreso es necesaria pero no suficiente.
Para declarar `abandonment_candidate` se requiere además una de estas autoridades:

1. consulta autoritativa que confirme la negativa para la identidad/producto/oferta; o
2. contrato del proveedor demostrado como exhaustivo para esa ventana y esos hechos.

Sólo después se evalúan condiciones operativas de **observación completa**:

- fuente de compras activa y autenticada;
- ventana de latencia/reintentos del proveedor vencida;
- backlog durable drenado y sin eventos retryable;
- ningún incidente de tracking vigente;
- correlación capaz de encontrar la compra aunque Hotmart omita alguno de los identificadores.

Si el único mecanismo disponible es “no llegó un webhook”, el outcome obligatorio es
`expired_unknown`, B permanece inactivo y se permite cero contacto. Ese límite no se levanta
por aceptación de riesgo comercial. El tracking roto mencionado en discovery obliga a fallar
cerrado, no a aumentar mensajes.

## 8. Consentimiento e identidad de canal

Enviar el formulario no implica automáticamente permiso de WhatsApp. El piloto necesita una
decisión aprobada sobre:

- copy exacto del opt-in;
- finalidad: asistencia sobre esta intención de compra;
- canal: WhatsApp;
- vigencia y revocación;
- vínculo entre evidencia de consentimiento e intención;
- tratamiento de teléfono sin JID canónico;
- retención y eliminación de PII.

La ausencia de opt-out no equivale a opt-in. Una autorización positiva nunca pisa
`denied`, `restricted`, opt-out o takeover vigentes.

## 9. Idempotencia y repetición

Se requieren claves distintas:

- **ingress:** una submission se admite una sola vez por `submission_ref`;
- **evento Hotmart:** una delivery se admite una sola vez por ID externo verificado;
- **semántica:** el mismo hecho bajo otra delivery no cambia de outcome;
- **clasificación:** una intención sólo tiene una clasificación vigente, con historial
  append-only y supersession monotónica por compra;
- **efecto:** como máximo un first-touch por `purchase_intent + first_touch`; `case_kind`,
  `policy_version` y release son parámetros inmutables del intento, no parte que reinicie la
  identidad del efecto;
- **seguimientos:** cada mensaje posterior requiere `effect_kind/ordinal` separado y nueva
  autorización;
- **reintento del usuario:** una submission nueva del mismo scope/contacto/producto/oferta se
  anexa como evidencia a la intención viva o la supersede según policy versionada; nunca deja
  dos intenciones correlacionables vivas para esa tupla.

Si la identidad todavía no permite decidir si dos submissions pertenecen a la misma tupla,
ambas quedan pendientes y todos sus efectos se bloquean. Una compra ambigua también pausa o
cancela preventivamente todos los efectos pendientes de los candidatos aunque su atribución
final siga sin resolverse.

Un mismo ID con payload de negocio diferente crea conflicto durable; no se oculta como
`duplicate`.

## 10. Métricas atribuibles

La intención permite un funnel mínimo medible:

```text
landing/BCL visto —si existe tracking autorizado—
→ formulario enviado
→ checkout iniciado
→ compra aprobada / fallo soportado / abandono candidato / unknown
→ mensaje autorizado
→ respuesta
→ compra posterior / handoff / opt-out / cierre
```

Métricas V1:

- submissions admitidas y duplicadas;
- compras correlacionadas;
- fallos correlacionados por causa soportada;
- abandonos candidatos;
- identidad conflictiva;
- tracking incompleto/unknown;
- contactos bloqueados por falta de opt-in;
- mensajes autorizados, respuestas y compras posteriores;
- compras que llegaron después del deadline pero antes del request;
- duplicados evitados.

No se atribuye una compra al agente sólo porque ocurrió después de un mensaje. Registrar
`assisted_after_message` como señal observacional y reservar causalidad para un diseño de
medición posterior.

## 11. Gates antes de la activación general

1. ~~capturar el payload real del formulario sin PII en Git~~ — contrato observado y fixtures sanitizados disponibles;
2. ~~definir autenticación, replay window y ownership del formulario~~ — HMAC server-side y bridge implementados;
3. confirmar campos exactos y copy/version de opt-in;
4. comprobar si un correlation ID puede viajar hasta y desde Hotmart;
5. ~~identificar la fuente autoritativa para compra y abandono~~ —
   `PURCHASE_APPROVED` y `PURCHASE_OUT_OF_SHOPPING_CART`; pago rechazado sigue abierto;
6. observar/confirmar eventos de fallo y su catálogo de causas;
7. ~~definir normalización y reglas de identidad con fixtures conflictivos~~ — implementado y probado localmente;
8. aprobar `X`, expiración, lineage de submissions repetidas y si una repetición conserva o
   modifica el deadline, sin reiniciar autorización ni first-touch;
9. definir retención, acceso y borrado de PII;
10. ~~reemplazar el adapter emulado mediante contratos observados y TDD~~ — adapter
    `lead.precheckout` observado desplegado; correlación Hotmart todavía no aplicada en Cloud.

Hasta completar los gates abiertos y el E2E oficial, el estado general sigue
`partial / no effects`. Existen admisión observada durable y correlación fail-closed local,
pero no autorización comercial derivada.

## 12. Impacto en el roadmap

El roadmap queda más preciso:

```text
A. inbound comercial — independiente del formulario

B/C. formulario intermedio
→ intención durable
→ correlación de identidad
→ observación después de X
→ purchased | failure_supported | abandonment_candidate | unknown
→ autorización por caso
→ outbound controlado
```

El siguiente trabajo de producto para B/C no es escribir templates ni programar el timer. Es
aplicar la correlación en Cloud, demostrar un evento Hotmart fresco contra una intención
observada y resolver el opt-in específico. La ausencia de compra no se considera evidencia.