-- 001-roles.sql  (base roles & defaults; SAFE before any tables exist)

-- Create app and audit roles
CREATE ROLE app_rw LOGIN PASSWORD 'muict';
CREATE ROLE audit_writer LOGIN PASSWORD 'auditw';
CREATE ROLE audit_reader LOGIN PASSWORD 'auditro';

-- Let roles connect to the application DB
GRANT CONNECT ON DATABASE hpc_app TO app_rw, audit_writer, audit_reader;

-- Schema-level permissions
GRANT USAGE, CREATE ON SCHEMA public TO app_rw;         -- app can create objects
GRANT USAGE ON SCHEMA public TO audit_writer, audit_reader;

-- Existing tables/sequences: give the app standard R/W and let audit roles read where applicable
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw;

-- Default privileges for objects CREATED BY app_rw in the future (most migrations run as app_rw):
ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;

-- Allow the audit writer to read rows it’s chaining to, and insert new ones, by default
ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public
  GRANT SELECT, INSERT ON TABLES TO audit_writer;

-- Allow the audit reader to read tables by default
ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public
  GRANT SELECT ON TABLES TO audit_reader;

-- Sequences created by app_rw in the future (e.g., audit_log_id_seq)
ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_rw, audit_writer, audit_reader;
