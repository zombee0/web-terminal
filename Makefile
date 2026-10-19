PYTHON ?= .venv/bin/python
GO ?= go
TEST_TARGETS ?= test-python test-go test-supabase

.PHONY: test test-python test-go test-terminal-ui test-terminal-apps test-terminal-tmux test-supabase test-supabase-db test-supabase-live test-supabase-recovery test-supabase-admin-live test-supabase-ui build bootstrap
test: $(TEST_TARGETS)

test-python:
	$(PYTHON) -m unittest discover -s hangar/test -v

test-go:
	$(GO) -C go test -race ./...

test-terminal-ui:
	mkdir -p build/terminal-ui
	$(GO) -C go run . --print-page > build/terminal-ui/terminal.html
	$(PYTHON) scripts/test_terminal_ui.py

test-terminal-apps:
	mkdir -p build/terminal-ui
	$(GO) -C go run . --print-page > build/terminal-ui/terminal.html
	$(PYTHON) scripts/test_terminal_apps.py

test-terminal-tmux:
	mkdir -p build/terminal-ui
	$(GO) -C go run . --print-page > build/terminal-ui/terminal.html
	$(PYTHON) scripts/test_terminal_tmux.py

test-supabase:
	$(PYTHON) -m unittest discover -s supabase/tests -v

test-supabase-db:
	WT_TEST_DATABASE=1 $(PYTHON) -m unittest discover -s supabase/tests -v

test-supabase-live:
	$(PYTHON) -m supabase.live_check

test-supabase-recovery:
	$(PYTHON) -m supabase.recovery_check

test-supabase-admin-live:
	$(PYTHON) -m supabase.admin_check

test-supabase-ui:
	PYTHONPATH=. $(PYTHON) supabase/tests/admin_ui_check.py
	PYTHONPATH=. $(PYTHON) supabase/tests/hosted_ui_check.py

build:
	bash scripts/build_web_terminal_binary.sh

bootstrap:
	bash scripts/bootstrap_host.sh
