# ADR-0015 — Consentimiento WhatsApp versionado desde la landing

- **Estado:** Aceptada; implementada localmente, pendiente de despliegue
- **Fecha:** 2026-08-21
- **Supersede parcialmente:** ADR-0011 sólo para el adapter observado `lead.precheckout`
- **Relacionado:** ADR-0014

## Contexto

El contrato observado `lead.precheckout` V1.0.0 conserva
`whatsapp_contact_authorized=false` y `activation_authorized=false`. Esa frontera
fue correcta mientras la landing no declaraba una autorización explícita para
contactar por WhatsApp.

El piloto necesita continuar el flujo técnico sin inferir autorización desde
Términos o Privacidad y sin habilitar todavía ningún efecto externo. La landing
incorporará una aclaración explícita y su relay server-side debe poder afirmar,
en un contrato autenticado, qué versión de esa aclaración fue presentada y
aceptada.

## Decisión

Se conserva `lead.precheckout` V1.0.0 sin cambios y se agrega V1.1.0.

V1.1.0 exige que el objeto `data.consent` contenga exactamente:

```text
marketing_optin=true
whatsapp_contact=true
copy_version=johanna-precheckout-whatsapp-disclosure-v1
```

Esos valores deben ser fijados por el relay server-side después de la interacción
correspondiente en la landing; no se aceptan como autoridad si vienen de parámetros
libres del navegador. El HMAC sigue cubriendo los bytes exactos del evento.

El bridge sólo deriva autorización durable cuando también se cumplen:

- contrato externo V1.1.0 exacto;
- scope piloto exacto;
- email válido;
- teléfono E.164 válido y consistente con país/prefijo;
- copy version exacta;
- identidad raw firmada igual a la identidad canónica persistida;
- scope, comercio y timestamp raw firmados iguales a su representación canónica;
- identidad no conflictiva.

Para una admisión V1.1.0 válida, la representación canónica fija:

```text
whatsapp_contact_authorized=true
activation_authorized=true
```

La segunda marca significa únicamente que este contrato observado y este scope
están habilitados para continuar la reevaluación interna. No autoriza por sí sola
un request outbound.

Compatibilidad y precedencia:

- V1.0.0 continúa creando intenciones no autorizadas;
- una V1.1.0 posterior puede promover una intención viva con identidad consistente;
- una V1.0.0 posterior no revoca un consentimiento V1.1.0 ya probado;
- `identity_conflict`, opt-out, takeover, compra y cualquier denial/restriction
  continúan prevaleciendo;
- replay exacto no duplica ni cambia autoridad;
- mismo delivery ID con contenido distinto sigue siendo `semantic_conflict`.

Cuando ambas autorizaciones locales están presentes y el timer vence, el corte
actual termina en `blocked_contact_binding_missing`. Eso prueba que la autorización
superó su gate, pero conserva cero contacto, cero caso comercial y cero outbound.
La creación/binding canónico de contacto pertenece a un contrato posterior.

## Consecuencias

- El cambio es aditivo y versionado; no reinterpretamos eventos históricos V1.0.0.
- La evidencia de copy version queda dentro del payload canónico durable.
- El relay de la landing debe actualizarse antes de emitir V1.1.0.
- Desplegar parser y migración no activa workers, dispatcher ni outbound.
- El texto concreto de la aclaración vive en la landing y debe conservar la
  `copy_version` declarada; este ADR no inventa ni aprueba redacción legal.

## Alternativas descartadas

### Inferir autorización desde Términos/Privacidad

Descartada porque no produce evidencia explícita y versionada de contacto por
WhatsApp.

### Reinterpretar V1.0.0 como autorizado

Descartada porque cambiaría retroactivamente el significado de eventos ya
persistidos.

### Permitir que el timer cree directamente una acción outbound

Descartada. Scheduling, autorización, binding comercial, request-start y envío
permanecen separados.

## Estado de implementación

Parser, migración, contrato y probe PostgreSQL 17 están implementados y
verificados localmente. Hasta merge, aplicación de migración, actualización del
relay y E2E Cloud controlado, producción conserva V1.0.0 y los efectos siguen
default-off.
