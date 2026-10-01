-- La respuesta a una plantilla del piloto entra a la admision entrante (H7).
--
-- Cuando el dispatcher del piloto manda una plantilla y Chatwoot la acepta,
-- record_and_finalize_followup_acceptance (20260805000300) crea la
-- conversacion canonica del caso con automation_status = 'enabled' y le ata el
-- caso de recuperacion. La admision entrante solo toma una conversacion que ya
-- existe si esta en 'draft_only' (20260816000200, 22000
-- inbound_canonical_conversation_conflict). Desde 1.3.0 la respuesta del lead
-- cae en esa misma conversacion de Chatwoot, asi que quien aprieta un boton de
-- la plantilla (primer contacto, carrito o pago fallido) no llega al agente:
-- sin respuesta, sin enlace y sin derivacion. Johanna no lo ve porque sus
-- envios no pasan por esa aceptacion: la admision crea la conversacion en
-- 'draft_only' cuando la persona contesta.
--
-- Esta migracion suma un entrypoint aparte, que solo llama el bridge con
-- manifiesto:
--
--   admit_portable_inbound_commercial_case_v1(scope_key, scope_version,
--       external_conversation_id, external_user_id)
--
-- Con el scope entrante publicado toma los mismos tres advisory locks que la
-- admision base, en el mismo orden, y si todavia no hay fila de admision para
-- esa conversacion de Chatwoot bloquea primero la identidad que contesta (la
-- del inbox del scope, activa) y despues su conversacion (commercial_context
-- exacto, 'enabled', sin human_takeover, en un status vivo): el orden de la
-- aceptacion y de la admision base. La adopta solo si, ademas:
--
-- 1. una plantilla del piloto salio ahi: un mensaje outbound de ai_agent con
--    strategy = durable_followup, que es el accepted_message_id de un intento
--    accepted_by_chatwoot de una accion de un caso de recuperacion de esa
--    misma identidad, contacto y conversacion, con su fila en
--    pilot_recovery_case_bindings (un caso sin binding, como los de Johanna,
--    no se adopta);
-- 2. el contacto no esta dado de baja: contact_permission fuera de opted_out,
--    blocked y restricted, lifecycle_status distinto de do_not_contact, y
--    ninguna baja de Chatwoot de ese movil en cualquiera de sus formas (las
--    del que contesta y las de contacts.phone) con correlation_status
--    applied, unmatched, ambiguous o evidence_conflict, los mismos estados
--    que frena el arranque del piloto (_portable_chatwoot_opt_out_stop): una
--    baja que no quedo aplicada a este contacto, porque entro por la otra
--    forma del movil o hay dos contactos, tambien frena (decision 5: lo
--    atiende una persona y el resultado es el de hoy);
-- 3. no hay un paso pendiente para esa persona (decision 6): ningun caso de
--    recuperacion del contacto atado a esta conversacion, o todavia sin
--    conversacion, tiene una accion en un estado vivo del constraint de
--    scheduled_actions (20260803000100): pending, deferred, retryable_failed o
--    delivery_unknown.
--
-- Si adopta, pasa la conversacion de 'enabled' a 'draft_only' (version + 1) y
-- deja un conversation_events inbound_adopted_template_conversation, con actor
-- integration, related_message_id = la plantilla y related_action_id = su
-- accion. Despues delega SIEMPRE en admit_inbound_commercial_case_v2, sin
-- tocarla: mismo contrato, mismos errores. Si no adopta, el resultado es
-- exactamente el de hoy. Todo corre en la misma transaccion: si la v2 levanta
-- un error, la adopcion tambien se deshace.
--
-- Locks: ademas de los de la admision base, solo la fila de la identidad y la
-- de la conversacion, en el mismo orden en que los toma la base. El de la
-- conversacion es defensa en profundidad: hoy todo camino que cambia esa
-- conversacion bloquea antes la identidad (la aceptacion, su reconciliacion,
-- la baja y la admision base), y la derivacion del caso de recuperacion, que
-- no la bloquea, no aplica al caso agotado de un toque; lo prueba con un
-- escritor directo el caso 5 de real_postgres_portable_inbound_adoption.py. El
-- contacto,
-- sus bajas, los casos, las acciones y los intentos se leen sin bloquear. Las
-- bajas se leen sin el advisory lock de opt-out del arranque del piloto:
-- apply_chatwoot_inbound_opt_out lo toma antes de bloquear la identidad, y la
-- adopcion ya la tiene bloqueada, asi que esperarlo aca invertiria el orden.
-- Una baja en vuelo no se espera; la frena despues el bridge, que antes de
-- correr al agente mira has_chatwoot_opt_out_stop. Una entrega repetida del
-- mismo entrante se serializa en el primer advisory lock y la segunda ve la
-- fila de admision: no adopta otra vez y la v2 da already_exists.
--
-- Johanna no cambia: es una funcion nueva, no reemplaza ni toca ninguna, y el
-- bridge sin manifiesto sigue llamando a admit_inbound_commercial_case_v2.
-- No siembra filas.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create function public.admit_portable_inbound_commercial_case_v1(
    p_scope_key text,
    p_scope_version integer,
    p_external_conversation_id bigint,
    p_external_user_id text
)
returns table (
    outcome text,
    commercial_case_id uuid,
    contact_id uuid,
    channel_identity_id uuid,
    conversation_id uuid,
    automation_status text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_scope public.inbound_commercial_scope_versions%rowtype;
    v_identity public.channel_identities%rowtype;
    v_conversation public.conversations%rowtype;
    v_contact public.contacts%rowtype;
    v_template_message_id uuid;
    v_template_action_id uuid;
    v_template_attempt_id uuid;
    v_recovery_case_id uuid;
    v_pilot_scope_key text;
    v_pilot_scope_version integer;
    v_conversation_version bigint;
begin
    -- Parametros invalidos o scope sin publicar: no se adopta nada y la v2
    -- levanta el error de siempre (22023 o 55000).
    if p_scope_key is not null
       and p_scope_key ~ '^[a-z0-9_-]{1,100}$'
       and p_scope_version is not null and p_scope_version >= 1
       and p_external_conversation_id is not null
       and p_external_conversation_id >= 1
       and p_external_user_id is not null
       and p_external_user_id ~ '^[0-9]+$' then
        select scope.* into v_scope
        from public.inbound_commercial_scope_versions scope
        where scope.scope_key = p_scope_key
          and scope.version = p_scope_version
          and scope.status = 'published'
        for share;

        if found then
            -- Los mismos tres locks, en el mismo orden, que la admision base:
            -- dos entregas del mismo entrante se serializan aca.
            perform pg_advisory_xact_lock(hashtextextended(
                concat_ws(':', 'inbound-commercial-case', p_scope_key,
                          p_scope_version, p_external_conversation_id), 0
            ));
            perform pg_advisory_xact_lock(hashtextextended(
                concat_ws(':', 'chatwoot-conversation-owner',
                          v_scope.chatwoot_account_id, p_external_conversation_id), 0
            ));
            perform pg_advisory_xact_lock(hashtextextended(
                concat_ws(':', 'chatwoot-channel-identity',
                          v_scope.chatwoot_account_id, v_scope.chatwoot_inbox_id,
                          p_external_user_id), 0
            ));

            if not exists (
                select 1
                from public.inbound_commercial_case_admissions admission
                where admission.scope_key = p_scope_key
                  and admission.scope_version = p_scope_version
                  and admission.external_conversation_id = p_external_conversation_id
            ) then
                -- Identidad primero y conversacion despues: el orden de la
                -- aceptacion y de la admision base.
                select identity.* into v_identity
                from public.channel_identities identity
                where identity.channel = 'whatsapp'
                  and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
                  and identity.external_user_id = p_external_user_id
                  and identity.metadata ->> 'inbox_id' = v_scope.chatwoot_inbox_id::text
                  and identity.identity_status = 'active'
                for update;

                if found then
                    select conversation.* into v_conversation
                    from public.conversations conversation
                    where conversation.channel_identity_id = v_identity.id
                      and conversation.contact_id = v_identity.contact_id
                      and conversation.commercial_context = jsonb_build_object(
                          'chatwoot_conversation_id', p_external_conversation_id::text
                      )
                      and conversation.automation_status = 'enabled'
                      and conversation.status in (
                          'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
                      )
                      and not conversation.human_takeover
                    for update;
                end if;

                if v_conversation.id is not null then
                    select contact.* into v_contact
                    from public.contacts contact
                    where contact.id = v_identity.contact_id;

                    -- La prueba de que una plantilla del piloto salio en esta
                    -- conversacion: mensaje -> intento aceptado -> accion ->
                    -- caso de esta identidad, contacto y conversacion -> su
                    -- binding del piloto. Si hay mas de una, la mas nueva.
                    select message.id, action.id, attempt.id, recovery.id,
                           binding.scope_key, binding.scope_version
                      into v_template_message_id, v_template_action_id,
                           v_template_attempt_id, v_recovery_case_id,
                           v_pilot_scope_key, v_pilot_scope_version
                    from public.messages message
                    join public.followup_delivery_attempts attempt
                      on attempt.accepted_message_id = message.id
                     and attempt.outcome = 'accepted_by_chatwoot'
                    join public.scheduled_actions action
                      on action.id = attempt.action_id
                    join public.recovery_cases recovery
                      on recovery.id = action.recovery_case_id
                    join public.pilot_recovery_case_bindings binding
                      on binding.recovery_case_id = recovery.id
                    where message.conversation_id = v_conversation.id
                      and message.direction = 'outbound'
                      and message.actor_type = 'ai_agent'
                      and message.semantic_metadata ->> 'strategy' = 'durable_followup'
                      and recovery.conversation_id = v_conversation.id
                      and recovery.contact_id = v_identity.contact_id
                      and recovery.selected_channel_identity_id = v_identity.id
                    order by message.occurred_at desc, message.created_at desc,
                             message.id desc
                    limit 1;

                    if v_template_message_id is not null
                       and v_contact.contact_permission not in (
                           'opted_out', 'blocked', 'restricted'
                       )
                       and v_contact.lifecycle_status <> 'do_not_contact'
                       -- Una baja de Chatwoot de ese movil, en cualquiera de
                       -- sus formas, aunque no haya quedado aplicada a este
                       -- contacto: los estados que frena el arranque del
                       -- piloto (_portable_chatwoot_opt_out_stop,
                       -- 20261001000100), sin su advisory lock (ver Locks
                       -- arriba).
                       and not exists (
                           select 1
                           from public.contact_opt_out_events optout
                           where optout.source = 'chatwoot'
                             and optout.channel = 'whatsapp'
                             and optout.canonical_account_id = v_scope.chatwoot_account_id
                             and optout.external_user_id = any(
                                 coalesce(
                                     public._whatsapp_phone_variants(p_external_user_id),
                                     array[]::text[]
                                 )
                                 || coalesce(
                                     public._whatsapp_phone_variants(v_contact.phone),
                                     array[]::text[]
                                 )
                             )
                             and optout.correlation_status in (
                                 'applied', 'unmatched', 'ambiguous',
                                 'evidence_conflict'
                             )
                       )
                       and not exists (
                           select 1
                           from public.scheduled_actions action
                           join public.recovery_cases recovery
                             on recovery.id = action.recovery_case_id
                           where recovery.contact_id = v_identity.contact_id
                             and (
                                 recovery.conversation_id = v_conversation.id
                                 or recovery.conversation_id is null
                             )
                             and action.status in (
                                 'pending', 'deferred', 'retryable_failed',
                                 'delivery_unknown'
                             )
                       ) then
                        update public.conversations conversation
                        set automation_status = 'draft_only',
                            version = conversation.version + 1
                        where conversation.id = v_conversation.id
                          and conversation.automation_status = 'enabled'
                        returning conversation.version
                          into strict v_conversation_version;

                        insert into public.conversation_events (
                            conversation_id, recovery_case_id, event_type,
                            actor_type, related_message_id, related_action_id,
                            data
                        ) values (
                            v_conversation.id, v_recovery_case_id,
                            'inbound_adopted_template_conversation', 'integration',
                            v_template_message_id, v_template_action_id,
                            jsonb_build_object(
                                'scope_key', p_scope_key,
                                'scope_version', p_scope_version,
                                'chatwoot_conversation_id',
                                p_external_conversation_id::text,
                                'pilot_scope_key', v_pilot_scope_key,
                                'pilot_scope_version', v_pilot_scope_version,
                                'attempt_id', v_template_attempt_id,
                                'previous_automation_status', 'enabled',
                                'conversation_version', v_conversation_version
                            )
                        );
                    end if;
                end if;
            end if;
        end if;
    end if;

    return query
    select result.outcome, result.commercial_case_id, result.contact_id,
           result.channel_identity_id, result.conversation_id,
           result.automation_status
    from public.admit_inbound_commercial_case_v2(
        p_scope_key,
        p_scope_version,
        p_external_conversation_id,
        p_external_user_id
    ) result;
end;
$function$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca explicito
-- y queda solo para service_role (es un entrypoint del bridge).
revoke all on function public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text) from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on function public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text) from %I',v_role);
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text) to service_role;
 end if;
end;
$roles$;

commit;
