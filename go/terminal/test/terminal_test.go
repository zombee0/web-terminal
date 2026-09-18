package test

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/zombee0/web-terminal/terminal"
)

func TestTerminalEnvironment(t *testing.T) {
	t.Setenv("TMUX", "stale-session")
	values := strings.Join(terminal.Environment(), "\n")
	for _, expected := range []string{"HOME=/home/boxlite", "LANG=C.UTF-8", "PATH=/home/boxlite/.local/bin:"} {
		if !strings.Contains(values, expected) {
			t.Errorf("missing %s", expected)
		}
	}
	if strings.Contains(values, "TMUX=") {
		t.Error("inherited TMUX must not reach a new terminal")
	}
}

func TestRejectCrossOriginWebSocket(t *testing.T) {
	r := httptest.NewRequest("GET", "http://terminal.example/ws", nil)
	r.Header.Set("Connection", "Upgrade")
	r.Header.Set("Upgrade", "websocket")
	r.Header.Set("Sec-WebSocket-Version", "13")
	r.Header.Set("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
	r.Header.Set("Origin", "https://untrusted.example")
	w := httptest.NewRecorder()
	_, err := terminal.Upgrader.Upgrade(w, r, nil)
	if err == nil || w.Code != 403 {
		t.Fatalf("cross-origin upgrade: status=%d error=%v", w.Code, err)
	}
}
