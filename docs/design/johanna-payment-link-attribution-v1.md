# Diseño — atribución y envío seguro del enlace de pago de Johanna V1

- **Estado:** Base aceptada e implementada localmente; release operativo pendiente
- **Alcance:** decisiones de diseño detrás del contrato `johanna-payment-link-v1`
- **Temas abiertos:** evidencia remota, compra sintética y autorización de activación

## Problema

El checkout observado en `lead.precheckout` contiene configuración comercial,
atribución externa y posiblemente PII. Permitir que un modelo lo construya o
que una capa intermedia lo normalice puede cambiar la oferta, perder
atribución, filtrar datos o enviar un enlace ajeno a la secuencia vigente.
Además, un timeout de Chatwoot no demuestra si el mensaje fue creado.

## Diseño aceptado

### Separación de autoridad

```text
Hermes: decide send_payment_link y redacta texto sin URL
   ↓
Supabase: resuelve la secuencia y checkout canónicos
   ↓
bridge determinístico: valida y añade src/xcod
   ↓
Supabase: reautoriza y reserva una command durable
   ↓
Chatwoot: relee identidad, assignee, pausa e historial
   ↓
autorizador SQL final
   ↓
POST único o reconciliación exacta
```

La decisión generativa no transporta autoridad ni datos sensibles. La última
decisión de envío combina evidencia canónica de Chatwoot con autoridad durable
de Supabase inmediatamente antes del efecto.

### Marcador

Se conserva el checkout byte por byte y sólo se añade
`src=hermes-<ULID completo>`. `xcod` es un fallback estricto cuando `src` ya
está ocupado. No se usa un ULID abreviado porque no existe evidencia de
truncamiento de Hotmart ni una prueba equivalente de unicidad y persistencia.

El marcador identifica la secuencia de origen; no sustituye `sck`, `fbclid` ni
UTMs. Una nueva submission crea una nueva secuencia, en vez de mutar la
identidad histórica.

### Efecto durable

El binding inmutable prueba qué checkout y marcador pertenecen a la secuencia.
La command durable separa `request_started`, aceptación e incertidumbre. Ante
una respuesta perdida no se reenvía: se inspecciona Chatwoot y sólo se promueve
`delivery_unknown` al encontrar el mensaje exacto.

### Fallo cerrado

Se eligió derivar a humano —no reconstruir ni relajar validaciones— cuando el
checkout está vencido, ambos campos están ocupados, una autoridad cambia, el
modelo incluye una URL o una respuesta remota es incompleta. Esto incluye
`conversation.meta.assignee`: ausencia y `null` son estados distintos; sólo la
clave explícita con valor `null` representa conversación sin assignee.

## Consecuencias

- Se preservan oferta, prellenado y atribución externa.
- El modelo no puede exfiltrar ni sustituir el checkout.
- Un efecto ambiguo exige reconciliación, no retry automático.
- El URL completo existe sólo en fronteras funcionales indispensables.
- La operación necesita un postflight remoto y una compra real de prueba antes
  de habilitar el feature.

## Alternativas descartadas

- Construir o buscar el enlace desde Hermes.
- Parsear y reserializar el query del checkout.
- Sobrescribir `sck`, `fbclid`, UTMs o un `src` existente.
- Usar shortlinks o abreviar el ULID sin evidencia.
- Reintentar el POST después de timeout sin reconciliación.
- Activar al mismo tiempo que se migra o despliega.

## Referencias

- [Contrato V1](../contracts/johanna-payment-link-v1.md)
- [Runbook de release](../operations/johanna-payment-link-release-v1.md)
- Migración: `supabase/migrations/20260911000400_payment_link_attribution.sql`
