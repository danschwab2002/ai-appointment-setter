# Runbook — release Supabase del primer infoproductor

- **Estado:** preparado; ejecución productiva bloqueada
- **Alcance:** reconciliación, bundle, postflight y rollback forward-only
- **No autoriza:** `migration repair`, `db push`, DDL remoto ni activación

## 1. Gates obligatorios

El release sólo puede avanzar si todos son verdaderos:

1. commit aprobado y limpio, idéntico a la rama remota;
2. PostgreSQL remoto y disposable en major `17`;
3. manifest exhaustivo del prefijo remoto igual al canonical prefix hasta
   `20260808000300`;
4. revisión independiente de toda diferencia de plataforma;
5. tracking reparado únicamente con autorización productiva separada;
6. `db push --dry-run` lista exactamente la cola del manifest;
7. el CLI fijado pasó un fallo inyectado en un proyecto disposable Supabase;
8. postflight estructural y ACL sin violaciones;
9. bridge receiver-only y todos los efectos apagados.

Cualquier resultado ausente, parcial o ambiguo es `blocked`.

## 2. Freeze y bundle

Usar Supabase CLI `2.113.0`. No poner tokens, URLs o passwords en argumentos ni
logs. Desde un checkout limpio del commit aprobado:

```bash
mkdir -p data/supabase-release
uv run python scripts/prepare_supabase_release_bundle.py \
  --output data/supabase-release
sha256sum data/supabase-release/{manifest.json,pending-tail.sql,postflight.sql}
```

El generador falla si el checkout está dirty, hay versiones duplicadas, falta el
boundary histórico o falta un probe. `manifest.json` fija commit, hashes de las
migraciones, prefix y cola. `production_authorized` permanece `false`.

La cola esperada se calcula, no se copia manualmente. En este corte comienza en
`20260808000400` y termina en el hardening ACL posterior al handoff.

## 3. Equivalencia exacta del prefijo

Seguir [`lancemos-supabase-schema-contract-runbook.md`](lancemos-supabase-schema-contract-runbook.md).
El expected usa baseline más `20260803000100..20260808000300`, PostgreSQL 17,
roles Supabase y default grants creados antes del baseline. El observed remoto se
genera sólo desde catálogos.

```bash
uv run python scripts/compare_supabase_schema_contract.py \
  data/schema-contract/expected-prefix.json \
  data/schema-contract/observed-remote.json
```

Sólo `exact_match` permite pedir revisión. Fingerprints presentes no reemplazan
este gate. No editar manifests ni usar un ignore para forzar igualdad.

## 4. Repair de tracking — mutación productiva separada

Sólo con equivalencia, revisión y autorización explícita:

```bash
npx --yes supabase@2.113.0 migration repair --linked --status applied \
  20260803000100 20260804000100 20260804000200 \
  20260805000100 20260805000200 20260805000300 \
  20260808000100 20260808000200 20260808000300
npx --yes supabase@2.113.0 migration list --linked
```

El resultado debe contener exactamente esas nueve versiones. Repair modifica
tracking, no debe usarse para aplicar o revertir objetos.

## 5. Dry-run y disposable

```bash
npx --yes supabase@2.113.0 db push --linked --dry-run
```

Comparar el listado en orden contra `manifest.json.pending_tail`. Cualquier
migración histórica, ausente, adicional o reordenada bloquea.

Antes de producción, usar otro proyecto Supabase disposable y el mismo CLI:

1. aplicar el prefix exacto y reparar su tracking;
2. agregar entre dos migraciones de prueba una migración que haga `raise exception`;
3. ejecutar `db push`;
4. exigir exit no-cero;
5. comprobar por `migration list` y catálogo que la migración fallida y todas las
   posteriores no quedaron registradas ni aplicadas;
6. borrar el proyecto disposable por su procedimiento normal.

El daemon Docker local no estuvo disponible durante la preparación. PGlite probó
sintaxis/ACL, pero no cuenta como evidencia de repair ni del failure mode del CLI.

## 6. Aplicación productiva — requiere nueva autorización

Con ingreso cerrado, runtime durable pausado y outbound/workers apagados:

```bash
npx --yes supabase@2.113.0 db push --linked
npx --yes supabase@2.113.0 migration list --linked
```

No ejecutar parcialmente `pending-tail.sql` como alternativa improvisada. El SQL
concatenado es un artefacto auditable/checksummed; el mecanismo preferido es el CLI
con tracking reconciliado.

## 7. Postflight independiente

Ejecutar, mediante conexión read-only y `search_path=pg_catalog,public`:

```bash
psql -X -v ON_ERROR_STOP=1 -f scripts/supabase_schema_inventory.sql
psql -X -v ON_ERROR_STOP=1 -f scripts/supabase_acl_inventory.sql
```

Exigir:

- historial igual a las 17 migraciones congeladas;
- 17 fingerprints `fingerprint_present`, ninguno partial;
- manifest exhaustivo remoto igual al full stack canonical;
- ACL inventory: todas las filas `ok`;
- 27 entrypoints exactos para `service_role`;
- cero `EXECUTE` para `anon`/`authenticated`;
- cero helpers trigger-only ejecutables por `service_role`;
- objetos de opt-out, pilot scope y handoff presentes;
- advisors de seguridad/performance rerun y clasificados;
- runtime aún inactivo.

La existencia de objetos prueba estructura, no comportamiento ni E2E.

## 8. Rollback forward-only

Si falla DDL o postflight:

1. mantener ingreso cerrado;
2. pausar autoridad durable antes de quitar consumidores;
3. reconciliar sólo effects cuyo `request_started` ya ocurrió;
4. apagar outbound y workers en el orden válido;
5. preservar tablas, ledgers y tracking;
6. corregir con una migración nueva e inmutable;
7. repetir clean install, postflight y revisión.

Nunca borrar estado durable, reescribir una migración aplicada ni marcar una
versión como reverted para simular rollback.

## 9. Evidencia sanitizada

Registrar sólo commit, hashes, versión CLI/PostgreSQL, conteos, status/reasons y
advisory links. No registrar project URL, IDs externos, tokens, connection strings,
filas, payloads ni cuerpos de funciones.
