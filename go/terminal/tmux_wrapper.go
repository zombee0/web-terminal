package terminal

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// RunTmuxWrapper handles the same binary when it is invoked as the tmux shim.
func RunTmuxWrapper() bool {
	if filepath.Base(os.Args[0]) != "tmux" {
		return false
	}
	os.Exit(runTmux(os.Args[1:]))
	return true
}

func wrapperDirectory() string {
	if os.Geteuid() == 0 {
		return "/var/lib/web-terminal/bin"
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".local", "state", "web-terminal", "bin")
}

// Put a tmux shim first on PATH so each attached client can be paired with
// this browser. The directory stays root-owned in protected guests, so the
// terminal user cannot replace the shim.
func prepareTmuxWrapper(command *exec.Cmd, bin string) error {
	var realTmux string
	for _, dir := range filepath.SplitList(envValue(command.Env, "PATH")) {
		path := filepath.Join(dir, "tmux")
		if info, err := os.Stat(path); err == nil && !info.IsDir() && info.Mode()&0111 != 0 {
			realTmux = path
			break
		}
	}
	if realTmux == "" {
		return nil
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(bin, 0755); err != nil {
		return err
	}
	shim := filepath.Join(bin, "tmux")
	if target, _ := os.Readlink(shim); target != executable {
		staged := shim + ".new"
		_ = os.Remove(staged)
		if err := os.Symlink(executable, staged); err != nil {
			return err
		}
		if err := os.Rename(staged, shim); err != nil {
			return err
		}
	}
	command.Env = setEnv(command.Env, "PATH", bin+string(os.PathListSeparator)+envValue(command.Env, "PATH"))
	command.Env = setEnv(command.Env, "WEB_TERMINAL_TMUX", realTmux)
	return nil
}

func envValue(env []string, key string) string {
	for _, entry := range env {
		if value, ok := strings.CutPrefix(entry, key+"="); ok {
			return value
		}
	}
	return ""
}

func setEnv(env []string, key, value string) []string {
	for i, entry := range env {
		if strings.HasPrefix(entry, key+"=") {
			env[i] = key + "=" + value
			return env
		}
	}
	return append(env, key+"="+value)
}

func withoutEnv(env []string, key string) []string {
	kept := env[:0:0]
	for _, entry := range env {
		if !strings.HasPrefix(entry, key+"=") {
			kept = append(kept, entry)
		}
	}
	return kept
}

func runTmux(args []string) int {
	// Preserve global options (including custom sockets and configs) verbatim.
	end := 0
	for end < len(args) && strings.HasPrefix(args[end], "-") {
		option := args[end]
		end++
		if option == "-f" || option == "-L" || option == "-S" || option == "-T" || option == "-c" {
			if end < len(args) {
				end++
			}
		}
	}
	command := exec.Command(os.Getenv("WEB_TERMINAL_TMUX"), args...)
	// The browse address belongs to this browser connection. Keep it out of the
	// tmux server environment, and therefore out of shells started inside it.
	command.Env = withoutEnv(os.Environ(), browseSocketEnv)
	command.Stdin, command.Stdout, command.Stderr = os.Stdin, os.Stdout, os.Stderr
	err := command.Start()
	if err == nil {
		stopBrowse := startTmuxBrowser(args[:end], command.Process.Pid)
		err = command.Wait()
		stopBrowse()
	}
	if err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return exit.ExitCode()
		}
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}
