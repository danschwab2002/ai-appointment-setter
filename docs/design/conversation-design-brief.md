# Brief mínimo de diseño conversacional

- **Estado:** Plantilla MVP
- **Propósito:** Reunir la información mínima necesaria antes de diseñar el comportamiento conversacional de un agente comercial.
- **No es:** Un system prompt, un árbol de decisiones ni una configuración activa del agente.

## 1. Negocio y oferta

- **¿Qué producto u oferta debe presentar el agente?**
- **¿A quién está dirigido?**
- **¿Qué información comercial puede comunicar?**
- **¿Qué no puede prometer, ofrecer o afirmar?**

## 2. Inicio de la conversación

- **¿Qué evento o situación inicia la conversación?**
- **¿Qué sabe el agente sobre la persona en ese momento?**

## 3. Objetivo principal

- **¿Qué resultado debería conseguir el agente?**

Elegir uno como objetivo principal y describir cualquier objetivo secundario:

- calificar;
- vender;
- recuperar una compra;
- agendar;
- derivar a una persona;
- responder consultas comerciales;
- otro.

- **¿Cómo sabemos que la conversación fue exitosa?**

## 4. Recorrido comercial

- **¿Qué necesita descubrir, explicar o confirmar antes de alcanzar el objetivo?**
- **¿Qué decisiones importantes debe tomar durante la conversación?**
- **¿En qué situaciones debe dejar de avanzar o derivar a una persona?**

## 5. Voz y forma de conversar

- **¿Cómo debería sonar el agente?**
- **¿Qué expresiones, tonos o comportamientos debería evitar?**
- **¿Debe presentarse explícitamente como asistente virtual? ¿Cómo?**

## 6. Ejemplos

Adjuntar, cuando existan:

- 3 conversaciones o respuestas que representen bien la forma deseada de vender;
- 3 conversaciones o respuestas que resulten inaceptables;
- una explicación breve de qué está bien o mal en cada ejemplo.

## 7. Límites y errores costosos

- **¿Qué respuesta incorrecta podría causar un perjuicio comercial importante?**
- **¿Qué temas o decisiones requieren siempre intervención humana?**
- **¿Qué información nunca debe inventar el agente?**

## 8. Información faltante

Registrar explícitamente todo lo que todavía no esté definido. La falta de información no debe completarse mediante suposiciones.

## Resultado del brief

Una vez completado, el brief debe permitir resumir:

```text
Oferta:
Público:
Situación de inicio:
Objetivo principal:
Objetivos secundarios:
Condición de éxito:
Información que debe obtener o comunicar:
Situaciones de derivación o cierre:
Voz deseada:
Conductas prohibidas:
Ejemplos disponibles:
Información todavía faltante:
```

Este resumen será el punto de partida para decidir si la conversación necesita un árbol de calificación, venta, recuperación, derivación u otra combinación, y para crear posteriormente una `Conversation Release`.