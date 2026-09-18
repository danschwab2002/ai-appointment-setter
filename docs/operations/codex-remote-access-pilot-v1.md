# Acceso SSH restringido de Codex — piloto Stalled Monitor

- Estado: acceso Unix implementado; selección del host/proyecto en Desktop pendiente de Dan.
- Fecha: 2026-09-18.
- Base del piloto: `1077282d8cc2001028803a26120a13c723a00b65`.
- Claim: `codex-remote-access-pilot-v1`.
- Rama: `ops/codex-remote-access-pilot-v1`.

## Frontera del piloto

La Mac es la interfaz. Codex CLI 0.155.0 está instalado y autenticado en la VPS como usuario `codex`, con home propio y SSH mediante el alias `contabo-codex`. El usuario no tiene sudo ni pertenece al grupo Docker. No se creó un clon local.

El checkout de integración sigue siendo `/opt/hermes/projects/ai-appointment-setter-integration` en el host y `/opt/data/projects/ai-appointment-setter-integration` en Hermes. Codex tiene lectura de los archivos versionados; no puede escribir en integración ni en los metadatos Git compartidos.

El worktree del piloto es `/opt/hermes/projects/ai-appointment-setter-codex-pilot` en el host y `/opt/data/projects/ai-appointment-setter-codex-pilot` en Hermes. Se creó con `scripts/agent_workspace.py` desde el entorno del integrador. Sólo el archivo `.git` de este nuevo worktree usa una referencia relativa, compatible con ambas vistas. No se repararon ni limpiaron worktrees ajenos.

Codex puede escribir en el árbol propio, pero el scope del claim de configuración se limita a este documento. No autoriza cambios del monitor. Una futura implementación requiere un claim y worktree propios y libres de solapamientos. El claim anterior del PR #156 debe ser reconciliado por el integrador antes de reservar esos mismos archivos.

## Permisos

Se instalaron utilidades ACL y se aplicaron ACL específicas al usuario `codex`:

- traversal de los ancestros necesarios;
- lectura de código versionado e información Git necesaria para consulta;
- escritura del árbol propio;
- denegación de otros directorios de Hermes, profiles activos, secretos, sesiones, logs y otros worktrees;
- denegación de archivos/directorios no versionados existentes, incluido el entorno Python creado dentro del contenedor;
- metadatos Git compartidos sin escritura.

Las ACL originales se conservaron en backups administrativos de sólo root. Las denegaciones predeterminadas en los directorios de integración protegen nuevas entradas; los nuevos archivos versionados pueden necesitar que el integrador actualice su permiso de lectura. Archivos movidos desde fuera conservan sus ACL: al introducir o reemplazar archivos, el integrador debe comprobar permisos nuevamente. Esto no es un aislamiento de red ni garantiza que un archivo sensible colocado en una ruta expresamente legible quede protegido.

El home y la caché de Codex son propios. Para pruebas futuras en el host se debe instalar uv y crear un entorno Python propio; no reutilizar `.venv` de Hermes ni cargar sus variables de entorno. Esta tarea documental no instaló dependencias del producto ni ejecutó E2E.

## Operaciones del integrador

El integrador conserva creación de claims y worktrees, transiciones, commits, push y PR. No se instaló un servicio privilegiado ni se concedió sudo a Codex.

El preflight global inspecciona otros worktrees y por eso se ejecuta desde el entorno del integrador. Antes de ejecutarlo, comprobar que el archivo `.git` del piloto mantiene la referencia relativa esperada y que el diff se limita al scope autorizado. Usar el coordinador de integración, no una copia modificable por el agente:

```sh
cd /opt/data/projects/ai-appointment-setter-integration
uv run python scripts/agent_workspace.py \
  --repo /opt/data/projects/ai-appointment-setter-codex-pilot preflight
```

El agente remoto solicita esta comprobación al integrador antes de editar y antes de entregar/commitear. No debe ampliar permisos para hacerla por su cuenta ni confundir `git status` local con un preflight global aprobado. Si el integrador no está disponible, se detiene antes de editar.

## Verificación

El preflight inicial y el posterior a la aplicación de permisos terminaron con código cero. Como `codex` se verificó lectura de AGENTS.md, reconocimiento de la rama y del worktree, y denegación de escritura de integración y `.git` compartido. También se comprobó denegación de acceso a `.env`, auth, profiles y sesiones de Hermes, un worktree ajeno y Docker. La escritura de este documento se realiza con la identidad `codex`.

La apertura nativa en Desktop aún requiere seleccionar Configuración → Conexiones → SSH → `contabo-codex`. La carpeta de control es integración; para editar este piloto se debe abrir el worktree existente y no pedir a Desktop que cree otro worktree con permisos de Codex.

## Producción

No se desplegó, reinició ni activó ningún servicio. El estado del Stalled Monitor debe revalidarse antes de un futuro despliegue o activación; configurar acceso de desarrollo no autoriza ninguna de esas acciones.
