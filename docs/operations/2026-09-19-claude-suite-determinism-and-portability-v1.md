# Determinismo de la suite SQL y portabilidad del instalador de client-copilot

- **Tipo:** evidencia operativa de una tarea de mantenimiento de la suite de pruebas.
- **Fecha:** 2026-09-19.
- **Tarea:** `claude-suite-determinism-v1`, rama `feat/claude-suite-determinism-v1`, base `origin/main` = `b04ad14`.
- **Alcance:** pruebas y un script de instalación de profile. **No** toca runtime del bridge, migraciones, configuración, despliegue ni datos.

## 1. Por qué existe esta tarea

Desde el 2026-09-19 la programación del repositorio se hace desde una Mac (`docs/current-state.md`, seccion 1). Al correr por primera vez las dos suites completas en ese entorno aparecieron cinco fallos que **no son defectos del producto**: tres de la suite Python y uno de la suite SQL que corta la cadena `npm test` (y con ella los 33 validadores siguientes).

Un rojo permanente que todos saben ignorar es peor que un chequeo que falta: esconde el rojo real del dia que aparezca. Esta tarea elimina cuatro de esos cinco y deja el quinto documentado con su causa y su dueño.

## 2. Medicion previa al cambio (base `b04ad14`)

### Suite Python

| Entorno | Resultado |
|---|---|
| macOS 15 (Darwin 25.5), Python 3.14.4, `uv run pytest` | **3 failed, 2126 passed** en 45 s |
| Linux (contenedor `infra_hermes`, Python del proyecto), mismos tres archivos de prueba | **22 passed** en 35 s |

Los tres fallos de macOS son el mismo mecanismo: `renameat2` es una llamada de sistema de Linux y en Darwin el simbolo no existe.

- `tests/test_client_copilot_profile_package.py::test_profile_installer_uses_private_permissions_and_preserves_env` y `::test_profile_update_recovers_after_staging_copy_failure`: `AttributeError: dlsym(RTLD_DEFAULT, renameat2): symbol not found`, en `scripts/install_client_copilot_profile.py::_exchange_directories`.
- `tests/test_att1_product_profiles.py::test_installer_creates_verified_private_home_without_overwrite`: `RuntimeError: atomic no-replace publication is unavailable`, en `scripts/install_att1_product_profiles.py`, que ya trataba la ausencia del simbolo como error explicito.

### Suite SQL (`tests/sql/followup_engine`, PGlite)

| Zona horaria del proceso | Resultado |
|---|---|
| `TZ=UTC` | **34 validadores en verde**, `npm test` sale 0 |
| `TZ=America/Argentina/Buenos_Aires` | `validate.mjs` falla con `defer did not select next business window` y corta la cadena |

Barrido individual de los 34 validadores con `TZ=America/Argentina/Buenos_Aires`: **33 OK, 1 falla**. El defecto esta acotado a un unico archivo.

## 3. Causa raiz del fallo de la suite SQL

La politica del caso, `cart-recovery-late-window`, declara `timezone` `UTC` y una ventana de negocio de 23:00 a 23:59. El motor (`reevaluate_followup_action`, migracion `20260803000100`) calcula el proximo inicio de ventana **en la timezone de la politica**:

```sql
((p_now at time zone v_policy.timezone)::date + day_offset)
  + (business_window ->> 'start')::time
) at time zone v_policy.timezone
```

La prueba calculaba la expectativa con `date_trunc('day', ...)` **sin** convertir, es decir en la timezone de la sesion de Postgres, que PGlite hereda del proceso Node. Con el proceso en UTC las dos formulas coinciden por casualidad; con cualquier otro desplazamiento dan instantes distintos y la comparacion falla.

El mismo problema afectaba al instante de entrada del caso: `deferAt` se fijaba como medianoche local mas 8 horas, asi que en `TZ=Asia/Tokyo` (UTC+9) caia *dentro* de la ventana 23:00-23:59 UTC y el caso dejaba de probar lo que dice probar.

## 4. Cambios aplicados

1. `tests/sql/followup_engine/validate.mjs`: las tres expresiones temporales del bloque de ventana de negocio (`deferAt`, la expectativa de `due_at` diferido y `noReplyAt`) se calculan ahora en la timezone de la politica (`at time zone 'UTC'`), no en la de la sesion. El invariante queda escrito al lado de la formula.
2. `scripts/install_client_copilot_profile.py`: `_exchange_directories` intenta `renameat2(RENAME_EXCHANGE)` y, si el simbolo no existe, usa `renamex_np(RENAME_SWAP)`, el equivalente de Darwin. Las dos son una unica operacion atomica, de modo que una interrupcion nunca deja el profile publicado a medias. Si no hay ninguna de las dos, falla cerrado con `atomic directory exchange is unavailable` en lugar de romper con `AttributeError`.
3. `tests/test_client_copilot_profile_package.py`: dos pruebas nuevas que fijan el contrato en vez de depender del camino indirecto del instalador. Una intercambia dos directorios reales en la plataforma que corre y verifica que los contenidos quedaron cruzados; la otra simula una libc sin ninguna de las dos llamadas y exige el error explicito, comprobando ademas que no se movio ningun archivo.

Ninguno de los tres archivos cambia el comportamiento en Linux: en Linux `renameat2` existe y se toma la misma rama de siempre.

## 5. Verificacion posterior al cambio

| Prueba | Resultado |
|---|---|
| `validate.mjs` con `TZ=UTC` / `TZ=America/Argentina/Buenos_Aires` / `TZ=Asia/Tokyo` | los tres en verde |
| `validate.mjs` **de la base** `origin/main` en las mismas tres zonas | UTC en verde, las otras dos en rojo (el arreglo se probo contra un rojo real) |
| `npm test` completo (34 validadores) sin fijar `TZ`, en la Mac | en verde |
| `tests/test_client_copilot_profile_package.py` en macOS | 14 passed (antes 10 passed, 2 failed) |
| Suite Python completa en macOS | **1 failed, 2130 passed** |
| Suite Python completa en Linux (contenedor `infra_hermes`, misma rama) | ver seccion 7 |
| `scripts/agent_workspace.py validate-tree` | 73 migraciones, 0 versiones duplicadas |

## 6. Lo que queda abierto, y por que

`tests/test_att1_product_profiles.py::test_installer_creates_verified_private_home_without_overwrite` **sigue en rojo en macOS**. Su causa es la misma (falta `renameat2`) y el arreglo seria simetrico: `renamex_np` con `RENAME_EXCL`, que es el equivalente de Darwin de `RENAME_NOREPLACE`.

No se aplico porque `scripts/install_att1_product_profiles.py` y `tests/test_att1_product_profiles.py` estan reservados por el claim `att1-product-hermes-runtime`, en estado `implementing` y con worktree sucio. Editarlos desde otra tarea viola el preflight y generaria un conflicto real con trabajo no publicado. Cuando ese claim se cierre o su dueno libere los dos archivos, el cambio es de unas quince lineas y ya tiene el molde en `install_client_copilot_profile.py`.

## 7. Limites de esta evidencia

- No acredita despliegue, aplicacion de DDL en Supabase Cloud, mensajes externos ni E2E productivo.
- No cambia ninguna funcion SQL ni ninguna migracion: el arreglo de la suite SQL es de la **expectativa** de la prueba, no del motor. El motor ya usaba la timezone de la politica y por eso es correcto en produccion.
- La prueba de `renamex_np` se ejecuto en Darwin arm64. En una libc sin `renameat2` ni `renamex_np` el instalador falla cerrado, que es el comportamiento buscado, pero ese caso no se probo en una plataforma real.
