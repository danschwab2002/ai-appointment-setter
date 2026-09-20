# Guardas de metadata de migraciones: por que serializan el trabajo y como dejar de hacerlo

- **Estado:** propuesta. No es una decision aceptada ni una implementacion.
- **Fecha:** 2026-09-19.
- **Origen:** el PR #161 esta bloqueado por esto desde el 2026-09-18, y hay cuatro migraciones en vuelo en tres tareas distintas que van a chocar entre si por la misma razon.
- **Decide:** Dan. La implementacion toca archivos reservados por el claim `daily-feedback-operational-context-v2-r3`, asi que no puede empezar hasta que ese claim los libere.

## 1. El problema

Agregar una migracion obliga a tocar dos archivos compartidos que ninguna tarea posee:

| Guarda | Archivo que hay que editar | Que exige |
|---|---|---|
| `tests/test_supabase_schema_inventory.py::test_supabase_schema_inventory_covers_every_canonical_migration` | `scripts/supabase_schema_inventory.sql` | que la lista literal de nombres de migracion escrita dentro del SQL sea exactamente el contenido del directorio |
| `tests/test_supabase_release_readiness.py::test_bundle_covers_exact_pending_tail_and_is_deterministic` | el propio archivo de prueba | que el nombre de la ultima migracion coincida con un literal escrito en la asercion |
| `tests/sql/followup_engine/validate_acl_hardening.mjs` | `scripts/supabase_schema_inventory.sql` | que exista una huella para cada migracion; corta la cadena `npm test` en el validador 24 de 34 |

El resultado es que **dos tareas que agregan migraciones en paralelo no pueden existir**: la segunda encuentra los dos archivos reservados por la primera y su suite en rojo. No es una hipotesis. Hoy conviven cuatro migraciones sin integrar en tres tareas (`20260908000200` en la de personalizacion por primer nombre, `20260915000100` y `20260917000200` en la de daily feedback, `20260918000100` en la de correlaciones), y las tres necesitan los mismos dos archivos.

Verificado el 2026-09-19 corriendo la suite completa sobre la rama del PR #161: fallan exactamente esas dos pruebas, con estos mensajes.

```text
test_supabase_schema_inventory ... Right contains one more item:
    '20260918000100_operator_correlation_prepared_at.sql'
test_supabase_release_readiness ... assert '20260918000100_...' == '20260917000100_...'
```

La suite SQL se detiene por la misma causa, en el validador 24 de 34:

```text
validate_acl_hardening.mjs ... Error: schema fingerprint inventory incomplete:
    {"missingFingerprints":["20260918000100_operator_correlation_prepared_at.sql"]}
```

Ninguna otra prueba falla por el contenido del PR. La regresion SQL propia de esa tarea pasa entera (5 casos) y `validate-tree` reporta 74 migraciones sin duplicados. Son tres chequeos y **dos archivos**, los dos reservados por la misma tarea ajena.

## 2. Que garantiza de verdad cada guarda

**El inventario** existe para que la consulta de huella del esquema cubra todas las migraciones canonicas: si alguien agrega una migracion y no la documenta, la huella deja de ser completa sin avisar. La garantia es legitima y hay que conservarla.

**La asercion del ultimo nombre** es distinta. La prueba ya deriva la lista esperada del directorio dos lineas antes:

```python
expected = [
    path.name
    for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
    if path.name.split("_", 1)[0] > MODULE.PREFIX_LAST
]
observed = [row["filename"] for row in first["pending_tail"]]
assert observed == expected
```

Despues de esa comparacion, las cuatro aserciones literales que siguen son centinelas contra una derivacion rota. Tres de ellas (el primer elemento y dos pertenencias) nombran migraciones antiguas y no caducan nunca. La cuarta, `observed[-1] == "<ultima migracion>"`, **caduca con cada migracion nueva** y no agrega cobertura: cualquier error que ella detectaria ya lo detecta `observed == expected`.

## 3. Propuesta

**A. Borrar la asercion que caduca.** Una linea menos en `tests/test_supabase_release_readiness.py`. Elimina la mitad del conflicto sin perder ninguna garantia. Si se quiere conservar un centinela de cola, la version que no caduca es comparar contra el ultimo nombre del directorio, que es de donde sale `expected`.

**B. Generar el inventario en vez de mantenerlo.** La lista de migraciones dentro de `scripts/supabase_schema_inventory.sql` es una copia del directorio escrita a mano. Un generador que la emita y una prueba que compare el archivo commiteado con el generado convierten el conflicto de edicion en un regenerado de un comando: dos tareas paralelas siguen tocando el archivo, pero la resolucion es determinista y no depende de que ninguna se acuerde de nada. Es el patron de archivo dorado regenerable.

Una variante mas barata, si se prefiere no agregar un generador: que el SQL reciba la lista como parametro y que la prueba la construya desde el directorio. El archivo deja de enumerar y el conflicto desaparece del todo.

**C. Mientras tanto, el orden de merge resuelve el caso puntual.** El PR #161 necesita exactamente dos ediciones, y las dos son mecanicas:

```text
scripts/supabase_schema_inventory.sql
  + agregar '20260918000100_operator_correlation_prepared_at.sql' al final de la lista
    (con su huella, que es lo que pide validate_acl_hardening.mjs)

tests/test_supabase_release_readiness.py
  - assert observed[-1] == "20260917000100_operator_correlation_abstention_reason.sql"
  + assert observed[-1] == "20260918000100_operator_correlation_prepared_at.sql"
```

Quien entre primero las aplica; quien entre despues repite el mismo movimiento con su propia migracion. Eso desbloquea el PR sin cambiar nada de fondo, y deja el problema intacto para la proxima vez.

## 4. Lo que hay que decidir

1. Si se hace A (una linea, sin riesgo) y cuando.
2. Si el inventario se genera (B) o se sigue manteniendo a mano.
3. Quien aplica las dos ediciones de C, dado que los archivos estan reservados por el claim `daily-feedback-operational-context-v2-r3`, en `implementing` y con worktree sucio desde el 2026-09-09. La frontera la decide Dan.

## 5. Alcance

Afecta solo a pruebas y a un archivo de inventario de solo lectura. Ninguna de las tres opciones cambia el comportamiento del producto, ni el esquema, ni el contenido de ninguna migracion.
