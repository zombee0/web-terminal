CREATE SCHEMA IF NOT EXISTS web_terminal;
REVOKE ALL ON SCHEMA web_terminal FROM PUBLIC, anon, authenticated;

CREATE TABLE IF NOT EXISTS web_terminal.profiles (
    id uuid PRIMARY KEY REFERENCES auth.users(id),
    email text NOT NULL,
    display_name text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS web_terminal.login_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_event_id uuid UNIQUE,
    user_id uuid,
    action text NOT NULL,
    source text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    occurred_at timestamptz NOT NULL DEFAULT now(),
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_events_user_time ON web_terminal.login_events(user_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS web_terminal.sessions (
    id text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES web_terminal.profiles(id),
    auth_session_id uuid NOT NULL,
    access_token text NOT NULL,
    refresh_token text NOT NULL,
    access_expires_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS sessions_user ON web_terminal.sessions(user_id);

CREATE TABLE IF NOT EXISTS web_terminal.oauth_flows (
    id text PRIMARY KEY,
    verifier text NOT NULL,
    next_path text NOT NULL DEFAULT '/',
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS web_terminal.boxes (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES web_terminal.profiles(id),
    name text NOT NULL,
    provider_name text NOT NULL UNIQUE,
    provider_id text UNIQUE,
    cpus integer NOT NULL DEFAULT 2 CHECK (cpus = 2),
    memory_mib integer NOT NULL DEFAULT 4096 CHECK (memory_mib = 4096),
    disk_size_gb integer NOT NULL DEFAULT 10 CHECK (disk_size_gb = 10),
    status text NOT NULL CHECK (status IN ('creating','starting','running','stopping','stopped','deleting','error','deleted')),
    desired_state text NOT NULL DEFAULT 'running' CHECK (desired_state IN ('running','stopped','deleted')),
    provisioned boolean NOT NULL DEFAULT false,
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_box_per_account ON web_terminal.boxes(owner_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS web_terminal.jobs (
    id uuid PRIMARY KEY,
    box_id uuid NOT NULL REFERENCES web_terminal.boxes(id),
    owner_id uuid NOT NULL REFERENCES web_terminal.profiles(id),
    operation text NOT NULL CHECK (operation IN ('create','resume','stop','delete')),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed')),
    idempotency_key text,
    attempts integer NOT NULL DEFAULT 0,
    lease_owner uuid,
    lease_until timestamptz,
    run_after timestamptz NOT NULL DEFAULT now(),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_box ON web_terminal.jobs(box_id) WHERE status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS job_idempotency ON web_terminal.jobs(owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_claim ON web_terminal.jobs(status, run_after, lease_until);

CREATE TABLE IF NOT EXISTS web_terminal.lifecycle_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    box_id uuid NOT NULL REFERENCES web_terminal.boxes(id),
    actor_id uuid,
    job_id uuid REFERENCES web_terminal.jobs(id),
    event text NOT NULL,
    from_state text,
    to_state text,
    detail jsonb NOT NULL DEFAULT '{}',
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lifecycle_box_time ON web_terminal.lifecycle_events(box_id, id);

CREATE TABLE IF NOT EXISTS web_terminal.cursors (
    name text PRIMARY KEY,
    position timestamptz NOT NULL
);

-- This private schema is intentionally absent from the browser's Data API.
-- Backend queries must still qualify every user resource with its owner ID.
REVOKE ALL ON ALL TABLES IN SCHEMA web_terminal FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA web_terminal FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA web_terminal REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA web_terminal REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated;

ALTER TABLE web_terminal.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.boxes ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.lifecycle_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.login_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.oauth_flows ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.cursors ENABLE ROW LEVEL SECURITY;

-- Narrow definer functions avoid granting runtime access to Auth token tables.
CREATE OR REPLACE FUNCTION web_terminal.auth_session_valid(session_id uuid, owner_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS(SELECT 1 FROM auth.sessions s WHERE s.id=session_id AND s.user_id=owner_id
        AND (s.not_after IS NULL OR s.not_after>now()));
$$;

CREATE OR REPLACE FUNCTION web_terminal.ingest_auth_audit()
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE count_inserted integer;
BEGIN
    INSERT INTO web_terminal.login_events(auth_event_id,user_id,action,source,metadata,occurred_at)
    SELECT a.id,
        CASE WHEN (a.payload->>'actor_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             THEN (a.payload->>'actor_id')::uuid ELSE NULL END,
        left(coalesce(a.payload->>'action','unknown'),128), 'supabase_auth',
        jsonb_build_object('provider',left(a.payload->>'provider',64)), a.created_at
    FROM auth.audit_log_entries a
    WHERE a.created_at >= coalesce((SELECT position - interval '1 day' FROM web_terminal.cursors WHERE name='auth_audit'),'1970-01-01'::timestamptz)
      AND NOT EXISTS(SELECT 1 FROM web_terminal.login_events e WHERE e.auth_event_id=a.id)
    ORDER BY a.created_at,a.id LIMIT 1000
    ON CONFLICT(auth_event_id) DO NOTHING;
    GET DIAGNOSTICS count_inserted = ROW_COUNT;
    INSERT INTO web_terminal.cursors(name,position)
    SELECT 'auth_audit',coalesce(max(occurred_at),'1970-01-01'::timestamptz)
      FROM web_terminal.login_events WHERE source='supabase_auth'
    ON CONFLICT(name) DO UPDATE SET position=greatest(web_terminal.cursors.position,excluded.position);
    RETURN count_inserted;
END;
$$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA web_terminal FROM PUBLIC, anon, authenticated;
