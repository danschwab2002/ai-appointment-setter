# Runbook — comparación exhaustiva del contrato Supabase

- **Estado:** preparado y validado localmente; comparación remota no ejecutada
- **Alcance:** metadata del schema, sin filas de aplicación
- **Autoridad:** procedimiento operativo; no autoriza reparar tracking ni aplicar DDL

## 1. Objetivo

Demostrar si dos bases PostgreSQL tienen el mismo contrato observable para el
piloto antes de reparar `supabase_migrations.schema_migrations` o ejecutar
migraciones pendientes.

Artefactos:

- `scripts/supabase_schema_contract.sql`: genera un manifest JSON determinista y
  read-only;
- `scripts/compare_supabase_schema_contract.py`: compara dos manifests de manera
  exacta y fail-closed.

El probe cubre:

- relaciones, tipo de relación, owner, RLS y privilegios efectivos;
- columnas, posición, tipo, default, nullability, identidad, generación y ACL;
- constraints e índices con sus definiciones y estados;
- triggers vinculados a tabla, función y estado;
- funciones por firma, cuerpo, resultado, atributos, owner, `search_path`/config y
  `EXECUTE` efectivo para `anon`, `authenticated`, `service_role` y `PUBLIC`;
- policies RLS y roles;
- secuencias y `OWNED BY`, vistas/materialized views, tipos públicos incluidos
  composite/range, foreign tables, extensiones, default ACL, ACL del schema y
  major version de PostgreSQL.

No consulta tablas de negocio ni incluye teléfonos, emails, mensajes o payloads.

## 2. Códigos de salida

| Código | Significado |
|---:|---|
| `0` | match exacto |
| `1` | faltan/sobran objetos o cambió algún campo |
| `2` | input ausente, inválido o ambiguo |

Para objetos cambiados, la salida contiene sólo tipo de objeto, paths de campos
cerrados y hashes SHA-256. Las identidades, cuerpos y valores completos no se
repiten en logs.

## 3. Preflight obligatorio

1. congelar commit, lista ordenada y SHA-256 de migraciones;
2. volver a consultar migration history read-only;
3. confirmar el project ref no secreto contra el target autorizado;
4. exigir misma major version PostgreSQL en expected y observed;
5. mantener apagados:
   - `RESOLUTION_WORKER_ENABLED`;
   - `HOTMART_PURCHASE_WORKER_ENABLED`;
   - `DURABLE_DISPATCHER_ENABLED`;
   - `DURABLE_OUTBOUND_ENABLED`;
   - `CHATWOOT_DURABLE_OPT_OUT_ENABLED`;
   - `LANCEMOS_PILOT_BOUNDARY_ENABLED`;
6. guardar manifests sólo bajo `data/` o un path privado excluido de Git;
7. no imprimir connection strings, passwords o tokens.

Si D se integró después del freeze, abortar, actualizar desde `main` y recongelar.

## 4. Generar el expected canónico

Para decidir si pueden marcarse como aplicadas las nueve migraciones históricas,
el expected debe construirse en PostgreSQL de la misma major version usando
únicamente:

1. `supabase/baseline/20260803_public_schema.sql`;
2. `20260803000100..20260808000300`, en orden.

No usar el stack completo para probar equivalencia del prefix remoto. No introducir
fixtures ni datos comerciales.

Obtener primero el major remoto mediante una consulta read-only a
`current_setting('server_version_num')`. Luego crear el disposable exacto:

```bash
set -euo pipefail
mkdir -p data/schema-contract
PG_MAJOR='<major-remoto-aprobado>'
CONTAINER="lancemos-schema-prefix-${PG_MAJOR}"

docker run --name "$CONTAINER" --rm -d \
  -e POSTGRES_PASSWORD='local-schema-probe-only' \
  -v "$PWD:/repo:ro" \
  "postgres:${PG_MAJOR}"
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true' EXIT

for attempt in {1..60}; do
  docker exec "$CONTAINER" pg_isready -U postgres -d postgres >/dev/null && break
  docker inspect -f '{{.State.Running}}' "$CONTAINER" | grep -qx true
  [ "$attempt" -lt 60 ]
  sleep 1
done
docker exec "$CONTAINER" pg_isready -U postgres -d postgres >/dev/null

docker exec -i "$CONTAINER" psql -X -U postgres -d postgres \
  -v ON_ERROR_STOP=1 <<'SQL'
create role anon nologin;
create role authenticated nologin;
create role service_role nologin;
alter default privileges in schema public
    grant execute on functions to anon, authenticated;
alter default privileges in schema public
    grant all on tables to service_role;
SQL

files=(
  supabase/baseline/20260803_public_schema.sql
  supabase/migrations/20260803000100_followup_engine_v1.sql
  supabase/migrations/20260804000100_followup_engine_permissions_hotfix.sql
  supabase/migrations/20260804000200_followup_identity_binding.sql
  supabase/migrations/20260805000100_followup_identity_audit.sql
  supabase/migrations/20260805000200_followup_contact_authorization_grant.sql
  supabase/migrations/20260805000300_per_case_conversation_anchor.sql
  supabase/migrations/20260808000100_hotmart_purchase_approved.sql
  supabase/migrations/20260808000200_hotmart_purchase_ordering_guard.sql
  supabase/migrations/20260808000300_hotmart_purchase_ordering_guard_privileges.sql
)
for file in "${files[@]}"; do
  docker exec -i "$CONTAINER" psql -X -U postgres -d postgres \
    -v ON_ERROR_STOP=1 < "$file"
done

docker exec -i \
  -e PGOPTIONS='-c search_path=pg_catalog,public' \
  "$CONTAINER" psql -X -qAt -U postgres -d postgres \
  -v ON_ERROR_STOP=1 \
  -f /repo/scripts/supabase_schema_contract.sql \
  > data/schema-contract/expected-prefix.json
```

El loop aborta ante el primer error. Registrar imagen/major version, commit y
hashes, pero no credenciales. Si Docker o la imagen fijada no están disponibles,
el gate queda `blocked`; no sustituirlos por PGlite para autorizar repair.

## 5. Generar el observed remoto

Después de confirmar target y con credencial read-only:

```bash
PGOPTIONS='-c search_path=pg_catalog,public' \
psql -X -qAt -v ON_ERROR_STOP=1 \
  -f scripts/supabase_schema_contract.sql \
  > data/schema-contract/observed-remote.json
```

Usar `PGSERVICE`/variables estándar de libpq; nunca poner la URL o password en el
comando. No usar una ruta que no permita fijar exactamente el `search_path`.

El comparador valida automáticamente array no vacío, sentinels, schema cerrado,
miembros JSON duplicados y números no estándar. Un output parcial, warning mezclado
en stdout o timeout produce código `2`.

## 6. Comparar

```bash
uv run python scripts/compare_supabase_schema_contract.py \
  data/schema-contract/expected-prefix.json \
  data/schema-contract/observed-remote.json \
  > data/schema-contract/comparison.json
```

Interpretación fail-closed:

- `exact_match`: habilita una revisión independiente; **no** autoriza por sí solo
  `migration repair`;
- `different`: tracking repair y `db push` siguen bloqueados;
- error de input: resultado desconocido y `NO-GO`.

No hay flags para ignorar campos. Toda diferencia de plataforma debe clasificarse
fuera de la herramienta, documentarse campo por campo y someterse a nueva revisión.
No editar manifests para forzar un match.

## 7. Gates posteriores

Antes de cualquier reparación de tracking:

1. comparación exacta o clasificación revisada de todas las diferencias;
2. probes conductuales históricos aprobados;
3. `scripts/supabase_acl_inventory.sql` sin filas distintas de `ok` para el target
   final;
4. revisión adversarial sobre commit y manifests congelados;
5. autorización productiva específica.

D ya está integrado en `main` mediante `94ee3a44fa29641106a394bf593e26cb917846aa`.
También se requiere una migración forward-only posterior para cerrar los cinco
leaks trigger-only ya documentados e incluir las funciones nuevas de handoff.
Esta herramienta no crea ni aplica esa migración.

## 8. Rollback operativo

El probe no produce efectos remotos. Si falla:

1. descartar sólo los manifests privados incompletos;
2. mantener tracking, DDL, ingreso, workers y outbound sin cambios;
3. corregir target/tooling o generar nuevamente el disposable;
4. repetir desde el freeze.

Nunca borrar objetos o modificar migration history para conseguir igualdad.

## 9. Evidencia local del artefacto

Validación realizada sobre baseline + las 16 migraciones de `main` después de
integrar el runtime de handoff:

```text
manifest_rows=1223
self_comparison=exact_match
changed_contract_probe=exit_1_without_value_echo
```

Esta evidencia prueba sintaxis y comportamiento del comparador en PGlite. No
sustituye PostgreSQL de la misma major version ni una comparación remota real.
