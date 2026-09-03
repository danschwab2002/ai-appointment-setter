# Diseño: runtime portable single-tenant por aliada v1

- Estado: propuesta con dos bloques estrechos implementados localmente
- Fecha: 2026-09-01
- Decisión relacionada: `docs/decisions/0005-reproducible-client-deployments.md`
- Contrato: `docs/contracts/commercial-ally-runtime-v1.md`

## Objetivo

Permitir instalar una nueva aliada en un stack aislado sin reutilizar secretos, PII, sesiones, memoria ni estado runtime de otra aliada. Los identificadores no secretos del cliente deben quedar en una configuración explícita y en una autoridad durable versionada; su ausencia o divergencia bloquea readiness.

## Alcance implementado en este bloque

1. `CommercialAllyConfig` representa un binding no secreto, exacto y versionado.
2. `COMMERCIAL_ALLY_CONFIG_PATH` carga un manifiesto JSON de claves exactas.
3. El parser y la RPC portable de lead precheckout validan producto, oferta,
   landing y scope contra ese binding y admiten el evento durable sin crear
   timers, mensajes ni otros efectos.
4. La fábrica ASGI permite únicamente esa admisión y el stop portable
   `PURCHASE_APPROVED`; rechaza al iniciar cualquier capacidad ATT1 cuya RPC,
   worker o sender todavía sea legado.
5. Un account/inbox distinto del legado exige un manifiesto; account e inbox deben coincidir exactamente con él.
6. La migración `20260901000100_commercial_ally_portability.sql` crea una autoridad durable sin sembrar estado de ninguna aliada.
7. `/ready` falla con `503` para un binding no legado si Supabase no devuelve exactamente la versión activa esperada.
8. La ausencia del manifiesto conserva sólo compatibilidad explícitamente acotada al account legado `1` y al inbox legado `9` o no configurado. No permite interpretar otro account/inbox como Johanna.
9. El segundo bloque admite sólo `PURCHASE_APPROVED` autenticado, correlaciona
   dentro del binding y detiene un intent exacto; no agenda ni envía nada.
10. Su lookback no se inventa: requiere una fila durable explícita, sin seed y
    `enabled=false` por defecto.

## Flujo

```text
manifiesto JSON no secreto
  -> validación local exacta
  -> Settings
  -> autoridad durable activa en Supabase
  -> /ready = 200 sólo si no hay drift
  -> admisión portable lead.precheckout y/o stop PURCHASE_APPROVED
  -> cero outbound y cero workers de efectos ATT1
```

Los secretos siguen siendo variables administradas por el runtime y nunca forman parte del manifiesto ni de la tabla.

## Estados del binding durable

- `draft`: capturado, todavía incompleto o no revisado;
- `validated`: pasó validaciones determinísticas;
- `approved`: aprobado operativamente, todavía sin activar;
- `active`: única versión resoluble por runtime;
- `retired`: versión fuera de uso.

Una restricción parcial permite como máximo una versión `active` por `(tenant_ref, funnel_ref)`.

## Fail-closed

Para una aliada no legada:

- falta el manifiesto: el account/inbox no legado es rechazado al iniciar;
- manifiesto inválido, incompleto o con claves extra: inicio rechazado;
- account/inbox del entorno distinto del manifiesto: inicio rechazado;
- fila durable ausente, no activa, ambigua, mal formada o distinta: `/ready` responde `503`;
- payload de lead o Hotmart fuera de producto/oferta/landing: rechazado por el parser;
- cualquier campo booleano que no sea tipo `bool`, cualquier flag no portado o
  `HOTMART_HOTTOK` sin el flag portable de stop: inicio rechazado, incluyendo
  workers, agente, dispatcher, respuestas y controles todavía no portados;
- las funcionalidades de efectos permanecen `default-off` y no son activables para ATT1 en este bloque.

## Límites deliberados de este bloque

Este bloque no afirma portabilidad completa de todos los efectos heredados. Aún existen rutas y nombres históricos para carrito, pago fallido, delayed first-touch, plantillas WABA y RPCs denominados Johanna. No deben activarse para ATT1 hasta que un bloque posterior:

1. haga que sus plantillas y copy versions provengan de la Conversation Release/configuración aprobada;
2. haga que sus RPCs deriven autoridad del binding durable;
3. elimine gates exactos de account `1`, inbox `9` y `chatwoot-inbox:9`;
4. pruebe una matriz Johanna/ATT1 sin contaminación cruzada;
5. construya una instalación limpia que no ejecute semillas o estado específico de otra aliada.

Por lo tanto, el resultado actual habilita configuración, readiness, admisión
durable de lead y stop durable de compra. No habilita agente ni outbound ATT1.

## Instalación limpia

La instalación ATT1 debe usar el paquete reproducible de referencia y no una copia de la base, profile, volumen o `.env` de Johanna. La migración de binding no inserta filas. Provisioning debe crear el binding ATT1 como `draft`, validarlo, aprobarlo y recién entonces promover una sola versión a `active`.

Los primeros facts comerciales recibidos están preservados, todavía sin autoridad
de activación, en `config/commercial-allies/att1/intake-v1.json` y en la
[`Conversation Release ATT1 V1`](att1-conversation-release-v1.md). Ya se conocen
nombre público de oferta, precio base reportado, moneda, audiencia, outcome de
compra y receptora humana candidata. Esos datos no completan el manifiesto
operativo: faltan las referencias canónicas de landing, Hotmart y Chatwoot.

## Próximo bloque técnico

Parametrizar por separado cualquier futura cadena de efectos (carrito, pago
fallido, incentivos versionados y precheckout diferido) y sus plantillas desde el
binding/Conversation Release. Este stop no fija descuentos, secuencias ni copy.
