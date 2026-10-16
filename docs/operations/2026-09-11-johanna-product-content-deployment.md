# Despliegue del contenido básico de Libre de Ansiedad — evidencia

- **Estado:** Implementado y verificado en el profile productivo
- **Fecha de ejecución:** 2026-09-11
- **Profile:** `agente-comercial`
- **Alcance:** Respuesta a preguntas básicas sobre el contenido del programa

## Cambio desplegado

El profile productivo ahora trata preguntas como “¿en qué consiste el programa?”,
“¿qué temas trae?” y “¿qué incluye?” como consultas comerciales simples. La
respuesta autorizada puede enumerar:

1. Fase 1 — Entiende y calma.
2. Fase 2 — Desarma y renueva.
3. Fase 3 — Restaura y sostén.
4. Botiquín para la crisis.
5. Cuaderno de Restauración.
6. Test de evaluación.
7. Clase de fe y restauración.

El cambio elimina la instrucción contradictoria que clasificaba todo el
“contenido detallado” como no confirmado. Conserva la prohibición de inventar
objetivos, resultados, duración, cantidad de lecciones o descripciones internas
no confirmadas.

La lógica de enlaces de compra permanece fuera de este cambio: el enlace
canónico continúa marcado como no confirmado en esta Conversation Release.

## Linaje revisado

- PR: `#123`
- Commit funcional revisado: `5baf874f95f89c6d13156b3ef158d5e47ccad31f`
- Merge commit en `main`: `968d0b322c89dc695e072f4f6c0961071b575d3f`
- GitHub CI `verify`: `success`
- Revisión independiente: `APPROVE`, cero bloqueadores
- Suite del snapshot revisado: `1861 passed`; una advertencia preexistente de
  deprecación Starlette/httpx
- Diff binario revisado SHA-256:
  `001275c41cf2212b21cc41e906ba19ee72a151ffc0387c0aaeb5457dcb5519f1`

El PR anterior `#121` fue cerrado como supersedido porque `main` avanzó y el
coordinador exigía recuperar el cambio desde una base inmutable actual.

## Despliegue

A las `2026-09-11T22:45:11Z` se copió el `SOUL.md` integrado al profile activo:

```text
/opt/data/profiles/agente-comercial/SOUL.md
```

Hash SHA-256 de la fuente integrada y del archivo desplegado:

```text
0b4cd05982acbc24fdea990d942acf223e53b7b693a11f99c23005c124610a08
```

Antes de reemplazarlo se creó el backup privado:

```text
/opt/data/profiles/agente-comercial/private-backups/SOUL.pre-product-content-20260911T224511Z.md
```

Hash SHA-256 del backup:

```text
5580e0c44efd3ad92a4b0a1f56bf8ec03ad76886eaffecb235cdecb14fbc3329
```

Después de la copia se reinició el gateway supervisado. El proceso nuevo quedó
activo como PID `620345`, iniciado a las `2026-09-11T22:45:21Z`.

## Verificación E2E del agente productivo

Se invocó directamente la API real del profile productivo con una conversación
sintética, sin pasar por WhatsApp y sin producir mensajes externos.

Entrada de prueba:

```text
Quería saber en qué consiste el programa?
```

Resultado observado:

- HTTP `200`;
- `decision="ask_question"`;
- `qualification_status="in_progress"`;
- `reason_code="johanna_e2e_response"`;
- los siete elementos autorizados aparecieron en la respuesta;
- no apareció “no está confirmado”;
- no apareció “revisión humana”;
- no se produjo `handoff`.

Respuesta visible observada:

```text
Libre de Ansiedad es un programa de la Psic. Johanna Ortega que combina psicología clínica, neurociencia y cosmovisión cristiana. Incluye estos elementos:

1. Fase 1 — Entiende y calma.
2. Fase 2 — Desarma y renueva.
3. Fase 3 — Restaura y sostén.
4. Botiquín para la crisis.
5. Cuaderno de Restauración.
6. Test de evaluación.
7. Clase de fe y restauración.

El precio actual es USD 49 e incluye una garantía de 7 días. ¿Hay algo en particular sobre el programa que quieras conocer mejor?
```

## Postflight y límites

- Gateway `agente-comercial`: activo.
- Bridge `/health`: HTTP `200`, `{"status":"ok"}`.
- Bridge `/ready`: HTTP `200`, `status="ready"`.
- `pilot_boundary="disabled"`.
- `automation_state="default_off"`.
- No se envió ningún mensaje real por WhatsApp.
- No se activó contacto proactivo, descuento, enlace de compra ni nueva política
  de seguimiento.
- El postflight observó un intento previo en
  `precheckout_delayed_delivery_unknown="1"`; este despliegue no lo creó, no lo
  modificó y no lo reconcilió.

## Rollback

Si fuera necesario revertir exclusivamente esta Conversation Release:

1. restaurar el backup privado indicado arriba en
   `/opt/data/profiles/agente-comercial/SOUL.md`;
2. verificar que el hash restaurado sea
   `5580e0c44efd3ad92a4b0a1f56bf8ec03ad76886eaffecb235cdecb14fbc3329`;
3. reiniciar únicamente el gateway `agente-comercial`;
4. comprobar gateway, `/health` y `/ready`;
5. ejecutar una prueba sintética directa antes de cualquier prueba física.
