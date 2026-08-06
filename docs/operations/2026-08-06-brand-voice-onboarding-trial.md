# Evidencia — prueba de onboarding de Brand Voice

- **Fecha:** 2026-08-06
- **Estado:** Completada
- **Alcance:** Validación del proceso de extracción, revisión manual y prueba de voz
- **Contenido sensible:** Este documento no contiene conversaciones, PII, credenciales ni valores detectados

## Objetivo

Comprobar si es posible producir un Brand Voice revisable a partir de conversaciones reales sin pedirle al propietario que redacte un manual de estilo desde cero.

La prueba evaluó el proceso de onboarding y la fidelidad percibida de la voz. No evaluó conocimiento comercial, políticas de negocio ni rendimiento de un árbol conversacional.

## Fuente procesada

- exportaciones de conversaciones: 4;
- mensajes totales: 799;
- mensajes identificados como escritos por el propietario: 456;
- mensajes utilizables para análisis de estilo: 392;
- mensajes del propietario excluidos por adjuntos, enlaces, datos sensibles o contenido no lingüístico: 64.

Las exportaciones originales permanecieron como adjuntos privados de la sesión. No se copiaron a documentación ni a los artefactos derivados del Brand Voice.

## Control de datos sensibles

El control previo detectó categorías de información que impedían considerar las exportaciones completamente sanitizadas:

- correos electrónicos;
- URLs;
- teléfonos o números largos;
- identificadores bancarios o de pago;
- posibles credenciales.

Los valores no se reprodujeron ni se incorporaron al análisis derivado. Esta observación establece que el escaneo de PII y secretos debe ser una puerta obligatoria del onboarding, incluso cuando el usuario declara haber sanitizado la fuente.

## Revisión manual

Se presentaron 14 decisiones individuales:

- reglas o restricciones confirmadas: 13;
- inferencias descartadas: 1;
- modificaciones solicitadas: 0.

La inferencia descartada describía la verbalización de incertidumbre. El propietario decidió que no representaba un rasgo de su voz. La obligación de no inventar información permanece separada como regla del kernel.

La prueba confirmó que el onboarding debe conservar el estado de cada propuesta de forma independiente: confirmada, modificada, descartada o agregada manualmente.

## Respuestas de validación

Se generaron seis escenarios ficticios y sanitizados:

1. apertura con lead informal;
2. consulta formal con varios puntos;
3. objeción de precio;
4. lead listo para avanzar;
5. reparación después de una demora;
6. vínculo existente e informal.

Resultado:

- respuestas aprobadas: 6 de 6;
- respuestas modificadas: 0;
- respuestas rechazadas: 0.

Los escenarios incluyeron contexto factual ficticio y explícito cuando era necesario. De esta manera la revisión evaluó la voz sin depender de conocimiento comercial todavía no definido.

## Artefactos privados de la prueba

Los artefactos derivados, sin conversaciones fuente ni PII, se guardaron bajo `data/brand-voice-trial/`:

- `draft.json`: decisiones y resultados estructurados;
- `brand-voice-v1.md`: perfil de voz aprobado;
- `test-cases-v1.md`: escenarios y respuestas aprobadas.

`data/` está excluido de Git por contener material de prueba y potencialmente sensible.

## Resultado operativo

La prueba respalda el flujo:

```text
fuentes reales
→ separación del autor
→ exclusión de PII, secretos y contenido no estilístico
→ inferencias explicables
→ decisiones simples obligatorias
→ Brand Voice compilado
→ respuestas de prueba
→ aprobación del propietario
```

El resultado no implica que el formato final de almacenamiento, la UI o los umbrales de evidencia estén decididos. Es evidencia favorable sobre el proceso conceptual de onboarding.

## Integración controlada con el agente comercial existente

El Brand Voice aprobado se incorporó al `SOUL.md` del profile operativo
`agente-comercial` como una capa de redacción subordinada al kernel, las políticas,
los modos, las skills y los contratos existentes.

La integración no modificó:

- el backend del bridge;
- el contrato JSON del agente;
- las reglas de calificación;
- las autorizaciones;
- el motor de seguimientos;
- la ejecución de acciones.

Debido a que el bridge publica un único campo `reply`, esta prueba evalúa voz y
redacción en una sola burbuja. No evalúa todavía secuencias reales de dos a cuatro
burbujas. Además, el contrato permite como máximo un carácter `?`; la adaptación
operativa conserva una sola pregunta y evita signos de cierre duplicados.

### Verificación offline

Se ejecutaron tres contextos sintéticos directamente contra el profile actualizado:

1. apertura con un lead informal;
2. continuidad informal dentro de una conversación activa;
3. consulta formal sobre precio, cuotas y duración.

Resultados:

- propuestas válidas según `_is_valid_proposal`: 3 de 3;
- aserciones específicas de Brand Voice: aprobadas;
- pruebas `tests/test_hermes.py`: 24 aprobadas;
- identidad transparente preservada;
- adaptación entre registro informal y formal observada;
- respuesta directa antes de retomar la calificación observada;
- máximo de una pregunta preservado.

### Disponibilidad del modelo y recarga

La prueba detectó que la cuota del modelo primario
`anthropic/claude-sonnet-4-6` estaba agotada. Sin reemplazarlo, se configuró mediante
el comando oficial de Hermes un fallback a `openai-codex/gpt-5.6-sol`, que ya estaba
autenticado en el mismo profile. Una ejecución sin override confirmó que el fallback
responde correctamente.

El gateway supervisado por s6 se reinició de forma acotada para el profile comercial.
La verificación posterior confirmó:

- proceso nuevo bajo el supervisor;
- gateway activo;
- API del profile escuchando;
- `GET /health`: HTTP 200 con estado `ok`.

### Estado del E2E por WhatsApp

Se realizaron dos interacciones desde el único JID de prueba autorizado.

La primera apertura real fue rechazada porque:

- utilizó una transición genérica equivalente a «con gusto» y «para empezar»;
- solicitó nombre y rol dentro de una misma pregunta compuesta;
- omitió la identidad transparente del asistente virtual.

La sesión técnica confirmó que esa respuesta fue producida por el gateway recargado y
que el Brand Voice nuevo estaba incluido en el system prompt. La causa no fue una
configuración obsoleta, sino una ambigüedad de control: «una sola pregunta» podía ser
interpretada como un único signo `?` aunque se solicitaran dos datos.

Se endureció la capa de voz sin cambiar el backend:

- exactamente un dato faltante como objetivo por respuesta;
- prohibición explícita de preguntas compuestas;
- identidad obligatoria en la primera respuesta;
- ejemplos negativos de transiciones genéricas;
- ejemplo positivo de apertura.

La apertura corregida pasó 3 de 3 ejecuciones offline con Claude. Después de una nueva
recarga del gateway, la continuidad real por WhatsApp respondió:

> «Buenísimo, Lucas. ¿A qué te dedicás o qué rol tenés en tu empresa?»

Esta respuesta fue aprobada porque reconoce el dato recibido, conserva un tono cercano
y solicita únicamente el campo conceptual `role`. La sesión del gateway confirmó que
el prompt endurecido estaba cargado y que la respuesta almacenada coincidía con la
publicada en WhatsApp.

El E2E queda aprobado para continuidad conversacional con un alcance acotado. La
apertura corregida está validada offline, pero deberá observarse nuevamente cuando se
inicie una conversación nueva de WhatsApp.
