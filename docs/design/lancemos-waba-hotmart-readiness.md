# Preparación del canal real de Lancemos — WABA, Chatwoot y Hotmart

- **Estado:** Preparación parcial; software local verificado, dependencias externas pendientes
- **Fecha:** 2026-08-11
- **Workstream:** E
- **Alcance:** dejar el canal oficial listo para una prueba controlada de una oferta y un destinatario allowlisted
- **No implica:** webhook WABA observado, templates aprobados, configuración productiva correcta, despliegue nuevo, activación ni mensajes enviados

## 1. Resultado buscado

La primera prueba del canal real debe demostrar este corte mínimo:

```text
un tenant Lancemos
→ un account/inbox WABA de Chatwoot
→ un producto y una oferta de Hotmart
→ un evento autenticado y admitido
→ un template aprobado
→ un único destinatario de prueba autorizado
→ un mensaje físico y un estado durable reconciliado
```

No se amplía la cohorte, no se habilita texto libre y no se activa el piloto por
haber completado solamente esta prueba.

## 2. Qué puede cerrarse sin los datos de Juan

| Capacidad | Estado comprobado en el repositorio | Evidencia | Qué no prueba |
|---|---|---|---|
| Receptor Hotmart autenticado | Implementado | `docs/contracts/hotmart-cart-abandonment-v1.md` y tests de webhook | Entrega desde una cuenta Hotmart real |
| Scope por tenant, account/inbox, proveedor, producto y oferta | Implementado localmente | `docs/contracts/lancemos-pilot-boundary-v1.md` | Scope publicado o activo en Supabase productivo |
| WABA sin fallback a texto libre | Implementado localmente | `docs/contracts/lancemos-pilot-boundary-runtime-v1.md` | Disponibilidad del inbox o aceptación del provider |
| Template de primer contacto y follow-up | Implementado con un placeholder de body | `src/bridge/messaging.py` y tests de payload | Que Meta haya aprobado los templates reales |
| Factory productivo fail-fast | Implementado localmente | `src/bridge/app.py` y `tests/test_hotmart_webhook.py` | Que EasyPanel contenga la configuración correcta |
| Destinatario único allowlisted | Implementado | `src/bridge/messaging.py` | Autorización de una cohorte real |
| Purchase y opt-out como stops durables | Implementado y verificado localmente | contratos y evidencia local enlazados desde `docs/architecture.md` | Prueba contra WABA y dependencias productivas |
| Handoff humano | Implementado y verificado localmente | `docs/contracts/executable-human-handoff-v1.md` y evidencia enlazada desde `docs/architecture.md` | Asignación/nota sobre el inbox WABA real o worker productivo |

La base de software está preparada para un **E2E controlado**, pero no para tráfico
real del piloto. El handoff ya no es un gap de implementación; siguen pendientes
su configuración operativa y la evidencia sobre una conversación WABA real.

## 3. Datos externos que bloquean la prueba

### 3.1 WABA y Chatwoot

Una inspección read-only del 2026-08-11 confirmó, sin publicar IDs ni secretos:

- un inbox `Channel::Whatsapp` con provider `whatsapp_cloud`;
- número, Phone Number ID, WABA ID y token de provider presentes en Chatwoot;
- dos miembros humanos en el inbox oficial;
- un inbox legacy distinto de tipo `Channel::Api`;
- un webhook HTTPS compartido de cuenta hacia `/webhooks/chatwoot`, suscrito sólo
  a `message_created`;
- la instancia Evolution desconectada a nivel transporte.

La misma inspección produjo `no-go`: el bridge desplegado todavía apunta al inbox
legacy con provider `evolution`, tiene replies/dispatcher/outbound habilitados,
además de shadow y `ResolutionWorker` activos. Evolution conserva su integración
Chatwoot activa y no existe ningún Team de
Chatwoot para handoff. El artefacto desplegado tampoco expone `/ready` y antecede
al runtime WABA/handoff integrado. Supabase remoto tampoco contiene todavía las
tablas del perímetro Lancemos ni del handoff ejecutable.

Pendientes operativos:

- cargar el account/inbox WABA y la referencia opaca de canal en el servicio;
- mantener boundary, dispatcher, outbound, replies, splitter, shadow, resolución,
  compra, pausa humana, opt-out y handoff admission/projection en default-off,
  con backlogs de proyección en cero;
- retirar de Evolution su integración Chatwoot sin borrar el inbox histórico;
- crear o seleccionar un Team humano y verificar sus miembros;
- configurar el único destinatario de prueba en el secret store;
- desplegar una revisión verificable antes del inbound controlado.
- aplicar y verificar las migraciones remotas de perímetro/handoff antes de probar
  esas capacidades; el inbound de captura default-off no depende de aplicarlas.

### 3.2 Templates

Se requieren dos templates explícitos:

1. primer contacto;
2. follow-up fuera de ventana.

El control plane mostró 12 templates sincronizados con estado `APPROVED`, pero la
inspección sanitizada no leyó nombres ni contenido. Sigue pendiente identificar
de forma autorizada cuáles son los dos de Lancemos y validar idioma, categoría y
variables; el conteo agregado no habilita outbound.

Todos los templates deben:

- haber sido revisados y aprobados expresamente por Juan;
- tener aprobación vigente de Meta antes del E2E;
- registrar nombre canónico, idioma, categoría y esquema de variables;
- coincidir con el contrato actual: exactamente un placeholder de body `{{1}}`.

El runtime actual usa un idioma y una categoría compartidos para ambos templates.
Si los templates aprobados necesitan idiomas, categorías o variables diferentes,
eso es un cambio de contrato y código que debe resolverse antes de configurar el
canal; no se adaptará el payload manualmente en producción.

### 3.3 Hotmart

Pendientes:

- website/subcuenta autoritativa;
- product ID exacto;
- offer code exacto;
- confirmación de que el evento objetivo es
  `PURCHASE_OUT_OF_SHOPPING_CART`;
- Hottok cargado sólo en EasyPanel;
- confirmación de si el primer E2E recibirá un webhook emitido por Hotmart o una
  reproducción manual autenticada del payload oficial V2.

Una reproducción manual comprueba el bridge y el canal, pero **no** comprueba la
entrega real desde Hotmart.

## 4. Mapeo de decisiones a configuración

No se guardan valores reales en Git.

| Decisión aprobada | Contrato/configuración afectada |
|---|---|
| Account Chatwoot | `CHATWOOT_ACCOUNT_ID` y scope durable |
| Inbox WABA | `CHATWOOT_INBOX_ID` y scope durable |
| Cuenta/canal WABA | `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF` |
| Proveedor oficial | `LANCEMOS_PILOT_CHANNEL_PROVIDER=waba` |
| Template de apertura | `WABA_FIRST_TOUCH_TEMPLATE_NAME` |
| Template de seguimiento | `WABA_FOLLOWUP_TEMPLATE_NAME` |
| Idioma común | `WABA_TEMPLATE_LANGUAGE` |
| Categoría común | `WABA_TEMPLATE_CATEGORY` |
| Producto y oferta | versión publicada del scope durable; no variables libres del caller |
| Destinatario controlado | `ALLOWED_WHATSAPP_JID` en el secret store |
| Hottok | `HOTMART_HOTTOK` en el secret store |

## 5. Gates antes de cualquier efecto

Debe mantenerse:

```text
LANCEMOS_PILOT_BOUNDARY_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
```

hasta verificar, en este orden:

- [ ] decisiones de la reunión registradas en
  [`questions-for-juan.md`](questions-for-juan.md);
- [ ] aprobación individual de Juan para ambos templates;
- [ ] aprobación vigente de Meta;
- [x] account, inbox y canal WABA verificados mediante lecturas read-only del
  control plane, con referencias sanitizadas;
- [ ] Evolution sin transporte ni integración Chatwoot activa;
- [ ] Team humano creado y con miembros verificados;
- [ ] bridge apuntando exclusivamente al inbox WABA con todos los efectos apagados;
- [ ] producto y oferta coinciden con la versión durable publicada;
- [ ] configuración completa en EasyPanel, sin copiar secretos a Git ni al chat;
- [ ] `/ready` devuelve un estado sanitizado y coherente con automatización
  inactiva;
- [ ] destinatario de prueba único y presupuesto máximo de un primer contacto;
- [ ] operador presente y rollback preparado.

La ausencia o inconsistencia de cualquiera de estos datos conserva el resultado
`no-go` para efectos externos.

## 6. Trabajo preparado ahora

El procedimiento ejecutable para cuando estén disponibles los datos está en
[`lancemos-controlled-channel-e2e-runbook.md`](../operations/lancemos-controlled-channel-e2e-runbook.md).
Hasta entonces pueden ejecutarse sin efectos externos:

- suite completa Python y SQL;
- `scripts/verify_chatwoot_waba_readiness.py` sobre un snapshot sanitizado; el
  script nunca devuelve IDs, teléfonos ni valores desconocidos del input;
- rechazo de un webhook firmado del mismo JID cuando account/inbox no coinciden,
  antes de captura, pausa, Hermes o efectos;
- factory WABA con valores ficticios y sender real no inyectado;
- matriz fail-fast al omitir cada campo de template;
- inspección de payloads `template_params` para apertura y follow-up;
- rechazo de pares provider/modo incompatibles;
- controles de contrato entre `.env.example`, `compose.yaml` y el runtime.

La corrida inbound previa al pago está definida separadamente en el runbook. Sólo
prueba WABA → Chatwoot → bridge y debe ejecutarse con replies, splitter, shadow,
resolución, compra Hotmart, pausa humana, dispatcher, outbound, opt-out
durable/projection, handoff admission/projection y perímetro apagados. Los
backlogs de proyección de opt-out y handoff deben ser cero. No usa template ni
habilita Hotmart.

## 7. Criterio de cierre del Workstream E

E sólo estará terminado cuando exista evidencia sanitizada de:

1. recursos oficiales verificados;
2. templates aprobados por Juan y Meta;
3. configuración desplegada default-off;
4. un E2E controlado con llegada física, replay sin duplicado y rechazo de scope
   incorrecto;
5. rollback comprobado;
6. cero activación de una cohorte general.

Hasta entonces el estado correcto es **preparación parcial**.
