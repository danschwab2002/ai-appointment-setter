# Flujo de desarrollo multiagente con worktrees

- **Estado:** Aceptada para implementación
- **Alcance:** coordinación de agentes de IA que desarrollan en este repositorio
- **No cubre:** aislamiento hostil de procesos ni autorización de despliegues productivos

## Problema

Dos agentes pueden usar ramas distintas y aun así interferirse si comparten el
mismo checkout, trabajan directamente sobre `main`, reutilizan una versión de
migración o implementan en paralelo el mismo contrato. Git worktrees aíslan los
archivos, pero no coordinan por sí solos el alcance semántico ni el orden de
integración.

## Decisión

El repositorio utilizará un flujo equivalente a un equipo de desarrollo con
pull requests:

1. `main` es una rama protegida de integración, no un espacio de desarrollo.
2. Cada tarea sustancial tiene un `task_id`, una rama y un worktree exclusivos.
3. Un registro compartido en el Git common dir mantiene claims visibles para
   todos los worktrees de este clon sin ensuciar ramas.
4. Cada claim declara paths y recursos semánticos. Los claims activos no pueden
   superponerse.
5. Un preflight obligatorio comprueba rama, worktree, claim, conflictos reales
   de archivos y versiones de migración antes de editar.
6. Hooks locales bloquean commits y pushes directos a `main` salvo una elevación
   explícita del integrador.
7. Cada agente entrega una rama limpia, probada, publicada y revisable.
8. Un solo integrador incorpora ramas de forma serial y reejecuta la suite
   combinada antes de desplegar.

## Capas de control

### Directriz versionada

`AGENTS.md` contiene las reglas breves y obligatorias. Enlaza al runbook en vez
de duplicarlo.

### Launcher y registro

`scripts/agent_workspace.py` será la interfaz canónica para:

- crear un claim y un worktree;
- adoptar de forma explícita un worktree existente;
- ejecutar preflight;
- consultar estado global;
- mover un claim a `review`, `merged`, `paused` o `abandoned`.

El registro vive bajo el Git common dir. No contiene secretos ni contenido de
la tarea, sólo metadatos operativos.

### Hooks

Los hooks versionados rechazan por defecto commits y pushes directos a ramas
protegidas. La variable `HERMES_INTEGRATOR=1` permite una operación de
integración consciente, pero no omite pruebas ni revisión.

### Skill global

Una skill de esta instancia instruye a futuros agentes a usar el launcher y el
flujo PR. La skill es una guía de procedimiento; las guardas ejecutables y la
protección de Git son las fronteras efectivas.

## Claim

Un claim contiene como mínimo:

```text
version
task_id
title
owner
branch
worktree
base_ref
base_sha
paths
resources
state
created_at
updated_at
```

Estados activos para exclusión: `claimed`, `implementing` y `review`. `paused`
conserva el handoff pero no bloquea un nuevo claim. `merged` y `abandoned` son
terminales.

## Reglas de solapamiento

Un nuevo claim falla cerrado cuando:

- usa una rama o worktree ya reclamados;
- declara el mismo recurso semántico que otro claim activo;
- un path es igual o ancestro/descendiente de otro path reclamado;
- los cambios reales de dos claims activos tocan el mismo archivo;
- dos claims introducen migraciones con el mismo prefijo de versión;
- `main` está sucio o un agente intenta usarlo para implementar.

Los paths son una ayuda de coordinación, no propiedad exclusiva perpetua. El
integrador puede resolver un solapamiento sólo pausando o cerrando uno de los
claims y dejando evidencia explícita.

## Flujo normal

```text
tarea
  → claim + worktree
  → preflight verde
  → implementación + pruebas
  → commit + push
  → estado review
  → revisión e integración serial
  → suite combinada
  → merge
  → estado merged
  → limpieza del worktree
```

## Recuperación de trabajo existente

Si un agente empezó sobre `main`, primero se crea una rama `wip/...` en ese
mismo checkout para preservar exactamente su índice y archivos sin commit. El
claim se registra como `paused` o `implementing` según corresponda. No se copian
archivos entre worktrees y no se usa `stash` como mecanismo de coordinación.

## Integración y despliegue

- Las ramas pueden publicarse automáticamente cuando quedan verificadas.
- El merge a `main` es serial y pertenece al integrador.
- La integración debe conservar el commit exacto fijado en `review` como
  ancestro de `main`; no usar squash ni rebase para una tarea coordinada.
- El despliegue referencia un commit limpio de `origin/main`.
- Una rama funcional no equivale a una capacidad desplegada.
- Una migración o despliegue remoto conserva sus autorizaciones independientes.

## Límites

- Un worktree no es un sandbox: un proceso con acceso al host puede escribir en
  directorios hermanos mediante rutas absolutas.
- El registro coordina este clon; GitHub Issues/PRs coordinan clones distintos.
- La protección local de hooks debe complementarse con branch protection en
  GitHub para impedir bypass desde otros equipos o clones.
- Los conflictos semánticos requieren revisión humana o de un integrador; Git no
  puede detectarlos todos automáticamente.

## Criterios de aceptación

- crear dos tareas disjuntas produce dos worktrees y claims independientes;
- un recurso o path superpuesto es rechazado antes de editar;
- `main` sucio bloquea nuevas tareas;
- un agente no puede pasar preflight en `main` ni en un worktree sin claim;
- commits y pushes directos a `main` son rechazados localmente;
- una versión de migración duplicada entre tareas activas es detectada;
- un claim no puede pasar a `review` con worktree sucio o rama sin publicar;
- el trabajo previo del otro agente permanece intacto en su rama `wip/...`;
- una prueba E2E sobre un repositorio temporal demuestra creación, exclusión,
  entrega y limpieza sin tocar el repositorio real.
