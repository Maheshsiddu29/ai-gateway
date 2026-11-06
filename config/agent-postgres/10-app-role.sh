#!/bin/sh
# Creates the agent runtime's application role when the Compose agent database is
# first initialized. Migrations (python -m agent_runtime.persistence upgrade) grant
# it the exact table privileges it needs; it can never change the schema.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_password="$AGENT_DB_APP_PASSWORD" <<'SQL'
CREATE ROLE agent_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
REVOKE ALL ON DATABASE agent FROM PUBLIC;
GRANT CONNECT ON DATABASE agent TO agent_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
