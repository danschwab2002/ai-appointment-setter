# Contrato — bootstrap proactivo de identidad V1

- **Estado:** implementado en migración; no desplegado ni activado.
- **Propósito:** preparar un único contacto autorizado antes de abrir el ingreso Hotmart.

## Frontera

`bootstrap_proactive_lead_identity(...)` sólo puede ejecutarse con `service_role` y exige:

- scope publicado y runtime `inactive` o `paused`;
- generación exacta;
- intención observada V1.1.0, vigente y autorizada;
- disclosure `johanna-precheckout-whatsapp-disclosure-v1`;
- target WABA inmutable fijado por scope;
- coincidencia exacta entre teléfono, identidad, account e inbox;
- contacto sin opt-out, bloqueo, restricción ni `do_not_contact`;
- cero ownership contradictorio del teléfono.

## Efecto

En una sola transacción:

1. agrega o verifica el `contact_point` de teléfono en el owner WABA;
2. enrola ese mismo contacto en la cohorte mediante el RPC existente;
3. registra un comando inmutable sin PII.

No crea casos, acciones, intentos outbound, mensajes ni llamadas externas. El comando es idempotente; reutilizar la misma clave con otra semántica falla cerrado.
