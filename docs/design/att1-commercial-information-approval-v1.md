# ATT1 — aprobación de información comercial V1

- **Estado:** Confirmación del operador registrada; ratificación de Marcela, materiales y descuento pendientes
- **Gate:** Estado: Pendiente de aprobación externa
- **Alcance:** materiales, contenido comercial, límites sanitarios, idioma, países y descuento
- **Fuentes:** [registro ATT1 V1](att1-source-register-v1.md), `operator-confirmation:2026-09-02-authority-content-health-language-countries` y `Documentación de Procesos Carritos Abandonados, Pagos Declinados y Pagos Offline.pdf`
- **No implica:** Conversation Release aprobada, templates aprobados, configuración de proveedores, despliegue ni autorización para contactar personas reales

## Regla de cierre

Esta macro tarea sólo queda aprobada cuando una autoridad comercial identificada responde por escrito las seis decisiones y, para los materiales, existe recepción y sanitización verificadas. La confirmación del operador del 2026-09-02 identifica a Marcela como autoridad y confirma valores candidatos para contenido, límites sanitarios, idioma y países; no constituye una respuesta ni ratificación de Marcela. Ratificación comercial, materiales y descuento permanecen abiertos, por lo que el gate continúa `pending_external_approval`.

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

**Estado:** pendiente; no publicada.

La fuente propone 10 % de descuento, pero contradice una vigencia estricta de seis horas con la afirmación de que sigue activo después de dos días. Deben aprobarse conjuntamente:

- porcentaje o importe exacto;
- triggers autorizados;
- posición existente en la que se presenta (`first_touch` o `later_step`);
- cupón o fuente canónica del código;
- inicio y duración exacta;
- países/monedas a los que aplica;
- texto permitido sobre urgencia;
- approver y vigencia de la política.

Recomendación: si se usa el incentivo, emitir una oferta versionada de 10 % con vigencia de seis horas desde su presentación; cualquier incentivo posterior debe ser una nueva versión autorizada, nunca una extensión silenciosa. La política durable continúa sin seeds y no publicada hasta recibir la decisión completa.

## Respuesta mínima solicitada a la autoridad comercial

Responder por escrito, en texto normal, estas líneas:

1. **Materiales:** archivos o enlaces privados autorizados, owner, versión/vigencia y permiso de uso.
2. **Contenido:** “Apruebo” o correcciones para identidad, oferta, USD 47, transformación, entregables, garantías, FAQs y outcome de compra.
3. **Límites sanitarios:** “Apruebo el baseline fail-closed” o cambios exactos con responsable de revisión.
4. **Idioma:** idioma único autorizado para el piloto.
5. **Países:** lista exacta de países autorizados.
6. **Descuento:** porcentaje/importe, triggers, `first_touch` o `later_step`, cupón/fuente, duración, alcance y approver.
7. **Autoridad:** nombre o referencia de quién emite esta aprobación comercial.

## Efecto de la aprobación

La aprobación permite compilar los facts comerciales y preparar la macro siguiente. Por sí sola no aprueba la Conversation Release, no publica descuento, no autoriza activación, no habilita outbound y no prueba disponibilidad de proveedores.