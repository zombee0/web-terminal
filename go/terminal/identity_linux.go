//go:build linux

package terminal

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

func verifyConfigOwner(info os.FileInfo) error {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 {
		return errors.New("gateway configuration must be root-owned")
	}
	return nil
}

func protectCommand(command *exec.Cmd, identity Identity) error {
	if identity.UID == 0 || identity.GID == 0 {
		return errors.New("root terminal denied")
	}
	if _, err := os.Stat("/usr/bin/setpriv"); err != nil {
		return err
	}
	command.Args = append([]string{"/usr/bin/setpriv", "--no-new-privs", "--"}, command.Args...)
	command.Path = "/usr/bin/setpriv"
	command.SysProcAttr = &syscall.SysProcAttr{
		Credential: &syscall.Credential{Uid: identity.UID, Gid: identity.GID, Groups: []uint32{identity.GID}},
		Pdeathsig:  syscall.SIGKILL,
	}
	return nil
}
