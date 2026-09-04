# ATT1 — aprobación de información comercial V1

- **Estado:** Product ID confirmado y evidencia GHL registrada; ratificación general de Marcela, materiales, template y ejecución del descuento pendientes
- **Gate:** Estado: Pendiente de aprobación externa
- **Alcance:** materiales, contenido comercial, límites sanitarios, idioma, países y descuento
- **Fuentes:** [registro ATT1 V1](att1-source-register-v1.md), `operator-confirmation:2026-09-02-authority-content-health-language-countries`, `operator-confirmation:2026-09-02-discount`, `operator-confirmation:2026-09-04-hotmart-product-id`, corpus GHL sanitizado y `Documentación de Procesos Carritos Abandonados, Pagos Declinados y Pagos Offline.pdf`
- **No implica:** Conversation Release aprobada, templates aprobados, configuración de proveedores, despliegue ni autorización para contactar personas reales

## Regla de cierre

Esta macro tarea sólo queda aprobada cuando la autoridad comercial identificada ratifica por escrito contenido, límites sanitarios, idioma y países y, para los materiales, existe recepción y sanitización verificadas. La confirmación del operador del 2026-09-02 identifica a Marcela como autoridad y confirma esos valores candidatos; no constituye una respuesta ni ratificación directa de Marcela. La aprobación de Marcela sobre el descuento fue reportada posteriormente por el operador: la decisión comercial del descuento ya no está abierta, aunque su plantilla, mapeo de variable, soporte runtime y publicación continúan pendientes. La ratificación comercial general y los materiales mantienen el gate `pending_external_approval`.

## Información recibida y confirmada por el operador, pendiente de ratificación de Marcela

- oferta candidata confirmada por el operador: **Alimenta Tu Tiroides**;
- identidad pública candidata confirmada por el operador: **Dra. Nina Garza**;
- precio base candidato confirmado por el operador: **USD 47**;
- outcome candidato confirmado por el operador: compra autoritativa observada;
- audiencia reportada: mujeres de 35 a 55 años, principalmente trabajadoras o emprendedoras con ingresos propios, con diagnóstico reportado de hipotiroidismo o Hashimoto;
- distribución de audiencia reportada: México 60 %, Estados Unidos 15 %, Colombia 10 %, Canadá 3 % y España 3 %;
- Mariana como candidata reportada para recibir handoffs;
- disponibilidad declarada de materiales de autoridad, oferta, audiencia, copy, páginas, ads y ofertas posteriores.

Los porcentajes geográficos suman 91 %. No se infiere el alcance geográfico del piloto ni el 9 % restante.

## Autoridad técnica UC-01 registrada

- Product ID Hotmart **`5071808`**: confirmado directamente por el operador.
- checkout/hotlink **`D98014973Y`**: observado de forma dominante en GHL; pendiente de ratificación como checkout canónico.
- offer code **`83utgyow`**: candidato dominante observado en GHL; no aprobado como binding productivo.
- landing **`raizana.com.mx/inscribirme-alimenta-tu-tiroides`**: candidata dominante observada en GHL; no acredita consentimiento ni vigencia.
- cupón **`SOYRAIZANA10`**: candidato histórico dominante observado en GHL; no se publica ni se entrega hasta aprobar template, variable y política durable exacta.

La evidencia ampliada cubre 768 conversaciones y 4.097 mensajes sanitizados. No sustituye autoridad de catálogo: carece de timestamps utilizables, mezcla campañas y mantiene pendiente la revisión humana de posibles nombres libres.

## Decisiones requeridas

### `att1-commercial-001-materials` — recepción y permiso de uso

**Estado:** pendiente; no se recibieron materiales ATT1 sanitizados.

Se necesita entregar por un canal privado aprobado el conjunto vigente que se autoriza usar:

1. historia y autoridad de la aliada;
2. transformación, temario, precio, order bumps y condiciones de la oferta;
3. perfil detallado de audiencia;
4. copy, promesa, pilares y mensajes;
5. páginas web y ads vigentes;
6. upsells y ofertas posteriores aplicables.

Cada entrega debe identificar owner, fecha o versión, permiso de uso y vigencia. Antes de incorporar contenido debe pasar custodia privada, escaneo de secretos/PII, revisión de cobertura y sanitización. Hasta entonces `materials_received_and_sanitized` permanece en `false`.

### `att1-commercial-002-content` — facts comerciales autorizados

**Estado:** confirmado por el operador; pendiente de ratificación de Marcela y de materiales.

La confirmación del operador propone **Dra. Nina Garza**, **Alimenta Tu Tiroides**, **USD 47** y `purchase_observed` como hechos básicos candidatos. Marcela todavía debe ratificarlos. Transformación prometida, temario, entregables, condiciones, order bumps, FAQs, garantías y contraindicaciones comerciales siguen pendientes de fuentes ATT1 sanitizadas y aprobación de la autoridad. Ninguna promesa o dato del PDF de procesos se incorpora automáticamente como conocimiento de la oferta.

### `att1-commercial-003-health-limits` — límites sanitarios

**Estado:** confirmado por el operador; pendiente de ratificación de Marcela.

El agente no puede diagnosticar, interpretar síntomas, solicitar historia clínica, recomendar tratamientos/suplementos/dosis/cambios de medicación, prometer cura o resultados clínicos ni presentar la oferta como sustituto de atención profesional. Las consultas clínicas personalizadas deben derivarse al canal humano aprobado. Cualquier ampliación requiere una nueva revisión sanitaria/legal y una versión posterior; esta confirmación del operador no habilita outbound.

### `att1-commercial-004-language` — idioma del piloto

**Estado:** confirmado por el operador — español latino neutral; pendiente de ratificación de Marcela.

El idioma operativo candidato para la primera versión es español latino neutral (`spanish_latam_neutral`). La elección no define por sí sola el código de template de Meta; eso se valida en la macro de templates y canal.

### `att1-commercial-005-countries` — alcance geográfico

**Estado:** confirmado por el operador — México únicamente; pendiente de ratificación de Marcela.

El alcance candidato del piloto inicial queda limitado a México (`MX`), pendiente de ratificación de Marcela. La distribución de audiencia no amplía ese alcance y ningún otro país se incorpora por inferencia.

### `att1-commercial-006-discount` — política económica

**Estado:** aprobación de Marcela reportada por el operador; vigencia indefinida representable; plantilla y ejecución runtime pendientes; no publicada.

La decisión comercial es un cupón porcentual de **10 %**, sin restricciones propias por país o moneda. Sólo puede ofrecerse en `payment_failure`, `confirmed_cart_abandonment` y `precheckout_without_purchase_signal`, después de recibir al menos una respuesta inbound posterior a la plantilla inicial de inicio de conversación de Meta; por eso su posición es `later_step`. Esto no amplía el piloto más allá de México ni modifica consentimiento, opt-out, stops, cantidad de mensajes o cadencia.

El cupón no vence y no se permite urgencia, escasez ni afirmaciones de expiración. El código será contenido variable de una plantilla de Meta, pero el texto final, la plantilla exacta y el mapeo de la variable continúan pendientes. La referencia concreta del cupón no se guarda ni se inventa antes de ese cierre.

La migración `20260903000200_commercial_ally_indefinite_discount.sql` permite representar explícitamente vigencia `indefinite`, `offer_valid_for = null`, respuesta inbound obligatoria, variable de template Meta y prohibición de urgencia. La política continúa sin seeds y no publicada porque aún faltan el template exacto, el mapeo de variable y la rama runtime inbound → `later_step`. El operador identifica a Marcela como aprobadora de esta decisión; la confirmación directa de su autoridad comercial general continúa siendo un gate separado.

## Respuesta mínima solicitada a la autoridad comercial

Responder por escrito, en texto normal, estas líneas:

1. **Materiales:** archivos o enlaces privados autorizados, owner, versión/vigencia y permiso de uso.
2. **Contenido:** “Apruebo” o correcciones para identidad, oferta, USD 47, transformación, entregables, garantías, FAQs y outcome de compra.
3. **Límites sanitarios:** “Apruebo el baseline fail-closed” o cambios exactos con responsable de revisión.
4. **Idioma:** idioma único autorizado para el piloto.
5. **Países:** lista exacta de países autorizados.
6. **Descuento:** no requiere repetir la decisión comercial; texto/template y variable se cerrarán en su gate técnico separado.
7. **Autoridad:** confirmación directa de Marcela como autoridad comercial general.

## Efecto de la aprobación

La aprobación permite compilar los facts comerciales y preparar la macro siguiente. Por sí sola no aprueba la Conversation Release, no publica descuento, no autoriza activación, no habilita outbound y no prueba disponibilidad de proveedores.