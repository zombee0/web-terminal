package main

import (
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/zombee0/web-terminal/terminal"
)

func main() {
	server := &http.Server{
		Addr:              ":3001",
		Handler:           terminal.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("web terminal listening addr=%s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
