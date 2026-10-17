-- Return only the current email of a verified Google identity, without exposing
-- Auth token tables to the application database role.
CREATE OR REPLACE FUNCTION web_terminal.verified_google_email(owner_id uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT u.email FROM auth.users u
    WHERE u.id=owner_id AND u.email_confirmed_at IS NOT NULL
      AND EXISTS(SELECT 1 FROM auth.identities i WHERE i.user_id=u.id AND i.provider='google'
          AND lower(i.identity_data->>'email')=lower(u.email)
          AND i.identity_data->>'email_verified'='true');
$$;
REVOKE ALL ON FUNCTION web_terminal.verified_google_email(uuid) FROM PUBLIC, anon, authenticated;

COMMENT ON TABLE web_terminal.admin_users IS 'Legacy administrator grants; no longer used. Access is configured in the private deployment file.';
