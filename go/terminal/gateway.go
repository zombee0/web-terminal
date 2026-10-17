package terminal

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// Identity is the unprivileged PTY identity, separate from the protected listener.
type Identity struct {
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
}

type GatewayConfig struct {
	BoxID     string   `json:"box_id"`
	PublicKey string   `json:"public_key"`
	Identity  Identity `json:"identity"`
}

type grant struct {
	Box    string `json:"box"`
	Method string `json:"method"`
	Path   string `json:"path"`
	Expiry int64  `json:"exp"`
	Nonce  string `json:"nonce"`
}

// ProtectedEnvironment never inherits deployment or platform credentials.
func ProtectedEnvironment() []string {
	return []string{
		"HOME=/home/boxlite", "USER=boxlite", "LOGNAME=boxlite", "SHELL=/bin/bash",
		"TERM=xterm-256color", "COLORTERM=truecolor", "LANG=C.UTF-8",
		"LC_ALL=C.UTF-8", "LC_CTYPE=C.UTF-8",
		"PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
	}
}

// GatewayGuard verifies authorization on the guest, even if its address is known.
// The health endpoint returns only a constant and is used by guest provisioning.
func GatewayGuard(config GatewayConfig, next http.Handler) (http.Handler, error) {
	key, err := base64.RawURLEncoding.DecodeString(config.PublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize || config.BoxID == "" {
		return nil, errors.New("invalid gateway verification configuration")
	}
	seen := make(map[string]int64)
	var mu sync.Mutex
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.URL.Path == "/healthz" && r.Method == http.MethodGet {
			next.ServeHTTP(w, r)
			return
		}
		deny := func() { http.Error(w, "access denied", http.StatusForbidden) }
		raw := r.Header.Get("X-Web-Terminal-Grant")
		if len(raw) > 2048 || r.URL.RawQuery != "" {
			deny()
			return
		}
		parts := strings.Split(raw, ".")
		if len(parts) != 2 {
			deny()
			return
		}
		payload, e1 := base64.RawURLEncoding.DecodeString(parts[0])
		signature, e2 := base64.RawURLEncoding.DecodeString(parts[1])
		if e1 != nil || e2 != nil || !ed25519.Verify(key, payload, signature) {
			deny()
			return
		}
		var g grant
		now := time.Now().Unix()
		if json.Unmarshal(payload, &g) != nil || g.Box != config.BoxID ||
			g.Method != r.Method || g.Path != r.URL.EscapedPath() ||
			g.Expiry <= now || g.Expiry > now+30 || len(g.Nonce) < 20 || len(g.Nonce) > 64 {
			deny()
			return
		}
		mu.Lock()
		for nonce, expiry := range seen {
			if expiry <= now {
				delete(seen, nonce)
			}
		}
		_, replay := seen[g.Nonce]
		full := len(seen) >= 4096
		if !replay && !full {
			seen[g.Nonce] = g.Expiry
		}
		mu.Unlock()
		if replay || full {
			deny()
			return
		}
		r.Header.Del("X-Web-Terminal-Grant")
		next.ServeHTTP(w, r)
	}), nil
}

// ConfiguredHandler preserves standalone behavior unless protection is explicitly set.
func ConfiguredHandler() (http.Handler, error) {
	path := os.Getenv("WEB_TERMINAL_GATEWAY_CONFIG")
	if path == "" {
		return Handler(), nil
	}
	if os.Geteuid() != 0 {
		return nil, errors.New("protected listener requires root")
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0022 != 0 {
		return nil, errors.New("gateway configuration must be a protected regular file")
	}
	if err := verifyConfigOwner(info); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, errors.New("cannot read gateway configuration")
	}
	var config GatewayConfig
	if json.Unmarshal(data, &config) != nil || config.Identity.UID == 0 || config.Identity.GID == 0 {
		return nil, errors.New("invalid unprivileged terminal identity")
	}
	return GatewayGuard(config, handler(&config.Identity))
}
