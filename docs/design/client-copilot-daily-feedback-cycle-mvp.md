# Ciclo diario de feedback del Client Copilot — MVP

- **Estado:** Propuesta para revisión
- **Alcance:** Revisión diaria, supervisada y auditable de conversaciones comerciales reales
- **Implementación:** No implementado
- **Relacionado con:** [ADR-0006](../decisions/0006-three-agent-product-surface.md), [Conversation Release MVP](conversation-release-mvp.md) y [preguntas para Juan](questions-for-juan.md)

## 1. Propósito

El ciclo diario permite que el infoproductor revise cómo conversó su agente comercial y entregue feedback sin editar prompts, reglas técnicas ni archivos de producción.

El producto debe convertir observaciones sobre conversaciones reales en evidencia y cambios candidatos controlados. El feedback nunca modifica directamente una `Conversation Release` activa.

El objetivo del MVP no es automatizar el aprendizaje completo. Es demostrar un circuito útil y seguro:

```text
actividad conversacional canónica
→ lote diario idempotente
→ revisión guiada conversación por conversación
→ decisión y feedback del infoproductor
→ interpretación confirmada
→ clasificación y cambio candidato
→ validación y aprobación posteriores
→ eventual nueva Conversation Release
```

## 2. Principios

1. **Revisar lo realmente entregado.** La fuente es la conversación canónica y los mensajes publicados; no la memoria del agente ni propuestas que nunca llegaron al prospecto.
2. **Una conversación a la vez.** El Copilot evita volcar transcripciones completas o mezclar correcciones de casos distintos.
3. **Decisiones simples.** Cada conversación admite `correcta`, `correcta_con_feedback` u `omitir`.
4. **Feedback como evidencia, no como mutación.** Se conserva el comentario original y se produce una interpretación separada.
5. **Cambios supervisados.** Una corrección crea como máximo un cambio candidato. Su validación, aprobación y activación ocurren fuera de la revisión diaria.
6. **Separar aprendizaje de incidentes.** Duplicados, envíos indebidos, fallas de autorización, takeover o entrega se derivan al circuito operativo y no se convierten en preferencias conversacionales.
7. **Aplicación determinística.** Scheduling, autorización, selección, idempotencia, persistencia y publicación pertenecen a la aplicación. El Copilot resume, guía, interpreta y propone.
8. **Aislamiento por cliente.** Un lote, revisor, conversación y cambio candidato pertenecen a un único tenant y alcance comercial.

## 3. Experiencia mínima del infoproductor

### 3.1. Inicio

Una vez por día, el sistema crea como máximo un lote por tenant, ventana de revisión y versión de criterio de selección. Si no existe actividad elegible, puede enviar un aviso breve o cerrar el lote vacío según la preferencia futura del cliente.

El Copilot inicia en un canal privado autorizado con un mensaje equivalente a:

> Hoy hay 7 conversaciones para revisar. Te las voy a mostrar de a una. Podés marcarlas como correctas, corregirlas o pasarlas.

El canal de mensajería es una superficie de interacción. No se convierte en fuente canónica ni otorga autoridad por conocer un identificador de chat.

### 3.2. Presentación de cada conversación

Para cada ítem se muestra solamente lo necesario para juzgar la conducta:

- contexto breve del prospecto y del caso, minimizado;
- objetivo aparente del agente;
- fragmentos relevantes de mensajes entrantes y salientes;
- resultado observado dentro de la ventana;
- `Conversation Release` utilizada;
- enlace a la conversación completa en Chatwoot cuando esté disponible y autorizado.

La presentación debe distinguir claramente:

- mensajes del prospecto;
- mensajes realmente enviados por el agente;
- mensajes humanos;
- eventos operativos relevantes, sin confundirlos con contenido conversacional.

No se deben incluir secretos, datos innecesarios ni transcripciones completas por defecto.

### 3.3. Decisión

El infoproductor elige:

- **Correcta:** la conducta fue aceptable; no implica convertir cada frase en ejemplo permanente.
- **Correcta con feedback:** existe algo que debería haber ocurrido de otra manera.
- **Omitir:** no se registra juicio positivo ni negativo.

Si elige `correcta_con_feedback`, el Copilot pregunta qué debería haber sucedido. Debe conservarse el comentario textual exacto antes de interpretarlo.

### 3.4. Confirmación de interpretación

El Copilot devuelve una interpretación acotada, por ejemplo:

> Entendí que primero debería responder la pregunta directa sobre el precio y recién después retomar la calificación. ¿Es correcto?

El infoproductor puede:

- confirmar;
- corregir la interpretación;
- cancelar el feedback sin perder el comentario original.

La confirmación valida qué quiso decir el usuario; todavía no aprueba una regla global ni una release.

### 3.5. Cierre

Al completar o cerrar la revisión se informa:

- conversaciones presentadas;
- correctas;
- corregidas;
- omitidas;
- pendientes;
- incidentes derivados;
- cambios candidatos creados;
- cambios activados durante el ciclo, que en el MVP debe ser siempre `0`.

Una revisión incompleta puede reanudarse desde el siguiente ítem pendiente sin duplicar decisiones ni candidatos.

## 4. Población de revisión del MVP

Para el primer piloto y mientras el volumen sea bajo, la propuesta es incluir **todas las conversaciones con nueva actividad comercial atribuible al agente dentro de la ventana**, siempre que puedan reconstruirse desde datos canónicos.

Una conversación puede abarcar varios días. En ese caso se revisa sólo la actividad nueva de la ventana y se agrega el contexto previo mínimo necesario.

### 4.1. Elegible

Un ítem es elegible cuando:

- pertenece al tenant y alcance configurados;
- tuvo al menos un mensaje comercial del agente realmente entregado durante la ventana;
- la aplicación puede identificar conversación, mensajes, autores y release utilizada;
- el revisor está autorizado para ese tenant;
- no fue incluido ya en el mismo lote lógico.

### 4.2. No elegible o derivado

No se presenta como aprendizaje conversacional cuando:

- sólo existen propuestas no enviadas;
- no puede probarse qué mensaje fue entregado;
- la conversación pertenece a otro tenant o alcance;
- existe una inconsistencia de identidad o autorización;
- el caso contiene un incidente crítico que exige atención operativa previa.

Los casos no elegibles deben quedar contabilizados mediante reason codes, sin descartarse silenciosamente.

Detectar un incidente y registrar la decisión conversacional son dimensiones
independientes. Un incidente puede bloquear la presentación hasta que sea seguro
continuar, pero no se convierte en una decisión del revisor ni borra una decisión
conversacional ya registrada. Su creación o reutilización pertenece a un workflow
operativo con identidad, idempotencia y ciclo de vida propios.

### 4.3. Escala futura

La priorización por riesgo, resultado, novedad o muestreo queda diferida hasta observar volumen real. El MVP no debe inventar un ranking prematuro.

## 5. Fronteras de responsabilidad

### Aplicación determinística

Es responsable de:

- timezone, cutoff y cálculo de la ventana;
- scheduler y reintentos;
- autorización del tenant y revisor;
- recuperación de conversaciones y mensajes canónicos;
- selección versionada e idempotente;
- creación y persistencia de lotes e ítems;
- orden y reanudación;
- registro de decisiones y feedback original;
- prevención de duplicados;
- autorización final y ledger durable de cada entrega al canal privado;
- reconciliación de entregas con resultado ambiguo;
- auditoría y retención;
- creación durable de cambios candidatos;
- validación, aprobación, publicación y rollback de releases.

### Client Copilot

Es responsable de:

- resumir el contexto permitido;
- explicar el objetivo aparente sin presentarlo como certeza interna;
- conducir la revisión de a un ítem;
- pedir una corrección concreta;
- proponer una interpretación fiel y acotada;
- clasificar el feedback;
- redactar un cambio candidato explicable;
- cerrar con un resumen.

### Canal privado

Es responsable solamente de entregar la conversación del Copilot y recibir respuestas. Telegram, Slack u otro conector soportado pueden utilizarse inicialmente. La identidad externa debe vincularse previamente con un revisor autorizado.

### Chatwoot y almacenamiento canónico

Chatwoot conserva la conversación completa visible para operación. La aplicación
conserva las identidades internas y evidencia inmutable, minimizada y autorizada de
lo efectivamente presentado: contenido o representación canónica versionada,
autoría, mensajes entregados, release, sanitización aplicada, renderer y hashes.
Conservar sólo referencias a Chatwoot no alcanza, porque el proveedor podría editar
o eliminar el contenido. La custodia de esta evidencia debe respetar acceso,
cifrado, retención y eliminación definidos antes de usar datos reales.

## 6. Modelo conceptual durable

Los nombres son conceptuales y no constituyen todavía un contrato SQL.

### 6.1. `review_schedule`

Configuración versionada por tenant:

- tenant y alcance;
- timezone;
- cadencia;
- cutoff;
- revisor autorizado;
- canal privado autorizado;
- versión del criterio de selección;
- versión exacta del algoritmo y configuración de selección;
- estado activo o pausado.

### 6.2. `review_batch`

Representa una ventana exacta:

- tenant y alcance;
- inicio inclusivo y fin exclusivo de ventana en UTC (`[start, end)`);
- timezone y cutoff usados;
- versión del criterio de selección;
- versión exacta del algoritmo y configuración de selección;
- revisor autorizado capturado;
- estado;
- contadores finales derivados de los estados durables, no mantenidos como autoridad independiente;
- timestamps y motivo de cierre.

Debe existir una única identidad lógica por tenant, alcance, ventana y versión de selección.

### 6.3. `review_item`

Vincula el lote con una conversación y actividad exactas:

- conversación canónica;
- IDs de mensajes relevantes y evidencia inmutable de lo presentado;
- mensajes del agente efectivamente entregados;
- release utilizada;
- rango de actividad revisado;
- orden estable;
- estado y decisión;
- enlace canónico opcional;
- reason code cerrado si fue excluido o derivado;
- versión del renderer, sanitizador y política de minimización;
- hash del payload presentado.

El snapshot minimizado se congela al crear el lote y se fija nuevamente el payload
exacto al presentarlo. Una edición posterior en el proveedor no puede cambiar
silenciosamente qué se evaluó. La política de retención puede eliminar contenido,
pero debe conservar evidencia hash-only y un tombstone auditable de la eliminación.

### 6.4. `owner_feedback`

Conserva:

- decisión del revisor;
- comentario original exacto;
- actor e identidad autorizada;
- momento;
- interpretación propuesta;
- interpretación confirmada o corregida;
- clasificación;
- vínculo independiente con cambio candidato;
- vínculo opcional con un incidente operativo creado por su workflow propio.

El comentario original es inmutable. Las interpretaciones posteriores son registros separados y auditables.

### 6.5. `candidate_change`

Describe una propuesta que todavía no afecta producción:

- clasificación;
- alcance sugerido y alcance resuelto antes de compilar un draft;
- evidencia de origen;
- release observada en la conversación;
- release base elegida explícitamente para el cambio;
- versiones exactas de los artefactos base;
- versión del clasificador y política de clasificación;
- artefacto objetivo;
- diff o intención propuesta;
- riesgos y casos afectados;
- release borrador resultante, si posteriormente se crea.

Varios feedbacks compatibles pueden respaldar un candidato futuro, pero no deben fusionarse silenciosamente durante la revisión.

Antes de compilar un draft se compara la release base del candidato con la release
vigente del alcance. Si cambió, el sistema bloquea la aplicación automática y exige
rebase o descarte explícito y auditable. La validación y aprobación productivas
pertenecen exclusivamente a una `Conversation Release` exacta y atómica, no al
candidato aislado.

### 6.6. `operational_incident_reference`

El ciclo sólo conserva la referencia a un incidente creado o reutilizado por el
workflow operativo. Su identidad semántica debe impedir tickets duplicados ante
replays. El incidente no comparte estados ni comandos con `review_item` y nunca
origina por sí mismo un cambio candidato.

## 7. Estados conceptuales

### Lote

```text
pending → ready → in_review → completed
   ├────→ completed_empty
   ├────→ blocked
   └────→ expired
ready/in_review ────→ partially_completed
ready/in_review ────→ blocked
ready/in_review ────→ expired
```

- `pending`: ventana identificada; materialización pendiente.
- `ready`: selección congelada y disponible.
- `in_review`: al menos un ítem fue presentado.
- `completed`: todos los ítems tienen una decisión final.
- `completed_empty`: la selección se materializó correctamente sin ítems elegibles.
- `partially_completed`: cierre explícito con ítems pendientes.
- `expired`: venció según la política, sin inventar decisiones.
- `blocked`: no puede continuar por autorización o inconsistencia canónica.

### Ítem

```text
pending → presented → accepted
                    ├→ awaiting_feedback → awaiting_confirmation → corrected
                    │                         └───────────────→ feedback_cancelled
                    ├→ skipped
```

Los estados terminales son `accepted`, `corrected`, `feedback_cancelled` y
`skipped`. `feedback_cancelled` conserva el comentario original, no crea candidato
y cuenta como ítem resuelto sin juicio positivo. La referencia a un incidente es
ortogonal y no constituye un estado del ítem.

Una decisión terminal no se sobrescribe. Si el revisor necesita corregirla, crea
una enmienda que referencia y supersede la decisión anterior. La misma transición
invalida de forma auditable cualquier interpretación o candidato derivado anterior
antes de permitir otro. Los contadores del lote se derivan sólo de la última
decisión vigente de cada ítem.

### Cambio candidato

```text
proposed → confirmed → ready_for_draft
   ├───────────────→ rejected
   ├───────────────→ needs_clarification
   └───────────────→ superseded
```

El candidato sólo atraviesa triage y confirmación. `ready_for_draft` significa que
puede evaluarse para compilar una release, no que el cambio esté validado o
aprobado. Validación, aprobación, activación y rollback pertenecen únicamente al
ciclo de vida de una `Conversation Release` exacta.

## 8. Clasificación del feedback

| Señal del infoproductor | Clasificación | Destino inicial |
|---|---|---|
| “El precio o condición está mal” | Corrección factual | Conocimiento comercial |
| “No uses tantos emojis” | Voz o redacción | Brand voice o ejemplos |
| “Respondé la duda antes de preguntar” | Camino u orden | Política conversacional o principio local |
| “Necesitamos preguntar el presupuesto” | Calificación | Política de calificación |
| “En este caso entendió mal algo puntual” | Caso aislado | Caso de evaluación/regresión |
| “Mandó dos veces / escribió cuando no debía” | Falla operativa | Incidente; no aprendizaje |

La clasificación del Copilot es una propuesta. Debe preservarse la declaración original y mostrarse la interpretación al usuario. Un comentario aislado no se generaliza automáticamente a todas las conversaciones, productos u ofertas.

## 9. Idempotencia, reanudación y concurrencia

El scheduler puede ejecutar más de una vez. Por eso:

- crear el mismo lote lógico reutiliza el existente;
- la selección se materializa una vez con orden estable;
- presentar un ítem repetidamente no duplica el feedback;
- cada comando durable tiene una clave estable, una versión/fence esperado y un
  fingerprint del payload semántico completo;
- reutilizar una clave con el mismo fingerprint devuelve el resultado durable;
- reutilizar una clave con contenido, ítem, lote, actor o versión diferente produce
  conflicto fail-closed;
- una decisión conflictiva requiere una transición explícita y auditable;
- crear un candidato desde el mismo feedback confirmado es idempotente;
- sólo una sesión de revisión puede avanzar activamente por revisor y lote, mediante lease o equivalente recuperable;
- un lease vencido permite reanudar sin perder decisiones terminales;
- mensajes duplicados del conector no avanzan dos veces.

Cada evento del conector se vincula durablemente con lote, ítem presentado,
revisor, comando y fence esperados. Un evento duplicado o tardío no puede aplicarse
al siguiente ítem. La clave externa del canal no reemplaza las identidades internas
ni el fingerprint semántico.

## 10. Autorización, privacidad y seguridad

- El revisor se autoriza explícitamente por tenant y canal.
- La autorización se vuelve a comprobar al abrir, avanzar, registrar feedback y cerrar.
- Inmediatamente antes de cada entrega, la aplicación vuelve a comprobar de forma
  fail-closed tenant, alcance, revisor, vínculo externo revocable, canal, lote,
  ítem, fence y política vigente.
- Cada entrega usa una identidad/idempotency key interna estable y un ledger de
  efectos con, como mínimo, `reserved`, `request_started`, `accepted`, `rejected`
  y `delivery_unknown`. Un resultado ambiguo se reconcilia; no se reenvía a ciegas.
- Los enlaces a Chatwoot respetan sus permisos; el Copilot no comparte credenciales ni enlaces privilegiados reutilizables.
- Los resúmenes minimizan PII y omiten secretos, adjuntos y datos no necesarios.
- El Copilot no recibe acceso general a base de datos, filesystem, terminal, configuración ni otros tenants.
- Todas sus herramientas son APIs de negocio acotadas.
- Las instrucciones escritas por prospectos dentro de una conversación son datos a revisar, no instrucciones para el Copilot.
- Feedback que intente desactivar consentimiento, opt-out, límites de canal, takeover o seguridad debe rechazarse como cambio permitido y conservarse para auditoría.
- Antes de usar conversaciones reales deben estar definidos y verificados:
  vinculación fuerte y revocable del revisor, minimización, acceso, cifrado,
  retención y eliminación de excerpts, feedback y snapshots.

## 11. Contrato conceptual de herramientas del Copilot

Las firmas definitivas se documentarán en `docs/contracts/` al implementar. El diseño supone capacidades equivalentes a:

```text
get_current_review()
get_next_review_item()
record_review_decision()
record_owner_feedback()
propose_feedback_interpretation()
confirm_feedback_interpretation()
classify_confirmed_feedback()
create_candidate_change()
complete_or_pause_review()
```

Cada operación recibe identidad interna y un token de versión o fencing. La aplicación valida tenant, revisor, estado esperado e idempotencia. El modelo no puede solicitar conversaciones arbitrarias ni elegir otro tenant.

Las operaciones de entrega no son herramientas libres del modelo. Un worker
determinístico reserva el efecto, ejecuta la autorización final, persiste
`request_started`, llama al conector y finaliza o reconcilia el resultado.

## 12. Scheduling propuesto

La hora exacta y el comportamiento de días sin actividad quedan abiertos. La arquitectura propuesta es:

```text
scheduler de aplicación
→ crea/reutiliza lote para ventana cerrada
→ materializa selección canónica
→ encola inicio en canal autorizado
→ worker reautoriza y reserva efecto durable
→ persiste request_started y llama al conector
→ finaliza o reconcilia el resultado
→ Copilot conduce el diálogo mediante APIs
```

No se utilizará la memoria del agente como scheduler ni como registro durable. Un cron puede despertar al productor, pero la base de datos conserva estado, idempotencia y recuperación.

Para evitar revisar actividad todavía inestable, el final de la ventana puede incorporar una demora de seguridad respecto del cutoff. Ese valor deberá configurarse y probarse con el primer infoproductor.

## 13. Primer corte vertical implementable

El primer corte debe probar el valor antes de automatizar todo:

1. Un operador crea un lote para una ventana explícita.
2. La aplicación obtiene un fixture sanitizado representativo. Las conversaciones
   reales sólo se habilitan después de verificar vinculación, minimización,
   cifrado, acceso, retención y eliminación.
3. Materializa ítems y orden estable.
4. El Copilot presenta un ítem en un canal privado autorizado.
5. El infoproductor elige `correcta`, `correcta_con_feedback` u `omitir`.
6. Si corrige, se guarda el texto exacto, se confirma la interpretación y se clasifica.
7. Se crea un cambio candidato sin tocar producción.
8. Se reanuda después de una interrupción y se comprueba que no hay duplicados.
9. Se inyecta un resultado ambiguo del conector y se comprueba reconciliación sin
   reenvío ciego.
10. Se cierra el lote con resumen y `activated_changes = 0`.

El scheduler diario automático, la agrupación de candidatos, las evaluaciones automáticas y la publicación de releases se agregan después de validar este circuito.

## 14. Criterios de aceptación del MVP

- Un lote diario puede reconstruirse exactamente por tenant, ventana y versión de selección.
- Cada ítem referencia mensajes realmente entregados y la release utilizada.
- El revisor autorizado puede evaluar de a una conversación.
- El feedback original se conserva separado de su interpretación.
- La interpretación requiere confirmación antes de crear un candidato.
- Los replays no duplican lotes, decisiones ni candidatos.
- Una clave reutilizada con payload distinto falla por conflicto.
- Una revisión interrumpida puede reanudarse.
- Los incidentes operativos no se convierten en reglas conversacionales.
- Un incidente y una decisión conversacional pueden coexistir sin compartir estado.
- Una entrega revocada antes de `request_started` no sale; una entrega ambigua se
  reconcilia sin reenvío ciego.
- Ningún feedback modifica una release activa.
- Un candidato obsoleto no puede aplicarse silenciosamente sobre otra release base.
- El cierre informa cantidades consistentes y cero cambios activados.
- El Copilot sólo usa APIs acotadas y no accede a otros tenants.

## 15. Temas abiertos para decidir con evidencia

1. Hora diaria, timezone y demora respecto del cutoff.
2. Canal inicial del primer infoproductor; el mecanismo concreto de vinculación
   debe satisfacer la vinculación fuerte y revocable ya exigida.
3. Avisar o permanecer silencioso cuando no hay actividad elegible.
4. Momento y política de expiración de una revisión incompleta.
5. Si existe un único revisor o roles separados de revisor y aprobador.
6. Cuánto contexto previo mostrar dentro de los límites de minimización aprobados.
7. Valores concretos de retención y eliminación; su definición y verificación son
   prerrequisito para usar datos reales.
8. Cuándo pasar de todas las conversaciones a priorización o muestreo.
9. Cómo se agrupan candidatos compatibles sin ocultar su evidencia individual.
10. Evaluaciones y umbrales necesarios antes de crear y aprobar una nueva release.

Estos temas no bloquean el corte con fixtures sanitizados. Vinculación fuerte,
minimización, acceso, cifrado, retención y eliminación sí bloquean cualquier corte
con conversaciones reales.

## 16. Fuera de alcance

- diseñar la personalidad general completa del Client Copilot;
- dashboard propio;
- aprendizaje autónomo o edición directa de prompts;
- activación automática de releases;
- ranking sofisticado de conversaciones;
- métricas comerciales generales no necesarias para revisar conducta;
- soporte multiaprobador avanzado;
- resolución de incidentes operativos dentro del ciclo conversacional;
- diseño definitivo de tablas, SQL, schemas y endpoints.

## 17. Próximo paso recomendado

Después de aceptar o corregir este diseño, definir el **contrato exacto del primer corte vertical**: identidades, estados, comandos y payloads mínimos para crear un lote manual, presentar un ítem y persistir una decisión. Recién sobre ese contrato conviene diseñar el prompt y las herramientas iniciales del Client Copilot.
