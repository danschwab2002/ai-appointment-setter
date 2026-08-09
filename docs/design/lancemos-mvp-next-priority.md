# Próxima prioridad de producto del MVP de Lancemos

- **Estado:** Prioridad de producto aceptada
- **Fecha de aceptación:** 2026-08-09
- **Fecha:** 2026-08-09
- **Alcance:** siguiente resultado de producto después de cerrar `PURCHASE_APPROVED`
- **No implica:** implementación, despliegue ni autorización para contactar leads reales
- **Dirección de producto:** [Dirección del piloto de Lancemos](./lancemos-pilot-product-direction.md)

## 1. Recomendación

La próxima prioridad es **cerrar el perímetro de seguridad del piloto**, comenzando por un **opt-out inbound durable y determinístico**.

El resultado buscado no es solamente “detectar la palabra baja”. Es demostrar que, ante una solicitud inequívoca de no recibir más mensajes, el sistema puede:

```text
reconocerla antes de invocar Hermes
→ persistir la restricción como estado autoritativo
→ cancelar seguimientos futuros
→ bloquear nuevos envíos del mismo propósito
→ sobrevivir duplicados, concurrencia y reinicios
→ dejar evidencia operativa comprensible
```

`PURCHASE_APPROVED`, que está siendo consolidado en otra tarea, cierra una causa de finalización. El opt-out es la siguiente causa de detención obligatoria todavía incompleta. Ambas forman la base de “saber cuándo dejar de escribir”, condición mínima para probar con la operación de Juan.

## 2. Por qué va antes que otras funcionalidades

### Impacto directo en el piloto

Sin opt-out durable no corresponde habilitar una cohorte real, aunque el agente converse bien. Un error de copy puede corregirse; continuar escribiendo después de una baja explícita es un fallo de confianza, operación y cumplimiento.

### Puede verificarse sin esperar dependencias externas

WABA, inbox, número y templates de Lancemos siguen siendo bloqueadores externos. El opt-out puede diseñarse y probarse ahora con fixtures, Postgres, Chatwoot controlado y el único JID allowlisted, sin sustituir silenciosamente WABA por Evolution como canal productivo.

### Reduce riesgo en todas las iteraciones posteriores

La misma restricción debe ganar frente a follow-ups, reintentos, mensajes ya reservados, respuestas generativas y cambios de política. Tenerla temprano permite que cada prueba posterior de conversación, WABA y handoff herede una frontera segura.

## 3. Alcance funcional mínimo

### Entrada

- Sólo mensajes públicos entrantes de la conversación canónica.
- Normalización conservadora de texto, puntuación, mayúsculas y espacios.
- Conjunto pequeño de solicitudes inequívocas en español, respaldado por ejemplos revisados.
- Frases ambiguas o contextuales no producen una baja automática; se escalan o continúan según política.

### Decisión y persistencia

- La detección ocurre antes de invocar Hermes.
- Un clasificador generativo nunca es la autoridad única.
- La restricción se persiste en la fuente canónica de autorización por canal y propósito.
- `denied`, `restricted` u opt-out vigente siempre prevalecen sobre autorizaciones anteriores.
- La operación es idempotente ante webhook duplicado o replay.

### Efectos

- Se cancelan acciones pendientes que todavía no comenzaron un efecto externo.
- Una acción en estado externo incierto no se declara cancelada como si nada hubiese ocurrido; se reconcilia fail-closed.
- Todo request saliente reevalúa la restricción inmediatamente antes del efecto.
- La confirmación de baja, si el negocio decide enviarla, tiene una política explícita y no abre una nueva secuencia comercial.
- El operador puede observar quién quedó bloqueado sin exponer PII innecesaria en logs.

### Límites

- No construir todavía un centro general de preferencias.
- No usar el opt-out para aprender Brand Voice.
- No agregar un dashboard general.
- No resolver todas las variantes legales o lingüísticas sin evidencia de conversaciones reales.
- No mezclar esta tarea con WABA, paquete conversacional o reply splitting.

## 4. Matriz de validación desde distintos ángulos

La funcionalidad no queda terminada sólo porque pase un happy path.

### Contrato lingüístico

- expresiones inequívocas: “no me escriban más”, “quiero darme de baja”, “dejen de contactarme”;
- mayúsculas, tildes, espacios, puntuación y mensajes breves;
- negaciones y falsos positivos: “no quiero dejar de recibir”, “dame de baja el precio”, citas o FAQs;
- mensaje compuesto con una baja y otra pregunta;
- texto vacío, multimedia y tipos no soportados.

Los ejemplos definitivos deben revisarse con Juan o el responsable operativo y convertirse en fixtures de regresión.

### Estado y ordenamiento

- opt-out antes de reservar una acción;
- opt-out después de reservar pero antes del request externo;
- opt-out mientras otro worker procesa la conversación;
- opt-out después de una compra o takeover;
- compra, takeover y opt-out concurrentes sin reabrir el caso;
- reautorización anterior que no pisa una baja posterior;
- replay del mismo mensaje sin duplicar auditoría ni efectos.

### Fallos y recuperación

- caída después de persistir la baja y antes de cancelar acciones;
- caída durante la cancelación;
- timeout o respuesta incierta de Chatwoot;
- reinicio del bridge con trabajo admitido;
- PostgREST temporalmente no disponible;
- historial canónico incompleto o fuera de orden.

En todos los casos, la ausencia de certeza debe bloquear nuevos mensajes comerciales.

### Seguridad y datos

- sólo tenant, inbox, conversación y contacto autorizados;
- privilegios SQL mínimos para bridge y roles no administrativos;
- ningún RPC público puede revertir silenciosamente la baja;
- logs sin cuerpo completo, JID, token ni PII innecesaria;
- payloads de prueba sanitizados.

### Verificación integrada

1. pruebas unitarias del detector conservador;
2. pruebas de contrato del bridge;
3. pruebas SQL de precedencia, idempotencia y concurrencia;
4. pruebas del worker con reservas y estados externos inciertos;
5. suite combinada del repositorio;
6. prueba HTTP real con webhook firmado;
7. E2E controlado con el JID allowlisted;
8. reinicio durante el flujo y confirmación de no duplicación;
9. revisión independiente adversarial;
10. evidencia sanitizada en `docs/operations/`.

## 5. Definición de terminado

El opt-out queda listo para el MVP sólo cuando se demuestra que:

- una solicitud inequívoca se persiste antes de cualquier razonamiento generativo;
- todos los caminos salientes relevantes vuelven a consultar la restricción;
- acciones futuras se cancelan sin ocultar efectos externos inciertos;
- duplicados, carreras y reinicios no generan otro mensaje comercial;
- falsos positivos representativos no bloquean contactos;
- existe procedimiento manual para inspeccionar, corregir y pausar;
- la prueba E2E controlada produce evidencia reproducible;
- Juan o el responsable designado aprueba los ejemplos y la política de confirmación.

## 6. Secuencia recomendada del MVP

1. **Finalizar e integrar `PURCHASE_APPROVED`** — tarea actualmente en curso.
2. **Implementar y verificar opt-out inbound durable** — siguiente tarea de ingeniería.
3. **Cerrar el límite del piloto** — tenant, inbox, producto, oferta, cohorte/presupuesto y kill switch.
4. **Probar handoff ejecutable** — pausa, señal visible, nota privada y responsable.
5. **Implementar WABA/template contra el inbox real** — en cuanto Juan entregue accesos y templates.
6. **Preparar una release conversacional manual de Lancemos** — una oferta, casos, hechos, límites, voz y ejemplos aprobados.
7. **Ejecutar la matriz E2E de go/no-go** — abandono, conversación, follow-up, compra, opt-out, takeover, handoff, reinicio y fallos inciertos.
8. **Activar una cohorte mínima supervisada** — sin ampliar oferta ni volumen durante el aprendizaje inicial.

WABA y recopilación de materiales deben avanzar en paralelo como frente operativo externo; no justifican saltar a funcionalidades secundarias mientras están pendientes las guardas internas.

## 7. Trabajo explícitamente postergado

Hasta obtener evidencia del piloto no compiten por prioridad:

- reply splitting;
- Automation Expert;
- Client Copilot;
- onboarding autoservicio;
- biblioteca dinámica completa de casos;
- dashboard general;
- soporte multi-oferta o multi-cliente;
- analítica avanzada;
- cambios automáticos del comportamiento.

## 8. Insumos que pedir a Juan en paralelo

- oferta exacta del piloto y sus identificadores;
- acceso/coordinación de WABA e inbox de Chatwoot;
- templates existentes o copy para aprobación;
- frases reales de baja recibidas por el negocio;
- política deseada de confirmación de baja;
- responsable y horario de takeover/handoff;
- FAQ, límites comerciales, promesas prohibidas y secuencia actual;
- tamaño máximo de la primera cohorte y criterio de pausa.

## 9. Métrica de esta prioridad

La métrica primaria no es cantidad de mensajes enviados. Es:

> **cero mensajes comerciales posteriores a una baja autoritativa, incluso bajo replay, carrera, reinicio o estado externo incierto, sin bloquear falsos positivos representativos.**

## 10. Temas abiertos para aceptación

- ejemplos exactos que el negocio considera baja inequívoca;
- si se envía una única confirmación y mediante qué modalidad de canal;
- quién puede corregir una baja aplicada por error y con qué auditoría;
- plazo operativo para atender una ambigüedad escalada;
- si el primer piloto necesita presupuesto diario además de una cohorte máxima.
