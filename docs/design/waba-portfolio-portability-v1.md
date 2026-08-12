# Portabilidad del portfolio WABA V1

- **Estado:** Propuesta para revisión; verificador y contrato local implementados
- **Alcance:** re-onboarding del portfolio, WABA, número e inbox sin mutaciones productivas
- **Base observada:** `origin/main` `f81a99098f1c0c4365411b25dccb8de13707bb45`

## Matriz de realidad vigente

| Capa | Implementado | Desplegado | E2E real | Depende del portfolio nuevo |
|---|---|---|---|---|
| Scope exacto account/inbox antes de captura | sí, en bridge y pruebas | sí, default-off sobre el WABA actual según evidencia de PR #23 | inbound físico no ejecutado | sí, referencias nuevas |
| Evolution fuera del scope | validado por configuración y probe | transporte desconectado e integración Chatwoot deshabilitada según PR #23 | no aplica | debe revalidarse |
| Readiness inbound sanitizado | sí, verificador anterior integral | ejecutado; bloqueó por Team/schema no verificable | no | sí |
| Sender WABA sin freeform | sí, factory y payload local | artefacto desplegado, outbound apagado | no | templates aprobados y pago |
| Templates first touch/follow-up | dos nombres, idioma/categoría comunes y `{{1}}` | no configurados para envío autorizado | no | sí |
| Piloto durable | código/migraciones locales | schema remoto pendiente | no | scope, oferta y Conversation Release reales |

PR #23 permanece abierto y aporta evidencia operativa sanitizada; no forma parte de `origin/main`. Este diseño no copia esa evidencia ni afirma que esté integrada.

## Inventario reemplazable

| Referencia | Autoridad | Forma versionable segura | Verificación antes de usar |
|---|---|---|---|
| portfolio de Meta | control plane autorizado | digest/presencia, no ID | accesible y owner correcto |
| WABA | Meta/Chatwoot | binding opaco | provider `whatsapp_cloud` único |
| número | Meta | presencia + referencia opaca | conectado al WABA esperado |
| Phone Number ID | Meta/Chatwoot | secret-store binding | presente y consistente |
| account/inbox | Chatwoot | scope durable/config | pertenencia exacta y provider oficial |
| templates | Meta + aprobación negocio | contrato V1 | nombre/idioma/categoría/schema exactos |
| tokens y bindings | EasyPanel/secret store | sólo `configured=true` | credencial mínima y lectura autorizada |
| inbox anterior | Chatwoot histórico | referencia de exclusión | mismo contacto produce cero admisión |

Los recursos históricos se conservan. Portar significa seleccionar bindings nuevos, no renombrar ni reciclar IDs.

## Invariantes

1. Conectar no activa: todo efecto permanece apagado y el scope durable, si existe, queda inactivo.
2. El scope nuevo se valida por account + inbox + `conversation.inbox_id`; el inbox anterior debe producir cero captura, Hermes, RPC, pausa y outbound.
3. No se infiere readiness superior. Team y schema no bloquean inbound observacional; pago/template no bloquean observar inbound.
4. Un template ambiguo, no aprobado o incompatible bloquea el nivel 2 sin fallback.
5. Primer contacto y follow-up sólo comparten configuración si idioma, categoría y placeholder coinciden exactamente. El runtime V1 exige esa coincidencia.
6. Ninguna salida sanitizada contiene IDs, nombres, copy, teléfonos, tokens ni campos desconocidos.

## Secuencia propuesta

`inventario read-only → snapshot/digest → cargar bindings en secret store → deploy default-off → comprobar scope nuevo y rechazo del viejo → inbound observacional autorizado por separado → validar templates/pago → template único controlado autorizado por separado → preparar schema/scope/Conversation Release → piloto supervisado autorizado por separado`.

Cada flecha productiva es una autorización independiente. El procedimiento exacto y rollback están en `docs/operations/waba-portfolio-reonboarding-runbook.md`.

## Temas abiertos

- portfolio, WABA, número e inbox definitivos;
- templates reales y su aprobación comercial/Meta;
- si ambos templates cumplen el esquema compartido del runtime V1;
- método de pago operativo;
- oferta, política y Conversation Release del piloto;
- responsable de handoff si esa capacidad se habilita.
