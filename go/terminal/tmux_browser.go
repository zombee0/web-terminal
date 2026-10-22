package terminal

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const browseSocketEnv = "WEB_TERMINAL_BROWSE_SOCKET"

type browseRequest struct {
	Type   string `json:"type"`
	ID     int    `json:"id"`
	Action string `json:"action"`
	Lines  int    `json:"lines"`
}

type browseState struct {
	Type      string `json:"type"`
	ID        int    `json:"id"`
	Available bool   `json:"available"`
	Active    bool   `json:"active"`
	Position  int    `json:"position"`
	History   int    `json:"history"`
	Error     string `json:"error,omitempty"`
	pane      string
	mode      string
}

// One private bridge per WebSocket. Commands execute in the attached tmux
// wrapper, with the terminal user's identity and its exact server/client.
type tmuxBrowser struct {
	mu       sync.Mutex
	listener net.Listener
	conn     net.Conn
	decoder  *json.Decoder
	dir      string
	closed   bool
}

func newTmuxBrowser(command *exec.Cmd, identity *Identity) (*tmuxBrowser, error) {
	// Keep Unix socket paths short on macOS as well as Linux.
	dir, err := os.MkdirTemp("/tmp", "wt-browse-")
	if err != nil {
		return nil, err
	}
	b := &tmuxBrowser{dir: dir}
	path := filepath.Join(dir, "control")
	b.listener, err = net.Listen("unix", path)
	if err == nil {
		err = os.Chmod(path, 0600)
	}
	if err == nil && identity != nil {
		err = os.Chown(dir, int(identity.UID), int(identity.GID))
		if err == nil {
			err = os.Chown(path, int(identity.UID), int(identity.GID))
		}
	}
	if err != nil {
		b.close()
		return nil, err
	}
	command.Env = setEnv(command.Env, browseSocketEnv, path)
	go func() {
		for {
			conn, err := b.listener.Accept()
			if err != nil {
				return
			}
			b.mu.Lock()
			if b.closed {
				conn.Close()
				b.mu.Unlock()
				return
			}
			if b.conn != nil {
				b.conn.Close()
			}
			b.conn, b.decoder = conn, json.NewDecoder(conn)
			b.mu.Unlock()
		}
	}()
	return b, nil
}

func (b *tmuxBrowser) close() {
	if b == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return
	}
	b.closed = true
	if b.listener != nil {
		b.listener.Close()
	}
	if b.conn != nil {
		// Finish cleanup while the wrapper and its PTY are still alive. A
		// subsequent SIGHUP can kill the wrapper before Go defers get to run.
		b.conn.SetDeadline(time.Now().Add(2 * time.Second))
		if json.NewEncoder(b.conn).Encode(browseRequest{Action: "close"}) == nil {
			var state browseState
			b.decoder.Decode(&state)
		}
		b.conn.Close()
	}
	os.RemoveAll(b.dir)
}

func (b *tmuxBrowser) request(request browseRequest) browseState {
	state := browseState{Type: "tmux-browse", ID: request.ID}
	switch request.Action {
	case "state", "enter", "leave":
	case "scroll":
		if request.Lines < -1000 || request.Lines > 1000 {
			state.Error = "Scroll distance is too large."
			return state
		}
	default:
		state.Error = "Unknown browse action."
		return state
	}
	if b == nil {
		return state
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.conn == nil {
		return state // Ordinary shells continue to use the saved-output reader.
	}
	b.conn.SetDeadline(time.Now().Add(3 * time.Second))
	err := json.NewEncoder(b.conn).Encode(request)
	if err == nil {
		err = b.decoder.Decode(&state)
	}
	state.Type, state.ID = "tmux-browse", request.ID
	if err != nil {
		b.conn.Close()
		b.conn, b.decoder = nil, nil
		state = browseState{Type: "tmux-browse", ID: request.ID, Error: "Terminal browsing disconnected. Try again."}
	}
	return state
}

// The wrapper owns this connection and cancels its own copy mode on disconnect.
// No shortcut keys or commands are ever injected into the application input.
func startTmuxBrowser(options []string, clientPID int) func() {
	path := os.Getenv(browseSocketEnv)
	if path == "" {
		return func() {}
	}
	conn, err := net.DialTimeout("unix", path, time.Second)
	if err != nil {
		return func() {}
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer conn.Close()
		client := tmuxBrowseClient{options: options, pid: clientPID}
		defer client.leave()
		decoder, encoder := json.NewDecoder(conn), json.NewEncoder(conn)
		for {
			var request browseRequest
			if decoder.Decode(&request) != nil {
				return
			}
			state, err := client.apply(request)
			state.Type, state.ID = "tmux-browse", request.ID
			if err != nil {
				state.Error = "Could not update tmux view. Try again."
			}
			if encoder.Encode(state) != nil {
				return
			}
		}
	}()
	return func() { conn.Close(); <-done }
}

type tmuxBrowseClient struct {
	options []string
	pid     int
	owned   string
}

func (c *tmuxBrowseClient) run(args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, os.Getenv("WEB_TERMINAL_TMUX"), append(append([]string{}, c.options...), args...)...)
	output, err := command.Output()
	return strings.TrimSpace(string(output)), err
}

func (c *tmuxBrowseClient) state() (browseState, error) {
	text, err := c.run("list-clients", "-F", "#{client_pid}|#{pane_id}|#{pane_mode}|#{scroll_position}|#{history_size}")
	if err != nil {
		// The client may still be starting its server, or has just exited.
		return browseState{}, nil
	}
	for _, line := range strings.Split(text, "\n") {
		fields := strings.Split(line, "|")
		if len(fields) != 5 || fields[0] != strconv.Itoa(c.pid) {
			continue
		}
		position, _ := strconv.Atoi(fields[3])
		history, _ := strconv.Atoi(fields[4])
		return browseState{Available: true, Active: fields[2] == "copy-mode",
			Position: position, History: history, pane: fields[1], mode: fields[2]}, nil
	}
	return browseState{}, nil
}

func (c *tmuxBrowseClient) leave() {
	if c.owned != "" {
		mode, err := c.run("display-message", "-p", "-t", c.owned, "#{pane_mode}")
		if err == nil && mode == "copy-mode" {
			// -X never falls through to application input, even if the mode ended.
			c.run("send-keys", "-X", "-t", c.owned, "cancel")
		}
		c.owned = ""
	}
}

func (c *tmuxBrowseClient) apply(request browseRequest) (browseState, error) {
	if request.Action == "close" {
		c.leave()
		return browseState{}, nil
	}
	state, err := c.state()
	if err != nil {
		return state, err
	}
	if c.owned != "" && c.owned != state.pane {
		c.leave()
	}
	if !state.Available {
		return state, nil
	}
	switch request.Action {
	case "state":
		return state, nil
	case "enter":
		if state.mode != "" && !state.Active {
			return state, errors.New("another tmux mode is active")
		}
		if _, err = c.run("copy-mode", "-t", state.pane); err == nil {
			c.owned = state.pane
		}
	case "leave":
		if state.Active {
			_, err = c.run("send-keys", "-X", "-t", state.pane, "cancel")
		}
		c.owned = ""
	case "scroll":
		if !state.Active {
			return state, nil
		}
		lines := request.Lines
		if lines == 0 || lines < -1000 || lines > 1000 {
			return state, nil
		}
		direction := "scroll-down"
		if lines < 0 {
			direction, lines = "scroll-up", -lines
		}
		_, err = c.run("send-keys", "-X", "-N", strconv.Itoa(lines), "-t", state.pane, direction)
	default:
		return state, errors.New("unknown browse action")
	}
	if err != nil {
		return state, err
	}
	return c.state()
}
