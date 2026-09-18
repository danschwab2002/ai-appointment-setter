# Verificacion operativa de checkout V2 desde Codex

- Estado: correccion local validada; E2E externo pendiente.
- Fecha: 2026-09-18.
- Claim: `codex-checkout-operations-v2`.
- Base: `0a79688645624a3ed641cfba3245b48feea414d2`.
- Preflight inicial del integrador: exit `0`, `2026-09-18T19:16:45.717149+00:00`.
- Contrato: [payment-link V2](../contracts/johanna-payment-link-v2.md).

## Evidencia heredada y limites

El registro [V1 recuperado](johanna-completion/records/johanna-payment-link-live-e2e-v1.md)
es historia de un intento bloqueado, no aceptacion de V1 ni V2. El claim V1 fue
supersedido y su trabajo preservado por el integrador. El caso termino en
`blocked_handoff`, con cero commands, enlaces y mensajes nuevos. La comparacion
equivocada entre telefono con `+` y representacion durable sin `+` habia ocultado
un owner y tres conversaciones bloqueantes.

El nuevo preflight debe resolver la identidad en el servidor usando su
representacion durable exacta, incluyendo owners de `contact_points` y
`channel_identities`, y revisar todas sus conversaciones. No liberar takeover,
pausa o assignee para fabricar elegibilidad ni reutilizar el caso V1 como fresco.

La inspeccion administrativa `d9c1102dd8904561bfb9a2fb5a2ab276`, completada a
`2026-09-18T19:21:10.138031+00:00`, encontro un bridge sano con admission Cut B,
agente, replies, monitor y payment-link apagados. Esto acredita disponibilidad y
configuracion en ese instante, no entrega ni compra. La inspeccion de solo lectura
`9e7c51f76d324e109f988e4c81d12699` termino a `2026-09-18T19:43:51.116079+00:00`:
target administrativo coincide con runtime, ledger `20260914000100` presente,
fingerprint V2 `5/5`, firmas/ACL seleccionadas correctas, catalogo con una oferta
activa y una default activa aprobada, y cero emisiones ambiguas en el scope.
No certifica ACL de toda la base ni todas las colas del bridge. No se copiaron
credenciales ni PII a la solicitud o respuesta.

La consulta historica de esa inspeccion busco en `webhook_events.external_event_id`
y no encontro el evento. Ese cero no acredita un destinatario fresco. La solicitud
de seguimiento `da16453cbe72483fa153f83660e9653e` corrige la referencia a
`precheckout_submissions.external_submission_id`, ligada a `purchase_intents`
mediante `purchase_intent_submissions.submission_id` / `purchase_intent_id`.
La migracion `20260818000200` valida y persiste alli el `id` de `lead.precheckout`.
La identidad de prueba y sus bloqueos siguen pendientes de confirmacion privada.

## Correccion local y prueba

`deliver_checkout_issuance_v2` recibia el inbox autorizado, pero no lo pasaba a
`ChatwootClient.send_agent_bot_reply`. El sender ejecuta su guard de inbox live
solo cuando recibe `expected_inbox_id`. Se propaga ahora `chatwoot_inbox_id`, sin
cambiar contratos ni migraciones.

La regresion usa `ChatwootClient` real con transporte HTTP simulado y devuelve una
conversacion de otro inbox. Antes del fix avanzo incorrectamente a leer labels y
fallo la prueba; despues devuelve `blocked / conversation_scope_changed` tras un
GET, sin autorizacion durable de POST ni envio.

```text
uv run --locked pytest tests/test_checkout_delivery.py -k live_inbox_mismatch -q
antes del fix: 1 failed

uv run --locked pytest tests/test_checkout_delivery.py tests/test_checkout_issuance.py tests/test_payment_link.py -q
despues del fix: 39 passed

git diff --check
exit 0
```

La suite canonica `uv run --locked pytest -q` termino con exit `1`: 2128 pruebas,
2125 aprobadas y tres fallos de `tests/test_att1_product_profiles.py`:
`test_installer_creates_verified_private_home_without_overwrite`,
`test_installer_rejects_symlinked_target_parent` y
`test_failure_before_publication_leaves_no_target`. El instalador intenta abrir
cada ancestro con `os.open`; el usuario restringido obtiene `PermissionError: 13`
al abrir `hermes`, que se transforma en `source package integrity mismatch`.
Estos fallos requieren verificacion en el entorno administrativo con permisos
adecuados; no se modifico el instalador ATT1 ni sus permisos. La recopilacion
`uv run --locked pytest --collect-only -o addopts='' -q` confirmo 2128 pruebas.

Estas son pruebas offline. No acreditan HTTP real, Supabase Cloud, Chatwoot
productivo, recepcion fisica ni reporte Hotmart.

## Recorrido de aceptacion pendiente

1. Publicar e integrar el SHA revisado; desplegar el bridge con gates apagados y
   comprobar SHA, salud, readiness y ausencia de efectos. El integrador serializa
   este release con monitor y correlaciones.
2. Verificar ledger `20260914000100`, cuerpos/firma/ACL de RPC V2 y tablas
   `checkout_offer_catalog` / `checkout_link_issuances`; OpenAPI por si sola no
   demuestra estos invariantes. Confirmar una default activa para la oferta
   autorizada `bxjge6zq` y su disponibilidad efectiva en Hotmart.
3. Verificar tenant, cuenta, inbox y destinatario por referencias privadas
   aprobadas; demostrar scope aislado, baseline y ausencia de entrega ambigua.
   Detener si el contacto esta bloqueado o su identidad no es univoca.
4. Acordar con coordinacion una unica ventana de links, sin E2E de monitor sobre
   ese destinatario. Revalidar dependencias de payment-link: admission Cut B,
   agente, replies y scoped inbound senders. La activacion no puede abrir efectos
   a contactos fuera del caso autorizado.
5. Probar controles negativos (pausa, takeover, compra, identidad/inbox divergente)
   y cero envios. Para el positivo, una solicitud inbound autorizada debe producir
   una emision durable y un unico mensaje con URL persistida y SCK V2. Replay
   conserva fila/ULID/URL y no duplica POST; `delivery_unknown` requiere
   reconciliacion del mensaje exacto, nunca reenvio ciego.
6. Separar fixture no financiero de compra: un fixture puede comprobar parser y
   correlacion, pero no prueba que Hotmart preserve el SCK. La compra real la
   completa una persona, sin que Codex reciba ni introduzca datos de pago. El
   reporte/webhook autoritativo debe devolver exactamente el SCK persistido y
   producir `purchase_matched`, intencion comprada y cero follow-up posterior.
7. Registrar SHA, estados, conteos, IDs opacos, igualdad/hash de SCK y cierre del
   alcance temporal. No registrar URL final, identidad, payload, credenciales ni
   datos de pago. Cualquier efecto ambiguo se contiene y se reconcilia antes de
   reintentar; conservar filas durables y restaurar gates al baseline acordado.

La evidencia de compra/recepcion requiere acciones humanas solo cuando el caso
este preparado; coordinacion mantiene una unica lista de esas acciones. Esta
correccion no cambia la arquitectura ni la interfaz V2 prometida y no modifica
documentacion compartida fuera del claim.
