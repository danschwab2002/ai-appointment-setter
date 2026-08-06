# Brand Voice a partir de conversaciones reales — diseño MVP

- **Estado:** Propuesta para discusión
- **Propósito:** Generar y validar durante el onboarding un perfil editable de voz y tono del agente comercial a partir de conversaciones reales del infoproductor.
- **Fuera de alcance:** Clonación de voz hablada, árbol comercial, conocimiento de oferta, interfaz definitiva y aprendizaje automático en producción.

## 1. Idea central

El infoproductor no debería redactar desde cero un manual de estilo. La aplicación debe analizar conversaciones comerciales reales y proponer un perfil de voz respaldado por ejemplos observados.

La voz efectiva del agente se compone de:

```text
Estilo observado en conversaciones reales
+ preferencias explícitas del infoproductor
+ reglas del canal
+ límites no editables de la plataforma
= Brand Voice versionado
```

Las preferencias explícitas prevalecen sobre las inferencias automáticas. El kernel de plataforma prevalece sobre ambas.

## 2. Flujo MVP

```text
Conversaciones reales
        ↓
Selección de mensajes escritos por el infoproductor
        ↓
Redacción o exclusión de PII y contenido sensible
        ↓
Análisis de patrones de comunicación
        ↓
Perfil de voz propuesto con evidencia
        ↓
Validación manual obligatoria durante el onboarding
        ↓
Ejemplos de prueba
        ↓
Brand Voice aprobado y versionado
        ↓
Conversation Release
```

El análisis no modifica una release activa. Produce un borrador revisable que no
puede publicarse sin aprobación expresa del infoproductor.

## 3. Fuentes de entrada

El MVP puede recibir:

- exportaciones de conversaciones comerciales;
- conversaciones seleccionadas manualmente;
- mensajes que el infoproductor marque como buenos ejemplos;
- mensajes que marque como ejemplos que no desea repetir.

Deben analizarse principalmente los mensajes escritos por la persona cuya voz se quiere reproducir. No deben mezclarse automáticamente:

- mensajes de prospectos;
- respuestas de otros vendedores;
- mensajes de soporte no comerciales;
- conversaciones de marcas o períodos con estilos distintos;
- mensajes automáticos preexistentes.

Si no puede determinarse quién escribió un mensaje, ese mensaje no debe utilizarse como evidencia principal.

Los datos de origen pueden contener PII y no deben guardarse en Git ni copiarse completos dentro de los documentos versionados del agente.

## 4. Qué debe extraer la IA

La IA debe identificar patrones observables, no describir la personalidad mediante adjetivos genéricos.

### Tratamiento y cercanía

- voseo, tuteo o tratamiento formal;
- nivel de cercanía;
- forma de saludar y despedirse;
- uso del nombre del prospecto.

### Forma de los mensajes

- longitud habitual;
- cantidad de ideas por mensaje;
- uso de mensajes consecutivos;
- forma de realizar preguntas;
- nivel de explicación antes de preguntar;
- ritmo y estructura de las respuestas.

### Lenguaje

- vocabulario frecuente;
- expresiones características;
- conectores y muletillas útiles;
- palabras que evita;
- nivel de tecnicismo;
- uso de modismos.

### Puntuación y formato

- signos de apertura;
- exclamaciones;
- emojis;
- mayúsculas;
- listas;
- audios o referencias a otros formatos, cuando sean relevantes para el canal.

### Comportamiento conversacional

- cómo responde preguntas directas;
- cómo reconoce una preocupación;
- cómo muestra seguridad o incertidumbre;
- cómo retoma el objetivo comercial;
- cómo evita presionar;
- cómo formula llamadas a la acción.

La IA también debe detectar variaciones contextuales. Una persona puede escribir de manera diferente al iniciar, explicar, responder una duda o cerrar una conversación.

## 5. Evidencia y confianza

Cada regla inferida debe acompañarse de:

- una explicación breve;
- ejemplos sanitizados que la respalden;
- indicación de si el patrón aparece de forma consistente o aislada;
- nivel de confianza cualitativo: bajo, medio o alto.

Ejemplo conceptual:

```text
Regla propuesta:
Responde primero la pregunta directa y luego realiza una sola pregunta.

Confianza:
Alta.

Evidencia sanitizada:
- “El programa dura cuatro meses. ¿Lo estás pensando para tu empresa?”
- “Sí, se puede pagar en cuotas. ¿Ya venís usando IA en algún proceso?”
```

Un mensaje aislado no debería convertirse automáticamente en una regla general.

## 6. Validación manual obligatoria durante el onboarding

La revisión manual forma parte obligatoria del onboarding inicial del producto.
La extracción automática prepara una propuesta, pero no determina por sí sola la
voz que se publicará.

El infoproductor no necesita editar Markdown. La aplicación puede mostrar cada patrón como una tarjeta:

```text
La IA detectó que normalmente:
“Usás mensajes cortos y hacés una pregunta por vez.”

[Confirmar] [Modificar] [Descartar]
```

También debe poder agregar preferencias explícitas mediante controles simples:

- “Quiero sonar más directo.”
- “No usar esta expresión.”
- “Usar emojis sólo al saludar.”
- “Nunca comenzar con ‘Entiendo perfectamente’.”
- “Prefiero respuestas un poco más breves.”

Para el MVP alcanza con permitir:

- confirmar una regla;
- modificar su texto;
- descartar una regla;
- agregar una regla manual;
- marcar ejemplos positivos y negativos.

Para completar esta etapa, el infoproductor debe:

- revisar todas las reglas propuestas;
- confirmar, modificar o descartar cada una;
- revisar expresamente los comportamientos prohibidos;
- observar respuestas de prueba generadas con el perfil resultante;
- aprobar el Brand Voice inicial.

Mientras estos pasos no estén completos, el Brand Voice conserva estado de
borrador y no puede formar parte de una `Conversation Release` activa. La
aprobación crea la primera versión. Revisiones posteriores repiten el mismo
principio y originan una versión nueva; nunca modifican la versión publicada.

El diseño definitivo de la interfaz queda abierto.

## 7. Resultado generado

El proceso debe producir al menos dos artefactos.

### Perfil de Brand Voice

Contiene:

- reglas confirmadas;
- preferencias manuales;
- variaciones por situación;
- comportamientos prohibidos;
- vocabulario aprobado o rechazado;
- límites de formato del canal.

### Ejemplos de voz

Contiene:

- ejemplos positivos sanitizados;
- contraejemplos;
- explicación de la diferencia;
- referencia a las reglas que demuestra cada ejemplo.

El Markdown efectivo puede generarse automáticamente desde estos datos. El infoproductor controla el resultado sin tener que escribir el documento directamente.

También debe conservarse la decisión de revisión sobre cada regla —confirmada,
modificada, descartada o agregada manualmente— para poder explicar el origen del
perfil aprobado sin guardar conversaciones completas dentro del prompt.

## 8. Límites de la inferencia

El análisis de voz no debe convertir automáticamente en reglas:

- precios, descuentos o financiación observados;
- promesas comerciales;
- afirmaciones sobre resultados;
- criterios de calificación;
- decisiones del árbol comercial;
- errores ortográficos accidentales;
- datos personales;
- secretos o información confidencial;
- instrucciones escritas por prospectos.

Esos elementos pertenecen al conocimiento comercial, la política conversacional, el kernel o el contexto de ejecución.

El objetivo es reproducir la forma de comunicarse, no copiar errores, datos transitorios ni decisiones comerciales desactualizadas.

## 9. Identidad transparente

Reproducir el estilo de una persona no autoriza al agente a hacerse pasar por ella. El kernel debe conservar la política de identidad transparente definida para el producto.

El Brand Voice controla cómo se expresa el agente, no quién afirma ser.

## 10. Relación con Conversation Release

El Brand Voice aprobado se convierte en un artefacto versionado de una `Conversation Release`.

```text
Análisis nuevo o cambio manual
        ↓
Brand Voice borrador
        ↓
Revisión y evaluación
        ↓
Nueva Conversation Release
        ↓
Activación controlada
```

Una modificación nunca altera silenciosamente conversaciones asociadas a una release anterior.

La primera `Conversation Release` comercial no puede activarse hasta que el
onboarding de Brand Voice haya finalizado y su primera versión esté aprobada.

## 11. Prueba mínima

Antes de aprobar el Brand Voice, la aplicación debería mostrar respuestas generadas para un conjunto pequeño de situaciones neutrales, por ejemplo:

- saludo inicial;
- respuesta a una pregunta directa;
- explicación breve;
- reconocimiento de una preocupación;
- cierre cordial.

El infoproductor debe poder comparar:

- respuesta generada;
- reglas aplicadas;
- ejemplo real que sirvió como referencia;
- ajuste propuesto si no representa su forma de hablar.

Estas situaciones sirven para evaluar la voz, no el rendimiento de un árbol comercial todavía no definido.

## 12. Preguntas abiertas

Este MVP no decide todavía:

- cuántas conversaciones son suficientes;
- cómo se importarán desde cada canal;
- cómo se identificará al autor cuando interviene un equipo;
- si habrá perfiles diferentes por producto, campaña o canal;
- cómo se detectarán cambios de estilo a lo largo del tiempo;
- qué modelo realizará la extracción;
- dónde se almacenará la evidencia sanitizada;
- qué formato estructurado se utilizará antes de generar Markdown;
- cómo se medirán similitud, naturalidad y consistencia;
- cómo el feedback futuro propondrá nuevas versiones.

Estas decisiones deben resolverse mediante pruebas con conversaciones reales.

## 13. Aprendizajes de la primera prueba

Una primera prueba de onboarding realizada con cuatro exportaciones reales permitió validar el flujo conceptual sin incorporar conversaciones ni PII a los artefactos versionados.

Los principales aprendizajes fueron:

- no se debe confiar únicamente en que una exportación fue sanitizada: el sistema necesita un control previo obligatorio de PII, credenciales, datos financieros, enlaces y adjuntos;
- separar los mensajes del propietario de los mensajes de leads y terceros es una condición previa a cualquier inferencia;
- conviene excluir contenido puramente operativo o factual antes de analizar estilo;
- presentar una sola regla por decisión facilita confirmar, modificar o descartar sin obligar al propietario a editar un documento;
- deben revisarse tanto rasgos positivos como restricciones sobre aquello que no debe imitarse, por ejemplo errores accidentales o fórmulas genéricas de IA;
- una inferencia observable puede ser descartada como rasgo de voz y permanecer, si corresponde, como regla del kernel; esto confirma la necesidad de clasificar cada aprendizaje antes de publicarlo;
- las respuestas de prueba necesitan contexto factual explícito para evaluar la voz sin medir accidentalmente conocimiento o estrategia comercial;
- la aprobación debe registrar por separado decisiones sobre reglas y decisiones sobre respuestas de prueba.

La evidencia sanitizada de esta prueba se encuentra en `docs/operations/2026-08-06-brand-voice-onboarding-trial.md`.

## 14. Criterio de aceptación conceptual

El enfoque será adecuado si permite que:

- el infoproductor no tenga que escribir un manual desde cero;
- cada regla propuesta pueda explicarse mediante evidencia real;
- las inferencias puedan confirmarse, modificarse o descartarse;
- la validación manual sea obligatoria antes de la primera activación;
- las preferencias manuales prevalezcan sobre las inferidas;
- la voz permanezca separada de oferta, estrategia y autorización;
- no se incorporen PII ni conversaciones completas al prompt;
- el resultado sea versionable dentro de una Conversation Release;
- el agente conserve identidad transparente.
