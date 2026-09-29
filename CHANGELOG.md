# Changelog

Cada versión dice qué cambia y **qué tiene que hacer quien actualiza una instancia**: nada, correr una migración o sumar un campo al manifiesto. Las versiones siguen [SemVer](https://semver.org/lang/es/) con el criterio de [ADR-0021](docs/decisions/0021-setter-producto-instalable.md):

- **Mayor:** la instancia tiene que cambiar algo para actualizar (un campo obligatorio nuevo en el manifiesto, una migración con paso manual).
- **Menor:** funcionalidad nueva, apagada por defecto.
- **Parche:** arreglos.

Cada tag `vX.Y.Z` publica tres imágenes en GHCR con ese tag: `ghcr.io/danschwab2002/setter-bridge`, `setter-slack-connector` y `setter-daily-feedback`. La release de GitHub lleva el digest de cada una. Cómo se pasa un servicio a la imagen y cómo se vuelve atrás: [docs/operations/release-por-imagen-v1.md](docs/operations/release-por-imagen-v1.md).

## [1.1.0] - sin publicar

El bridge lee el manifiesto de la instancia. Es la primera versión con la que una aliada se instala desde su repo (`setter-instancia-<aliada>`) sin código propio. Apagado por defecto: sin `INSTANCE_MANIFEST_PATH` el bridge arranca igual que en 1.0.0.

### Agregado

- `INSTANCE_MANIFEST_PATH`: el binding de la aliada sale de `instancia.toml` (excluyente con `COMMERCIAL_ALLY_CONFIG_PATH`). La oferta por defecto es la del binding y las demás landings van en `additional_offer_codes`, así los eventos de Hotmart de cualquier landing de la instancia se admiten.
- `COMMERCIAL_KNOWLEDGE_ENABLED`: el conocimiento aprobado de `agente.conocimiento` viaja como mensaje `system` en cada pedido a Hermes. Con manifiesto, las respuestas automáticas lo exigen.
- El manifiesto es el techo de los flags del runtime: un flag prendido con su flujo en `false` no arranca, y `HERMES_MODEL_NAME` tiene que coincidir con `agente.modelo`. La tabla flag → flujo está en `docs/contracts/instance-runtime-v2.md`.
- La guarda de medicación usa `guardas.terminos_sensibles` y `guardas.acciones_sensibles` del manifiesto en vez de la lista por defecto.
- `/ready` informa `instance_ally`, `instance_product_version` y `commercial_knowledge` (versión y hash del bloque).
- SOUL común del agente comercial en `profiles/agente-comercial-comun/SOUL.md`: el de Johanna sin lo que es de Johanna, con un test que compara las secciones invariantes byte a byte.
- `docs/instalar.md`: la guía de instalación, escrita paso a paso con ATT1.
- La frontera del piloto acepta varias ofertas por scope (migración `20260929000100`): `pilot_scope_versions.additional_offer_codes`, con la misma forma que en el binding. Sin eso, un carrito o un pago fallido de una landing que no es la de la oferta por defecto pasaba la admisión y moría al planificarse (`pilot_offer_mismatch`, `payment_failure_scope_binding_mismatch`) o al arrancar el envío. Los scopes existentes quedan con el conjunto vacío: Johanna no cambia.

### Qué tiene que hacer una instancia

- Nada, si no usa manifiesto: Johanna sigue con el binding del código.
- Una instancia nueva monta `instancia.toml`, define `INSTANCE_MANIFEST_PATH` y, para responder automáticamente, `COMMERCIAL_KNOWLEDGE_ENABLED=true` con el conocimiento en `estado = "aprobado"`.
- En la base: aplicar la migración `20260929000100` (agrega una columna con valor por defecto; no toca filas). Una instancia con más de una landing carga en su scope del piloto las otras ofertas en `additional_offer_codes`; el scope todavía se siembra a mano (no lo crea ninguna migración del producto ni la guía).

## [1.0.0] - 2026-09-28

Primera versión con tag. **El código es el que corre para Johanna.** Servicios desplegados al 2026-09-29: bridge en `f7dd227`, revisión diaria en `a50bfa1`, conector de Slack en `bfae40c`. Contra `f7dd227` ninguno tiene diferencias en su código (la revisión diaria y el conector, solo en `Dockerfile`, `pyproject.toml` y `uv.lock`). Pasar un servicio a esta imagen no cambia su comportamiento.

### Agregado

- Workflow `release.yml`: un tag `vX.Y.Z` en `main` construye y publica las tres imágenes en GHCR y crea la release con el changelog y los digests. Antes de publicar, verifica que el tag coincida con `pyproject.toml`, que exista la entrada en este archivo y que el commit esté en `main`.
- Las imágenes llevan `SETTER_VERSION` y `GIT_SHA` adentro. Si el servicio no define `GIT_SHA`, el bridge toma la de la imagen para la procedencia de cada pedido al agente (`bridge_release`), en vez de `unknown`.

### Qué tiene que hacer una instancia

- Nada en la base ni en el manifiesto.
- Para usar la imagen en vez de construir desde el repo: seguir [release-por-imagen-v1.md](docs/operations/release-por-imagen-v1.md). Hace falta un token de GitHub de solo lectura de paquetes (`read:packages`) cargado en EasyPanel como credencial de registro.

### Probado con

- Hermes `nousresearch/hermes-agent:v2026.8.31`.
