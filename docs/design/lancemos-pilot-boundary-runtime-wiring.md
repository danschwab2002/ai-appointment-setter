# Wiring runtime del perímetro Lancemos

- **Estado:** Implementado en el árbol; pendiente de integración y despliegue
- **Fecha:** 2026-08-10
- **Fase:** 2 del perímetro durable
- **Autoridad:** PostgreSQL/Supabase
- **No implica:** despliegue, publicación de IDs reales, activación del runtime ni mensajes

## 1. Resultado

Hacer que el perímetro integrado en fase 1 gobierne dos fronteras ejecutables:

```text
abandono autoritativo + identidad resuelta
→ planificación atómica dentro del scope activo
→ claim y reevaluación existentes
→ autorización del piloto + request_started en una sola transacción
→ recién entonces request externo
```

Una configuración faltante, parcial, inválida, no publicada, no armada o fuera de scope debe fallar cerrada. El LLM y el sender no pueden convertir un rechazo en permiso.

## 2. Configuración runtime

El bridge recibe un paquete explícito y default-off:

- `LANCEMOS_PILOT_BOUNDARY_ENABLED`;
- `LANCEMOS_PILOT_SCOPE_KEY`;
- `LANCEMOS_PILOT_SCOPE_VERSION`;
- `LANCEMOS_PILOT_TENANT_KEY`;
- `LANCEMOS_PILOT_CHANNEL_PROVIDER`;
- `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF`.

Cuando el paquete está habilitado, todos sus campos son obligatorios y la versión es positiva. `source=hotmart`, `source_event_type=PURCHASE_OUT_OF_SHOPPING_CART`, `channel=whatsapp` y `purpose=cart_recovery` son invariantes de esta vertical, no variables de despliegue.

`DURABLE_OUTBOUND_ENABLED=true` exige el paquete habilitado. No queda un modo outbound del piloto que dependa sólo de `ALLOWED_WHATSAPP_JID`.

## 3. Frontera de planificación

El runtime no encadena “evaluar por REST” y “planificar por REST”. Usa un único RPC transaccional que:

1. bloquea el runtime del scope;
2. evalúa scope publicado, versión activa, estado `armed`, tenant, account/inbox, canal/cuenta, fuente/evento, producto/oferta y cohorte;
3. si rechaza, no materializa caso, secuencia ni acción;
4. si permite, invoca la planificación autoritativa con identidad;
5. devuelve el mismo contrato estricto de plan existente.

Pausar o cambiar la generación no puede intercalarse entre evaluación y commit del plan.

## 4. Frontera absoluta de request-start

La migración agrega un RPC transaccional que:

1. autoriza el intento mediante `authorize_lancemos_pilot_request_start`;
2. consume presupuesto sólo una vez por `attempt_id`;
3. ejecuta el `mark_followup_request_started` protegido por opt-out, compra, takeover, lease y revisiones;
4. revierte también la autorización si la transición de request-start falla;
5. devuelve intento y autorización como una respuesta tipada.

La planificación persiste además un binding inmutable `caso → scope/version →
evento admitido`. El request-start deriva de ese binding tenant y routing; no
los acepta desde Python como evidencia de autorización.

Para WABA, el factory construye el sender de Chatwoot únicamente con templates
aprobados configurados para primer contacto y seguimiento. Cada uno usa un
placeholder de body; configuración ausente o categoría inválida aborta el
arranque y no cae a freeform ni a Evolution.

El RPC histórico `mark_followup_request_started` conserva compatibilidad de firma sólo para composición interna, exige una autorización durable del piloto para el mismo action/attempt y no tiene `EXECUTE` para roles API. Así no queda un bypass de `service_role` por el endpoint anterior. Las demás funciones internas continúan igualmente cerradas.

Un replay sólo es válido si la autorización y el intento ya muestran el request iniciado. Una autorización huérfana no habilita un efecto nuevo.

## 5. Runtime Python

- `ResolutionWorker` pasa el paquete al planificador sólo para abandono durable.
- `DurableDispatcher` usa exclusivamente la frontera atómica del piloto antes del sender.
- `SupabaseClient` selecciona y valida estrictamente los RPC nuevos.
- reason codes y logs contienen sólo IDs internos y enums; no contienen JID, teléfono, email, payload ni secretos.

## 6. Operación autónoma

La entrega debe poder desplegarse desde Git/imagen sin comandos manuales en la consola de EasyPanel:

- contrato de variables versionado en `.env.example`, `compose.yaml` y README;
- defaults desactivados;
- validación de startup antes de iniciar workers;
- `/health` para liveness y `/ready` para validar scope/runtime sin exponer
  secretos ni exigir consola;
- migración y postflight reproducibles por automatización con credenciales acotadas;
- diagnóstico por estados/reason codes sanitizados, no por inspección manual de payloads.

Credenciales, IDs reales y activación siguen fuera de Git. Si el entorno remoto no ofrece una identidad automatizable para migraciones o configuración, ese permiso es un bloqueo externo explícito; no se traslada al usuario como una rutina de consola.

## 7. Cortes TDD

1. configuración default-off y fail-closed;
2. cliente + wiring de planificación atómica;
3. SQL de planificación sin efectos al rechazar;
4. cliente + dispatcher de request-start atómico;
5. SQL de request-start, bypass, rollback y replay;
6. HTTP real local y PostgreSQL real con carreras de pausa/opt-out.

## 8. Fuera de alcance

- publicar el scope real;
- armar el runtime;
- desplegar DDL;
- configurar WABA/templates;
- enviar mensajes;
- pedir al usuario comandos manuales de EasyPanel.
