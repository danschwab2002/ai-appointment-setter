-- Lectura del modo de audiencia de un scope publicado del piloto (decisiones
-- D4, D8 y D10; docs/contracts/ghl-precheckout-adapter-v1.md, Riesgos).
--
-- El token del adaptador de GHL es la unica barrera de sus envios y lo lee
-- cualquier usuario de la subcuenta: una intencion que entro por el adaptador
-- no se distingue en la base de la de una landing. Una instancia con
-- [adaptadores.ghl] en el manifiesto y sin la aceptacion escrita de ese riesgo
-- no puede usar esas intenciones como audiencia. Los flujos precheckout y
-- pago_fallido se cortan con el manifiesto solo; la audiencia del scope del
-- piloto (consented_intent o consented_intent_in_cohort) vive en la base, en
-- pilot_scope_versions.audience_mode, y esa tabla tiene RLS y esta revocada a
-- todos los roles de la API: el bridge no la puede leer por PostgREST.
--
-- Esta migracion suma la lectura que le faltaba al bridge para hacer cumplir
-- esa guarda en el arranque y en /ready:
--
--   get_lancemos_pilot_scope_audience_mode(scope_key, scope_version)
--
-- Devuelve el audience_mode de esa version si esta publicada, y null si no
-- existe o no esta publicada (el bridge trata null como bloqueo). Es una
-- funcion NUEVA: no reemplaza ni toca ninguna existente. No se cambia
-- get_lancemos_pilot_runtime_status porque sumarle una columna a su returns
-- table exige drop + create, pierde los grants y toca una funcion que Johanna
-- ejecuta en cada /ready.
--
-- Solo lee una fila por clave primaria: no toma locks sobre tablas calientes,
-- no reescribe filas y no siembra nada.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create or replace function public.get_lancemos_pilot_scope_audience_mode(
    p_scope_key text,
    p_scope_version integer
)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $function$
    select scope.audience_mode
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
$function$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca explicito
-- y queda solo para service_role (es un entrypoint del bridge).
revoke all on function public.get_lancemos_pilot_scope_audience_mode(text,integer) from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on function public.get_lancemos_pilot_scope_audience_mode(text,integer) from %I',v_role);
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.get_lancemos_pilot_scope_audience_mode(text,integer) to service_role;
 end if;
end;
$roles$;

commit;
