-- Guest ports a Box owner exposed publicly, without authentication. Port 3001
-- is the terminal and stays behind owner sign-in.
CREATE TABLE web_terminal.box_ports (
    box_id uuid NOT NULL REFERENCES web_terminal.boxes(id),
    port integer NOT NULL CHECK (port BETWEEN 1 AND 65535 AND port <> 3001),
    url text NOT NULL CHECK (url LIKE 'https://%'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (box_id, port)
);
ALTER TABLE web_terminal.box_ports ENABLE ROW LEVEL SECURITY;
