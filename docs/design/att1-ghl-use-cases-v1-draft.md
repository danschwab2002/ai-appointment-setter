# Casos de uso ATT1 derivados de conversaciones GHL — V1

- **Estado:** Propuesta para revisión; no activa
- **Fuente privada:** corpus sanitizado de GHL
- **Muestra de descubrimiento:** 25 conversaciones con intervención humana; 689 mensajes
- **Validación ampliada:** 768 conversaciones; 4.097 mensajes sanitizados
- **Unidad de conteo:** conversación, como máximo una vez por categoría
- **Clasificación:** multietiqueta; los conteos no deben sumarse como conversaciones únicas
- **Alcance:** situaciones observadas y requisitos para diseñar casos; no define políticas ni autoriza acciones

## Resumen

Se identificaron 9 familias mediante revisión de las conversaciones completas y
72 asignaciones verificadas. La muestra fue seleccionada por intervención humana,
por lo que estos conteos sirven para priorizar diseño, no para estimar la
frecuencia total en GHL.

| ID | Caso observado | Conversaciones | Riesgo | Confianza |
|---|---|---:|---|---|
| UC-01 | Checkout, descuento y estado de pago | 13 | Alto | Alta |
| UC-02 | Alcance, formato y diferencias de la oferta | 12 | Medio | Alta |
| UC-03 | Recuperación de acceso y datos de cuenta | 12 | Medio-alto | Alta |
| UC-04 | Inicio, navegación y descarga de contenidos | 9 | Medio | Alta |
| UC-05 | Confirmación y acceso a clases o eventos | 8 | Medio | Alta |
| UC-06 | Solicitudes de consulta profesional | 6 | Alto | Alta |
| UC-07 | Membresía, comunidad y aprobación de grupos | 5 | Medio-alto | Media-alta |
| UC-08 | Consultas médicas y adecuación clínica | 5 | Crítico | Alta |
| UC-09 | Reembolso o cancelación | 2 | Alto | Media |

## Prioridad candidata

1. **Primero por riesgo:** UC-08, UC-01 y UC-09.
2. **Después por volumen y dependencia operativa:** UC-03, UC-02 y UC-06.
3. **Luego:** UC-04, UC-05 y UC-07.

La prioridad sólo ordena el trabajo de definición y revisión. No habilita respuestas
automáticas ni efectos.

## Validación con el corpus completo

Las nueve familias aparecieron también en el corpus ampliado; no surgió una
décima familia material. La muestra inicial estaba saturada para descubrir la
taxonomía, pero no para medir peso operativo, secuencias o stops. El corpus
completo está dominado por automatizaciones: 2.035 mensajes frente a 493 de
asesores humanos verificados; 251 salidas `app` mantienen autoría no resuelta.

Para UC-01 se observaron 150 conversaciones con triggers automáticos de recupero
y 37 con señal del cliente sobre intento incompleto, abandono o fallo. En 132 de
las 150 hubo al menos una contradicción con la política vigente. La evidencia no
autoriza copiar esos flujos: 123 presentaban el 10 % en el primer contacto con
vencimiento, siete repetían el contacto inicial y 18 agregaban outbound antes de
una respuesta.

Las tres conversaciones con señal de cancelación o reembolso recibieron alguna
automatización posterior. Por ello cancelación, devolución o reembolso deben
suprimir inmediatamente recupero y promociones del caso; la atención humana
transaccional puede continuar.

## Casos

### UC-01 — Checkout, descuento y estado de pago

- **Situación observada:** la persona intenta retomar o completar una compra,
  encuentra un error, ve una discrepancia de país, producto o promoción, o pide
  confirmar el pago.
- **Scope Hotmart confirmado por el operador:** producto numérico `5071808`,
  hotlink `D98014973Y` y checkout canónico
  `https://pay.hotmart.com/D98014973Y`. La oferta `83utgyow` es el candidato
  dominante observado en GHL, no una referencia canónica aprobada todavía.
- **Promoción confirmada:** 10 %, sin vencimiento, urgencia ni escasez. Sólo se
  presenta después de una respuesta inbound posterior al mensaje inicial y su
  código se trata como contenido de una variable de canal; GHL aporta un
  candidato observado, no autoridad para publicarlo.
- **Hechos necesarios:** país y moneda; oferta canónica; estado autoritativo de
  la transacción; medio y plazo documentados; correspondencia segura con la
  cuenta.
- **Patrón observado:** orientación de navegación y pago, con verificaciones
  manuales. También aparecen contradicciones entre checkout, promociones y
  mensajes automáticos.
- **Flujo V1 decidido:** como máximo un mensaje inicial por trigger. Sólo una
  persona que responda a ese mensaje puede avanzar a un seguimiento con el
  descuento del 10 %. `PURCHASE_APPROVED` confirma compra y detiene el flujo;
  `PURCHASE_CANCELED` representa pago fallido; cualquier estado desconocido
  produce cero efectos y handoff humano.
- **Precedencia:** los flujos históricos de GHL sirven para completar facts y
  ejemplos faltantes. Cuando contradicen este flujo V1, prevalece el flujo V1 y
  la variante histórica queda como candidata para una release posterior.
- **Cierre verificable:** proveedor confirma pago aceptado y la persona confirma
  acceso. Un comprobante o una intención de pago no bastan.
- **Handoff candidato:** error o estado ambiguo; discrepancia comercial; disputa
  de cobro; modificación manual; pérdida de confianza.
- **Límites:** no pedir datos de tarjeta, ejecutar compras, alterar precios ni
  declarar éxito sin evidencia autoritativa.

### UC-02 — Alcance, formato y diferencias de la oferta

- **Situación observada:** preguntas sobre contenido, formato, acceso, costo,
  acompañamiento o diferencia entre ofertas y complementos.
- **Hechos necesarios:** oferta exacta; catálogo versionado; contenido, formato,
  acceso, precio, moneda, garantía y acompañamiento aprobados.
- **Patrón observado:** explicación de componentes y diferencias, seguida de una
  ruta de compra. Es evidencia descriptiva, no un guion aprobado.
- **Cierre verificable:** la persona confirma comprensión o elige una opción; un
  enlace enviado no demuestra ninguna de las dos cosas.
- **Handoff candidato:** catálogo o checkout contradictorio; facts ausentes;
  recomendación basada en síntomas; producto adquirido incierto.
- **Límites:** no reutilizar facts históricos ni inferir beneficios, garantías o
  adecuación clínica.

### UC-03 — Recuperación de acceso y datos de cuenta

- **Situación observada:** correo no recibido, fallo de inicio de sesión,
  contraseña, carga o correo registrado incorrecto.
- **Hechos necesarios:** compra confirmada; cuenta identificada de forma segura;
  correo enmascarado; estado de entrega, activación y producto asociado.
- **Patrón observado:** verificar compra, reenviar acceso, orientar recuperación y
  corregir datos cuando corresponde.
- **Cierre verificable:** la persona inicia sesión y visualiza el producto correcto.
- **Handoff candidato:** cambio de correo o titular; identidad no validada;
  reenvíos fallidos; producto ausente; error persistente.
- **Límites:** no revelar existencia de cuenta, exponer correos completos, pedir
  contraseñas o modificar datos sin autorización y trazabilidad.

### UC-04 — Inicio, navegación y descarga de contenidos

- **Situación observada:** la persona no encuentra dónde comenzar, un módulo, una
  grabación, un calendario o un recurso descargable.
- **Hechos necesarios:** producto y versión; estado de acceso; mapa vigente de
  módulos; dispositivo; recurso exacto.
- **Patrón observado:** indicar la sección correspondiente y pedir evidencia
  visual si el recurso no aparece.
- **Cierre verificable:** la persona abre o descarga el recurso solicitado.
- **Handoff candidato:** contenido no incluido o ausente; interfaz distinta;
  fallo persistente; necesidad de accesibilidad.
- **Límites:** no prometer inclusión sin verificar compra ni distribuir contenido
  fuera del canal autorizado.

### UC-05 — Confirmación y acceso a clases o eventos

- **Situación observada:** confirmación de asistencia o dudas sobre costo, enlace,
  modalidad, horario, ubicación o grabación.
- **Hechos necesarios:** evento; fecha, hora y zona horaria; modalidad y costo;
  inscripción; acceso; disponibilidad y caducidad de repetición.
- **Patrón observado:** automatización de confirmación y aclaraciones humanas sobre
  acceso, modalidad y grabaciones.
- **Cierre verificable:** inscripción registrada y acceso válido. La asistencia
  requiere evidencia del evento.
- **Handoff candidato:** enlace incorrecto; conflicto horario; excepción de acceso;
  inscripción ausente.
- **Límites:** no reutilizar datos históricos, prometer grabación ni exponer enlaces
  privados sin elegibilidad.

### UC-06 — Solicitudes de consulta profesional

- **Situación observada:** solicitud de cita presencial, virtual o privada, o duda
  sobre si una compra incluye consulta.
- **Hechos necesarios:** tipo de consulta; agenda autoritativa; canal de reserva;
  modalidad; relación con el producto; señales de urgencia declaradas.
- **Patrón observado:** distinguir programa y consulta, informar disponibilidad y
  derivar al canal de agenda.
- **Cierre verificable:** el sistema de agenda confirma la reserva.
- **Handoff candidato:** urgencia o síntomas preocupantes; excepción de agenda;
  elegibilidad dudosa; orientación clínica; disponibilidad no verificable.
- **Límites:** no prometer turnos, prioridad o tiempos ni presentar contenido
  educativo como sustituto de consulta.

### UC-07 — Membresía, comunidad y aprobación de grupos

- **Situación observada:** dudas sobre comunidad correspondiente, canal de
  consultas, ingreso o solicitud pendiente.
- **Hechos necesarios:** producto o membresía; canales incluidos; estado de la
  solicitud; grupo vigente; autoridad de aprobación.
- **Patrón observado:** distinguir canales, compartir la ruta y verificar o aprobar
  solicitudes.
- **Cierre verificable:** la persona ingresa al grupo correcto y puede usarlo.
- **Handoff candidato:** acceso no incluido; aprobación requerida; grupo no
  verificable; identidad inconsistente; moderación.
- **Límites:** no aprobar miembros, compartir enlaces privados ni prometer plazos
  sin autorización.

### UC-08 — Consultas médicas y adecuación clínica

- **Situación observada:** preguntas sobre síntomas, diagnóstico, medicación,
  adecuación del programa o resultados clínicos.
- **Hechos necesarios:** naturaleza general de la pregunta; protocolo clínico y de
  seguridad aprobado; señales de urgencia; canal profesional; alcance educativo.
- **Patrón observado:** algunas respuestas delimitan el alcance y derivan; otras
  contienen afirmaciones generales que no deben reutilizarse sin revisión.
- **Cierre verificable:** límite comunicado y derivación segura, sin emitir una
  opinión clínica.
- **Handoff candidato:** síntomas severos o persistentes; diagnóstico, medicación,
  estudios o pronóstico; adaptación clínica; expectativa de curación.
- **Límites:** no diagnosticar, atribuir causas, interpretar estudios, modificar
  tratamientos ni prometer resultados. No activar sin revisión clínica y legal.

### UC-09 — Reembolso o cancelación

- **Situación observada:** solicitud de devolución o cancelación por expectativas,
  dificultad de uso u otro motivo.
- **Hechos necesarios:** compra y producto; fecha y estado; solicitud explícita;
  política vigente; estado del proveedor; canal autorizado.
- **Patrón observado:** explorar el motivo, ofrecer soporte y después indicar una
  devolución o canal de cancelación si la solicitud persiste.
- **Cierre verificable:** proveedor confirma cancelación o reembolso y se comunica
  ese estado verificable.
- **Handoff candidato:** toda ejecución o aprobación; elegibilidad dudosa; disputa,
  cargo no reconocido o plazo vencido; rechazo explícito de soporte alternativo.
- **Límites:** no obstaculizar con presión, prometer importe o plazo, ejecutar sin
  autorización ni declarar cierre antes de confirmación.
- **Stop global:** una cancelación, devolución o reembolso autoritativos suprimen
  recupero y promociones posteriores del caso.

## Brechas antes de convertir casos en una librería activa

- El corpus ampliado aporta cobertura temática, pero no timestamps, tasa de
  resolución ni tiempos confiables.
- Faltan fuentes versionadas para catálogo, precios, promociones, garantías,
  agenda y reembolsos.
- Muchos hilos no contienen confirmación final del resultado.
- Existen discrepancias entre mensajes automáticos y respuestas humanas.
- No están definidos los controles de identidad ni las autoridades para modificar
  cuentas o aprobar comunidades.
- No existe en este corpus un protocolo clínico aprobado.
- La sanitización automática aún requiere revisión humana de nombres libres.
- Para UC-01 faltan el Offer code canónico y el código de cupón aprobado. No se
  requiere template WABA hasta que ATT1 disponga de número y portfolio de Meta;
  el runtime debe permanecer cerrado justo antes del efecto Meta.

## Condiciones de uso

Este documento no autoriza automatización, pagos, modificaciones de cuenta,
entrega de accesos, reservas, reembolsos ni decisiones clínicas. Los patrones
observados no son procedimientos aprobados. Toda afirmación comercial debe venir
de una fuente vigente y versionada; los estados operativos requieren lectura del
sistema autoritativo. Hasta aprobar controles específicos, los casos médicos,
financieros y de modificación de cuenta requieren handoff humano.
