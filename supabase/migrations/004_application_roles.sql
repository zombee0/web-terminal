-- Application roles are independent of Supabase project/team membership.
-- Legacy admin_users and deployment email lists are deliberately not imported.
CREATE TABLE web_terminal.app_roles (
    user_id uuid PRIMARY KEY REFERENCES web_terminal.profiles(id),
    role text NOT NULL CHECK (role IN ('admin','owner')),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE web_terminal.role_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id uuid NOT NULL,
    target_id uuid NOT NULL,
    previous_role text NOT NULL CHECK (previous_role IN ('user','admin','owner')),
    new_role text NOT NULL CHECK (new_role IN ('user','admin','owner')),
    source text NOT NULL CHECK (source IN ('bootstrap','dashboard')),
    occurred_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE web_terminal.app_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_terminal.role_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON web_terminal.app_roles,web_terminal.role_events FROM PUBLIC,anon,authenticated;

CREATE FUNCTION web_terminal.application_role(uid uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT coalesce((SELECT r.role FROM web_terminal.app_roles r WHERE r.user_id=uid
        AND web_terminal.verified_google_email(uid) IS NOT NULL),'user');
$$;

-- Only this operation may change roles through the runtime connection. Resolve
-- the actor from the authenticated server session; never accept a browser actor.
CREATE FUNCTION web_terminal.change_application_role(session_hash text, target uuid, next_role text, expected_role text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; previous text;
BEGIN
    PERFORM pg_advisory_xact_lock(793105732);
    SELECT s.user_id INTO actor FROM web_terminal.sessions s
      WHERE s.id=session_hash AND s.revoked_at IS NULL AND s.expires_at>now()
        AND web_terminal.auth_session_valid(s.auth_session_id,s.user_id);
    IF actor IS NULL OR web_terminal.application_role(actor)<>'owner' THEN
        RAISE EXCEPTION 'Owner access required' USING ERRCODE='42501';
    END IF;
    IF next_role IS NULL OR next_role NOT IN ('user','admin','owner')
       OR expected_role IS NULL OR expected_role NOT IN ('user','admin','owner') THEN
        RAISE EXCEPTION 'Invalid role' USING ERRCODE='22023';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM web_terminal.profiles WHERE id=target) THEN
        RAISE EXCEPTION 'User not found' USING ERRCODE='P0002';
    END IF;
    IF next_role<>'user' AND web_terminal.verified_google_email(target) IS NULL THEN
        RAISE EXCEPTION 'A verified Google account is required' USING ERRCODE='22023';
    END IF;
    SELECT coalesce((SELECT role FROM web_terminal.app_roles WHERE user_id=target),'user') INTO previous;
    IF previous<>expected_role THEN
        RAISE EXCEPTION 'Role changed; refresh and try again' USING ERRCODE='P0001';
    END IF;
    -- Count usable owners, so a disabled/unverified account is not a fallback.
    IF previous='owner' AND next_role<>'owner' AND NOT EXISTS(
        SELECT 1 FROM web_terminal.app_roles WHERE role='owner' AND user_id<>target
        AND web_terminal.verified_google_email(user_id) IS NOT NULL) THEN
        RAISE EXCEPTION 'The last owner cannot be removed' USING ERRCODE='P0001';
    END IF;
    IF previous=next_role THEN RETURN next_role; END IF;
    IF next_role='user' THEN
        DELETE FROM web_terminal.app_roles WHERE user_id=target;
    ELSE
        INSERT INTO web_terminal.app_roles(user_id,role) VALUES(target,next_role)
        ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,updated_at=now();
    END IF;
    INSERT INTO web_terminal.role_events(actor_id,target_id,previous_role,new_role,source)
        VALUES(actor,target,previous,next_role,'dashboard');
    RETURN next_role;
END;
$$;
REVOKE ALL ON FUNCTION web_terminal.application_role(uuid),
    web_terminal.change_application_role(text,uuid,text,text) FROM PUBLIC,anon,authenticated;
COMMENT ON TABLE web_terminal.admin_users IS 'Obsolete grants; never used for application authorization.';
