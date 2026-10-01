# Conocimiento del agente de Johanna desde las respuestas del equipo: medición en sandbox

- **Fecha de la medición:** 2026-10-01
- **Rama:** `feat/claude-johanna-conocimiento-desde-equipo-v1`
- **Archivos:** `profiles/agente-comercial/SOUL.md`, `tests/test_agente_comercial_profile.py`
- **Escalón:** abierto. La instalación del SOUL en el profile la autoriza Dan.

## El problema

El SOUL listaba como no confirmados la modalidad, la duración, la fecha de acceso, el soporte y la elegibilidad geográfica, y la política manda derivar cuando falta un dato. Entre el 2026-09-08 y el 2026-09-30 el equipo tomó a mano conversaciones del inbox 9 para contestar justo esas preguntas.

## El dato

Lectura de solo lectura de la base de Chatwoot el 2026-10-01: 41 conversaciones del inbox 9 con al menos un mensaje público de una persona del equipo, 486 mensajes, 68 intervenciones. De esas 68:

- cerca de la mitad son macros de saludo o de reanudación, o el enlace enviado a mano;
- 15 son respuestas con información del programa;
- el resto son acciones sobre una compra puntual (reenviar accesos, corregir un correo, ofrecer otra forma de pago).

Las 15 respuestas con información dicen siempre lo mismo. De ahí sale la sección nueva del SOUL, `Modalidad y acceso confirmados`: 100% online, sin material físico ni sesiones por videollamada; módulos con clases, ejercicios y guías; sin duración fija ni horarios; acceso vitalicio; accesos por correo al pagar; sin consultas individuales; soporte de acceso y plataforma; personas de distintos países; sin garantía de resultado; y la respuesta para quien está en tratamiento o toma medicación.

Lo que el equipo resolvió como excepción o revisando una compra no pasó al SOUL y sigue en la lista de no confirmados: pago por transferencia, bonos, reembolso, consultas con Johanna.

## El tope de 20.000 caracteres

Hermes corta por el medio todo SOUL que pase de su tope (`_truncate_content`, `agent/prompt_builder.py`, imagen `v2026.8.31`): conserva el 70 % del principio y el 20 % del final. El piso del tope es 20.000 caracteres. El SOUL medía 18.833; con este cambio mide 19.939. El test `test_profile_cabe_sin_truncarse_en_hermes` lo fija. El próximo conocimiento no entra en el SOUL: corresponde pasar a Johanna al archivo de conocimiento por instancia (`docs/contracts/commercial-knowledge-v1.md`).

## La medición

Sandbox local, sin bridge, Chatwoot ni Supabase: Hermes 0.21.2 con un profile sin canales, tools, skills ni memoria, `z-ai/glm-5.3-flash` por OpenRouter servido por Together, y el mismo cuerpo de pedido que arma `src/bridge/hermes.py`. Un turno de un mensaje entró con 5.497 tokens; el turno equivalente del runtime real midió 5.481 el 2026-09-30 (`docs/operations/2026-09-30-agente-comercial-glm-5-3-flash.md`).

23 casos reales, 2 corridas cada uno, contra el SOUL de `origin/main` (md5 `94ad7e36…`) y contra el de esta rama (md5 `d901e9bb…`). Cada caso es el historial hasta el último mensaje del lead antes de la intervención de la persona; la referencia es lo que la persona respondió. Las propuestas se validaron con `_parse_agent_proposal` y `_is_valid_proposal`. Un juez (Claude Sonnet) comparó el contenido de cada respuesta contra la referencia.

| Categoría | Casos | Puntaje antes | Puntaje después | Derivó antes | Derivó después |
|---|---|---|---|---|---|
| Información del programa | 15 | 28 | 62 | 13 de 30 | 5 de 30 |
| Acción sobre una compra (derivar es lo correcto) | 4 | 68 | 78 | 7 de 8 | 8 de 8 |
| Pedido del enlace | 4 | 88 | 90 | 0 de 8 | 0 de 8 |

- 10 casos mejoran 15 puntos o más; ninguno empeora 15 o más.
- Ninguna respuesta contradice a la referencia.
- Propuestas válidas: 45 de 46 antes, 44 de 46 después. Las inválidas son respuestas en prosa sin JSON, el defecto ya medido de este modelo.

## Lo que la medición no cubre

- El `config.yaml` del profile real no está en el repo: el sandbox usó el de `profiles/att1/agente-comercial/` más el modelo. La coincidencia de tokens indica un prompt equivalente, no idéntico.
- El runtime real corre Hermes 0.21.0; el sandbox, 0.21.2.
- El sandbox no reintenta una propuesta inválida; el bridge reintenta una vez.
- Las 5 derivaciones que quedan en información del programa son pedidos de consejo personal (el SOUL manda no darlo) y un "sí" a una oferta de revisión humana que el SOUL anterior había hecho.
- Dos corridas por caso no miden la variación fina.
- El efecto con leads reales no está medido.
