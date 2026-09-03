import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant all on tables to service_role;
  alter default privileges in schema public grant all on functions to service_role;
`);
const stack = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];
for (const file of stack) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

await db.exec(`
  insert into public.commercial_ally_runtime_bindings (
    tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
    lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
    product_name, product_price, currency, offer_code, consent_copy_version,
    hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
    inbound_scope_key, inbound_scope_version
  ) values (
    'att1', 'att1-main', 1, 'active', 'ally-one', 'Ally One',
    'ally-one-site', 'main', 'ally-one.example', '/offer/main', 'ATT1HOTLINK',
    'ATT1 Offer', 49, 'USD', 'att1offer', 'att1-whatsapp-v1',
    123456, 42, 24, 'att1-inbound', 1
  );
`);

const resolvePolicy = (triggerKind) => db.query(`
  select * from public.resolve_commercial_ally_discount_policy(
    'att1', 'att1-main', 1, $1
  )
`, [triggerKind]);

if ((await resolvePolicy('precheckout_without_purchase_signal')).rows.length !== 0) {
  throw new Error('empty policy table must resolve default-off');
}

let nullFixedCurrencyRejected = false;
try {
  await db.exec(`
    insert into public.commercial_ally_discount_policy_versions (
      tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
      trigger_kind, discount_kind, discount_value, currency, coupon_reference,
      offer_valid_for, presentation_stage, template_key, copy_version
    ) values (
      'att1', 'att1-main', 1, 'invalid-fixed-currency', 1,
      'payment_failure', 'fixed_amount', 5, null, 'invalid-ref',
      interval '1 hour', 'later_step', 'invalid_template', 'invalid-copy-v1'
    );
  `);
} catch { nullFixedCurrencyRejected = true; }
if (!nullFixedCurrencyRejected) throw new Error('fixed amount accepted NULL currency');

await db.exec(`
  insert into public.commercial_ally_discount_policy_versions (
    tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
    trigger_kind, status, discount_kind, discount_value, currency,
    coupon_reference, offer_valid_for, presentation_stage,
    template_key, copy_version, valid_from, valid_until,
    approved_by, approved_at, published_at
  ) values (
    'att1', 'att1-main', 1, 'att1-recovery-discount', 1,
    'precheckout_without_purchase_signal', 'draft', 'percentage', 10, null,
    'coupon-policy-ref-v1', interval '6 hours', 'first_touch',
    'att1_recovery_first_touch', 'att1-recovery-first-touch-v1',
    statement_timestamp() - interval '1 hour', null, null, null, null
  );
`);
if ((await resolvePolicy('precheckout_without_purchase_signal')).rows.length !== 0) {
  throw new Error('draft policy must not resolve');
}

let draftRetirementRejected = false;
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set status='retired', approved_by='operator-test',
        approved_at=statement_timestamp()
    where policy_key='att1-recovery-discount' and policy_version=1;
  `);
} catch { draftRetirementRejected = true; }
if (!draftRetirementRejected) throw new Error('unapproved draft was retired');

await db.exec(`
  update public.commercial_ally_discount_policy_versions
  set status='approved', approved_by='operator-test',
      approved_at=statement_timestamp()
  where policy_key='att1-recovery-discount' and policy_version=1;
`);

let forgedRetirementPublicationRejected = false;
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set status='retired', published_at=statement_timestamp()
    where policy_key='att1-recovery-discount' and policy_version=1;
  `);
} catch { forgedRetirementPublicationRejected = true; }
if (!forgedRetirementPublicationRejected) {
  throw new Error('approved policy retirement forged publication metadata');
}

await db.exec(`
  update public.commercial_ally_discount_policy_versions
  set status='published', published_at=statement_timestamp() + interval '1 day'
  where policy_key='att1-recovery-discount' and policy_version=1;
`);
const published = (await resolvePolicy('precheckout_without_purchase_signal')).rows;
if (published.length !== 1
    || published[0]?.discount_kind !== 'percentage'
    || Number(published[0]?.discount_value) !== 10
    || published[0]?.presentation_stage !== 'first_touch'
    || new Date(published[0]?.published_at) > new Date()) {
  throw new Error(`published policy did not resolve exactly: ${JSON.stringify(published)}`);
}

let publishedMutationRejected = false;
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set discount_value=15
    where policy_key='att1-recovery-discount' and policy_version=1;
  `);
} catch { publishedMutationRejected = true; }
if (!publishedMutationRejected) throw new Error('published policy content was mutable');

let approvalMetadataMutationRejected = false;
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set approved_by='different-operator'
    where policy_key='att1-recovery-discount' and policy_version=1;
  `);
} catch { approvalMetadataMutationRejected = true; }
if (!approvalMetadataMutationRejected) {
  throw new Error('published policy approval metadata was mutable');
}

let publicationTimestampMutationRejected = false;
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set published_at=published_at + interval '1 day'
    where policy_key='att1-recovery-discount' and policy_version=1;
  `);
} catch { publicationTimestampMutationRejected = true; }
if (!publicationTimestampMutationRejected) {
  throw new Error('published policy publication timestamp was mutable');
}

let createdAtMutationRejected = false;
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set created_at=created_at - interval '1 day'
    where policy_key='att1-recovery-discount' and policy_version=1;
  `);
} catch { createdAtMutationRejected = true; }
if (!createdAtMutationRejected) throw new Error('policy created_at was mutable');

let publishedDeleteRejected = false;
try {
  await db.exec(`
    delete from public.commercial_ally_discount_policy_versions
    where policy_key='att1-recovery-discount' and policy_version=1;
  `);
} catch { publishedDeleteRejected = true; }
if (!publishedDeleteRejected) throw new Error('published policy was deletable');

let duplicatePublishedRejected = false;
try {
  await db.exec(`
    insert into public.commercial_ally_discount_policy_versions (
      tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
      trigger_kind, status, discount_kind, discount_value, currency,
      coupon_reference, offer_valid_for, presentation_stage,
      template_key, copy_version, valid_from,
      approved_by, approved_at, published_at
    ) values (
      'att1', 'att1-main', 1, 'att1-recovery-discount', 2,
      'precheckout_without_purchase_signal', 'draft', 'percentage', 15, null,
      'coupon-policy-ref-v2', interval '6 hours', 'later_step',
      'att1_recovery_later', 'att1-recovery-later-v2',
      statement_timestamp() - interval '1 hour', null, null, null
    );
    update public.commercial_ally_discount_policy_versions
    set status='approved', approved_by='operator-test', approved_at=statement_timestamp()
    where policy_key='att1-recovery-discount' and policy_version=2;
    update public.commercial_ally_discount_policy_versions
    set status='published', published_at=statement_timestamp()
    where policy_key='att1-recovery-discount' and policy_version=2;
  `);
} catch { duplicatePublishedRejected = true; }
if (!duplicatePublishedRejected) throw new Error('second published policy was accepted');

const privileges = (await db.query(`
  select
    has_table_privilege('service_role', 'public.commercial_ally_discount_policy_versions', 'select') as sel,
    has_table_privilege('service_role', 'public.commercial_ally_discount_policy_versions', 'insert') as ins,
    has_table_privilege('service_role', 'public.commercial_ally_discount_policy_versions', 'update') as upd,
    has_table_privilege('service_role', 'public.commercial_ally_discount_policy_versions', 'delete') as del,
    has_function_privilege('service_role',
      'public.resolve_commercial_ally_discount_policy(text,text,integer,text)',
      'execute') as exec
`)).rows[0];
if (privileges?.sel || privileges?.ins || privileges?.upd || privileges?.del
    || !privileges?.exec) {
  throw new Error(`discount policy ACL diverged: ${JSON.stringify(privileges)}`);
}

await db.exec(`
  update public.commercial_ally_discount_policy_versions
  set status='retired'
  where policy_key='att1-recovery-discount' and policy_version=1;
  insert into public.commercial_ally_discount_policy_versions (
    tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
    trigger_kind, status, discount_kind, discount_value, currency,
    coupon_reference, offer_valid_for, presentation_stage,
    template_key, copy_version, valid_from, valid_until
  ) values (
    'att1', 'att1-main', 1, 'att1-recovery-discount', 3,
    'precheckout_without_purchase_signal', 'draft', 'percentage', 10, null,
    'coupon-policy-ref-v3', interval '6 hours', 'first_touch',
    'att1_recovery_first_touch', 'att1-recovery-first-touch-v3',
    statement_timestamp() - interval '2 hours',
    statement_timestamp() - interval '1 hour'
  );
  update public.commercial_ally_discount_policy_versions
  set status='approved', approved_by='operator-test', approved_at=statement_timestamp()
  where policy_key='att1-recovery-discount' and policy_version=3;
  update public.commercial_ally_discount_policy_versions
  set status='published', published_at=statement_timestamp()
  where policy_key='att1-recovery-discount' and policy_version=3;
`);
if ((await resolvePolicy('precheckout_without_purchase_signal')).rows.length !== 0) {
  throw new Error('expired policy must not resolve');
}

let finiteNullDurationRejected = false;
try {
  await db.exec(`
    insert into public.commercial_ally_discount_policy_versions (
      tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
      trigger_kind, discount_kind, discount_value, coupon_reference,
      offer_valid_for, offer_expiration_mode, presentation_stage,
      template_key, copy_version
    ) values (
      'att1', 'att1-main', 1, 'invalid-finite-null-duration', 1,
      'confirmed_cart_abandonment', 'percentage', 10, 'literal-coupon',
      null, 'finite', 'later_step', 'finite_null', 'finite-null-v1'
    );
  `);
} catch { finiteNullDurationRejected = true; }
if (!finiteNullDurationRejected) {
  throw new Error('finite offer accepted NULL duration');
}

let strictTransportNullRejected = false;
try {
  await db.exec(`
    insert into public.commercial_ally_discount_policy_versions
      (tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
       trigger_kind, status, discount_kind, discount_value, currency,
       coupon_reference, offer_valid_for, offer_expiration_mode,
       presentation_stage, template_key, copy_version,
       requires_inbound_reply_after_initial_template, coupon_delivery_mode,
       urgency_copy_allowed, channel_provider, delivery_mode, template_language,
       template_category, coupon_template_component,
       coupon_template_parameter_index, release_requires_exact_trigger_set)
    values
      ('att1','att1-main',1,'strict-null-bypass',1,'payment_failure','draft',
       'percentage',10,null,'META_VARIABLE',null,'indefinite','later_step',
       'att1_discount_later','att1-discount-v1',true,
       'meta_template_variable',false,'waba','approved_template','es_MX',
       null,null,null,true)
  `);
} catch { strictTransportNullRejected = true; }
if (!strictTransportNullRejected) {
  throw new Error('strict Meta-template policy accepted NULL transport fields');
}

let zeroDurationRejected = false;
try {
  await db.exec(`
    insert into public.commercial_ally_discount_policy_versions (
      tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
      trigger_kind, discount_kind, discount_value, coupon_reference,
      offer_valid_for, presentation_stage, template_key, copy_version
    ) values (
      'att1', 'att1-main', 1, 'invalid-zero-duration', 1,
      'confirmed_cart_abandonment', 'percentage', 10, 'variable-from-meta',
      interval '0', 'later_step', 'att1_discount_later', 'att1-discount-v1'
    );
  `);
} catch { zeroDurationRejected = true; }
if (!zeroDurationRejected) throw new Error('zero offer duration was accepted');

await db.exec(`
  insert into public.commercial_ally_discount_policy_versions (
    tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
    trigger_kind, discount_kind, discount_value, currency,
    coupon_reference, offer_valid_for, offer_expiration_mode, presentation_stage,
    template_key, copy_version, valid_from, valid_until
  ) values (
    'att1', 'att1-main', 1, 'att1-payment-failure-discount', 1,
    'payment_failure', 'percentage', 10, null,
    'variable-from-meta-template', null, 'indefinite', 'later_step',
    'att1_discount_later', 'att1-discount-v1',
    statement_timestamp() - interval '1 hour', null
  );
  update public.commercial_ally_discount_policy_versions
  set status='approved', approved_by='operator-test',
      approved_at=statement_timestamp()
  where policy_key='att1-payment-failure-discount' and policy_version=1;
  update public.commercial_ally_discount_policy_versions
  set status='published', published_at=statement_timestamp()
  where policy_key='att1-payment-failure-discount' and policy_version=1;
`);
const indefinite = (await resolvePolicy('payment_failure')).rows;
if (indefinite.length !== 1
    || Number(indefinite[0]?.discount_value) !== 10
    || indefinite[0]?.offer_valid_for_seconds !== null
    || indefinite[0]?.valid_until !== null
    || indefinite[0]?.presentation_stage !== 'later_step') {
  throw new Error(`indefinite policy did not resolve exactly: ${JSON.stringify(indefinite)}`);
}

await db.exec(`
  update public.commercial_ally_discount_policy_versions
  set status='retired'
  where tenant_ref='att1' and funnel_ref='att1-main'
    and binding_version=1 and status='published';

  insert into public.commercial_ally_discount_policy_versions (
    tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
    trigger_kind, discount_kind, discount_value, coupon_reference,
    offer_valid_for, offer_expiration_mode, presentation_stage,
    template_key, copy_version, release_requires_exact_trigger_set,
    requires_inbound_reply_after_initial_template, coupon_delivery_mode,
    urgency_copy_allowed, channel_provider, delivery_mode,
    template_language, template_category,
    coupon_template_component, coupon_template_parameter_index,
    valid_from
  ) values
    ('att1','att1-main',1,'att1-recovery-triplet',1,
     'payment_failure','percentage',10,'meta-variable',
     null,'indefinite','later_step','att1_discount_later','att1-discount-v1',
     true,true,'meta_template_variable',false,'waba','approved_template',
     'es_MX','marketing','body',1,statement_timestamp()-interval '1 hour'),
    ('att1','att1-main',1,'att1-recovery-triplet',1,
     'confirmed_cart_abandonment','percentage',10,'meta-variable',
     null,'indefinite','later_step','att1_discount_later','att1-discount-v1',
     true,true,'meta_template_variable',false,'waba','approved_template',
     'es_MX','marketing','body',1,statement_timestamp()-interval '1 hour'),
    ('att1','att1-main',1,'att1-recovery-triplet',1,
     'precheckout_without_purchase_signal','percentage',10,'meta-variable',
     null,'indefinite','later_step','att1_discount_later','att1-discount-v1',
     true,true,'meta_template_variable',false,'waba','approved_template',
     'es_MX','marketing','body',1,statement_timestamp()-interval '1 hour');

  update public.commercial_ally_discount_policy_versions
  set status='approved', approved_by='operator-test',
      approved_at=statement_timestamp()
  where policy_key='att1-recovery-triplet' and policy_version=1;
`);

let partialPublishError = '';
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set status='published', published_at=statement_timestamp()
    where policy_key='att1-recovery-triplet' and policy_version=1
      and trigger_kind='payment_failure';
  `);
} catch (error) { partialPublishError = String(error?.message ?? error); }
if (!partialPublishError.includes('commercial_ally_discount_release_')) {
  throw new Error(`partial strict release failure was not authoritative: ${partialPublishError}`);
}

await db.exec(`
  begin;
  update public.commercial_ally_discount_policy_versions
  set status='published', published_at=statement_timestamp()
  where policy_key='att1-recovery-triplet' and policy_version=1;
  commit;
`);
for (const trigger of [
  'payment_failure',
  'confirmed_cart_abandonment',
  'precheckout_without_purchase_signal',
]) {
  const rows = (await resolvePolicy(trigger)).rows;
  if (rows.length !== 1
      || rows[0]?.policy_key !== 'att1-recovery-triplet'
      || Number(rows[0]?.discount_value) !== 10
      || rows[0]?.offer_expiration_mode !== 'indefinite'
      || rows[0]?.requires_inbound_reply_after_initial_template !== true
      || rows[0]?.coupon_delivery_mode !== 'meta_template_variable'
      || rows[0]?.urgency_copy_allowed !== false
      || rows[0]?.channel_provider !== 'waba'
      || rows[0]?.delivery_mode !== 'approved_template'
      || rows[0]?.coupon_template_component !== 'body'
      || Number(rows[0]?.coupon_template_parameter_index) !== 1) {
    throw new Error(`strict release did not resolve for ${trigger}: ${JSON.stringify(rows)}`);
  }
}

let partialRetirementError = '';
try {
  await db.exec(`
    update public.commercial_ally_discount_policy_versions
    set status='retired'
    where policy_key='att1-recovery-triplet' and policy_version=1
      and trigger_kind='payment_failure';
  `);
} catch (error) { partialRetirementError = String(error?.message ?? error); }
if (!partialRetirementError.includes('commercial_ally_discount_release_')) {
  throw new Error(`partial strict retirement failure was not authoritative: ${partialRetirementError}`);
}

let partialDeleteError = '';
try {
  await db.exec(`
    delete from public.commercial_ally_discount_policy_versions
    where policy_key='att1-recovery-triplet' and policy_version=1
      and trigger_kind='payment_failure';
  `);
} catch (error) { partialDeleteError = String(error?.message ?? error); }
if (!partialDeleteError.includes('commercial_ally_discount_policy_content_immutable')
    && !partialDeleteError.includes('commercial_ally_discount_release_')) {
  throw new Error(`partial strict delete failure was not authoritative: ${partialDeleteError}`);
}

console.log('discount_policy_default_off=OK');
console.log('discount_policy_single_published=OK');
console.log('discount_policy_runtime_read_only=OK');
console.log('discount_policy_indefinite_offer=OK');
console.log('discount_policy_strict_triplet_atomic=OK');
