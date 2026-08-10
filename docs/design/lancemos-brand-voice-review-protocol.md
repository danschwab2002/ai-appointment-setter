# Protocolo — revisión de Brand Voice y ejemplos de Lancemos

- **Estado:** Protocolo propuesto para el onboarding manual
- **Alcance:** extraer, sanitizar, revisar y aprobar voz escrita para la primera release
- **No implica:** Brand Voice aprobado, identidad personal clonada ni cambio del perfil productivo

## 1. Resultado esperado

Producir dos artefactos revisables:

1. reglas observables de Brand Voice, cada una con evidencia sanitizada y decisión del responsable;
2. ejemplos positivos y negativos que puedan reutilizarse como evaluación.

La voz controla cómo se expresa el agente. No define hechos, precio, estrategia, autorización, secuencia, identidad canónica ni ejecución.

La prueba documentada en
`docs/operations/2026-08-06-brand-voice-onboarding-trial.md` valida el proceso
conceptual y una voz privada anterior. No demuestra que esa voz pertenezca a
Lancemos ni autoriza reutilizar sus artefactos privados. Este protocolo exige
fuentes y aprobación específicas del owner de voz de Lancemos.

## 2. Fuente y custodia

Fuentes aceptables:

- exportaciones de conversaciones donde pueda identificarse al autor objetivo;
- mensajes seleccionados manualmente por el responsable;
- ejemplos positivos o negativos aportados deliberadamente.

Antes de analizar:

1. mantener archivos y PII fuera de Git;
2. identificar mensajes del autor objetivo;
3. excluir leads, otros vendedores, bots y soporte irrelevante;
4. escanear PII, URLs, credenciales, datos financieros y adjuntos;
5. eliminar hechos comerciales transitorios y texto no necesario;
6. detener el proceso y alertar sin repetir valores si aparece un posible secreto.

Una fuente declarada “sanitizada” no evita este control.

## 3. Registro de revisión

```yaml
review_id: lancemos-brand-voice-v1
status: source_review_pending
source_summary:
  exports: 0
  messages_total: 0
  owner_messages: 0
  usable_owner_messages: 0
  raw_content_in_repository: false
sanitization:
  status: pending
  detected_categories: []
  values_retained: false
approval:
  all_candidates_resolved: false
  prohibited_behaviors_reviewed: false
  representative_tests_reviewed: false
  owner_approved: false
source_refs: []
decision_refs: []
```

No registrar nombres, teléfonos, emails, URLs privadas ni fragmentos que permitan reidentificar una conversación.

Cada `source_ref` registra owner, fecha de consulta, alcance respaldado y estado
de sanitización. Cada `decision_ref` identifica regla/test, versión, decisión,
reviewer, fecha y evidencia sanitizada.

## 4. Candidatos de voz

Una regla por decisión:

| Campo | Contenido |
|---|---|
| ID | estable y sin PII |
| Dimensión | tratamiento, longitud, ritmo, preguntas, vocabulario, puntuación, directness, cierre |
| Regla propuesta | conducta observable |
| Confianza | baja, media o alta |
| Evidencia | resumen agregado o paráfrasis sanitizada |
| Estado | pending, confirmed, modified, discarded |
| Nota del responsable | explicación opcional |
| Destino alternativo | knowledge, policy, kernel, evaluation o none |

Una regla descartada como voz puede pertenecer a otra capa, pero nunca se publica automáticamente allí.

## 5. Dimensiones a revisar

- voseo, tuteo o formalidad;
- cercanía y forma de presentarse;
- longitud y ritmo de los mensajes;
- cantidad semántica de datos solicitados por turno;
- respuesta a preguntas directas;
- vocabulario y expresiones permitidas o evitadas;
- puntuación, emojis, listas y mayúsculas;
- reconocimiento de dudas o preocupaciones;
- grado de directness e incertidumbre;
- transición hacia el próximo objetivo y cierre.

“Una sola pregunta” significa pedir un solo dato u objetivo, no contar signos `?`.

## 6. Exclusiones obligatorias

No inferir como voz:

- precios, descuentos, financiación o garantías;
- promesas y afirmaciones de resultados;
- reglas de calificación o de follow-up;
- decisiones de árbol o playbook;
- errores accidentales;
- PII, secretos o instrucciones de leads;
- identidad engañosa o imitación personal.

La identidad transparente permanece en el kernel y debe validarse por separado.

## 7. Ejemplos positivos y negativos

| ID | Momento | Contexto factual ficticio | Mensaje del lead | Respuesta deseada | Contraejemplo | Regla demostrada | Estado |
|---|---|---|---|---|---|---|---|
| voice-example-001 | apertura | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | `PENDIENTE` | pending |

Todo test usa hechos ficticios explícitos. No debe exigir que el modelo recuerde una oferta real todavía no cargada.

Momentos mínimos:

- apertura de conversación nueva;
- continuidad después de una respuesta;
- pregunta factual directa;
- explicación breve;
- reconocimiento de una preocupación;
- pedido de ayuda humana;
- cierre cordial.

Apertura y continuidad se aprueban por separado.

## 8. Sesión de aprobación

1. revisar el resumen de fuentes y sanitización;
2. confirmar, modificar o descartar cada regla;
3. revisar comportamientos expresamente prohibidos;
4. ejecutar varios resultados por escenario para detectar variación;
5. registrar por separado la decisión sobre reglas y sobre respuestas de prueba;
6. resolver contradicciones con oferta, política, canal y kernel;
7. obtener aprobación explícita del responsable.

Un comentario aislado produce una propuesta de cambio, no mutación directa.

## 9. Gate de finalización

- [ ] autoría de las fuentes verificada;
- [ ] sanitización independiente completada;
- [ ] cero contenido crudo en Git;
- [ ] todas las reglas resueltas;
- [ ] comportamientos prohibidos revisados;
- [ ] ejemplos positivos y negativos aprobados;
- [ ] pruebas repetidas con hechos ficticios;
- [ ] apertura y continuidad evaluadas;
- [ ] identidad transparente preservada;
- [ ] aprobación del responsable registrada;
- [ ] resultado incorporable como versión inmutable, no como edición live.
