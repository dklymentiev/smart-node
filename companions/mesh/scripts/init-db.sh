#!/bin/bash
# Create the mesh_app role used by the application.
# This script runs once during initial PostgreSQL setup (docker-entrypoint-initdb.d).
# The application also creates this role idempotently at startup if MESH_APP_PASSWORD is set.

set -e

MESH_APP_PASSWORD="${MESH_APP_PASSWORD:-}"

if [ -z "$MESH_APP_PASSWORD" ]; then
    echo "MESH_APP_PASSWORD not set -- skipping mesh_app role creation"
    exit 0
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mesh_app') THEN
            CREATE ROLE mesh_app LOGIN PASSWORD '${MESH_APP_PASSWORD}';
        END IF;
    END
    \$\$;

    GRANT ALL ON ALL TABLES IN SCHEMA public TO mesh_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mesh_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO mesh_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO mesh_app;
EOSQL

echo "mesh_app role created/verified"
