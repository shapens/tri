//go:build !windows

package main

import (
	"os/exec"
	"syscall"
)

// Workers run in their own process group: a terminal Ctrl-C reaches only the
// front server, which drains first, and a timeout kills the worker together
// with any command it started.
func setProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func killTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	_ = cmd.Process.Kill()
}
