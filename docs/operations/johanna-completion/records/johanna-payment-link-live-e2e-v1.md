# Registro de trabajo y aprendizaje — johanna-payment-link-live-e2e-v1

- **Título:** E2E controlado de payment link atribuido de Johanna
- **Estado:** `implementando`
- **Área:** precheckout → checkout Hotmart → recuperación Chatwoot
- **Owner:** `session-20260911_215928_8e97b2`
- **Claim / rama / worktree:** `johanna-payment-link-live-e2e-v1` / `ops/johanna-payment-link-live-e2e-v1` / worktree exclusivo
- **Snapshot o commit probado:** `169685e5838e51419d3002cf9f7db0fb3795a71d`
- **Recurso del claim:** `johanna-completion:johanna-payment-link-live-e2e-v1`
- **Path obligatorio:** `docs/operations/johanna-completion/records/johanna-payment-link-live-e2e-v1.md`

## Resultado esperado

Ejecutar un caso nuevo y aislado con el segundo contacto controlado, producir como máximo un envío atribuido usando el checkout canónico opaco y comprobar después una compra sintética real por igualdad del marcador persistido con el reporte de Hotmart. No reutilizar ni reconciliar el command histórico ya dispuesto.

## Prerrequisitos y gates

| Prerrequisito | Estado | Owner/autoridad | Evidencia o condición de desbloqueo |
|---|---|---|---|
| Migración payment-link y disposición one-shot en Cloud | verificado | Supabase Cloud | ledger hasta `20260913000100`; readiness sin unknown no dispuesto |
| Command histórico cerrado administrativamente sin resend | verificado | Supabase Cloud | disposición append-only; command conserva `delivery_unknown` e IDs proveedor nulos |
| Identidad canónica del segundo contacto y WhatsApp móvil | verificado | operador / validación local | operador confirmó Argentina y celular; E.164 móvil derivado sólo con el marcador internacional `9`, validado y conservado fuera de Git con permisos `0600`; no se registra PII |
| Destinatario fresco, sin presupuesto consumido ni stops | falló; falso negativo del preflight | Supabase + Chatwoot | el preflight comparó `+549…` contra identidades durables guardadas sin `+`; la reevaluación canónica encontró un owner y tres conversaciones con takeover/pausa y terminó `blocked_handoff` |
| Formulario/relay con consentimiento explícito | verificado con incidencia contenida | landing desplegada | la navegación física llegó a Hotmart pero no dejó submission; un único POST controlado al mismo `/api/lead` productivo devolvió `delivered=true` y creó el evento nuevo |
| Backlog cero y runtime aislado | verificado para armar | Supabase + bridge | `0` reevaluaciones activas, `0` one-shot commands activos, `0` bindings/send commands payment-link; `/health=200`, `/ready=200` |
| Autorización para efecto real controlado | autorizada para preparar y ejecutar este E2E | Dan Schwab | mensaje de chat “VAmos con eso”; cualquier deploy/flag global sigue siendo frontera separada si resulta necesario |
| Activación gradual | no autorizada | Dan Schwab | requiere compra sintética y marcador exacto en Hotmart |

## Ejecución y evidencia

- **Entorno:** `desplegado inerte`
- **Acción reproducible:** preflight multiagente, validación E.164 local, matriz durable exacta, búsqueda CRM por el adapter de producción y un único POST controlado al relay productivo con los datos consentidos de la captura guardados fuera de Git.
- **Resultado esperado:** exactamente un grafo de prueba nuevo, marcador `hermes-<ULID>` persistido, un POST como máximo y evidencia física separada.
- **Resultado observado:** evento `01M2EFAF7PWTH28K9WAMQXBCZ0`; intención/submission y checkout válidos. Al vencer, el timer terminó `completed / blocked_handoff`: la identidad durable canónica tenía un owner y tres conversaciones con takeover/estado bloqueante. No se reservó command.
- **Efectos externos permitidos:** un único caso controlado nuevo después de pasar todos los gates; ningún efecto sobre el contacto histórico.
- **Delta final:** `0` commands nuevos, `0` mensajes Chatwoot, `0` bindings y `0` send commands; el destinatario no se consumió, pero este caso no puede continuar como fresh-recipient.
- **Evidencia sanitizada:** sólo hashes, IDs internos opacos, estados, reason codes, conteos y booleanos.
- **No probado:** aceptación Chatwoot, recepción física, respuesta inbound, envío del payment link, compra real y marcador en reporte Hotmart.

## Diagnóstico

- **Síntoma observado:** el submit físico redirigió correctamente a Hotmart pero no dejó un submission; el relay sí admitió el mismo caso mediante un POST controlado directo.
- **Hipótesis consideradas:** el E2E requiere una activación controlada y temporal de un único flag después de crear el caso; el contrato efectivo confirmó que los cuatro gates dependientes ya están activos.
- **Causa confirmada:** además de la pérdida no explicada del POST del browser, el preflight de frescura normalizó por dígitos para localizar el intent pero comparó la identidad CRM/durable con el E.164 que incluía `+`; producción usa la representación durable sin `+`. Ese desacople ocultó el owner y las conversaciones bloqueantes.
- **Evidencia causal:** la reevaluación efectiva terminó `blocked_handoff`; replicar su CTE usando `purchase_intents.normalized_phone` devolvió `owner_count=1`, `handoff_conversation_count=3`, todas con takeover/estado/automatización bloqueantes. Consultar con la variante `+549…` devolvía cero.

## Cambio, contención y rollback

- **Cambio aplicado:** sólo claim/worktree y este registro; sin deploy ni configuración remota.
- **Contención inmediata:** `PAYMENT_LINK_ENABLED=false`; no reutilizar el contacto Chatwoot con formato no canónico; crear una identidad nueva mediante el evento fresco y mantener el flag apagado hasta la ventana inbound controlada.
- **Rollback o recuperación:** cerrar ingreso, pausar autoridad durable, reconciliar cualquier efecto cruzado y sólo después apagar outbound; preservar `delivery_unknown` ante ambigüedad.
- **Verificación posterior:** pendiente del caso nuevo.

## Disposición de aprendizajes

| Hallazgo | Clase | Artefacto destino | Actualizado ahora | Pendiente/owner |
|---|---|---|---|---|
| El protocolo de cierre está en una rama aún no integrada respecto del snapshot operativo | incertidumbre | este registro; protocolo/ledger tras integración | sí | integrador documental |
| El E2E debe probar frescura durable y CRM independientemente antes de consumir el contacto | procedimiento | runbook de E2E controlado | no | esta tarea, después de evidencia real |
| Inbound iniciado por el prospecto que después solicita un link de compra no tiene todavía una política/autoridad definida | requisito a resolver / incertidumbre abierta | futuro contrato y diseño de `inbound → purchase-link`, separado del recuperador de carrito | sí, como pendiente; sin implementar | producto + ingeniería; definir fuente canónica del checkout, elegibilidad, consentimiento, idempotencia, atribución y fail-closed antes de habilitar |
| **Alta prioridad — decisiones comerciales recibidas:** toda venta originada por el agente declara `Hermes` como source; por ahora no se conserva atribución a anuncios. El `sck` usa barras verticales, pero no expone `chatwoot_conversation_id`: lleva un token opaco único que resuelve la conversación mediante la fila durable. El `sck` anterior se guarda íntegro en esa fila, no se descarta | decisión comercial parcialmente cerrada + contrato técnico pendiente | futura revisión de `johanna-payment-link-v1`, parser/correlador Hotmart y pruebas de reporte | sí, corrección registrada; sin implementar | Formato recomendado: `hermes|v1|<issuance_ulid>`. Verificar que Hotmart preserve/exponga el string semántico con `|` aunque viaje URL-encoded como `%7C`; fijar charset/longitud/duplicados. No concatenar automáticamente el `sck` anterior al nuevo: conservarlo en DB y versionar una incorporación futura si se aprueba |
| **Oferta inbound aprobada para Johanna:** el agente no elige entre ofertas ni infiere una desde la conversación. La `default_inbound_offer` es `ads-a`, código exacto `bxjge6zq`, server-owned y versionada. Cada emisión captura un snapshot inmutable de producto/oferta; cero o más de una default activa bloquean el link | decisión comercial cerrada | catálogo/contrato inbound purchase-link | sí, decisión registrada; sin implementar | implementación futura debe confirmar que `bxjge6zq` continúa activa en Hotmart antes de habilitar efectos y no depender del default implícito del proveedor |
| Un preflight de fresh-recipient debe usar la representación canónica durable exacta y construir owners desde `contact_points ∪ channel_identities`; comparar una variante con `+` contra otra sin `+` produce falsos negativos | incidente / procedimiento reusable | test y runbook de fresh-recipient controlled E2E | sí, evidencia real capturada; sin cambiar código en esta tarea | nueva tarea coordinada; corregir el preflight antes de otro E2E |

## Documentos afectados

- [ ] Ledger actualizado; lo realiza el integrador después de integrar este registro.
- [ ] Evidencia operativa enlazada; pendiente del E2E.
- [x] Contrato marcado no aplicable por ahora: no cambia interfaz.
- [x] Prueba de regresión marcada no aplicable por ahora: etapa operacional.
- [x] Arquitectura marcada no aplicable por ahora: no cambia diseño.
- [ ] Runbook/checklist actualizado o pendiente explícito después de observar el E2E.
- [x] ADR marcado no aplicable: no hay decisión arquitectónica nueva.

## Criterio de cierre

- [ ] Resultado funcional verificado.
- [ ] Evidencia reproducible y sanitizada.
- [x] Límites y no-probado declarados.
- [x] Causa no confundida con hipótesis.
- [x] Rollback/contención documentado cuando corresponde.
- [ ] Todos los aprendizajes clasificados y destinados.
- [x] Pendientes con owner y artefacto destino.
- [ ] Registro listo para que el integrador actualice el ledger después de la integración.
