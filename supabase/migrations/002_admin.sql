-- Administrator membership is managed only by the deployment/database owner.
CREATE TABLE web_terminal.admin_users (
    user_id uuid PRIMARY KEY REFERENCES web_terminal.profiles(id),
    granted_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON web_terminal.admin_users FROM PUBLIC, anon, authenticated;
ALTER TABLE web_terminal.admin_users ENABLE ROW LEVEL SECURITY;

CREATE INDEX boxes_created_at ON web_terminal.boxes(created_at DESC,id DESC);
CREATE INDEX profiles_last_login ON web_terminal.profiles(last_login_at DESC,id DESC);
CREATE INDEX jobs_created_at ON web_terminal.jobs(created_at DESC,id DESC);
CREATE INDEX lifecycle_recent ON web_terminal.lifecycle_events(occurred_at DESC,id DESC);
CREATE INDEX login_events_recent ON web_terminal.login_events(occurred_at DESC,id DESC);
