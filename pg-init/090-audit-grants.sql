-- 090-audit-acl.sql  (run after audit_log table exists)

DO $do$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'audit_log'
  ) THEN
    -- Table-level ACLs
    REVOKE ALL ON TABLE public.audit_log FROM PUBLIC;

    -- Writer needs to read latest row to chain, and insert new rows
    GRANT SELECT, INSERT ON TABLE public.audit_log TO audit_writer;

    -- Reader and app get SELECT only
    GRANT SELECT ON TABLE public.audit_log TO audit_reader, app_rw;

    -- Block UPDATE/DELETE explicitly (append-only)
    REVOKE UPDATE, DELETE ON TABLE public.audit_log FROM app_rw, audit_reader, audit_writer;

    -- Also ensure sequences are usable (covers audit_log_id_seq if present)
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw, audit_writer, audit_reader;

    -- Defense-in-depth trigger to forbid UPDATE/DELETE
    CREATE OR REPLACE FUNCTION public.audit_log_forbid_ud()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $fn$
    BEGIN
      RAISE EXCEPTION 'audit_log is append-only';
    END
    $fn$;

    DROP TRIGGER IF EXISTS trg_audit_log_forbid_ud ON public.audit_log;
    CREATE TRIGGER trg_audit_log_forbid_ud
      BEFORE UPDATE OR DELETE ON public.audit_log
      FOR EACH ROW EXECUTE FUNCTION public.audit_log_forbid_ud();
  END IF;
END
$do$;
