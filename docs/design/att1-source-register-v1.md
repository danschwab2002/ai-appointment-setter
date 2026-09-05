# ATT1 — registro de fuentes y decisiones V1

- **Estado:** Product ID confirmado; corpus GHL completo sanitizado y candidatos técnicos registrados; ratificación general, materiales y publicación pendientes
- **Fechas de fuente:** 2026-09-01, 2026-09-02 y 2026-09-04
- **Alcance:** respuestas iniciales de Mariana sobre una oferta del piloto
- **Fuente opaca:** `operator-interview:2026-09-01-mariana-01`
- **Confirmación posterior:** `operator-confirmation:2026-09-02-authority-content-health-language-countries`
- **Confirmación de descuento:** `operator-confirmation:2026-09-02-discount`
- **Aprobadora de descuento reportada:** `operator-confirmation:2026-09-02-discount-approver-marcela`
- **Confirmación Product ID:** `operator-confirmation:2026-09-04-hotmart-product-id`
- **Evidencia GHL:** `ghl-sanitized-corpus:2026-09-04-full` — 768 conversaciones, 4.097 mensajes; sin payload original persistido
- **Custodia:** las capturas originales permanecen fuera de Git; este documento conserva sólo información comercial sanitizada
- **No implica:** Conversation Release aprobada, binding productivo, autorización comercial, deploy ni contacto real
- **Paquete de cierre comercial:** [aprobación de información comercial V1](att1-commercial-information-approval-v1.md)

## 1. Preguntas realizadas

1. oferta única del piloto;
2. audiencia;
3. moneda;
4. resultado observable de éxito;
5. responsable operativo de Lancemos;
6. responsable de handoffs humanos;
7. materiales disponibles.

La entrevista inicial no resolvió país ni idioma. El operador confirmó después
México y español latino neutral como valores candidatos; Marcela todavía debe
ratificarlos como autoridad comercial.

## 2. Unidades recibidas

| Item | Clase actual | Contenido sanitizado recibido | Destino | Estado |
|---|---|---|---|---|
| `att1-item-001` | `reported_fact` | La oferta principal reportada es **Alimenta Tu Tiroides**. | scope / offer knowledge | operator_confirmed_pending_marcela_ratification |
| `att1-item-002` | `reported_fact` | Precio base reportado: **USD 47**. | offer knowledge / runtime manifest futuro | operator_confirmed_pending_marcela_ratification |
| `att1-item-003` | `reported_fact` | Audiencia principal reportada: mujeres de 35 a 55 años, trabajadoras o emprendedoras, con ingresos propios y diagnosticadas con hipotiroidismo y/o Hashimoto. | audience / safety review | received_unverified |
| `att1-item-004` | `reported_fact` | Distribución reportada: México 60%, USA 15%, Colombia 10%, Canadá 3% y España 3%. | pilot scope | received_unverified |
| `att1-item-005` | `proposed_rule` | Resultado de conversión propuesto: compra observada. | acceptance / outcome | operator_confirmed_pending_marcela_ratification |
| `att1-item-006` | `reported_fact` | Mariana reporta que atiende estos chats y sería la receptora humana. | handoff business policy | received_unverified |
| `att1-item-006a` | `reported_fact` | La identidad pública candidata para la aliada es **Dra. Nina Garza**. | ally identity / offer knowledge | operator_confirmed_pending_marcela_ratification |
| `att1-item-007` | `unknown` con inventario | Se reportan materiales sobre historia/autoridad, oferta, audiencia, copy, páginas, ads y upsell/post-oferta. No fueron recibidos ni sanitizados todavía. | source intake | pending |
| `att1-item-008` | `confirmed_identifier` | Product ID Hotmart **`5071808`**. | runtime binding | operator_confirmed |
| `att1-item-009` | `observed_identifier` | Checkout/hotlink **`D98014973Y`**. | runtime binding | ghl_observed_pending_ratification |
| `att1-item-010` | `observed_identifier` | Offer code dominante **`83utgyow`**. | runtime binding | ghl_observed_pending_ratification |
| `att1-item-011` | `observed_identifier` | Landing dominante `raizana.com.mx/inscribirme-alimenta-tu-tiroides`. | lead binding | ghl_observed_pending_consent_and_ratification |
| `att1-item-012` | `observed_identifier` | Cupón histórico dominante **`SOYRAIZANA10`**. | discount policy variable | ghl_observed_not_authorized_for_publication |

`att1-item-004` suma 91%. El 9% restante no se asigna por inferencia y no debe usarse para definir países del piloto.

La confirmación del operador del 2026-09-02 registra además México, español
latino neutral y el baseline sanitario fail-closed como valores candidatos.
Marcela todavía debe ratificarlos. Estas decisiones no validan el resto de la
distribución de audiencia, no sustituyen los materiales ATT1 y no autorizan
activación.

El operador confirmó además un cupón general de 10 %, sin vencimiento ni copy de
urgencia, para los tres triggers durables existentes y sólo en `later_step`, una
vez recibida al menos una respuesta inbound posterior a la plantilla inicial de inicio de conversación de Meta. El código será una variable de Meta.
La aprobación de Marcela fue reportada por el operador. El contrato durable ya
representa vigencia indefinida, inbound obligatorio, variable Meta y ausencia de
urgencia. La plantilla, su mapeo y la ejecución runtime siguen pendientes; no
existe una política publicada. Su confirmación directa como autoridad comercial general
permanece en el gate consolidado.

## 3. Materiales declarados disponibles

- historia y autoridad de la aliada;
- transformación, temario, precio y order bumps de `Alimenta Tu Tiroides`;
- perfil detallado de audiencia;
- copy, promesa, pilares y mensajes principales;
- páginas web;
- ads;
- upsell y oferta posterior de `Método Raizana Tiroidea` y `Gut Raiz`.

Esta lista demuestra disponibilidad declarada, no recepción, vigencia, permiso de uso, autoría ni sanitización. Ningún contenido de esos materiales fue incorporado al agente.

## 4. Gates que esta fuente avanza

| Gate | Estado anterior | Estado actual |
|---|---|---|
| Oferta única identificada | faltante | candidato confirmado por el operador; Product ID confirmado, checkout/oferta GHL pendientes de ratificación |
| Precio y moneda | faltante | candidatos confirmados por el operador; ratificación y contraste con checkout/configuración canónica pendientes |
| Audiencia inicial | faltante | recibida; México confirmado por el operador y pendiente de ratificación de Marcela |
| Condición de éxito | faltante | `purchase_observed` confirmado por el operador y pendiente de ratificación de Marcela |
| Receptor humano | faltante | Mariana identificada; falta Team/assignee Chatwoot, horario y SLA |
| Inventario de fuentes | faltante | categorías declaradas; archivos y custodia pendientes |
| Política de descuento | faltante | aprobación de Marcela reportada; contrato indefinite compatible, template/ejecución pendientes; no publicada |

## 5. Bloqueos conservados

- propietario comercial; Marcela fue identificada como autoridad de aprobación;
- alcance exacto de la responsabilidad operativa de Lancemos;
- website y Offer code canónicos de Hotmart; el Product ID `5071808` ya está confirmado;
- ratificación del checkout/hotlink observado, landing/formulario y consentimiento comercial;
- account, inbox, AgentBot y Team/assignee de Chatwoot;
- horario, zona horaria, SLA y conducta fuera de horario para Mariana;
- recepción privada, escaneo, vigencia y permiso de uso de los materiales;
- claims comerciales específicos y cualquier ampliación futura del baseline sanitario;
- templates WABA y Conversation Release completa;
- texto/template y variable del cupón, rama runtime inbound → `later_step` y política económica publicada;
- autorización separada para contactar personas reales.

## 6. Límite sanitario fail-closed

El dato de audiencia menciona diagnósticos de salud. El operador confirmó el
baseline fail-closed, pero Marcela aún debe ratificarlo y siguen faltando fuentes
sanitizadas. Mientras el gate permanezca abierto, el sistema no puede
diagnosticar, interpretar síntomas, recomendar tratamientos o medicación,
prometer resultados clínicos ni solicitar historia médica. Cualquier consulta
clínica personalizada debe salir del objetivo comercial y derivarse según la
política humana aprobada.

## 7. Gate consolidado de información comercial

Los seis dominios de la macro de información comercial —materiales, contenido,
límites sanitarios, idioma, países y descuento— se consolidan en
`att1-commercial-information-approval-v1.md`. Su estado es
`pending_external_approval`: este registro preserva evidencia recibida, pero no
puede suplir la respuesta escrita de la autoridad comercial ni la recepción y
sanitización de los materiales declarados.

## 8. Fuentes deliberadamente excluidas

Dos PDFs privados recibidos durante el onboarding de Johanna —una VSL y un
material complementario de oferta/proceso— pertenecen a ese cliente anterior y
no constituyen evidencia de ATT1. Permanecen fuera de Git y no pueden alimentar
facts, voz, ejemplos, límites, templates ni decisiones de la nueva aliada. Su
presencia local no cambia `materials_received_and_sanitized: false`.

## 9. Cobertura y sanitización GHL ampliadas

El corpus completo preserva 761 conversaciones con mensajes y siete vacías. Incluye 2.035 mensajes de automatización, 1.318 de clientes, 493 de asesores humanos verificados y 251 `other_outbound` de autoría no resuelta. Los detectores automáticos encontraron cero emails, teléfonos, URLs o IDs largos directos, pero 52 conversaciones contienen elementos no textuales omitidos y la revisión humana de posibles nombres libres sigue pendiente. Por ello el corpus puede descubrir candidatos y contradicciones, pero no aprobar facts ni templates.
