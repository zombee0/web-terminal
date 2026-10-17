package test

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/zombee0/web-terminal/terminal"
)

func TestGatewayAuthorizationAndReplay(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	guard, err := terminal.GatewayGuard(terminal.GatewayConfig{BoxID: "box-a", PublicKey: base64.RawURLEncoding.EncodeToString(pub)}, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Web-Terminal-Grant") != "" {
			t.Error("grant reached application handler")
		}
		w.WriteHeader(204)
	}))
	if err != nil {
		t.Fatal(err)
	}
	sign := func(box, method, path string, expiry int64, nonce string) string {
		data, _ := json.Marshal(map[string]any{"box": box, "method": method, "path": path, "exp": expiry, "nonce": nonce})
		return base64.RawURLEncoding.EncodeToString(data) + "." + base64.RawURLEncoding.EncodeToString(ed25519.Sign(priv, data))
	}
	now := time.Now().Unix()
	nonce := "unique-nonce-for-each-request"
	for name, token := range map[string]string{
		"missing": "", "malformed": "broken", "bad-signature": "e30.AA",
		"wrong-box":      sign("box-b", "GET", "/ws", now+20, nonce),
		"wrong-method":   sign("box-a", "POST", "/ws", now+20, nonce),
		"wrong-path":     sign("box-a", "GET", "/", now+20, nonce),
		"expired":        sign("box-a", "GET", "/ws", now-1, nonce),
		"too-long-lived": sign("box-a", "GET", "/ws", now+3600, nonce),
		"empty-nonce":    sign("box-a", "GET", "/ws", now+20, ""),
	} {
		t.Run(name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/ws", nil)
			r.Header.Set("X-Web-Terminal-Grant", token)
			w := httptest.NewRecorder()
			guard.ServeHTTP(w, r)
			if w.Code != 403 {
				t.Fatalf("status %d", w.Code)
			}
		})
	}
	valid := sign("box-a", "GET", "/ws", now+20, nonce)
	var accepted atomic.Int32
	var wg sync.WaitGroup
	for range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r := httptest.NewRequest("GET", "/ws", nil)
			r.Header.Set("X-Web-Terminal-Grant", valid)
			w := httptest.NewRecorder()
			guard.ServeHTTP(w, r)
			if w.Code == 204 {
				accepted.Add(1)
			}
		}()
	}
	wg.Wait()
	if accepted.Load() != 1 {
		t.Fatalf("accepted a grant %d times", accepted.Load())
	}
}

func TestProtectedEnvironmentDoesNotInheritSecrets(t *testing.T) {
	t.Setenv("BOXLITE_API_KEY", "must-not-reach-shell")
	t.Setenv("SUPABASE_SECRET_KEY", "must-not-reach-shell")
	t.Setenv("UNRECOGNIZED_SECRET", "must-not-reach-shell")
	got := strings.Join(terminal.ProtectedEnvironment(), "\n")
	if strings.Contains(got, "must-not-reach-shell") {
		t.Fatal("credential inherited")
	}
	if !strings.Contains(got, "USER=boxlite") {
		t.Fatal("missing unprivileged user")
	}
}

func TestInvalidGatewayConfigFailsClosed(t *testing.T) {
	t.Setenv("WEB_TERMINAL_GATEWAY_CONFIG", "/nonexistent/gateway-config")
	if _, err := terminal.ConfiguredHandler(); err == nil {
		t.Fatal("invalid config enabled standalone access")
	}
}
