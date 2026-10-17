package terminal

import (
	"errors"
	"net"
	"os"
	"strconv"
)

// Listener accepts a root supervisor's socket in hosted mode. The supervisor
// retains its descriptor while the HTTP child restarts, preventing a terminal
// user from taking over port 3001 during a restart or a child crash.
func Listener() (net.Listener, error) {
	raw := os.Getenv("WEB_TERMINAL_LISTEN_FD")
	if raw == "" {
		return net.Listen("tcp", ":3001")
	}
	fd, err := strconv.Atoi(raw)
	if err != nil || fd < 3 || os.Geteuid() != 0 || os.Getenv("WEB_TERMINAL_GATEWAY_CONFIG") == "" {
		return nil, errors.New("invalid protected listener descriptor")
	}
	file := os.NewFile(uintptr(fd), "web-terminal-listener")
	defer file.Close()
	listener, err := net.FileListener(file)
	if err != nil {
		return nil, err
	}
	address, ok := listener.Addr().(*net.TCPAddr)
	if !ok || address.Port != 3001 || !address.IP.IsUnspecified() {
		listener.Close()
		return nil, errors.New("protected listener must bind all interfaces on port 3001")
	}
	return listener, nil
}
