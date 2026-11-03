-- READ ONLY, execute on rag_qabug, after DBA migration.
BEGIN READ ONLY;
SELECT current_database() AS database_name,
       to_regclass('public.qabug_records') AS qa_registry,
       to_regclass('public.qabug_software_spec_versions') AS software_version_maps,
       to_regclass('public.qabug_spec_candidates') AS offline_candidates,
       to_regclass('public.specifications') AS invalid_if_not_null;
SELECT status, count(*) AS count
FROM public.qabug_software_spec_versions GROUP BY status ORDER BY status;
SELECT status, count(*) AS count
FROM public.qabug_spec_candidates GROUP BY status ORDER BY status;
-- If any rows appear, an individual BUG's context resolves to multiple distinct
-- revision identities for the same specification; the indexer will refuse.
SELECT r.issue_key,m.spec_scope,m.spec_code,
       count(DISTINCT (m.spec_version_id,m.spec_document_id)) AS competing_revisions
FROM public.qabug_records r
JOIN public.qabug_software_spec_versions m ON m.status='APPROVED'
   AND m.model=coalesce(r.model,'') AND m.destination=coalesce(r.destination,'')
   AND ((m.issue_version_field='affected_version' AND m.software_version=r.affected_version)
     OR (m.issue_version_field='software_version' AND m.software_version=r.software_version))
WHERE r.is_current AND r.is_queryable AND r.record_type='BUG'
GROUP BY r.issue_key,m.spec_scope,m.spec_code
HAVING count(DISTINCT (m.spec_version_id,m.spec_document_id))>1;
ROLLBACK;
