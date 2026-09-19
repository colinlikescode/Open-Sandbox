//go:build linux

package main

import (
	"bytes"
	"syscall"
)

// Read the kernel log through a syscall, not a binary supplied by the user image.
func gvisorKernel() bool {
	buffer := make([]byte, 1<<17)
	n, err := syscall.Klogctl(3, buffer)
	return err == nil && bytes.Contains(buffer[:n], []byte("Starting gVisor"))
}
