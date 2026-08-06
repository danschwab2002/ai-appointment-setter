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
