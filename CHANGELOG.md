# Changelog

Cada versión dice qué cambia y **qué tiene que hacer quien actualiza una instancia**: nada, correr una migración o sumar un campo al manifiesto. Las versiones siguen [SemVer](https://semver.org/lang/es/) con el criterio de [ADR-0021](docs/decisions/0021-setter-producto-instalable.md):

- **Mayor:** la instancia tiene que cambiar algo para actualizar (un campo obligatorio nuevo en el manifiesto, una migración con paso manual).
- **Menor:** funcionalidad nueva, apagada por defecto.
- **Parche:** arreglos.

Cada tag `vX.Y.Z` publica tres imágenes en GHCR con ese tag: `ghcr.io/danschwab2002/setter-bridge`, `setter-slack-connector` y `setter-daily-feedback`. La release de GitHub lleva el digest de cada una. Cómo se pasa un servicio a la imagen y cómo se vuelve atrás: [docs/operations/release-por-imagen-v1.md](docs/operations/release-por-imagen-v1.md).

## [1.0.0] - 2026-09-28

Primera versión con tag. **El código es el mismo que corre hoy para Johanna.** Los tres servicios desplegados al 2026-09-28 (bridge en `4cb421d`, revisión diaria en `a50bfa1`, conector de Slack en `bfae40c`) no tienen diferencias en `src/` contra esta versión. Pasar un servicio a esta imagen no cambia su comportamiento.

### Agregado

- Workflow `release.yml`: un tag `vX.Y.Z` en `main` construye y publica las tres imágenes en GHCR y crea la release con el changelog y los digests. Antes de publicar, verifica que el tag coincida con `pyproject.toml`, que exista la entrada en este archivo y que el commit esté en `main`.
- Las imágenes llevan `SETTER_VERSION` y `GIT_SHA` adentro. Si el servicio no define `GIT_SHA`, el bridge toma la de la imagen para la procedencia de cada pedido al agente (`bridge_release`), en vez de `unknown`.

### Qué tiene que hacer una instancia

- Nada en la base ni en el manifiesto.
- Para usar la imagen en vez de construir desde el repo: seguir [release-por-imagen-v1.md](docs/operations/release-por-imagen-v1.md). Hace falta un token de GitHub de solo lectura de paquetes (`read:packages`) cargado en EasyPanel como credencial de registro.

### Probado con

- Hermes `nousresearch/hermes-agent:v2026.8.31`.
