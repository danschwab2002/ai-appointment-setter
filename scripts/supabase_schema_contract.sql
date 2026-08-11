-- Exhaustive, metadata-only schema contract for Supabase reconciliation.
-- Returns one deterministic row per catalog object. It never reads application rows.

with
roles(role_name) as (
    values ('anon'::text), ('authenticated'::text), ('service_role'::text)
),
manifest_metadata as (
    select
        'manifest_metadata'::text as object_type,
        'supabase_schema_contract/v1'::text as identity,
        jsonb_build_object(
            'format_version', 1,
            'query_kind', 'metadata_only',
            'scope', 'public'
        ) as contract
),
relation_contracts as (
    select
        'relation'::text as object_type,
        format('%I.%I', n.nspname, c.relname) as identity,
        jsonb_build_object(
            'kind', c.relkind,
            'persistence', c.relpersistence,
            'owner', pg_get_userbyid(c.relowner),
            'row_security', c.relrowsecurity,
            'force_row_security', c.relforcerowsecurity,
            'replica_identity', c.relreplident,
            'access_method', access_method.amname,
            'tablespace', tablespace.spcname,
            'populated', c.relispopulated,
            'partition_key', pg_get_partkeydef(c.oid),
            'partition_bound', pg_get_expr(c.relpartbound, c.oid),
            'parents', (
                select coalesce(
                    jsonb_agg(
                        format('%I.%I', parent_namespace.nspname, parent.relname)
                        order by parent_namespace.nspname, parent.relname
                    ),
                    '[]'::jsonb
                )
                from pg_inherits inheritance
                join pg_class parent on parent.oid = inheritance.inhparent
                join pg_namespace parent_namespace on parent_namespace.oid = parent.relnamespace
                where inheritance.inhrelid = c.oid
            ),
            'options', coalesce(to_jsonb(c.reloptions), '[]'::jsonb),
            'acl', (
                select coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'grantor', pg_get_userbyid(acl.grantor),
                            'grantee', case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            'privilege', acl.privilege_type,
                            'grantable', acl.is_grantable
                        )
                        order by
                            case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            pg_get_userbyid(acl.grantor),
                            acl.privilege_type,
                            acl.is_grantable
                    ),
                    '[]'::jsonb
                )
                from aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) acl
            ),
            'role_privileges', (
                select jsonb_object_agg(
                    r.role_name,
                    jsonb_build_object(
                        'select', has_table_privilege(r.role_name, c.oid, 'select'),
                        'insert', has_table_privilege(r.role_name, c.oid, 'insert'),
                        'update', has_table_privilege(r.role_name, c.oid, 'update'),
                        'delete', has_table_privilege(r.role_name, c.oid, 'delete'),
                        'truncate', has_table_privilege(r.role_name, c.oid, 'truncate'),
                        'references', has_table_privilege(r.role_name, c.oid, 'references'),
                        'trigger', has_table_privilege(r.role_name, c.oid, 'trigger')
                    )
                    order by r.role_name
                )
                from roles r
            )
        ) as contract
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    left join pg_am access_method on access_method.oid = c.relam
    left join pg_tablespace tablespace on tablespace.oid = c.reltablespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p', 'v', 'm', 'f')
),
column_contracts as (
    select
        'column'::text as object_type,
        format('%I.%I.%I', n.nspname, c.relname, a.attname) as identity,
        jsonb_build_object(
            'position', a.attnum,
            'type', format_type(a.atttypid, a.atttypmod),
            'not_null', a.attnotnull,
            'identity', a.attidentity,
            'generated', a.attgenerated,
            'default', pg_get_expr(d.adbin, d.adrelid),
            'collation', case
                when a.attcollation = 0 then null
                else a.attcollation::regcollation::text
            end,
            'compression', a.attcompression,
            'storage', a.attstorage,
            'acl', (
                select coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'grantor', pg_get_userbyid(acl.grantor),
                            'grantee', case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            'privilege', acl.privilege_type,
                            'grantable', acl.is_grantable
                        )
                        order by
                            case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            pg_get_userbyid(acl.grantor),
                            acl.privilege_type,
                            acl.is_grantable
                    ),
                    '[]'::jsonb
                )
                from aclexplode(coalesce(a.attacl, acldefault('c', c.relowner))) acl
            ),
            'role_privileges', (
                select jsonb_object_agg(
                    r.role_name,
                    jsonb_build_object(
                        'select', has_column_privilege(r.role_name, c.oid, a.attnum, 'select'),
                        'insert', has_column_privilege(r.role_name, c.oid, a.attnum, 'insert'),
                        'update', has_column_privilege(r.role_name, c.oid, a.attnum, 'update'),
                        'references', has_column_privilege(r.role_name, c.oid, a.attnum, 'references')
                    )
                    order by r.role_name
                )
                from roles r
            )
        ) as contract
    from pg_attribute a
    join pg_class c on c.oid = a.attrelid
    join pg_namespace n on n.oid = c.relnamespace
    left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
    where n.nspname = 'public'
      and c.relkind in ('r', 'p', 'v', 'm', 'f')
      and a.attnum > 0
      and not a.attisdropped
),
constraint_contracts as (
    select
        'constraint'::text as object_type,
        format('%I.%I.%I', n.nspname, c.relname, con.conname) as identity,
        jsonb_build_object(
            'type', con.contype,
            'definition', pg_get_constraintdef(con.oid, false),
            'deferrable', con.condeferrable,
            'initially_deferred', con.condeferred,
            'validated', con.convalidated,
            'no_inherit', con.connoinherit
        ) as contract
    from pg_constraint con
    join pg_class c on c.oid = con.conrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
),
index_contracts as (
    select
        'index'::text as object_type,
        format('%I.%I.%I', n.nspname, table_class.relname, index_class.relname) as identity,
        jsonb_build_object(
            'definition', pg_get_indexdef(index_class.oid),
            'unique', idx.indisunique,
            'primary', idx.indisprimary,
            'exclusion', idx.indisexclusion,
            'immediate', idx.indimmediate,
            'valid', idx.indisvalid,
            'ready', idx.indisready,
            'live', idx.indislive,
            'replica_identity', idx.indisreplident,
            'clustered', idx.indisclustered,
            'access_method', access_method.amname,
            'tablespace', tablespace.spcname
        ) as contract
    from pg_index idx
    join pg_class index_class on index_class.oid = idx.indexrelid
    join pg_class table_class on table_class.oid = idx.indrelid
    join pg_namespace n on n.oid = table_class.relnamespace
    join pg_am access_method on access_method.oid = index_class.relam
    left join pg_tablespace tablespace on tablespace.oid = index_class.reltablespace
    where n.nspname = 'public'
),
trigger_contracts as (
    select
        'trigger'::text as object_type,
        format('%I.%I.%I', n.nspname, c.relname, t.tgname) as identity,
        jsonb_build_object(
            'enabled', t.tgenabled,
            'definition', pg_get_triggerdef(t.oid, false),
            'function', format(
                '%I.%I(%s)',
                function_namespace.nspname,
                p.proname,
                oidvectortypes(p.proargtypes)
            )
        ) as contract
    from pg_trigger t
    join pg_class c on c.oid = t.tgrelid
    join pg_namespace n on n.oid = c.relnamespace
    join pg_proc p on p.oid = t.tgfoid
    join pg_namespace function_namespace on function_namespace.oid = p.pronamespace
    where n.nspname = 'public'
      and not t.tgisinternal
),
function_contracts as (
    select
        'function'::text as object_type,
        format('%I.%I(%s)', n.nspname, p.proname, oidvectortypes(p.proargtypes)) as identity,
        jsonb_build_object(
            'owner', pg_get_userbyid(p.proowner),
            'language', language.lanname,
            'kind', p.prokind,
            'arguments', pg_get_function_arguments(p.oid),
            'identity_arguments', pg_get_function_identity_arguments(p.oid),
            'result', pg_get_function_result(p.oid),
            'security_definer', p.prosecdef,
            'leakproof', p.proleakproof,
            'strict', p.proisstrict,
            'volatility', p.provolatile,
            'parallel', p.proparallel,
            'estimated_cost', p.procost,
            'estimated_rows', p.prorows,
            'config', (
                select coalesce(jsonb_agg(setting order by setting), '[]'::jsonb)
                from unnest(p.proconfig) setting
            ),
            'source', p.prosrc,
            'binary', p.probin,
            'definition', pg_get_functiondef(p.oid),
            'sql_body', p.prosqlbody::text,
            'support', case
                when support_proc.oid is null then null
                else format(
                    '%I.%I(%s)',
                    support_namespace.nspname,
                    support_proc.proname,
                    oidvectortypes(support_proc.proargtypes)
                )
            end,
            'role_execute', (
                select jsonb_object_agg(
                    r.role_name,
                    has_function_privilege(r.role_name, p.oid, 'execute')
                    order by r.role_name
                )
                from roles r
            ),
            'public_execute', exists (
                select 1
                from aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl
                where acl.grantee = 0
                  and acl.privilege_type = 'EXECUTE'
            ),
            'acl', (
                select coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'grantor', pg_get_userbyid(acl.grantor),
                            'grantee', case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            'privilege', acl.privilege_type,
                            'grantable', acl.is_grantable
                        )
                        order by
                            case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            pg_get_userbyid(acl.grantor),
                            acl.privilege_type,
                            acl.is_grantable
                    ),
                    '[]'::jsonb
                )
                from aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl
            )
        ) as contract
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    join pg_language language on language.oid = p.prolang
    left join pg_proc support_proc on support_proc.oid = p.prosupport
    left join pg_namespace support_namespace on support_namespace.oid = support_proc.pronamespace
    where n.nspname = 'public'
),
policy_contracts as (
    select
        'policy'::text as object_type,
        format('%I.%I.%I', n.nspname, c.relname, policy.polname) as identity,
        jsonb_build_object(
            'command', policy.polcmd,
            'permissive', policy.polpermissive,
            'roles', (
                select coalesce(jsonb_agg(role_name order by role_name), '[]'::jsonb)
                from (
                    select case
                        when role_oid = 0 then 'PUBLIC'
                        else pg_get_userbyid(role_oid)
                    end as role_name
                    from unnest(policy.polroles) role_oid
                ) policy_roles
            ),
            'using', pg_get_expr(policy.polqual, policy.polrelid),
            'with_check', pg_get_expr(policy.polwithcheck, policy.polrelid)
        ) as contract
    from pg_policy policy
    join pg_class c on c.oid = policy.polrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
),
sequence_contracts as (
    select
        'sequence'::text as object_type,
        format('%I.%I', n.nspname, c.relname) as identity,
        jsonb_build_object(
            'owner', pg_get_userbyid(c.relowner),
            'persistence', c.relpersistence,
            'data_type', format_type(sequence.seqtypid, null),
            'start', sequence.seqstart,
            'increment', sequence.seqincrement,
            'minimum', sequence.seqmin,
            'maximum', sequence.seqmax,
            'cache', sequence.seqcache,
            'cycle', sequence.seqcycle,
            'owned_by', (
                select coalesce(
                    jsonb_agg(
                        format('%I.%I.%I', owner_namespace.nspname, owner_table.relname, owner_column.attname)
                        order by owner_namespace.nspname, owner_table.relname, owner_column.attname
                    ),
                    '[]'::jsonb
                )
                from pg_depend dependency
                join pg_class owner_table on owner_table.oid = dependency.refobjid
                join pg_namespace owner_namespace on owner_namespace.oid = owner_table.relnamespace
                join pg_attribute owner_column
                  on owner_column.attrelid = dependency.refobjid
                 and owner_column.attnum = dependency.refobjsubid
                where dependency.classid = 'pg_class'::regclass
                  and dependency.objid = c.oid
                  and dependency.objsubid = 0
                  and dependency.refclassid = 'pg_class'::regclass
                  and dependency.refobjsubid > 0
                  and dependency.deptype in ('a', 'i')
            ),
            'acl', (
                select coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'grantor', pg_get_userbyid(acl.grantor),
                            'grantee', case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            'privilege', acl.privilege_type,
                            'grantable', acl.is_grantable
                        )
                        order by
                            case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            pg_get_userbyid(acl.grantor),
                            acl.privilege_type,
                            acl.is_grantable
                    ),
                    '[]'::jsonb
                )
                from aclexplode(coalesce(c.relacl, acldefault('s', c.relowner))) acl
            ),
            'role_privileges', (
                select jsonb_object_agg(
                    r.role_name,
                    jsonb_build_object(
                        'usage', has_sequence_privilege(r.role_name, c.oid, 'usage'),
                        'select', has_sequence_privilege(r.role_name, c.oid, 'select'),
                        'update', has_sequence_privilege(r.role_name, c.oid, 'update')
                    )
                    order by r.role_name
                )
                from roles r
            )
        ) as contract
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    join pg_sequence sequence on sequence.seqrelid = c.oid
    where n.nspname = 'public'
      and c.relkind = 'S'
),
view_contracts as (
    select
        'view_definition'::text as object_type,
        format('%I.%I', n.nspname, c.relname) as identity,
        jsonb_build_object('definition', pg_get_viewdef(c.oid, false)) as contract
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('v', 'm')
),
type_contracts as (
    select
        'type'::text as object_type,
        format('%I.%I', n.nspname, t.typname) as identity,
        jsonb_build_object(
            'kind', t.typtype,
            'category', t.typcategory,
            'owner', pg_get_userbyid(t.typowner),
            'not_null', t.typnotnull,
            'default', t.typdefault,
            'base_type', case when t.typbasetype = 0 then null else t.typbasetype::regtype::text end,
            'enum_labels', (
                select coalesce(jsonb_agg(e.enumlabel order by e.enumsortorder), '[]'::jsonb)
                from pg_enum e
                where e.enumtypid = t.oid
            ),
            'acl', (
                select coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'grantor', pg_get_userbyid(acl.grantor),
                            'grantee', case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            'privilege', acl.privilege_type,
                            'grantable', acl.is_grantable
                        )
                        order by
                            case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            pg_get_userbyid(acl.grantor),
                            acl.privilege_type,
                            acl.is_grantable
                    ),
                    '[]'::jsonb
                )
                from aclexplode(coalesce(t.typacl, acldefault('T', t.typowner))) acl
            ),
            'role_privileges', (
                select jsonb_object_agg(
                    r.role_name,
                    has_type_privilege(r.role_name, t.oid, 'usage')
                    order by r.role_name
                )
                from roles r
            ),
            'range_subtype', case when subtype.oid is null then null else format('%I.%I', subtype_namespace.nspname, subtype.typname) end,
            'range_multirange', case when multirange.oid is null then null else format('%I.%I', multirange_namespace.nspname, multirange.typname) end,
            'range_opclass', case when range_opclass.oid is null then null else format('%I.%I', opclass_namespace.nspname, range_opclass.opcname) end,
            'range_collation', case when range_collation.oid is null then null else format('%I.%I', collation_namespace.nspname, range_collation.collname) end,
            'range_canonical', case when canonical_proc.oid is null then null else format('%I.%I(%s)', canonical_namespace.nspname, canonical_proc.proname, oidvectortypes(canonical_proc.proargtypes)) end,
            'range_subdiff', case when subdiff_proc.oid is null then null else format('%I.%I(%s)', subdiff_namespace.nspname, subdiff_proc.proname, oidvectortypes(subdiff_proc.proargtypes)) end
        ) as contract
    from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    left join pg_range range_info on range_info.rngtypid = t.oid
    left join pg_type subtype on subtype.oid = range_info.rngsubtype
    left join pg_namespace subtype_namespace on subtype_namespace.oid = subtype.typnamespace
    left join pg_type multirange on multirange.oid = range_info.rngmultitypid
    left join pg_namespace multirange_namespace on multirange_namespace.oid = multirange.typnamespace
    left join pg_opclass range_opclass on range_opclass.oid = range_info.rngsubopc
    left join pg_namespace opclass_namespace on opclass_namespace.oid = range_opclass.opcnamespace
    left join pg_collation range_collation on range_collation.oid = range_info.rngcollation
    left join pg_namespace collation_namespace on collation_namespace.oid = range_collation.collnamespace
    left join pg_proc canonical_proc on canonical_proc.oid = range_info.rngcanonical
    left join pg_namespace canonical_namespace on canonical_namespace.oid = canonical_proc.pronamespace
    left join pg_proc subdiff_proc on subdiff_proc.oid = range_info.rngsubdiff
    left join pg_namespace subdiff_namespace on subdiff_namespace.oid = subdiff_proc.pronamespace
    where n.nspname = 'public'
      and (
          t.typtype in ('d', 'e', 'r', 'm')
          or (
              t.typtype = 'c'
              and exists (
                  select 1 from pg_class composite_relation
                  where composite_relation.oid = t.typrelid
                    and composite_relation.relkind = 'c'
              )
          )
      )
),
composite_attribute_contracts as (
    select
        'composite_attribute'::text as object_type,
        format('%I.%I.%I', n.nspname, t.typname, a.attname) as identity,
        jsonb_build_object(
            'position', a.attnum,
            'type', format_type(a.atttypid, a.atttypmod),
            'not_null', a.attnotnull,
            'collation', case when a.attcollation = 0 then null else a.attcollation::regcollation::text end,
            'storage', a.attstorage
        ) as contract
    from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    join pg_class c on c.oid = t.typrelid and c.relkind = 'c'
    join pg_attribute a on a.attrelid = c.oid
    where n.nspname = 'public'
      and a.attnum > 0
      and not a.attisdropped
),
domain_constraint_contracts as (
    select
        'domain_constraint'::text as object_type,
        format('%I.%I.%I', n.nspname, t.typname, con.conname) as identity,
        jsonb_build_object(
            'definition', pg_get_constraintdef(con.oid, false),
            'validated', con.convalidated,
            'deferrable', con.condeferrable,
            'initially_deferred', con.condeferred
        ) as contract
    from pg_constraint con
    join pg_type t on t.oid = con.contypid
    join pg_namespace n on n.oid = t.typnamespace
    where n.nspname = 'public'
      and con.contypid <> 0
),
extension_contracts as (
    select
        'extension'::text as object_type,
        extension.extname as identity,
        jsonb_build_object(
            'version', extension.extversion,
            'schema', namespace.nspname
        ) as contract
    from pg_extension extension
    join pg_namespace namespace on namespace.oid = extension.extnamespace
),
foreign_table_contracts as (
    select
        'foreign_table'::text as object_type,
        format('%I.%I', n.nspname, c.relname) as identity,
        jsonb_build_object(
            'server', server.srvname,
            'table_options', (
                select coalesce(jsonb_agg(option order by option), '[]'::jsonb)
                from unnest(foreign_table.ftoptions) option
            ),
            'server_type', server.srvtype,
            'server_version', server.srvversion,
            'fdw', wrapper.fdwname
        ) as contract
    from pg_foreign_table foreign_table
    join pg_class c on c.oid = foreign_table.ftrelid
    join pg_namespace n on n.oid = c.relnamespace
    join pg_foreign_server server on server.oid = foreign_table.ftserver
    join pg_foreign_data_wrapper wrapper on wrapper.oid = server.srvfdw
    where n.nspname = 'public'
),
default_acl_contracts as (
    select
        'default_acl'::text as object_type,
        format(
            '%I.%s.%s',
            pg_get_userbyid(default_acl.defaclrole),
            coalesce(namespace.nspname, '<global>'),
            default_acl.defaclobjtype
        ) as identity,
        jsonb_build_object(
            'owner', pg_get_userbyid(default_acl.defaclrole),
            'schema', namespace.nspname,
            'object_kind', default_acl.defaclobjtype,
            'acl', (
                select coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'grantor', pg_get_userbyid(acl.grantor),
                            'grantee', case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            'privilege', acl.privilege_type,
                            'grantable', acl.is_grantable
                        )
                        order by
                            case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            pg_get_userbyid(acl.grantor),
                            acl.privilege_type,
                            acl.is_grantable
                    ),
                    '[]'::jsonb
                )
                from aclexplode(default_acl.defaclacl) acl
            )
        ) as contract
    from pg_default_acl default_acl
    left join pg_namespace namespace on namespace.oid = default_acl.defaclnamespace
    where namespace.nspname = 'public'
       or default_acl.defaclnamespace = 0
),
schema_contracts as (
    select
        'schema'::text as object_type,
        n.nspname as identity,
        jsonb_build_object(
            'owner', pg_get_userbyid(n.nspowner),
            'acl', (
                select coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'grantor', pg_get_userbyid(acl.grantor),
                            'grantee', case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            'privilege', acl.privilege_type,
                            'grantable', acl.is_grantable
                        )
                        order by
                            case when acl.grantee = 0 then 'PUBLIC' else pg_get_userbyid(acl.grantee) end,
                            pg_get_userbyid(acl.grantor),
                            acl.privilege_type,
                            acl.is_grantable
                    ),
                    '[]'::jsonb
                )
                from aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) acl
            ),
            'role_privileges', (
                select jsonb_object_agg(
                    r.role_name,
                    jsonb_build_object(
                        'usage', has_schema_privilege(r.role_name, n.oid, 'usage'),
                        'create', has_schema_privilege(r.role_name, n.oid, 'create')
                    )
                    order by r.role_name
                )
                from roles r
            )
        ) as contract
    from pg_namespace n
    where n.nspname = 'public'
),
server_contract as (
    select
        'server'::text as object_type,
        'postgresql'::text as identity,
        jsonb_build_object(
            'major_version', current_setting('server_version_num')::integer / 10000
        ) as contract
),
manifest as (
    select * from manifest_metadata
    union all select * from relation_contracts
    union all select * from column_contracts
    union all select * from constraint_contracts
    union all select * from index_contracts
    union all select * from trigger_contracts
    union all select * from function_contracts
    union all select * from policy_contracts
    union all select * from sequence_contracts
    union all select * from view_contracts
    union all select * from type_contracts
    union all select * from composite_attribute_contracts
    union all select * from domain_constraint_contracts
    union all select * from extension_contracts
    union all select * from foreign_table_contracts
    union all select * from default_acl_contracts
    union all select * from schema_contracts
    union all select * from server_contract
)
select coalesce(
    jsonb_agg(
        jsonb_build_object(
            'object_type', object_type,
            'identity', identity,
            'contract', contract
        )
        order by object_type, identity
    ),
    '[]'::jsonb
) as manifest
from manifest;
