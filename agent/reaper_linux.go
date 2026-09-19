package main

import (
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// PID 1 must reap orphaned grandchildren. Reap only zombies that aren't a direct
// command managed by os/exec; never race Cmd.Wait for its exit status.
func startReaper(s *server) {
	if os.Getpid() != 1 {
		return
	}
	go func() {
		for range time.Tick(time.Second) {
			s.mu.Lock()
			managed := map[int]bool{}
			for _, r := range s.runs {
				if r.snapshot().FinishedAt == nil && r.cmd.Process != nil {
					managed[r.cmd.Process.Pid] = true
				}
			}
			entries, _ := os.ReadDir("/proc")
			for _, entry := range entries {
				pid, err := strconv.Atoi(entry.Name())
				if err != nil || managed[pid] {
					continue
				}
				data, err := os.ReadFile("/proc/" + entry.Name() + "/stat")
				if err != nil {
					continue
				}
				end := strings.LastIndex(string(data), ")")
				if end < 0 {
					continue
				}
				fields := strings.Fields(string(data)[end+1:])
				if len(fields) > 1 && fields[0] == "Z" && fields[1] == "1" {
					var status syscall.WaitStatus
					_, _ = syscall.Wait4(pid, &status, syscall.WNOHANG, nil)
				}
			}
			s.mu.Unlock()
		}
	}()
}
