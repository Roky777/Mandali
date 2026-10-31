-- Run as the Cloud SQL administrator inside the mandali database.
-- Create SQL roles directly so they do not inherit cloudsqlsuperuser.
-- Set login passwords afterwards through the Cloud SQL UI and Secret Manager.
CREATE ROLE mandali_app LOGIN;
CREATE ROLE mandali_migrator LOGIN;
-- Apply within the mandali database, BEFORE the migration job.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO mandali_migrator;
GRANT CONNECT ON DATABASE mandali TO mandali_app, mandali_migrator;
GRANT USAGE ON SCHEMA public TO mandali_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mandali_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mandali_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mandali_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mandali_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mandali_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO mandali_app;
