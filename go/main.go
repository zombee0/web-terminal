package main

import (
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/zombee0/web-terminal/terminal"
)

func main() {
	if terminal.RunTmuxWrapper() {
		return
	}
	if len(os.Args) == 2 && os.Args[1] == "--print-page" {
		fmt.Print(terminal.PageHTML())
		return
	}
	handler, err := terminal.ConfiguredHandler()
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{
		Addr:              ":3001",
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("web terminal listening addr=%s", server.Addr)
	listener, err := terminal.Listener()
	if err != nil {
		log.Fatal(err)
	}
	if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
