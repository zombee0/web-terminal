-- Per-Box choice to install the Antigravity CLI (agy) for Google's free Gemini tier.
ALTER TABLE web_terminal.boxes ADD COLUMN agy_trial boolean NOT NULL DEFAULT false;
