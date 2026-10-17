//go:build !linux

package terminal

import (
	"errors"
	"os"
	"os/exec"
)

func verifyConfigOwner(os.FileInfo) error      { return errors.New("protected guests require Linux") }
func protectCommand(*exec.Cmd, Identity) error { return errors.New("protected guests require Linux") }
