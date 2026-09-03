# Evidencia local — paquete de perfiles ATT1 V1

- **Fecha:** 2026-09-03
- **Estado:** candidato local verificado; no desplegado
- **Worktree:** `/opt/data/projects/ai-appointment-setter-att1-product-profiles`
- **Branch:** `feat/att1-product-profiles`
- **Bundle SHA-256 fijado por el instalador:** `f66d30d310027f5b6483941c1dd963156732837f323189864b549fefb71f2ea6`

## Alcance verificado

- bundle exacto de `agente-comercial`, `automation-expert` y `client-copilot`;
- aislamiento fuente bajo `profiles/att1/` sin reemplazar el profile comercial de otra aliada;
- configuración Hermes v40 de los tres paquetes ATT1;
- instalación create-only con permisos privados y recibos de integridad;
- rechazo de bundle o archivo fuente mutado;
- rechazo de profile desconocido, incluido `default`;
- carrera de creación concurrente resuelta sin reemplazar el destino;
- intercambio concurrente del parent por symlink sin escritura en el referent;
- rechazo de symlinks en parents de la fuente;
- ausencia deliberada de cleanup automático tras fallo; el staging privado se
  retiene para impedir que una carrera convierta la limpieza en el borrado de un
  objeto ajeno;
- preservación de la capacidad acotada preexistente de Client Copilot;
- cero credenciales productivas y cero efectos externos.

## Pruebas y verificaciones ejecutadas

### TDD dirigido

El primer corte produjo 14 fallos esperados y un control existente aprobado.
Después de implementar y aislar el paquete:

```text
uv run pytest tests/test_att1_product_profiles.py tests/test_client_copilot_profile_package.py
31 passed
```

La suite completa del repositorio y los checks estáticos también fueron
ejecutados después del empaquetado final:

```text
uv run pytest
1246 passed, 1 warning

uv run python -m compileall -q scripts/install_att1_product_profiles.py profiles/att1/client-copilot/plugins
git diff --check
uv run python scripts/agent_workspace.py preflight
exit code 0
```

La advertencia corresponde a una deprecación existente de Starlette TestClient;
no es un fallo introducido por este paquete.

### Hermes real

Cada profile fue instalado en un home nuevo dentro de:

```text
/opt/data/cache/att1-namespaced-profiles-probe-H3Q0xv
```

Para los tres homes se ejecutó `hermes config check` mediante la instalación de
auditoría de Hermes y se comparó el SHA-256 de `config.yaml` antes y después.
Resultado:

```text
hermes_v40_profiles_probe=OK
```

No hubo migración ni drift de configuración. Los recibos se releyeron y cada
archivo instalado coincidió con su hash declarado.

### HTTP físico

Se levantaron tres gateways Hermes separados, sólo en loopback y con credenciales
sintéticas efímeras no registradas. Para cada API:

```text
GET /health                 -> 200
GET /v1/models autenticado  -> 200
GET /v1/models sin auth     -> 401
```

Puertos efímeros usados: `18645`, `18646` y `18647`. Los tres procesos fueron
detenidos después del probe.

### Carrera física de publicación

Se lanzaron 16 procesos del instalador en paralelo contra el mismo home vacío.
La publicación create-only produjo exactamente un ganador. Doce procesos vieron
el destino antes de crear staging; tres perdieron la carrera después de crearlo y
retuvieron sus directorios privados para reconciliación manual. Ningún proceso
reemplazó el destino ni borró estado concurrente:

```text
concurrent_installers=16 successful_publishers=1 retained_private_stagings=3
probe=/opt/data/cache/att1-installer-retain-race-probe-tTfw15
```

## Límites de la evidencia

No se invocó un modelo ni se validó contenido comercial final. No se creó stack,
servicio, volumen, red, base PostgreSQL o dominio en EasyPanel. No hubo push,
merge, deploy, migración remota, proveedor externo, activación comercial ni
mensaje real.

La Conversation Release comercial permanece `draft_incomplete`; Automation
Expert permanece `not_released`; Client Copilot permanece candidato y no activo.
