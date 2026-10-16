select json_build_object(
  '_captured', json_build_object('origin','att1-db (stack setter-att1), psql con default_transaction_read_only, solo filas de configuracion','captured_at', to_char(now() at time zone 'utc','YYYY-MM-DD"T"HH24:MI:SS"Z"')),
  'pilot_scope_versions', (select json_agg((to_jsonb(s) - 'channel_account_ref') || jsonb_build_object('channel_account_ref_present', s.channel_account_ref is not null) order by s.scope_key, s.version) from public.pilot_scope_versions s),
  'followup_policy_versions', (select json_agg(to_jsonb(p) order by p.policy_key, p.version) from public.followup_policy_versions p),
  'pilot_runtime_controls', (select json_agg(to_jsonb(c) order by c.scope_key) from public.pilot_runtime_controls c),
  'pilot_proactive_contact_caps', (select json_agg(to_jsonb(k)) from public.pilot_proactive_contact_caps k),
  'pilot_control_events_control_level', (select json_agg(to_jsonb(e) - 'contact_id' - 'action_id' - 'attempt_id' order by e.created_at) from public.pilot_control_events e where e.contact_id is null and e.action_id is null and e.attempt_id is null),
  'pilot_control_events_counts', (select json_agg(x) from (select scope_key, scope_version, event_type, count(*) n from public.pilot_control_events group by 1,2,3 order by 1,2,3) x)
);
