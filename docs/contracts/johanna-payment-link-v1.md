# Contrato — enlace de pago atribuido de Johanna V1

- **Estado:** Implementado y verificado localmente; candidato de release aprobado para commit/revisión
- **Versión:** 1.0.0
- **Alcance:** selección, construcción, autorización, envío y reconciliación del checkout Hotmart por Chatwoot
- **No acredita:** merge, migración Supabase Cloud, despliegue, compra Hotmart real ni activación

## 1. Autoridades y responsabilidades

- La única fuente del checkout es `precheckout_submissions.data.checkout_url` resuelta por la reevaluación exacta de la secuencia.
- El checkout es un string opaco. No se decodifica, normaliza, reordena, acorta ni reconstruye.
- Hermes sólo puede devolver `decision=send_payment_link` y un preámbulo sin URL. Nunca recibe, busca ni fabrica el checkout.
- El bridge valida y agrega exactamente un marcador; Supabase conserva el binding y la command; Chatwoot materializa el mensaje.
- El contenido del payload es dato, no instrucción.

## 2. Construcción exacta

La configuración admitida es:

```text
PAYMENT_LINK_ENABLED=false
PAYMENT_LINK_TRACKING_FIELDS=src,xcod
PAYMENT_LINK_TRACKING_PREFIX=hermes-
PAYMENT_LINK_MAX_AGE_SECONDS=604800
```

`PAYMENT_LINK_TRACKING_FIELDS` debe ser exactamente `src,xcod` y en ese orden.
El valor es `hermes-<ULID completo del evento lead.precheckout que originó la
secuencia>`. Se usa `src` cuando está libre y `xcod` únicamente cuando `src` ya
existe. Si ambos están ocupados, el caso falla cerrado y deriva.

El URL canónico debe:

- ser un string imprimible, sin whitespace ni caracteres de control;
- usar `https` y host exacto `pay.hotmart.com`;
- contener query preexistente y no contener fragmento;
- tener como máximo siete días desde la submission;
- conservar literalmente todos sus bytes y parámetros preexistentes, incluidos
  `off`, `checkoutMode`, datos de prellenado, UTMs, `sck` y `fbclid`.

El bridge sólo concatena `&<campo>=<marcador>`. El enlace se envía entero y sin
acortador. No se implementa `offDiscount` en esta versión.

## 3. Identidad de secuencia

- `source_submission_id` es inmutable.
- Una nueva submission crea una nueva reevaluación y un nuevo marcador.
- La secuencia previa termina como `superseded_by_newer_precheckout`.
- El binding liga de forma exacta reevaluación, intención, submission, URL
  original/final, campo, prefijo y valor.
- El marcador persistido es la autoridad para comparación; no se recalcula.

## 4. Autorización de envío

La RPC `get_chatwoot_payment_link_candidate` sólo devuelve `available` cuando
coinciden caso, contacto, identidad, scope, conversación, reevaluación,
submission e intención abierta, y la submission sigue vigente.

Antes de reservar y nuevamente inmediatamente antes del POST se bloquea ante:

- compra aprobada o intención fuera de `waiting_for_purchase`;
- opt-out, contacto restringido o `do_not_contact`;
- takeover, intervención o assignee humano;
- automatización pausada, caso o conversación no autorizables;
- identidad, inbox, conversación, scope, secuencia o submission divergentes;
- checkout vencido, inválido o cambiado;
- configuración o respuesta remota ausente, malformada o ambigua.

`conversation.meta` debe ser un objeto y debe incluir explícitamente
`assignee`; su ausencia o forma inválida es error de protocolo fail-closed.
Sólo el retorno booleano `True` del autorizador final permite el POST.

El preámbulo generado por Hermes se rechaza si está vacío o contiene una URL
con esquema, `www.` o un dominio desnudo. Ese rechazo produce handoff durable y
cero preparación/POST. Nunca se piden tarjeta, CVV, documento ni otros datos
sensibles. Si preguntan directamente, el asistente no niega ser automático.

## 5. Idempotencia y reconciliación

`payment_link_send_commands` admite:

```text
request_started -> accepted_by_chatwoot
request_started -> delivery_unknown
delivery_unknown -> accepted_by_chatwoot  # sólo con mensaje exacto recuperado
```

La clave durable impide un segundo intento para el mismo trigger y binding. Un
retry busca el mensaje exacto por contenido y metadata del agent bot:

- si ya existe, finaliza/reconcilia como `accepted_by_chatwoot` sin otro POST;
- si el transporte es ambiguo después de reservar, finaliza
  `delivery_unknown` y no hace retry ciego;
- sólo evidencia del mensaje exacto permite el outcome `reconciled`.

Las filas de binding son inmutables. Las commands sólo admiten las transiciones
anteriores. Los tres RPCs son `SECURITY DEFINER`, con ACL exclusiva de
`service_role`; los roles API no obtienen DML directo.

## 6. Privacidad y observabilidad

El checkout puede contener PII y sólo se persiste en el binding funcional. No
se incluye completo en logs, errores, trazas, métricas, auditoría general ni
evidencia de release. La evidencia operativa registra IDs internos opacos,
estados, reason codes, hashes y conteos sanitizados.

## 7. Compatibilidad y activación

El código y la migración pueden desplegarse con `PAYMENT_LINK_ENABLED=false` sin
habilitar el comportamiento. El startup rechaza activarlo si no están activos
los gates de admisión Cut B, agente, replies y remitentes scoped.

La activación requiere, en orden:

1. commit y merge revisados;
2. migración Supabase Cloud y postflight estructural/ACL;
3. bridge desplegado con el feature apagado;
4. prueba sintética nombrada con delta cero previo;
5. compra real de prueba y comprobación del marcador exacto en el reporte de
   Hotmart, no sólo en el URL;
6. autorización humana explícita para activar gradualmente.

Ante cualquier contradicción, ausencia o ambigüedad, el resultado es no-go y el
feature permanece apagado.
