# ADR-0024: la revisión diaria en una instancia autohospedada, con tres excepciones declaradas

- Estado: aceptada por Dan el 2026-10-10 para el disco sin cifrar; el resto es el default recomendado
- Fecha: 2026-10-10
- Decide: Dan
- Afina: el contrato [daily-feedback-production-v1](../contracts/daily-feedback-production-v1.md), con una enmienda de la misma fecha
- Se aparta de: [ADR-0021](0021-setter-producto-instalable.md) §7, en los dos secretos compartidos del punto 5
- Implementación: versión 1.5.0, sin migraciones

## Contexto

La revisión diaria es un servicio aparte del bridge (`setter-daily-feedback`). Nació con Johanna, y su arranque exige tres cosas que esa instancia cumple:

- la base por https, que en Johanna es Supabase Cloud;
- la evidencia de que el almacenamiento va cifrado, que en Supabase Cloud es la del proveedor;
- el AgentBot vinculado al inbox, verificado con el endpoint del inbox. El vínculo de Johanna pasó a `inactive` el 2026-09-23 y el endpoint igual devuelve el bot (`docs/operations/2026-09-23-chatwoot-agent-bot-inbox-unlink.md`; captura del 2026-10-10 en `tests/fixtures/chatwoot_inbox_9_agent_bot_linked_20261010.json`).

ATT1 es la primera instancia autohospedada ([ADR-0021](0021-setter-producto-instalable.md)) y no cumple ninguna de las tres:

- su base es la propia, en el VPS, y el stack la alcanza por http interno: el bridge de ATT1 le habla por `http://att1-gateway:8080`;
- el disco del VPS es ext4 sin cifrar (medido el 2026-10-09);
- su AgentBot no tiene vínculo con el inbox, a propósito. Con un vínculo activo, Chatwoot hace nacer cada conversación nueva en `pending` y el bridge trabaja solo las `open`, así que el agente dejaría de contestar sin un error. El endpoint del inbox devuelve un objeto vacío (captura del 2026-10-10 en `tests/fixtures/chatwoot_inbox_11_agent_bot_unlinked_20261010.json`).

Sin cambios, la revisión no arranca en ATT1. Cumplir las tres reglas a la fuerza cuesta más de lo que protege (ver «Alternativas descartadas»).

## Decisión

1. **La revisión de ATT1 corre en su instancia, con el estado en su propia base.** Ninguna instancia guarda la revisión de otra.

2. **Tres excepciones explícitas, cada una con su variable y su control compensatorio.** Cada variable es opcional: ausente o vacía, deja el comportamiento de la 1.4.0.
   - **`authority_internal_http`: la base por http, solo hacia un servicio del stack.** Se declara con `DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP=true`, que acepta solo `true` o `false` (otro valor da `invalid_daily_feedback_supabase_internal_http`).
     - Control compensatorio: `SUPABASE_BASE_URL` tiene que ser un origen interno. Lleva esquema http, un host de una sola etiqueta (`^[a-z0-9][a-z0-9_-]{0,62}$`, el nombre de un servicio de Docker), puerto explícito, ni usuario ni contraseña, ruta vacía o `/`, y ni query ni fragmento. Una IP, un host con punto o un host sin puerto dan `invalid_supabase_internal_origin`.
     - Una sola función aplica la regla, en la lectura del entorno y en el repositorio (`validate_internal_http_origin`).
     - Chatwoot, el conector de Slack y el origen público de la página siguen exigiendo https.
   - **`storage_unencrypted_risk_accepted`: el almacenamiento sin cifrar, con el riesgo aceptado por escrito.** Se declara con `DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED=false` más `DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF`.
     - Control compensatorio: la aceptación escrita, versionada en el repo de la instancia, con su permalink https y sin credenciales. Tiene que decir qué se guarda, dónde, por cuánto tiempo, quién lo lee, por qué se acepta y cuándo se revisa.
     - La retención sigue acotada, de 24 a 168 horas, y nunca se declara cifrado lo que no lo está: la configuración y la política del colector llevan `storage_encryption_verified=false`.
     - Las dos referencias a la vez dan `ambiguous_storage_protection`; la aceptación mal formada, `invalid_storage_risk_acceptance_ref`; sin ninguna de las dos formas completas, `storage_encryption_not_verified`, como hasta la 1.4.0.
     - `VERIFIED=false` sin la aceptación da también `storage_encryption_not_verified`. En una instancia sin cifrar, ese error quiere decir que falta la aceptación: la salida nunca es pasar `VERIFIED` a `true`.
     - Nada lee la referencia de la aceptación, así que un valor de ejemplo con forma de URL arrancaría y declararía la excepción contra un documento que no existe. Por eso el de `deploy/daily-feedback.env.example` no es una URL: copiado sin reemplazar, el arranque falla con `invalid_storage_risk_acceptance_ref`.
   - **`chatwoot_agent_bot_unlinked`: el AgentBot sin vincular al inbox, a propósito.** Se declara con `DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING=unlinked`; el valor por defecto es `inbox`.
     - Control compensatorio: el chequeo del arranque se invierte, y el invariante que protege al agente de la instancia pasa a ser condición para arrancar. Son tres pedidos, en orden:
       1. el inbox configurado, con los chequeos de siempre;
       2. su `agent_bot`, que tiene que dar 200 sin bot: un objeto vacío, como el de ATT1, o `agent_bot` ausente o `null`. Si trae un bot, el arranque falla con `chatwoot_agent_bot_unexpectedly_linked`;
       3. `GET /api/v1/accounts/{cuenta}/agent_bots/{id}`, que tiene que devolver el bot configurado, con su `id` y un `account_id` igual a la cuenta (si no, `chatwoot_scope_verification_failed`). Sin vínculo con el inbox, que el bot sea de la cuenta es lo que lo autoriza a escribir en ella.
     - Esa última respuesta trae el token y el secreto del bot: se leen solo el id y la cuenta, y nada de ella se loguea ni se guarda.
     - Los mensajes se siguen atribuyendo al bot por `sender.type` y su id, como en Johanna.
     - **Es un chequeo del arranque, no un control continuo.** Si después alguien vincula un bot al inbox, la revisión no se entera hasta su próximo arranque. Lo que avise antes tiene que ser un monitoreo de la instancia.
     - Sin la variable, el modo es `inbox`, igual que hasta la 1.4.0. Un inbox sin bot no pasa ese arranque, y el error es `chatwoot_scope_verification_failed`, el mismo que dan un token rotado, un inbox equivocado o un error de red. Si una instancia con el bot sin vincular a propósito no arranca con ese error, lo primero es mirar la variable. La lectura del entorno lo muestra sin red: sus `exceptions` no traen `chatwoot_agent_bot_unlinked`.

3. **Se activan solo por variable y se ven en `/ready`.** El 200 de `/ready` suma `"exceptions": [...]` solo si hay alguna, siempre en este orden: `authority_internal_http`, `storage_unencrypted_risk_accepted`, `chatwoot_agent_bot_unlinked`. Los 503 no cambian. La configuración no puede declarar lo que no es: el http interno exige un origen interno, y el almacenamiento va cifrado con su evidencia o sin cifrar con su aceptación, nunca las dos cosas.

4. **Tres ajustes sin excepción, también opcionales:**
   - `DAILY_FEEDBACK_BRAND_NAME`: la marca de la página de revisión, de 1 a 60 caracteres imprimibles, escapada al mostrarse; por defecto «Johanna». Las páginas de login, de error y de lote completado no llevan marca.
   - `DAILY_FEEDBACK_MAX_CONVERSATION_PAGES`: el tope de páginas del listado de Chatwoot, de 1 a 400; por defecto 20.
   - `DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS`: la lease de la recolección, de 30 a 900 segundos, el rango que acepta la SQL; por defecto 120. En un lote grande, el límite real es la lease antes que el tope de páginas: el listado, los mensajes de cada conversación, el contexto y la procedencia tienen que entrar en ella. Si no entran, el lote no se confirma (`stale_collection_lease`).

5. **Dos secretos se comparten entre instancias**, en contra de lo que pide el §7 del ADR-0021 (cada instancia, con secretos propios):
   - **El Client Secret de OIDC.** La revisión de ATT1 usa la misma app de Slack que la de Johanna, con una URL de vuelta más, así que el mismo Client ID y el mismo Client Secret quedan en los dos servicios. Si se filtra desde uno, se rota en los dos. El §7 sí permite compartir el workspace de Slack.
   - **El token de Chatwoot.** La revisión de ATT1 reusa el token de control del bridge de ATT1, que es el de administrador de Dan y abre las cuentas 1 (Johanna) y 2 (ATT1) del Chatwoot compartido. El código pide solo la cuenta y el inbox configurados, pero ese token, filtrado desde ATT1, lee también las conversaciones de Johanna. Si Dan lo rota, se cortan juntos el bridge y la revisión de ATT1.

   La salida, cuando se revise: una app de OIDC propia para ATT1 y un usuario de Chatwoot propio para la revisión, con acceso solo al inbox de ATT1.

## Cuándo se revisa

Las tres excepciones se revisan juntas cuando pase cualquiera de estas cosas:

- el volumen de la base de la instancia se cifra: va `DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED=true` con su evidencia y se retira la aceptación;
- la base pasa a un servicio gestionado con https: se retira el http interno;
- se suma una tercera instancia: lo que hoy es la excepción de una instancia pasa a ser un camino del producto, y los dos secretos compartidos se resuelven antes de sumarla.

Además, el http interno se revisa si el stack pasa a más de un nodo, porque ese tráfico cruzaría la red entre hosts sin TLS. Y el modo sin vincular, si el AgentBot llega a ser una entrada real: lo que haría falta está en el documento de operaciones del 2026-09-23.

## Consecuencias

- **Johanna no cambia, aunque se redespliegue desde main.** No carga ninguna variable nueva; sin ellas, el servicio arranca igual, hace los mismos pedidos y su `/ready` devuelve el mismo JSON. Lo fijan los tests de la revisión (`tests/test_daily_feedback_app.py`, `test_daily_feedback_service.py` y `test_daily_feedback_export.py`).
- **ATT1 va a declarar las tres en cada 200 de `/ready`.** Quien lo mire las ve.
- **`deploy/daily-feedback-compose.yaml` sigue exigiendo la evidencia de cifrado** y no admite `storage_unencrypted_risk_accepted`: con la evidencia y la aceptación a la vez, el servicio no arranca (`ambiguous_storage_protection`). Una instancia con esa excepción define su propio servicio, como va a hacer ATT1 en el stack de su repo.
- **La copia identificada del lote vive en un disco sin cifrar durante la retención.** Los mismos datos ya viven sin cifrar en la base de la instancia y en la de Chatwoot, en el mismo disco. Los respaldos de la base de la instancia la copian más allá de la retención, y la aceptación escrita lo tiene que decir.
- **Una instancia con excepciones no vuelve atrás por imagen.** Una imagen anterior a la 1.5.0 no conoce las excepciones y no arranca con esa configuración. Volver atrás es sacar el servicio, después de que se purgue el último lote: si sale antes, la copia identificada queda en la base sin nadie que la borre.
- **La primera corrida confirma el lote y no lo notifica.** El lote nace con `notification_next_attempt_at = clock_timestamp()`, posterior al `now` de la corrida que lo confirma. Esa corrida da `collected: true` y `notified: false`, y la tarjeta sale en la siguiente. Un E2E con el run-now son dos llamadas. Pasa igual en Johanna, y esta decisión no lo cambia.
- **El modo sin vincular recibe, en cada arranque, una respuesta con el token y el secreto del bot.** Se descarta sin loguearla ni guardarla.

## Alternativas descartadas

- **El estado de ATT1 en el Supabase Cloud de Johanna, con su propio scope.** Resolvía el https y la evidencia de cifrado. Se descarta por tres razones:
  - cruza secretos entre instancias, contra el §7 del ADR-0021: el servicio de ATT1 tendría la `service_role` de Johanna, con acceso a todos sus leads, o el de Johanna tendría el token de la cuenta de ATT1;
  - el contexto de cada conversación (derivaciones, links, compras, bajas y procedencia del prompt) vive en la base de ATT1, y saldría vacío;
  - las reactivaciones y las reanudaciones se buscan solo por el número de conversación, que es el de cada cuenta: la conversación N de ATT1 mostraría los eventos de la N de Johanna.
- **Cifrar el volumen de la base antes del domingo 2026-10-11.** Son de medio día a un día, con corte de todos los flujos de ATT1, y hay que decidir dónde vive la clave. Queda como la mejora que retira la excepción del disco, junto con un respaldo fuera del VPS.
- **Declarar el almacenamiento cifrado** (`DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED=true`). Es falso.
- **Crear el vínculo del bot en `inactive`, como el de Johanna**, con `rails runner` en el Chatwoot compartido. Es cero código, pero deja un vínculo que el panel muestra y que el script de tokens de la instancia manda desconectar. Si alguien lo pasa a activo, todas las conversaciones nuevas de ATT1 nacen en `pending` y el agente deja de contestar sin un error.
- **Publicar la API de la base (PostgREST) por Traefik, con TLS.** Deja la API de la base en internet.
- **Un proxy TLS interno con certificado propio.** Suma un certificado, una CA en la imagen y una pieza más, para el mismo tráfico que el bridge de la instancia ya manda por http.
