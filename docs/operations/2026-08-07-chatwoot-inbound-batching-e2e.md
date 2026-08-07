# Registro operativo: batching inbound de Chatwoot E2E

- **Fecha:** 2026-08-07
- **Estado:** validado mediante ejecución real en producción
- **Alcance:** varios mensajes públicos entrantes de una conversación → una evaluación lógica → una respuesta pública

## Versión desplegada

La validación se ejecutó después de desplegar `main` con:

```text
7f32234 feat: batch inbound Chatwoot messages durably
CHATWOOT_INBOUND_DEBOUNCE_SECONDS=30
```

El endpoint `/health` respondió HTTP 200 después del redeploy.

## Caso real

Desde el único WhatsApp autorizado se enviaron cuatro mensajes consecutivos dentro de la ventana configurada. No se preservan en este documento el JID, el contacto, la conversación ni el contenido de los mensajes.

El comportamiento visible confirmó:

- una sola respuesta pública después del grupo de cuatro mensajes;
- la respuesta atendió la consulta planteada y mantuvo contexto conversacional anterior;
- no apareció una segunda respuesta durante el minuto adicional de observación;
- no hubo duplicados visibles.

La captura sólo muestra precisión de minuto, por lo que no se usa como evidencia de segundos exactos. La configuración desplegada y la finalización durable verifican el caso con la ventana de 30 segundos.

## Estado durable sanitizado

Una inspección agregada del inbox privado del bridge, limitada a la ventana reciente y sin imprimir payloads ni identificadores, devolvió:

```text
work_dir_exists: true
recent_files: 4
recent_statuses: completed=4
recent_attempts: 0=4
malformed_recent: 0
pending_group_journals: 0
```

Esto confirma que las cuatro admisiones terminaron como grupo exitoso al primer intento, sin trabajo pendiente, envelopes malformados ni transición grupal incompleta.

## Verificación previa al despliegue

Antes del E2E:

- la suite completa pasó;
- `compileall` pasó;
- `git diff --check` pasó;
- una prueba HTTP local firmada produjo dos respuestas 202 en menos de un segundo y dos admisiones durables;
- la revisión independiente fail-closed final terminó con `passed=true`, sin errores de seguridad ni lógica.

## Resultado

El MVP inbound queda validado en producción para la conversación autorizada:

```text
webhooks individuales
→ ACK durable inmediato
→ ventana reiniciable por conversación
→ batch canónico
→ una evaluación Hermes
→ una respuesta pública
→ completion grupal sin duplicados
```

La división de una respuesta lógica en varias burbujas outbound continúa fuera de alcance.
