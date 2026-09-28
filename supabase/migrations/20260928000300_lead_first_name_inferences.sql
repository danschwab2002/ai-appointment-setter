-- Migration: el primer nombre inferido de cada lead, calculado una vez al llegar
-- el formulario y leido por toda plantilla de primer contacto.
--
-- El hueco que cierra. Los cuatro caminos que mandan la primera plantilla de
-- Johanna (precheckout de prueba, precheckout diferido, carrito abandonado y
-- pago rechazado) leen el nombre de precheckout_submissions.canonical_payload
-- #>> '{lead,full_name}' y lo pasan sin tocar a la variable {{1}}. Medido el
-- 2026-09-28 sobre el inbox 9 de Chatwoot: 147 de las 162 plantillas enviadas
-- son johanna_interes_precheckout_01 y la variable llevo el nombre completo tal
-- como la persona lo escribio ("Adriana Maritza Sanchez Ivanez", "VIGO ARAUJO",
-- "juan collado"). Cortar por la primera palabra resuelve la mayoria, pero
-- rompe los compuestos ("Juan Carlos", "Andres Felipe") y los que empiezan con
-- otra cosa ("San Juana Gonzalez" daria "San").
--
-- Como se resuelve. Al admitir el formulario, el bridge le pide a un modelo el
-- primer nombre y guarda aca la respuesta. Al enviar, busca la fila por la
-- clave del nombre completo y aplica la cadena de tres niveles: la inferencia
-- si el modelo esta seguro, si no el primer nombre deterministico, y como
-- ultimo recurso el nombre tal cual. Esta tabla solo guarda el primer nivel.
--
-- Por que la clave es un sha256 del nombre y no el id de la submission. La
-- inferencia depende solo del texto: el mismo nombre escrito igual da el mismo
-- primer nombre, sea quien sea. Con la clave por texto, los cuatro caminos de
-- envio la encuentran con el nombre que ya reciben, sin tocar ninguna de las
-- RPC que arman el envio. El nombre completo no se copia: ya vive en la
-- submission canonica.
--
-- Orden de despliegue. Indistinto. Sin esta migracion, el bridge no encuentra
-- la inferencia y cae al primer nombre deterministico; sin el bridge nuevo, la
-- tabla queda vacia.

create table public.lead_first_name_inferences (
    name_key text primary key,
    result text not null,
    first_name text,
    model_name text not null,
    prompt_version text not null,
    created_at timestamptz not null default clock_timestamp(),
    check (name_key ~ '^[0-9a-f]{64}$'),
    check (result in ('confident', 'uncertain')),
    check (
        (result = 'confident'
            and first_name is not null
            and char_length(first_name) between 1 and 80)
        or (result = 'uncertain' and first_name is null)
    ),
    check (char_length(model_name) between 1 and 200),
    check (char_length(prompt_version) between 1 and 100)
);

alter table public.lead_first_name_inferences enable row level security;

create or replace function public.record_lead_first_name_inference_v1(
    p_name_key text,
    p_result text,
    p_first_name text,
    p_model_name text,
    p_prompt_version text
)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_inserted integer;
begin
    if p_name_key is null or p_name_key !~ '^[0-9a-f]{64}$'
       or p_result is null or p_result not in ('confident', 'uncertain')
       or (p_result = 'confident'
           and (p_first_name is null
                or char_length(p_first_name) not between 1 and 80))
       or (p_result = 'uncertain' and p_first_name is not null)
       or p_model_name is null
       or char_length(p_model_name) not between 1 and 200
       or p_prompt_version is null
       or char_length(p_prompt_version) not between 1 and 100 then
        raise exception using errcode = '22023',
            message = 'invalid_lead_first_name_inference';
    end if;

    -- La primera respuesta manda: un segundo formulario con el mismo nombre no
    -- vuelve a escribir, asi un saludo no cambia entre dos envios al mismo lead.
    insert into public.lead_first_name_inferences (
        name_key, result, first_name, model_name, prompt_version
    )
    values (
        p_name_key, p_result, p_first_name, p_model_name, p_prompt_version
    )
    on conflict on constraint lead_first_name_inferences_pkey do nothing;
    get diagnostics v_inserted = row_count;

    return case when v_inserted = 1 then 'inserted' else 'existing' end;
end;
$function$;

create or replace function public.get_lead_first_name_inference_v1(
    p_name_key text
)
returns table (
    result text,
    first_name text
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_name_key is null or p_name_key !~ '^[0-9a-f]{64}$' then
        raise exception using errcode = '22023',
            message = 'invalid_lead_first_name_key';
    end if;

    return query
    select inference.result, inference.first_name
    from public.lead_first_name_inferences inference
    where inference.name_key = p_name_key;
end;
$function$;

revoke all on table public.lead_first_name_inferences from public;
revoke execute on function public.record_lead_first_name_inference_v1(text, text, text, text, text) from public;
revoke execute on function public.get_lead_first_name_inference_v1(text) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke all on table public.lead_first_name_inferences from anon;
        revoke execute on function public.record_lead_first_name_inference_v1(text, text, text, text, text) from anon;
        revoke execute on function public.get_lead_first_name_inference_v1(text) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on table public.lead_first_name_inferences from authenticated;
        revoke execute on function public.record_lead_first_name_inference_v1(text, text, text, text, text) from authenticated;
        revoke execute on function public.get_lead_first_name_inference_v1(text) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        revoke all on table public.lead_first_name_inferences from service_role;
        grant execute on function public.record_lead_first_name_inference_v1(text, text, text, text, text) to service_role;
        grant execute on function public.get_lead_first_name_inference_v1(text) to service_role;
    end if;
end;
$roles$;
