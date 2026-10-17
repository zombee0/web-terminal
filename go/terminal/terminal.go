package terminal

import (
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

const (
	keepalivePeriod = 15 * time.Second
	terminalPage    = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Box Terminal</title><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2032%2032%22%3E%3Crect%20width%3D%2232%22%20height%3D%2232%22%20rx%3D%227%22%20fill%3D%22%2310151c%22%2F%3E%3Cpath%20d%3D%22m8%2010%206%206-6%206m10%200h6%22%20fill%3D%22none%22%20stroke%3D%22%234ade80%22%20stroke-width%3D%223%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%2F%3E%3C%2Fsvg%3E"><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"><style>html,body{margin:0;height:100%;background:#10151c;color:#d5e1eb;font:14px system-ui}#term{height:100%;width:100%}</style></head><body><div id="term"></div><script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script><script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script><script src="https://cdn.jsdelivr.net/npm/xterm-addon-unicode11@0.6.0/lib/xterm-addon-unicode11.js"></script><script>const term=new Terminal({cursorBlink:true,convertEol:true,scrollback:10000,scrollOnOutput:false,fontFamily:"ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Noto Sans Mono CJK SC','PingFang SC','Microsoft YaHei',monospace",theme:{background:'#090d12'}});const fitAddon=new FitAddon.FitAddon();const unicodeAddon=new Unicode11Addon.Unicode11Addon();term.loadAddon(fitAddon);term.loadAddon(unicodeAddon);term.unicode.activeVersion='11';term.open(document.querySelector('#term'));fitAddon.fit();const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws'+location.search);ws.onopen=()=>{fitAddon.fit();sendSize();term.focus()};ws.onmessage=e=>term.write(e.data);ws.onclose=()=>term.write('\r\n[Connection closed]\r\n');ws.onerror=()=>term.write('\r\n[Connection error]\r\n');term.onData(data=>{if(ws.readyState===WebSocket.OPEN)ws.send(data)});function sendSize(){if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'resize',cols:term.cols,rows:term.rows}))}window.addEventListener('resize',()=>{fitAddon.fit();sendSize()});</script></body></html>`
)

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
	source := strings.Replace(terminalPage, `+location.host+'/ws'+location.search`, `+location.host+location.pathname.replace(/\/?$/, '/')+'ws'+location.search`, 1)
	page := strings.Replace(
		source,
		`const unicodeAddon=new Unicode11Addon.Unicode11Addon();term.loadAddon(fitAddon);term.loadAddon(unicodeAddon);term.unicode.activeVersion='11';`,
		`term.loadAddon(fitAddon);if(window.Unicode11Addon){try{const unicodeAddon=new Unicode11Addon.Unicode11Addon();term.loadAddon(unicodeAddon);term.unicode.activeVersion='11'}catch(error){console.warn('Unicode addon disabled',error)}}`,
		1,
	)
	return strings.Replace(
		page,
		`ws.onmessage=e=>term.write(e.data);`,
		`ws.binaryType='arraybuffer';ws.onmessage=e=>term.write(typeof e.data==='string'?e.data:new Uint8Array(e.data));`,
		1,
	)
}

func sendBinary(conn *websocket.Conn, mu *sync.Mutex, payload []byte) error {
	mu.Lock()
	defer mu.Unlock()
	_ = conn.SetWriteDeadline(time.Now().Add(20 * time.Second))
	return conn.WriteMessage(websocket.BinaryMessage, payload)
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

	var writeMu sync.Mutex
	stop := make(chan struct{})
	var stopOnce sync.Once
	stopKeepalive := func() { stopOnce.Do(func() { close(stop) }) }

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

	go func() {
		buffer := make([]byte, 64*1024)
		for {
			count, readErr := ptmx.Read(buffer)
			if count > 0 {
				if writeErr := sendBinary(conn, &writeMu, buffer[:count]); writeErr != nil {
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
		if json.Unmarshal(payload, &resize) == nil && resize.Type == "resize" && resize.Cols > 0 && resize.Rows > 0 {
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
	stopKeepalive()
	log.Printf("ws finished remote=%s", remote)
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
