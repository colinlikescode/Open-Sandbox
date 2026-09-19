//go:build !linux

package main

// Used only by development tests. Production sandbox companions are Linux binaries.
func startReaper(s *server) {}
