-- segment-entry-answers-v1: lo que contesto cada conversacion a la pregunta de
-- entrada de una campana (E-317, decision de Dan del 03/11, opcion b; D4 del
-- diseno E-319). Contrato: docs/contracts/campaign-inbound-entry-v1.md.
--
-- Por que. Mientras una campana (un segmento de [[agente.segmentos]]) tiene la
-- pregunta de entrada prendida, a quien escribe por su cuenta sin la palabra
-- clave el agente le pregunta si viene por la campana, y devuelve
-- campana_respuesta: si | no. Con "si" el bridge pone la etiqueta de la campana
-- (y desde ahi va el conocimiento por la etiqueta). Con "no" no hay etiqueta que
-- poner: sin esta tabla, el turno siguiente volveria a preguntar. D4: el "no"
-- queda en la base, por conversacion y campana, no como etiqueta (no ensucia
-- Chatwoot). El "si" tambien se guarda: la etiqueta la pone un macro en un job,
-- y la respuesta queda para medir cuantos entraron por la pregunta.
--
-- Que crea.
-- 1. segment_entry_answers: una fila por (cuenta, inbox, conversacion de
--    Chatwoot, clave del segmento), con la respuesta y la hora. Sin telefono,
--    mail, nombre ni texto de la persona.
-- 2. record_segment_entry_answer_v1: registrar. La primera respuesta queda: una
--    segunda devuelve replayed con la que ya estaba y no cambia nada.
-- 3. get_segment_entry_answers_v1: las respuestas de una conversacion.
-- Solo service_role (el bridge) ejecuta las dos funciones; la tabla no tiene
-- permisos para ningun rol de la API.
--
-- Correrla dos veces aborta con 55000 sin cambiar nada. Errores: 22023 entrada
-- invalida.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $guard$
begin
    if to_regclass('public.segment_entry_answers') is not null then
        raise exception using errcode = '55000',
            message = 'segment_entry_answers_already_applied';
    end if;
end
$guard$;

-- 1. La tabla.

create table public.segment_entry_answers (
    id uuid primary key default gen_random_uuid(),
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    chatwoot_conversation_id bigint not null check (chatwoot_conversation_id > 0),
    segment_key text not null check (segment_key ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    answer text not null check (answer in ('si', 'no')),
    answered_at timestamptz not null,
    unique (chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id, segment_key)
);

alter table public.segment_entry_answers enable row level security;

-- 2. Registrar una respuesta.

create function public.record_segment_entry_answer_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_segment_key text,
    p_answer text,
    p_now timestamptz default clock_timestamp()
)
returns table (outcome text, answer text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_answer text;
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id <= 0
       or p_segment_key is null or p_segment_key !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or p_answer is null or p_answer not in ('si', 'no')
       or p_now is null then
        raise exception using errcode = '22023',
            message = 'invalid_segment_entry_answer';
    end if;
    insert into public.segment_entry_answers as stored (
        chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id,
        segment_key, answer, answered_at
    )
    values (
        p_chatwoot_account_id, p_chatwoot_inbox_id, p_chatwoot_conversation_id,
        p_segment_key, p_answer, p_now
    )
    on conflict (chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id, segment_key)
    do nothing
    returning stored.answer into v_answer;
    if v_answer is not null then
        outcome := 'recorded';
        answer := v_answer;
        return next;
        return;
    end if;
    select stored.answer into v_answer
      from public.segment_entry_answers stored
     where stored.chatwoot_account_id = p_chatwoot_account_id
       and stored.chatwoot_inbox_id = p_chatwoot_inbox_id
       and stored.chatwoot_conversation_id = p_chatwoot_conversation_id
       and stored.segment_key = p_segment_key;
    outcome := 'replayed';
    answer := v_answer;
    return next;
end;
$function$;

-- 3. Las respuestas de una conversacion.

create function public.get_segment_entry_answers_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint
)
returns table (segment_key text, answer text)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id <= 0 then
        raise exception using errcode = '22023',
            message = 'invalid_segment_entry_answers_query';
    end if;
    return query
    select stored.segment_key, stored.answer
      from public.segment_entry_answers stored
     where stored.chatwoot_account_id = p_chatwoot_account_id
       and stored.chatwoot_inbox_id = p_chatwoot_inbox_id
       and stored.chatwoot_conversation_id = p_chatwoot_conversation_id
     order by stored.segment_key;
end;
$function$;

-- Permisos. Postgres le da execute a PUBLIC en toda funcion nueva y Supabase
-- expone las tablas a la API: todo se revoca, y solo service_role (el bridge)
-- ejecuta las dos funciones. La tabla no tiene permisos para ningun rol de la API.
do $acl$
declare
    v_function text;
    v_role text;
begin
    revoke all on table public.segment_entry_answers from public;
    foreach v_function in array array[
        'public.record_segment_entry_answer_v1(bigint,bigint,bigint,text,text,timestamptz)',
        'public.get_segment_entry_answers_v1(bigint,bigint,bigint)'
    ] loop
        execute format('revoke all on function %s from public', v_function);
        foreach v_role in array array['anon', 'authenticated'] loop
            if to_regrole(v_role) is not null then
                execute format('revoke all on function %s from %I', v_function, v_role);
            end if;
        end loop;
        if to_regrole('service_role') is not null then
            execute format('grant execute on function %s to service_role', v_function);
        end if;
    end loop;
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on table public.segment_entry_answers from %I', v_role);
        end if;
    end loop;

    -- Por presencia: service_role ejecuta las dos y ningun rol de la API.
    foreach v_function in array array[
        'public.record_segment_entry_answer_v1(bigint,bigint,bigint,text,text,timestamptz)',
        'public.get_segment_entry_answers_v1(bigint,bigint,bigint)'
    ] loop
        if exists (select 1 from pg_roles where rolname = 'service_role'
                      and not has_function_privilege(rolname, v_function, 'EXECUTE'))
           or exists (select 1 from pg_roles where rolname in ('anon', 'authenticated')
                         and has_function_privilege(rolname, v_function, 'EXECUTE')) then
            raise exception using errcode = '55000',
                message = 'segment_entry_answers_acl_unexpected', detail = v_function;
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname in ('anon', 'authenticated', 'service_role')
                  and has_table_privilege(rolname, 'public.segment_entry_answers', 'SELECT')) then
        raise exception using errcode = '55000',
            message = 'segment_entry_answers_acl_unexpected', detail = 'tabla';
    end if;
end
$acl$;

commit;
