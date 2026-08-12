-- The role a caller-supplied SQL query runs as.
--
-- The MCP `run_sql` tool validates queries before running them, but a parser
-- is the layer most likely to be wrong: the first draft of this one had four
-- evasions. This role is the boundary that does not depend on getting parsing
-- right — it can SELECT the nine catalogue tables and nothing else, so a query
-- that slips past the allowlist still cannot read `rejected_records`,
-- `resolution_attempts`, or anything added to this database later.
--
-- It does not replace the allowlist: PostgreSQL lets any role read much of
-- pg_catalog. The parser keeps system catalogues out; this keeps our own
-- non-public tables out.
--
-- Idempotent. Apply to every environment, including local and CI.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catalogue_readonly') THEN
        -- NOLOGIN: it is never connected as, only switched to with SET LOCAL
        -- ROLE inside a transaction that is already READ ONLY.
        CREATE ROLE catalogue_readonly NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM catalogue_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM catalogue_readonly;
REVOKE CREATE ON SCHEMA public FROM catalogue_readonly;

GRANT USAGE ON SCHEMA public TO catalogue_readonly;
GRANT SELECT ON
    books,
    authors,
    book_authors,
    subjects,
    book_subjects,
    series,
    book_series,
    book_sources,
    ingestion_runs
TO catalogue_readonly;

-- The service switches to the role, so it must be a member of it.
GRANT catalogue_readonly TO CURRENT_USER;
