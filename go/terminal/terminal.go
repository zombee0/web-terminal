package terminal

import (
	_ "embed"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/creack/pty/v2"
	"github.com/gorilla/websocket"
)

const keepalivePeriod = 15 * time.Second

// Embed local assets into one page for authenticated proxies and --print-page.
//
//go:embed web/terminal.html
var terminalPage string

//go:embed web/terminal.css
var terminalCSS string

//go:embed web/terminal.js
var terminalJS string

// Upgrader uses the default Gorilla policy (Origin must match Host). The
// platform proxy must preserve the public Host and authenticate BOTH / and
// /ws. This is defense in depth, not authentication; never expose directly.
var Upgrader = websocket.Upgrader{}

type resizeMessage struct {
	Type string `json:"type"`
	Cols uint16 `json:"cols"`
	Rows uint16 `json:"rows"`
}

func Environment() []string {
	values := make(map[string]string)
	for _, item := range os.Environ() {
		parts := strings.SplitN(item, "=", 2)
		values[parts[0]] = parts[1]
	}
	values["HOME"] = "/home/boxlite"
	values["TERM"] = "xterm-256color"
	values["COLORTERM"] = "truecolor"
	values["LANG"] = "C.UTF-8"
	values["LC_ALL"] = "C.UTF-8"
	values["LC_CTYPE"] = "C.UTF-8"
	values["PATH"] = "/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
	delete(values, "TMUX")
	env := make([]string, 0, len(values))
	for key, value := range values {
		env = append(env, key+"="+value)
	}
	return env
}

func PageHTML() string {
	return strings.NewReplacer("/* TERMINAL_CSS */", terminalCSS, "/* TERMINAL_JS */", terminalJS).Replace(terminalPage)
}

func sendMessage(conn *websocket.Conn, mu *sync.Mutex, kind int, payload []byte) error {
	mu.Lock()
	defer mu.Unlock()
	_ = conn.SetWriteDeadline(time.Now().Add(20 * time.Second))
	return conn.WriteMessage(kind, payload)
}

func HandleWS(w http.ResponseWriter, r *http.Request) {
	handleWS(w, r, nil)
}

func handleWS(w http.ResponseWriter, r *http.Request, identity *Identity) {
	conn, err := Upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("ws upgrade failed remote=%s error=%v", r.RemoteAddr, err)
		return
	}
	defer conn.Close()
	remote := r.RemoteAddr
	if identity == nil {
		log.Printf("ws opened remote=%s path=%s origin=%q extensions=%q", remote, r.URL.Path, r.Header.Get("Origin"), r.Header.Get("Sec-WebSocket-Extensions"))
	} else {
		log.Print("protected terminal connected")
	}
	conn.SetReadLimit(4 * 1024 * 1024)
	_ = conn.SetReadDeadline(time.Now().Add(60 * time.Second))
	conn.SetPongHandler(func(string) error {
		return conn.SetReadDeadline(time.Now().Add(60 * time.Second))
	})

	shell := "/bin/bash"
	if _, err := os.Stat(shell); err != nil {
		shell = "/bin/sh"
	}
	command := exec.Command(shell, "-i")
	command.Dir = "/home/boxlite"
	command.Env = Environment()
	if identity != nil {
		command.Env = ProtectedEnvironment()
	}
	var browser *tmuxBrowser
	if err := prepareTmuxWrapper(command, wrapperDirectory()); err != nil {
		log.Printf("tmux wrapper setup failed: %v", err)
	} else if envValue(command.Env, "WEB_TERMINAL_TMUX") != "" {
		if browser, err = newTmuxBrowser(command, identity); err != nil {
			log.Print("native tmux browsing setup failed")
		}
	}
	defer browser.close()
	if identity != nil {
		if err := protectCommand(command, *identity); err != nil {
			log.Print("protected terminal startup failed")
			return
		}
	}
	ptmx, err := pty.Start(command)
	if err != nil {
		log.Printf("pty start failed remote=%s error=%v", remote, err)
		return
	}
	defer ptmx.Close()
	defer func() {
		_ = command.Process.Kill()
		_ = command.Wait()
	}()
	_ = pty.Setsize(ptmx, &pty.Winsize{Cols: 120, Rows: 40})
	serveTerminal(conn, ptmx, browser)
	log.Printf("ws finished remote=%s", remote)
}

func serveTerminal(conn *websocket.Conn, ptmx *os.File, browser *tmuxBrowser) {
	remote := conn.RemoteAddr().String()
	var writeMu sync.Mutex
	ready, _ := json.Marshal(map[string]any{"type": "browse-ready", "tmux": browser != nil})
	if err := sendMessage(conn, &writeMu, websocket.TextMessage, ready); err != nil {
		return
	}
	stop := make(chan struct{})
	var stopOnce sync.Once
	stopKeepalive := func() { stopOnce.Do(func() { close(stop) }) }
	defer stopKeepalive()

	go func() {
		for {
			select {
			case <-stop:
				return
			case <-time.After(keepalivePeriod):
				writeMu.Lock()
				err := conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(20*time.Second))
				writeMu.Unlock()
				if err != nil {
					log.Printf("ws keepalive stopped remote=%s error=%v", remote, err)
					return
				}
			}
		}
	}()

	outputDone := make(chan struct{})
	defer func() { browser.close(); ptmx.Close(); <-outputDone }()
	go func() {
		defer close(outputDone)
		defer conn.Close()
		buffer := make([]byte, 64*1024)
		for {
			count, readErr := ptmx.Read(buffer)
			if count > 0 {
				if writeErr := sendMessage(conn, &writeMu, websocket.BinaryMessage, buffer[:count]); writeErr != nil {
					log.Printf("pty output failed remote=%s error=%v", remote, writeErr)
					stopKeepalive()
					return
				}
			}
			if readErr != nil {
				if !errors.Is(readErr, io.EOF) {
					log.Printf("pty closed remote=%s error=%v", remote, readErr)
				}
				stopKeepalive()
				return
			}
		}
	}()

	for {
		messageType, payload, readErr := conn.ReadMessage()
		if readErr != nil {
			if closeErr, ok := readErr.(*websocket.CloseError); ok {
				log.Printf("ws closed by peer remote=%s code=%d text=%q", remote, closeErr.Code, closeErr.Text)
			} else {
				log.Printf("ws read failed remote=%s error=%v", remote, readErr)
			}
			break
		}
		_ = conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		if messageType != websocket.TextMessage && messageType != websocket.BinaryMessage {
			continue
		}
		var resize resizeMessage
		if messageType == websocket.TextMessage && json.Unmarshal(payload, &resize) == nil && resize.Type == "tmux-browse" {
			var request browseRequest
			if json.Unmarshal(payload, &request) != nil {
				continue
			}
			state, _ := json.Marshal(browser.request(request))
			if err := sendMessage(conn, &writeMu, websocket.TextMessage, state); err != nil {
				break
			}
			continue
		}
		if messageType == websocket.TextMessage && resize.Type == "resize" && resize.Cols > 0 && resize.Rows > 0 {
			if err := pty.Setsize(ptmx, &pty.Winsize{Cols: resize.Cols, Rows: resize.Rows}); err != nil {
				log.Printf("pty resize failed remote=%s cols=%d rows=%d error=%v", remote, resize.Cols, resize.Rows, err)
			}
			continue
		}
		if _, err := ptmx.Write(payload); err != nil {
			log.Printf("pty input failed remote=%s error=%v", remote, err)
			break
		}
	}
}

func Handler() http.Handler {
	return handler(nil)
}

func handler(identity *Identity) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(PageHTML()))
			return
		}
		if r.URL.Path == "/ws" {
			handleWS(w, r, identity)
			return
		}
		http.NotFound(w, r)
	})
	return mux
}
