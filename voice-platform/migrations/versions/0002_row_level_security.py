"""Row-level security, tenant-context helpers, narrow cross-tenant lookup functions,
append-only audit log and grants for the runtime role.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import os

from alembic import op

from voice_platform.core.models import TENANT_TABLES

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("VP_DB_APP_ROLE", "vp_app")


def _run(sql: str) -> None:
    """Execute statements separated by lines containing only ``--;`` (asyncpg runs one
    statement per call)."""
    for stmt in sql.split("\n--;\n"):
        if stmt.strip():
            op.execute(stmt)


def upgrade() -> None:
    _run(
        """
        CREATE OR REPLACE FUNCTION vp_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
        $$;
--;
        CREATE OR REPLACE FUNCTION vp_is_platform() RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT coalesce(current_setting('app.platform_admin', true), '') = 'on'
        $$;
--;
        CREATE OR REPLACE FUNCTION vp_current_user() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT nullif(current_setting('app.user_id', true), '')::uuid
        $$;
--;
        """
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = vp_current_tenant() OR vp_is_platform())
            WITH CHECK (tenant_id = vp_current_tenant() OR vp_is_platform())
            """
        )

    # tenants: a tenant context sees only itself; platform admins see all
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    _run(
        """
        CREATE POLICY tenant_self ON tenants
        USING (id = vp_current_tenant() OR vp_is_platform())
        WITH CHECK (vp_is_platform())
        """
    )

    # users: self, members of the current tenant, or platform admin
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    _run(
        """
        CREATE POLICY user_visibility ON users
        USING (
            vp_is_platform()
            OR id = vp_current_user()
            OR EXISTS (SELECT 1 FROM memberships m
                       WHERE m.user_id = users.id AND m.tenant_id = vp_current_tenant())
        )
        WITH CHECK (vp_is_platform() OR id = vp_current_user())
        """
    )

    # audit_logs: tenant rows readable by tenant; platform rows by platform admin only
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY")
    _run(
        """
        CREATE POLICY audit_read ON audit_logs FOR SELECT
        USING (vp_is_platform() OR (tenant_id IS NOT NULL AND tenant_id = vp_current_tenant()));
--;
        CREATE POLICY audit_insert ON audit_logs FOR INSERT
        WITH CHECK (vp_is_platform() OR tenant_id = vp_current_tenant());
--;
        CREATE OR REPLACE FUNCTION vp_audit_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only';
        END $$;
--;
        CREATE TRIGGER audit_logs_immutable BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION vp_audit_immutable();
--;
        """
    )

    # jobs: tenant sees own jobs; the job runner works in platform scope
    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    _run(
        """
        CREATE POLICY job_access ON jobs
        USING (vp_is_platform() OR tenant_id = vp_current_tenant())
        WITH CHECK (vp_is_platform() OR tenant_id = vp_current_tenant())
        """
    )

    # Narrow lookups that legitimately cross tenants. Each returns only what the caller needs
    # and elevates visibility only for the duration of the call (restored before returning;
    # a failing call aborts the transaction or subtransaction, which also reverts it).
    _run(
        """
        CREATE OR REPLACE FUNCTION vp_auth_find_user(p_email text)
        RETURNS TABLE (id uuid, password_hash text, disabled boolean, is_platform_admin boolean)
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE prev text := coalesce(current_setting('app.platform_admin', true), '');
        BEGIN
            -- elevate visibility for this lookup only, then restore the caller's scope
            PERFORM set_config('app.platform_admin', 'on', true);
            RETURN QUERY SELECT u.id, u.password_hash, u.disabled, u.is_platform_admin
            FROM users u WHERE u.email = lower(p_email);
            PERFORM set_config('app.platform_admin', prev, true);
            RETURN;
        END $$;
--;

        CREATE OR REPLACE FUNCTION vp_auth_find_api_key(p_prefix text)
        RETURNS TABLE (id uuid, tenant_id uuid, key_hash text, role text,
                       expires_at timestamptz, revoked_at timestamptz)
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE prev text := coalesce(current_setting('app.platform_admin', true), '');
        BEGIN
            -- elevate visibility for this lookup only, then restore the caller's scope
            PERFORM set_config('app.platform_admin', 'on', true);
            RETURN QUERY SELECT k.id, k.tenant_id, k.key_hash, k.role, k.expires_at, k.revoked_at
            FROM api_keys k JOIN tenants t ON t.id = k.tenant_id
            WHERE k.prefix = p_prefix AND t.status = 'active';
            PERFORM set_config('app.platform_admin', prev, true);
            RETURN;
        END $$;
--;

        CREATE OR REPLACE FUNCTION vp_user_memberships(p_user uuid)
        RETURNS TABLE (tenant_id uuid, role text, slug text, name text, status text)
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE prev text := coalesce(current_setting('app.platform_admin', true), '');
        BEGIN
            -- elevate visibility for this lookup only, then restore the caller's scope
            PERFORM set_config('app.platform_admin', 'on', true);
            RETURN QUERY SELECT m.tenant_id, m.role, t.slug, t.name, t.status
            FROM memberships m JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = p_user;
            PERFORM set_config('app.platform_admin', prev, true);
            RETURN;
        END $$;
--;

        CREATE OR REPLACE FUNCTION vp_route_inbound_number(p_e164 text)
        RETURNS TABLE (tenant_id uuid, agent_id uuid, environment text, phone_number_id uuid)
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE prev text := coalesce(current_setting('app.platform_admin', true), '');
        BEGIN
            -- elevate visibility for this lookup only, then restore the caller's scope
            PERFORM set_config('app.platform_admin', 'on', true);
            RETURN QUERY SELECT p.tenant_id, p.agent_id, p.environment, p.id
            FROM phone_numbers p JOIN tenants t ON t.id = p.tenant_id
            WHERE p.e164 = p_e164 AND p.status = 'active' AND t.status = 'active';
            PERFORM set_config('app.platform_admin', prev, true);
            RETURN;
        END $$;
--;

        CREATE OR REPLACE FUNCTION vp_find_conversation_tenant(p_conversation uuid)
        RETURNS uuid
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE prev text := coalesce(current_setting('app.platform_admin', true), '');
    result uuid;
        BEGIN
            -- elevate visibility for this lookup only, then restore the caller's scope
            PERFORM set_config('app.platform_admin', 'on', true);
            result := (SELECT tenant_id FROM conversations WHERE id = p_conversation);
            PERFORM set_config('app.platform_admin', prev, true);
            RETURN result;
        END $$;
--;
        """
    )

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
                         TO {APP_ROLE}';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {APP_ROLE}';
                EXECUTE 'REVOKE UPDATE, DELETE ON audit_logs FROM {APP_ROLE}';
                EXECUTE 'REVOKE ALL ON alembic_version FROM {APP_ROLE}';
                EXECUTE 'GRANT SELECT ON alembic_version TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    _run(
        """
        DROP FUNCTION IF EXISTS vp_find_conversation_tenant(uuid);
--;
        DROP FUNCTION IF EXISTS vp_route_inbound_number(text);
--;
        DROP FUNCTION IF EXISTS vp_user_memberships(uuid);
--;
        DROP FUNCTION IF EXISTS vp_auth_find_api_key(text);
--;
        DROP FUNCTION IF EXISTS vp_auth_find_user(text);
--;
        DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs;
--;
        DROP FUNCTION IF EXISTS vp_audit_immutable();
--;
        DROP POLICY IF EXISTS audit_read ON audit_logs;
--;
        DROP POLICY IF EXISTS audit_insert ON audit_logs;
--;
        DROP POLICY IF EXISTS job_access ON jobs;
--;
        DROP POLICY IF EXISTS user_visibility ON users;
--;
        DROP POLICY IF EXISTS tenant_self ON tenants;
--;
        """
    )
    for table in (*TENANT_TABLES, "tenants", "users", "audit_logs", "jobs"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
