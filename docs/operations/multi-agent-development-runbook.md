# Runbook de desarrollo multiagente

- **Estado:** Implementado localmente; branch protection remota pendiente
- **Fuente de diseño:** [Flujo de desarrollo multiagente](../design/multi-agent-development-workflow.md)
- **Herramienta:** `scripts/agent_workspace.py`

## Mapa simple

- **Claim:** reserva operativa de una tarea, sus paths y recursos semánticos.
- **Worktree:** checkout aislado donde trabaja un solo agente.
- **Rama:** historial publicable de esa tarea.
- **Integrador:** único responsable de incorporar PRs a `main` en serie.
- **Registro:** metadatos compartidos bajo el Git common dir de este clon.

## Regla de entrada

Un agente no debe editar hasta que este comando termine con código cero desde su
worktree:

```text
uv run python scripts/agent_workspace.py preflight
```

`main`, un worktree sin claim, un scope solapado o una versión de migración
repetida fallan cerrados.

## Crear una tarea

Desde cualquier worktree del clon:

```text
uv run python scripts/agent_workspace.py start TASK_ID \
  --title "Descripción corta" \
  --owner "session-or-agent-id" \
  --branch "feat/TASK_ID" \
  --worktree "/ruta/absoluta/al/worktree" \
  --base origin/main \
  --path src/area \
  --resource contract:domain-name
```

Usar varios `--path` y `--resource` cuando corresponda. Los paths son relativos
al repositorio. Un recurso representa una frontera semántica aunque todavía no
se conozcan todos los archivos: contrato, migración, proveedor o subsistema.
Todo claim debe declarar al menos un path o recurso; un claim vacío se rechaza
porque no podría prevenir superposición semántica.

El comando es atómico bajo un lock del registro. Si falla al persistir el claim,
revierte el worktree y la rama sólo si siguen limpios e idénticos a la base. Si
apareció trabajo concurrente, los conserva y reporta la ruta para recuperación.

La base debe ser una rama protegida remota (`origin/main` o equivalente
`.../master`). La rama nueva se crea sin heredar el upstream de `main`; sólo
queda publicable después de `git push -u` hacia su propia rama.

Si durante la implementación aparece un archivo o recurso nuevo, extender el
claim antes de editarlo. La extensión es atómica y rechaza solapamientos:

```text
uv run python scripts/agent_workspace.py extend TASK_ID \
  --path docs/nuevo-archivo.md \
  --resource contract:nuevo-recurso
```

Un claim en `review` queda congelado: para extender scope o generar otro commit,
debe volver explícitamente a `implementing` y pasar nuevamente por revisión.

## Adoptar trabajo existente

Si ya existe un worktree:

```text
uv run python scripts/agent_workspace.py adopt TASK_ID \
  --title "Trabajo existente" \
  --owner "session-id" \
  --worktree "/ruta/al/worktree" \
  --base origin/main \
  --state paused \
  --resource contract:domain-name
```

Usar `paused` para preservar un handoff detenido. Un claim pausado conserva sus
reservas: otro trabajo solapado se rechaza hasta que el integrador lo consolide
y lo mueva a un estado terminal. No copiar archivos y no usar `stash` como
coordinación.

## Ciclo del agente

1. Mover el claim nuevo de `claimed` a `implementing`.
2. Ejecutar `preflight` antes de la primera edición y antes de cada commit.
3. Modificar sólo el scope declarado.
4. Ejecutar pruebas relevantes y la suite canónica.
5. Commitear en la rama de la tarea.
6. Publicar la rama y comprobar sincronización con upstream.
7. Mover el claim a review:

```text
uv run python scripts/agent_workspace.py transition TASK_ID implementing
uv run python scripts/agent_workspace.py transition TASK_ID review
```

La transición a `review` exige worktree limpio, upstream configurado y cero
divergencia local/remota, y consulta el remoto para fijar el commit realmente
publicado. También vuelve a ejecutar el preflight sobre cambios commiteados: no
puede usarse para omitir scope, solapamientos o migraciones.

## Estado global

```text
uv run python scripts/agent_workspace.py status
```

Clasifica worktrees como:

- `protected`: `main`/`master`;
- `claimed`: administrado por el registro;
- `unmanaged`: bloqueado para trabajo hasta adopción.

## Integración

El integrador procesa una rama por vez:

1. revisar claim, diff completo, archivos no trackeados y resultados;
2. actualizar la rama contra el `origin/main` vigente;
3. resolver conflictos semánticos, no sólo marcadores de Git;
4. ejecutar suite combinada;
5. mergear mediante PR;
6. marcar `merged`; la transición consulta y descarga la rama del remoto,
   verifica que el commit exacto fijado al entrar en `review` siga siendo el
   `HEAD` de la tarea y ya esté contenido en la referencia protegida remota;
7. limpiar el worktree sólo después de verificar el commit remoto:

```text
uv run python scripts/agent_workspace.py cleanup TASK_ID
```

`cleanup` sólo acepta claims `merged` o `abandoned`, exige un worktree limpio y
verifica que path, clon y rama sigan coincidiendo antes de removerlo sin
`--force`. Conserva la rama local para auditoría; su borrado es una operación
posterior del integrador.

Los hooks permiten commit/push a rama protegida únicamente cuando el integrador
exporta conscientemente `HERMES_INTEGRATOR=1`. La vía preferida sigue siendo PR
con branch protection remota.

## Activar hooks en este clon

Después de integrar los archivos versionados:

```text
uv run python scripts/agent_workspace.py install-hooks
```

Verificación:

```text
git config --get core.hooksPath
```

Debe devolver `.githooks`.

## Recuperación

### Trabajo accidental sobre main

1. detener al agente;
2. crear una rama `wip/...` en el mismo checkout, sin limpiar ni stashear;
3. adoptar ese worktree como `paused` o `implementing`;
4. crear un worktree limpio separado para integración;
5. comparar la rama WIP con tareas activas antes de reanudar.

### Claim abandonado

Moverlo a `abandoned`; comprobar que el worktree esté limpio y ejecutar
`cleanup`. Los estados terminales no pueden reabrirse y la rama queda conservada.

## Límites y protección remota

Los hooks sólo protegen este clon y pueden omitirse desde otro equipo. Configurar
en GitHub branch protection para `main` con push directo bloqueado, PR y tests
requeridos. El workflow `.github/workflows/ci.yml` ejecuta la suite Python, la
suite SQL y `validate-tree`; este último rechaza versiones duplicadas de
migraciones dentro del árbol propuesto. Branch protection debe marcar ese job
como requerido.

Los worktrees no son sandbox de filesystem; agentes no confiables requieren
contenedor o clon aislado.
