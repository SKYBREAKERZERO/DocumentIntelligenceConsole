-- Read-only structural and grant audit; run separately on EACH engineering database.
-- Returns NO PASS unless the expected function, active triggers and project scope exist.
SELECT (SELECT count(*) FROM public.project_info) AS project_count,
       (SELECT string_agg(project_code, ', ') FROM public.project_info) AS projects,
       to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)') AS review_function,
       (SELECT count(*) FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
          JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relname IN ('ei_content_relations','ei_relation_review_audit')
            AND t.tgname IN ('dic_stage2_relation_guard','dic_stage2_audit_guard')
            AND t.tgenabled='O' AND NOT t.tgisinternal) AS enabled_guards;
-- Reviewer login must have NO direct DML; DBA must explicitly verify every
-- application/ETL login too. Returned role names are NOT passwords.
SELECT r.rolname AS login_role,
       r.rolsuper AS is_superuser,
       r.rolbypassrls AS bypassrls,
       pg_catalog.has_table_privilege(r.oid,'public.ei_content_relations','UPDATE') AS relation_update,
       pg_catalog.has_table_privilege(r.oid,'public.ei_content_relations','DELETE') AS relation_delete,
       pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','INSERT') AS direct_audit_insert,
       pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','UPDATE') AS audit_update,
       pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','DELETE') AS audit_delete,
       pg_catalog.has_function_privilege(r.oid,'dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)','EXECUTE') AS authorized_execute
FROM pg_catalog.pg_roles r WHERE r.rolcanlogin AND r.rolname !~ '^pg_'
ORDER BY r.rolname;

-- Identity provenance counts (historic approvals are intentionally NOT trusted).
SELECT identity_provenance, new_status, count(*) AS review_events
FROM public.ei_relation_review_audit
GROUP BY identity_provenance,new_status ORDER BY identity_provenance,new_status;
