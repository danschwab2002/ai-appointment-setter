# Experiencia de configuración mediante Automation Expert

- **Estado:** Base de diseño aprobada; implementación diferida
- **Fecha de aceptación:** 2026-08-07
- **Alcance:** Interacción del infoproductor para proponer automatizaciones y comportamiento comercial
- **Implementación:** No iniciada
- **Fuente:** Reunión con Juan Martitegui del 7 de agosto de 2026 ([grabación de Fathom](https://fathom.video/share/yj78Kt41tfdyWwPwTsqk-SUcDC3x9JSi))

## 1. Propósito

El infoproductor no debería necesitar comprender prompts, schedulers, árboles técnicos ni schemas internos para dirigir el sistema. La experiencia principal debe parecerse a explicar una tarea a un empleado inteligente y responsable.

ADR-0006 define que Automation Expert diseña y explica mientras la aplicación valida y ejecuta. Este documento detalla la experiencia conversacional propuesta sin cambiar esa frontera.

## 2. Flujo principal

```text
instrucción del infoproductor
→ interpretación de intención y alcance
→ preguntas por información faltante
→ contraste con datos y buenas prácticas
→ pushback cuando corresponda
→ propuesta concreta y comprensible
→ confirmación humana
→ representación estructurada
→ validación determinística
→ aprobación y publicación versionada
```

Ejemplo:

> «Quiero que el primer seguimiento salga a las tres horas.»

El Automation Expert no obliga al usuario a editar una tabla. Debe poder explicar:

- qué caso y política se modificarán;
- cómo está configurado actualmente;
- qué cambio propone;
- qué consecuencias previsibles tiene;
- si contradice evidencia, costos o buenas prácticas;
- qué límites duros siguen aplicando;
- cuándo comenzaría a utilizarse.

## 3. Pushback

El agente debe comportarse como un especialista, no como un formulario obediente. Si una instrucción parece ineficaz, incoherente o riesgosa debe:

1. explicar el problema en lenguaje simple;
2. citar la evidencia o regla disponible;
3. proponer una alternativa;
4. distinguir una recomendación de un bloqueo obligatorio;
5. permitir confirmar una preferencia permitida;
6. rechazar únicamente aquello que viola una restricción no negociable.

Ejemplo permitido con advertencia:

> Cambiar el primer seguimiento de seis a tres horas, si la política y el canal lo permiten.

Ejemplo bloqueado:

> Enviar un mensaje por segundo hasta que la persona compre.

El bloqueo no depende sólo del criterio del modelo. Frecuencia, consentimiento, opt-out, reglas del canal y límites de seguridad deben validarse mediante políticas determinísticas.

## 4. Resultado estructurado

Aunque la conversación sea flexible, el resultado debe ser preciso. Automation Expert produce una propuesta que identifique, como mínimo:

- alcance afectado;
- versión base;
- cambios solicitados;
- razón declarada por el usuario;
- advertencias;
- alternativas consideradas;
- configuración resultante;
- validaciones requeridas;
- aprobación pendiente;
- efecto esperado y forma de revertirlo.

La forma exacta se definirá en un contrato cuando se implemente. El agente no escribe directamente en tablas activas ni modifica profiles de producción.

## 5. Límites de autoridad

### El infoproductor puede decidir, dentro de límites

- objetivos comerciales;
- tiempos permitidos;
- número de seguimientos dentro de máximos;
- tono y estrategia;
- casos prioritarios;
- criterios comerciales;
- responsables y condiciones de escalamiento.

### El Automation Expert puede

- interpretar;
- pedir aclaraciones;
- hacer pushback;
- comparar alternativas;
- proponer configuración;
- explicar el resultado;
- iniciar un borrador mediante una API acotada.

### Los servicios determinísticos conservan

- validación de schema;
- máximos de frecuencia;
- consentimiento y opt-out;
- restricciones de WABA y templates;
- horarios y zonas permitidas;
- takeover humano;
- idempotencia;
- auditoría;
- publicación y rollback autorizados.

## 6. Relación con controles visuales

Los diales, formularios y presets no se descartan, pero no son el punto de partida. Pueden aparecer cuando la experiencia real demuestre dimensiones repetibles y comprensibles.

Un control visual debe ser una proyección de una configuración concreta, no una metáfora ambigua. Por ejemplo, «agresividad» sólo debería exponerse si el sistema puede explicar qué tiempos, frecuencias, condiciones y objetivos cambia.

## 7. Estrategia para el piloto

Automation Expert no es requisito para activar la primera oferta de Lancemos. Durante el piloto, nosotros podemos cumplir manualmente este rol:

1. recibir la instrucción del negocio;
2. hacer preguntas y pushback;
3. preparar la configuración;
4. mostrar el resultado;
5. obtener aprobación;
6. publicar de forma controlada.

Las interacciones repetidas del piloto servirán para diseñar el contrato y la experiencia real del agente, evitando automatizar supuestos.

## 8. Temas abiertos

- contrato estructurado de propuesta;
- permisos y roles de aprobación;
- fuentes de evidencia para hacer pushback;
- distinción entre advertencia y bloqueo;
- explicación previa del efecto de un cambio;
- simulación o preview;
- integración con políticas de seguimiento, casos y Conversation Releases;
- controles visuales que complementen la conversación;
- métricas para evaluar la calidad de las recomendaciones.

## 9. Documentos relacionados

- [ADR-0006: producto compuesto por tres agentes](../decisions/0006-three-agent-product-surface.md)
- [Diseño del motor de seguimiento](followup-engine.md)
- [Conversation Release MVP](conversation-release-mvp.md)
- [Biblioteca de casos](case-library-and-supervised-skills.md)
- [Dirección del piloto de Lancemos](lancemos-pilot-product-direction.md)
