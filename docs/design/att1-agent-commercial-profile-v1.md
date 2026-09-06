# ATT1 — profile candidato de agente comercial

- **Estado:** Implementado como paquete candidato inerte; no aprobado ni activo
- **Alcance:** distribución versionada e instalación create-only de `agente-comercial`
- **No implica:** Brand Voice aprobada, Conversation Release aprobada, binding con el bridge, credenciales, routing, mensajes ni efectos externos

## Objetivo inmediato

Publicar un profile ATT1 reproducible que pueda instalarse sin heredar secretos,
sesiones, memoria ni conocimiento de otro cliente. Mientras la autoridad comercial
no cierre los gates de ATT1, el profile responde únicamente con un fallback neutro
y no posee herramientas ni capacidad de efecto.

## Paquete implementado

`profiles/att1/agente-comercial/` contiene:

- identidad genérica y fallback JSON fijo;
- conocimiento `fallback_only` sin marca, oferta, precio, links, IDs ni descuento;
- política conversacional `draft fallback only`;
- output contract compatible con el envelope actual del bridge;
- `config.yaml` sin toolsets, plugins, memoria ni superficies de runtime habilitadas; el API Server permanece deshabilitado;
- manifiesto con `activation_capability=false`, `provider_effect_capability=false` y `runtime_binding_included=false`.

`profiles/att1/att1-product-bundle-v1.json` fija tamaños y SHA-256. El instalador
`scripts/install_att1_product_profiles.py` verifica el bundle completo, escribe un
home privado mediante staging y publicación atómica create-only, y deja un recibo
de hashes. Nunca reemplaza un profile existente.

## Gates externos conservados

La publicación de una Conversation Release comercial continúa bloqueada por:

- ratificación directa de Marcela como autoridad comercial general;
- decisión sobre las 11 reglas y ejemplos de Brand Voice;
- ratificación de identidad, oferta, precio, idioma, país y baseline sanitario;
- recepción/autorización de materiales vigentes y sanitizados;
- cierre del destino y SLA de handoff;
- templates, consentimiento y bindings canónicos del canal;
- aprobación explícita de la Conversation Release.

Hasta cerrar esos gates, este paquete no puede adquirir facts comerciales, Brand
Voice, credenciales, runtime binding ni autorización de contacto.

## Verificación requerida

1. tests del paquete y del instalador;
2. suite `uv run pytest`;
3. instalación en un home ATT1 nuevo y aislado;
4. coincidencia exacta de hashes del recibo;
5. `hermes config check` y confirmación de plataformas, plugins y toolsets deshabilitados;
6. cero conexión con el bridge y cero efectos de proveedor.
