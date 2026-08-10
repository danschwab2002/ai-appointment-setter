# Dudas de diseño para conversar con Juan

- **Estado:** Revisada después de la reunión; contiene respuestas, direcciones aceptadas y temas todavía abiertos
- **Última revisión:** 2026-08-10
- **Propósito:** Conservar las preguntas originales, registrar qué se resolvió con Juan y enlazar los diseños o decisiones derivados.
- **Alcance actual:** Onboarding comercial, alcance mínimo del MVP, control del agente, complejidad conversacional, biblioteca de casos, creación de contenido y una decisión personal sobre el foco profesional.
- **Fuente de respuestas:** Reunión con Juan Martitegui del 7 de agosto de 2026 ([grabación de Fathom](https://fathom.video/share/yj78Kt41tfdyWwPwTsqk-SUcDC3x9JSi)).
- **No implica:** Que las direcciones aceptadas estén implementadas. El estado ejecutable continúa documentado en `docs/architecture.md`, contratos, código y evidencia operativa.

## Estado resumido

| Tema | Estado después de la reunión |
|---|---|
| Transmisión de conocimiento | Parcialmente resuelta: onboarding manual, documentos reales y casos; el formato definitivo sigue abierto |
| Control del infoproductor | Parcialmente resuelta: dirección mediante lenguaje natural y pushback; interfaz y contrato pendientes |
| Creación de contenido | Resuelta como estrategia personal |
| Desarrollos personalizados vs. producto | Resuelta como orientación personal |
| Complejidad conversacional | Diferida a evidencia real; no sobrediseñar ahora |
| MVP mínimo | Dirección aceptada: piloto estrecho y supervisado en Lancemos |
| Onboarding | Dirección aceptada: manual primero, automatización progresiva |
| Biblioteca de casos | Decisión arquitectónica aceptada; implementación pendiente |

## 1. ¿Cómo debería el infoproductor transmitir el conocimiento de sus productos y ofertas?

- **Estado:** Parcialmente resuelta

### Respuesta y dirección acordada

El primer onboarding no será autoservicio ni dependerá de una consigna abierta. Para Lancemos se comenzará con materiales reales del negocio —FAQs, flujos, mensajes, secuencias, templates y conocimiento de una oferta— recopilados mediante acompañamiento humano.

La fuente inicial puede ser un Google Doc grande y estructurado con tipos de caso y pasos. Nosotros ayudaremos a interpretar ese material y convertirlo en conocimiento y skills revisadas. El cuestionario `agent-driven` permanece como una posible evolución, pero no es requisito ni formato decidido para el piloto.

Ver [Dirección del piloto de Lancemos](lancemos-pilot-product-direction.md) y [Biblioteca de casos y skills supervisadas](case-library-and-supervised-skills.md).

¿Cómo se imagina Juan la dinámica mediante la cual un infoproductor recién incorporado le brinda al agente toda la información necesaria sobre:

- los productos que vende;
- las ofertas y condiciones comerciales;
- la forma en que actualmente los vende;
- la forma en que quiere que el agente los venda;
- los objetivos que espera alcanzar en cada conversación;
- los argumentos, límites y promesas permitidas;
- los distintos tipos de seguimiento y el propósito de cada uno?

La duda central no es solamente **qué información pedir**, sino **cómo obtenerla sin marear ni perder al infoproductor**.

Preguntas relacionadas para conversar:

- ¿Juan imagina un formulario guiado, una entrevista conversacional, la carga de documentos, el análisis de conversaciones reales o una combinación de estas alternativas?
- ¿La configuración debería hacerse producto por producto, oferta por oferta o a partir de objetivos comerciales más generales?
- ¿Qué información sería obligatoria para comenzar y qué podría completarse progresivamente?
- ¿Cómo se transforma lo que aporta el infoproductor en conocimiento comercial, objetivos y secuencias concretas del agente?
- ¿Cómo revisa y aprueba el infoproductor la interpretación realizada por la aplicación antes de activarla?
- ¿Cómo se evita pedirle que diseñe por su cuenta un árbol de conversación o un sistema comercial completo?

### Hipótesis inicial: onboarding `agent-driven`

El enfoque principal podría ser que todo el proceso de extracción de conocimiento esté conducido por el agente. No alcanzaría con hacerle al infoproductor una pregunta vaga como «Necesito que me des tu conocimiento sobre los infoproductos que tenés», porque existen demasiadas formas posibles de responder y demasiados caminos por los que podría avanzar la entrega de información.

En lugar de dejar el proceso completamente abierto, el sistema debería proponer dos o tres carriles claros para extraer esa información. El agente guiaría al infoproductor dentro del carril elegido, controlaría qué información ya obtuvo, detectaría qué falta y ayudaría a transformar las respuestas en conocimiento utilizable por el agente comercial.

#### Carril propuesto 1: cuestionario guiado

Uno de esos carriles podría ser un cuestionario conducido por el agente. Sería relativamente extenso, pero se presentaría progresivamente para no abrumar al infoproductor.

El cuestionario no debería improvisarse libremente. Tendría que estar prediseñado y contar con objetivos concretos sobre:

- qué información necesita obtener;
- para qué se utilizará cada dato;
- de qué forma conviene formular cada pregunta;
- qué respuestas requieren profundización;
- cuándo existe información suficiente para avanzar;
- cómo se detectan contradicciones o datos todavía faltantes.

Los otros posibles carriles del onboarding quedan abiertos para definirlos y compararlos con Juan.

## 2. ¿Qué debería poder controlar el infoproductor sobre el agente?

- **Estado:** Parcialmente resuelta; interfaz detallada diferida

### Respuesta y dirección acordada

La experiencia principal debería parecerse a dirigir a un empleado inteligente. El infoproductor expresa una intención en lenguaje natural; el sistema pide aclaraciones, hace pushback cuando contradice buenas prácticas, explica cómo quedaría configurado y solicita confirmación.

El usuario puede elegir preferencias permitidas, pero no eliminar restricciones de consentimiento, frecuencia, opt-out, canal, takeover humano o seguridad. Los diales y paneles no se descartan; se difieren hasta que la operación real revele parámetros repetibles y comprensibles.

Ver [Experiencia de configuración mediante Automation Expert](automation-expert-configuration-experience.md).

¿Qué nivel de control imagina Juan para el infoproductor?

En un extremo podría existir un control general o abstracto, por ejemplo:

- qué tan agresivo o prudente debe ser comercialmente;
- cuál es el objetivo principal de la conversación;
- cuándo debe insistir y cuándo debe retirarse;
- qué grado de autonomía tiene el agente.

En el otro extremo podría existir un control mucho más específico, por ejemplo:

- cuántos seguimientos debe realizar;
- cuánto tiempo debe esperar entre seguimientos;
- qué objetivo tiene cada seguimiento;
- qué mensaje o tipo de mensaje debe enviar en cada paso;
- qué caminos conversacionales puede seguir;
- qué acciones o afirmaciones están prohibidas.

Preguntas relacionadas para conversar:

- ¿Juan imagina que el infoproductor configure principalmente intenciones generales o reglas operativas detalladas?
- ¿Qué controles deberían ser simples y visibles para cualquier usuario, y cuáles deberían quedar como opciones avanzadas?
- ¿El usuario debería escribir mensajes exactos, elegir entre estrategias prearmadas o aprobar propuestas generadas por la aplicación?
- ¿Qué aspectos puede adaptar el agente libremente y cuáles deben quedar fijados de forma determinista?
- ¿Cómo se muestra el efecto de una configuración antes de publicarla?
- ¿Qué configuración mínima permitiría obtener un resultado útil sin obligar al usuario a entender cómo funciona un agente de IA?

### Hipótesis iniciales para plantearle a Juan

#### Hipótesis A: una parte importante del control puede surgir de ciclos diarios de feedback

La aplicación podría incluir una dinámica diaria en la que un agente de IA se comunique con el infoproductor para:

- resumir cómo condujo las conversaciones del día;
- mostrar ejemplos representativos de las decisiones y respuestas del agente;
- explicar qué intentó conseguir en esas conversaciones;
- pedir feedback concreto sobre lo que estuvo bien y lo que debería corregirse.

Esta dinámica permitiría que el infoproductor ejerza un control real sin tener que anticipar y configurar por adelantado todas las situaciones posibles. Una parte del comportamiento comercial podría ajustarse progresivamente a partir de ciclos supervisados entre el sistema y el infoproductor.

La hipótesis es que estos ciclos podrían resolver una porción importante del problema de configuración detallada. De todos modos, habría que definir cómo se transforma el feedback diario en cambios propuestos, cómo los revisa el infoproductor y cuándo comienzan a aplicarse, evitando que un comentario aislado modifique automáticamente el comportamiento activo.

#### Hipótesis B: un centro de control con diales para parámetros comerciales subjetivos

Otra posibilidad, ya conversada inicialmente con Juan, es ofrecer una especie de centro de control con diales. Cada dial representaría un parámetro comercial subjetivo que el infoproductor puede aumentar o disminuir sin tener que editar reglas técnicas, árboles o prompts.

Por ejemplo, un dial de **agresividad comercial** podría traducirse de la siguiente manera:

- en un nivel bajo, el agente realiza menos seguimientos, insiste menos y acepta antes que la oportunidad no avance;
- en un nivel alto, el agente realiza más seguimientos, busca activamente retomar la conversación e insiste más en alcanzar la venta, dentro de los límites permitidos.

La misma lógica podría aplicarse a otros parámetros subjetivos. La interfaz expondría controles simples, mientras que el sistema traduciría cada nivel a decisiones concretas y comprobables, como cantidad y frecuencia de seguimientos, condiciones de abandono, nivel de insistencia y objetivos perseguidos.

Esta hipótesis abre algunas preguntas para conversar con Juan:

- ¿Qué diales representarían dimensiones realmente comprensibles y útiles para un infoproductor?
- ¿Cada dial debería controlar una sola conducta o un conjunto coherente de comportamientos?
- ¿Cómo se le muestra al usuario qué consecuencias concretas tendrá subir o bajar un dial?
- ¿Cómo se evita que dos diales produzcan instrucciones contradictorias?
- ¿Qué límites no deberían poder superarse aunque el usuario lleve un dial al máximo?
- ¿Los ciclos diarios de feedback ajustarían estos diales, propondrían cambios más específicos o ambas cosas?

## 3. ¿Se puede empezar a crear contenido a partir de lo que estamos haciendo?

- **Estado:** Resuelta como estrategia personal; fuera de la arquitectura

Juan considera válido grabar, documentar y crear el contenido desde ahora. La recomendación es acumularlo y comenzar a publicarlo cuando exista una oferta o botón de compra y, de ser posible, evidencia de tres o cuatro implementaciones. Publicarlo antes no está prohibido si sirve como motivación, pero no es la estrategia recomendada.

Preguntarle a Juan si considera que ya se puede empezar a crear contenido a partir de este proceso, de las decisiones que estamos tomando y de lo que estamos aprendiendo mientras construimos el producto.

## 4. Opinión personal sobre desarrollos personalizados, producto y formación

- **Estado:** Resuelta como orientación personal; fuera de la arquitectura

La recomendación es tomar dos o tres trabajos o clientes empaquetados para generar cash flow, evitando depender exclusivamente de desarrollo por hora. Es compatible con continuar construyendo productos de mayor plazo; el objetivo es financiar el proceso sin quedar atrapado permanentemente en intercambio de tiempo por dinero.

Preguntarle qué opina, desde un lugar más personal, sobre si debería seguir tomando desarrollos personalizados o enfocarme en producto y formación.

## 5. ¿Qué tanta complejidad tendrán las conversaciones con los potenciales clientes?

- **Estado:** Diferida a evidencia real

No se asumió un nivel de complejidad. La dirección es no seguir perfeccionando el system prompt en abstracto. Primero se conectará una oferta real, se observarán casos y conversaciones y se invertirá en diseño conversacional según la complejidad efectivamente encontrada.

Preguntarle qué nivel de complejidad imagina que tendrán las conversaciones que el sistema mantenga con los potenciales clientes del infoproductor que compre la aplicación.

Hace poco veníamos hablando sobre cómo crear —o, mejor dicho, cómo diseñar— el system prompt del agente comercial. Ese diseño está muy determinado por la complejidad de las conversaciones que el agente tenga que llevar a cabo.

Si esas conversaciones no van a ser tan complicadas o no van a tener un nivel de complejidad muy alto, el diseño del system prompt pasa a un segundo plano y empiezan a tomar más relevancia otras partes del producto. La pregunta busca entender cuánto esfuerzo conviene poner ahora en ese diseño y qué capacidades deberían priorizarse en función de la complejidad conversacional esperada.

## 6. ¿Qué funcionalidades mínimas debe tener el MVP para implementarlo en un negocio real?

- **Estado:** Dirección aceptada; detalles del go-live todavía abiertos

El MVP no se definió como una lista extensa de features. Debe recibir una prueba adicional y, cuando exista confianza suficiente para no producir daños críticos, activarse de manera muy controlada en una oferta de Lancemos. Competirá contra una operación hoy desatendida, no contra un equipo comercial maduro.

El piloto será pequeño, allowlisted, observado de cerca y sin acceso a refunds ni otras acciones peligrosas. WhatsApp oficial, templates, cierre por compra y takeover humano forman parte de la vertical mínima que debe quedar segura.

Ver [Dirección del piloto de Lancemos](lancemos-pilot-product-direction.md).

Preguntarle a Juan qué cree que tiene que tener el sistema: cuáles son las funcionalidades mínimas y básicas que debería incluir el MVP para que ya pueda implementarse en un negocio real, probablemente en su agencia.

Esta pregunta es especialmente importante porque la respuesta determina completamente el rumbo y las prioridades de las próximas acciones. La idea es identificar qué capacidades son realmente indispensables para una primera implementación útil y cuáles pueden quedar para etapas posteriores.

## 7. ¿Cómo se imagina Juan el proceso completo de onboarding?

- **Estado:** Dirección aceptada

Los primeros onboardings serán manuales, personalizados y con contacto humano cercano. Se acepta una primera semana más desprolija y costosa si permite que las semanas siguientes requieran menos intervención. Después de varias ofertas o implementaciones deberían aparecer patrones suficientes para automatizar partes del proceso.

La automatización debe surgir de lo observado en los primeros clientes, no de diseñar anticipadamente un onboarding universal.

Preguntarle cómo se imagina todo el proceso de onboarding de un nuevo cliente al sistema: qué tendría que aportar, qué decisiones tendría que tomar, cuánto acompañamiento necesitaría y en qué momento podría empezar a obtener valor real.

Es probable que el onboarding sea largo debido a toda la información comercial, configuración y validación que requiere el agente. Esto puede transformarse en un problema de **speed to value** si el cliente tarda demasiado en ver el sistema funcionando en su negocio.

### Hipótesis inicial: onboarding manual y personalizado en la primera etapa

Durante la etapa inicial del producto, este problema podría resolverse mediante onboardings mucho más manuales, personalizados y con mayor contacto humano. Nosotros participaríamos activamente para configurar el sistema junto con cada cliente, en lugar de intentar automatizar todo desde el comienzo.

Además de facilitar las primeras implementaciones, este acompañamiento permitiría observar:

- qué información cuesta más obtener;
- qué preguntas generan confusión;
- qué partes requieren intervención experta;
- qué pasos se repiten entre clientes;
- qué decisiones puede tomar un agente de IA;
- qué partes conviene automatizar progresivamente.

La dirección sería comenzar con un onboarding asistido y utilizar la experiencia real para reducir gradualmente la dependencia de nuestro tiempo, sin automatizar prematuramente un proceso que todavía no comprendemos por completo.

## 8. Propuesta: una biblioteca de casos concretos que el agente debe poder resolver

- **Estado:** Decisión arquitectónica aceptada; implementación pendiente

Juan validó organizar el conocimiento operativo mediante tipos de caso y propuso llevar la idea a skills: un componente tipifica la situación y una skill específica guía su resolución. Para el piloto se comienza con un Google Doc estructurado por casos y pasos.

Cuando un caso desconocido se escala y una persona lo resuelve, el sistema puede proponer un nuevo caso o skill. Esa propuesta debe ser revisada, evaluada, aprobada y publicada como una versión nueva; nunca modifica producción automáticamente.

Ver [ADR-0009](../decisions/0009-case-oriented-operational-knowledge.md) y [Biblioteca de casos y skills supervisadas](case-library-and-supervised-skills.md).

Plantearle a Juan qué opina sobre representar una parte del conocimiento y del comportamiento del sistema mediante una biblioteca de casos posibles.

Cada caso documentaría una situación concreta que aparece con frecuencia en conversaciones reales y que el agente comercial debería ser capaz de resolver. Por ejemplo, si los compradores suelen tener problemas al intentar pagar una formación en Hotmart, podría existir un caso como **«recuperación de compra fallida mediante Hotmart»**.

La biblioteca debería reunir, para cada caso, toda la información necesaria para que el agente pueda conducirlo de punta a punta. Inicialmente podría documentar:

- cómo reconocer que el caso está ocurriendo;
- qué información necesita obtener del potencial cliente;
- qué explicaciones o alternativas puede ofrecer;
- qué pasos debe seguir;
- qué límites, riesgos o afirmaciones prohibidas existen;
- cuándo puede considerar el caso resuelto;
- cuándo debe derivarlo a una persona;
- ejemplos concretos de resolución correcta e incorrecta.

Parte del proceso mediante el cual el infoproductor aporta conocimiento al sistema podría consistir en identificar, crear y documentar estos casos. A medida que surjan situaciones nuevas en conversaciones reales, la biblioteca podría ampliarse de forma supervisada para que el agente cuente con instrucciones cada vez más completas.

La pregunta para Juan es si esta biblioteca de casos representa una forma útil y comprensible de organizar el conocimiento operativo del agente, y qué tipos de casos considera indispensables para una primera implementación real.

## Distinción que conviene mantener durante la conversación

Las dos primeras dudas están relacionadas, pero no son la misma:

1. **Conocimiento comercial:** qué sabe el agente sobre el producto, la oferta y sus restricciones.
2. **Control de comportamiento:** qué intenta conseguir, cómo conduce la conversación y qué seguimientos realiza.

La conversación con Juan debería aclarar tanto el mecanismo de entrada de información como el nivel de control final que tendrá el infoproductor sobre cada dimensión.

## Próxima reunión — definiciones para activar el piloto de Lancemos

- **Estado:** Pendientes de Juan

### 1. Número y cuenta de WhatsApp oficial

- ¿Qué número se utilizará para el piloto?
- ¿Cuál es la cuenta WABA (`WhatsApp Business Account`) propietaria del número?
- ¿Qué cuenta e inbox de Chatwoot deben quedar vinculados a ese número?

### 2. Templates de WhatsApp

- Confirmar qué templates se utilizarán para el primer contacto y para los
  seguimientos que ocurran fuera de la ventana permitida.
- Juan debe revisar y aprobar expresamente todos los templates antes de que se
  solicite su aprobación en Meta o se habiliten para el piloto.
- Para cada template deben quedar definidos el copy, el idioma y las variables
  permitidas.

### 3. Producto y oferta de Hotmart

- ¿Cuál es el producto exacto de Hotmart que participará del piloto?
- ¿Cuál es la oferta exacta cuyo abandono debe disparar el webhook de
  `PURCHASE_OUT_OF_SHOPPING_CART`?
- Registrar los identificadores canónicos de website, producto y oferta una vez
  confirmados, sin incluir credenciales en la documentación.

Estas tres definiciones son suficientes como agenda mínima de la próxima reunión.
Los valores continúan pendientes y no deben asumirse ni configurarse antes de la
confirmación de Juan.
