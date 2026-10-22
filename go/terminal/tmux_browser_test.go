package terminal

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/creack/pty/v2"
	"github.com/gorilla/websocket"
)

func TestMain(m *testing.M) {
	if RunTmuxWrapper() {
		return
	}
	os.Exit(m.Run())
}

// Real PTY and production WebSocket loop, with only shell startup redirected to
// a disposable directory so host tests need neither /home/boxlite nor root.
func browseTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	dir := t.TempDir()
	bin := filepath.Join(dir, "bin")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			_, _ = w.Write([]byte(PageHTML()))
			return
		}
		conn, err := Upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		command := exec.Command("/bin/bash", "--noprofile", "--norc", "-i")
		command.Dir = dir
		command.Env = []string{"PATH=" + os.Getenv("PATH"), "TERM=xterm-256color", "LANG=en_US.UTF-8", "PS1=fixture> ", "HISTFILE=/dev/null"}
		if err := prepareTmuxWrapper(command, bin); err != nil {
			t.Error(err)
			return
		}
		browser, err := newTmuxBrowser(command, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer browser.close()
		ptmx, err := pty.Start(command)
		if err != nil {
			t.Error(err)
			return
		}
		defer ptmx.Close()
		defer func() { _ = command.Process.Kill(); _ = command.Wait() }()
		serveTerminal(conn, ptmx, browser)
	}))
	t.Cleanup(server.Close)
	return server
}

// The wrapper pairs an attached client with its WebSocket and scrolls tmux's
// own copy mode, including over an alternate-screen application.
func TestTmuxBrowseWebSocket(t *testing.T) {
	real, err := exec.LookPath("tmux")
	if err != nil {
		t.Skip("tmux not installed")
	}
	dir := t.TempDir()
	socket := filepath.Join(dir, "tmux.sock")
	config := filepath.Join(dir, "tmux.conf")
	if err := os.WriteFile(config, []byte("set -g history-limit 1000\nset -g status off\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = exec.Command(real, "-S", socket, "kill-server").Run() })
	tmux := func(args ...string) string {
		t.Helper()
		output, err := exec.Command(real, append([]string{"-S", socket}, args...)...).CombinedOutput()
		if err != nil {
			t.Fatalf("tmux %v: %v: %s", args, err, output)
		}
		return strings.TrimSpace(string(output))
	}
	server := browseTestServer(t)
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http")+"/ws", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetReadDeadline(time.Now().Add(20 * time.Second))
	var output strings.Builder
	read := func(match func(kind int, data []byte) bool) []byte {
		t.Helper()
		for {
			kind, data, err := conn.ReadMessage()
			if err != nil {
				t.Fatal(err)
			}
			if kind == websocket.BinaryMessage {
				output.Write(data)
			}
			if match(kind, data) {
				return data
			}
		}
	}
	read(func(kind int, data []byte) bool {
		return kind == websocket.TextMessage && string(data) == `{"tmux":true,"type":"browse-ready"}`
	})
	read(func(int, []byte) bool { return strings.Contains(output.String(), "fixture>") })
	send := func(text string) {
		t.Helper()
		if err := conn.WriteMessage(websocket.BinaryMessage, []byte(text)); err != nil {
			t.Fatal(err)
		}
	}
	id := 0
	browse := func(action string, lines int) browseState {
		t.Helper()
		id++
		request, _ := json.Marshal(browseRequest{Type: "tmux-browse", ID: id, Action: action, Lines: lines})
		if err := conn.WriteMessage(websocket.TextMessage, request); err != nil {
			t.Fatal(err)
		}
		var state browseState
		read(func(kind int, data []byte) bool {
			return kind == websocket.TextMessage && json.Unmarshal(data, &state) == nil && state.Type == "tmux-browse" && state.ID == id
		})
		if state.Error != "" {
			t.Fatalf("%s: %s", action, state.Error)
		}
		return state
	}
	if state := browse("state", 0); state.Available {
		t.Fatal("a plain shell reported a tmux client")
	}
	send("tmux -S " + socket + " -f " + config + " new-session -s fixture '/bin/bash --noprofile --norc -i'\r")
	deadline := time.Now().Add(5 * time.Second)
	for !browse("state", 0).Available {
		if time.Now().After(deadline) {
			t.Fatal("tmux client never paired with the browser")
		}
		time.Sleep(50 * time.Millisecond)
	}
	if env := tmux("show-environment", "-g"); strings.Contains(env, browseSocketEnv) {
		t.Fatal("browse address leaked into the tmux server environment")
	}
	// Print rows, then cover them with a full-screen application.
	tmux("send-keys", "-t", "fixture", "seq -f 'ROW-%04g' 0 199; printf '\\033[?1049hFULL-SCREEN-APP'", "Enter")
	deadline = time.Now().Add(5 * time.Second)
	for !strings.Contains(tmux("capture-pane", "-p", "-t", "fixture"), "FULL-SCREEN-APP") {
		if time.Now().After(deadline) {
			t.Fatal("fixture output missing")
		}
		time.Sleep(50 * time.Millisecond)
	}
	state := browse("enter", 0)
	if !state.Active || tmux("display-message", "-p", "-t", "fixture", "#{pane_mode}") != "copy-mode" {
		t.Fatalf("enter: %+v", state)
	}
	state = browse("scroll", -1000)
	if state.Position != state.History || state.History == 0 {
		t.Fatalf("scroll to oldest row: %+v", state)
	}
	// Scrolling past the oldest row stays in copy mode at the top.
	if again := browse("scroll", -5); !again.Active || again.Position != state.History {
		t.Fatalf("scroll past top: %+v", again)
	}
	if state = browse("scroll", 3); state.Position != state.History-3 {
		t.Fatalf("scroll down: %+v", state)
	}
	if state = browse("leave", 0); state.Active || tmux("display-message", "-p", "-t", "fixture", "#{pane_in_mode}") != "0" {
		t.Fatalf("leave: %+v", state)
	}
	// A disconnected browser cancels the copy mode it started.
	browse("enter", 0)
	conn.Close()
	deadline = time.Now().Add(5 * time.Second)
	for tmux("display-message", "-p", "-t", "fixture", "#{pane_in_mode}") != "0" {
		if time.Now().After(deadline) {
			t.Fatal("copy mode survived the browser")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestNativeTmuxBrowser(t *testing.T) {
	if os.Getenv("WT_TEST_BROWSER") != "1" {
		t.Skip("run make test TEST_TARGETS=test-terminal-native")
	}
	server := browseTestServer(t)
	command := exec.Command(os.Getenv("WT_TEST_PYTHON"), "../../scripts/test_terminal_native.py", server.URL)
	output, err := command.CombinedOutput()
	t.Log(string(output))
	if err != nil {
		t.Fatal(err)
	}
}
