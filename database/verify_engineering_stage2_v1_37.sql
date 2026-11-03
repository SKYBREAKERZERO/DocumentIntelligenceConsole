-- Operator-invoked, read-only smoke verification of the v1.37 server parser.
-- Run as the trusted DB function owner (NOLOGIN through vetted DBA SET ROLE).
-- This verifies only synthetic parser boundaries, not live DB role/transaction
-- negative tests or customer specification evidence.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='30s';
DO $body$
DECLARE
  ok boolean;
BEGIN
  IF to_regprocedure('dic_review.has_unambiguous_section_reference(text,text,text)') IS NULL THEN
    RAISE EXCEPTION 'v1.37 semantic reference helper not installed';
  END IF;
  ok := dic_review.has_unambiguous_section_reference('Refer to Spec 550 section 4.1 for details','550','4.1');
  IF ok IS DISTINCT FROM TRUE THEN
    RAISE EXCEPTION 'v1.37 valid English explicit citation parser smoke check failed';
  END IF;
  ok := dic_review.has_unambiguous_section_reference('参照 Spec 550 section 4.1。','550','4.1');
  IF ok IS DISTINCT FROM TRUE THEN
    RAISE EXCEPTION 'v1.37 valid Chinese/Japanese citation parser smoke check failed';
  END IF;
  ok := dic_review.has_unambiguous_section_reference('Refer to Spec 550 for details. See section 4.1 in another spec.','550','4.1');
  IF ok IS DISTINCT FROM FALSE THEN
    RAISE EXCEPTION 'v1.37 cross sentence false-positive check failed';
  END IF;
  ok := dic_review.has_unambiguous_section_reference('Refer to Spec 550 section 4.1 and section 5.2','550','4.1');
  IF ok IS DISTINCT FROM FALSE THEN
    RAISE EXCEPTION 'v1.37 multi-section false-positive check failed';
  END IF;
  ok := dic_review.has_unambiguous_section_reference('Section 4.1 is required; see Spec 550.','550','4.1');
  IF ok IS DISTINCT FROM FALSE THEN
    RAISE EXCEPTION 'v1.37 reversed reference direction false-positive check failed';
  END IF;
  ok := dic_review.has_unambiguous_section_reference('See Spec 542 section 4.1','550','4.1');
  IF ok IS DISTINCT FROM FALSE THEN
    RAISE EXCEPTION 'v1.37 wrong target code false-positive check failed';
  END IF;
  ok := dic_review.has_unambiguous_section_reference('See Spec 550 section 4.1.2','550','4.1');
  IF ok IS DISTINCT FROM FALSE THEN
    RAISE EXCEPTION 'v1.37 wrong target subsection false-positive check failed';
  END IF;
END $body$;
ROLLBACK;
