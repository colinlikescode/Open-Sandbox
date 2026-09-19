// The companion lives inside each gVisor sandbox. Only the head can reach its
// loopback API through Kubernetes port-forward. It holds no cluster credentials.
package main

import (
	"archive/tar"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/creack/pty"
)

const agentPort = "49321"

type event struct {
	Seq      int    `json:"seq"`
	Type     string `json:"type"`
	Text     string `json:"text,omitempty"`
	ExitCode *int   `json:"exit_code,omitempty"`
	Status   string `json:"status,omitempty"`
}
type commandInfo struct {
	ID         string     `json:"id"`
	Status     string     `json:"status"`
	ExitCode   *int       `json:"exit_code"`
	Stdout     string     `json:"stdout"`
	Stderr     string     `json:"stderr"`
	Truncated  bool       `json:"truncated"`
	StartedAt  time.Time  `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at"`
	Error      *string    `json:"error"`
}
type commandRequest struct {
	Command    json.RawMessage   `json:"command"`
	Env        map[string]string `json:"env"`
	Cwd        string            `json:"cwd"`
	Timeout    float64           `json:"timeout"`
	Background bool              `json:"background"`
	Stdin      bool              `json:"stdin"`
	Pty        *pty.Winsize      `json:"-"`
	Tag        string            `json:"-"`
}
type run struct {
	mu         sync.Mutex
	info       commandInfo
	cmd        *exec.Cmd
	events     []event
	eventBytes int
	sequence   int
	limit      int
	notify     chan struct{}
	done       chan struct{}
	input      io.WriteCloser
	terminal   *os.File
	config     processConfig
	tag        string
}
type streamWriter struct {
	run    *run
	stream string
}

func (w streamWriter) Write(p []byte) (int, error) {
	r := w.run
	r.mu.Lock()
	defer r.mu.Unlock()
	target := &r.info.Stdout
	if w.stream == "stderr" {
		target = &r.info.Stderr
	}
	*target += string(p)
	if len(*target) > r.limit {
		*target = (*target)[len(*target)-r.limit:]
		r.info.Truncated = true
	}
	// Keep event history bounded even if a child writes a single huge buffer.
	for offset := 0; offset < len(p); offset += 16384 {
		end := offset + 16384
		if end > len(p) {
			end = len(p)
		}
		r.emitLocked(event{Type: w.stream, Text: string(p[offset:end])})
	}
	return len(p), nil
}
func (r *run) emitLocked(e event) {
	e.Seq = r.sequence
	r.sequence++
	r.events = append(r.events, e)
	r.eventBytes += len(e.Text)
	for r.eventBytes > r.limit && len(r.events) > 1 {
		r.eventBytes -= len(r.events[0].Text)
		r.events = r.events[1:]
	}
	close(r.notify)
	r.notify = make(chan struct{})
}
func (r *run) snapshot() commandInfo { r.mu.Lock(); defer r.mu.Unlock(); return r.info }
func (r *run) kill(status string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.info.Status != "running" {
		return
	}
	r.info.Status = status
	if r.cmd.Process != nil {
		_ = syscall.Kill(-r.cmd.Process.Pid, syscall.SIGKILL)
	}
}

type server struct {
	mu          sync.Mutex
	runs        map[string]*run
	workdir     string
	outputLimit int
	uploadLimit int64
	expires     atomic.Int64
}

func newServer(workdir string, outputLimit int, uploadLimit int64) *server {
	return &server{runs: make(map[string]*run), workdir: workdir, outputLimit: outputLimit, uploadLimit: uploadLimit}
}
func reply(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}
func failure(w http.ResponseWriter, code int, err error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": "sandbox_error", "message": err.Error()}})
}
func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		failure(w, 400, err)
		return false
	}
	return true
}
func (s *server) start(req commandRequest) (*run, error) {
	var argv []string
	if err := json.Unmarshal(req.Command, &argv); err != nil {
		var command string
		if err = json.Unmarshal(req.Command, &command); err != nil {
			return nil, err
		}
		if command == "" {
			return nil, errors.New("empty command")
		}
		argv = []string{"/bin/sh", "-lc", command}
	}
	if len(argv) == 0 || argv[0] == "" {
		return nil, errors.New("empty command")
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Dir = req.Cwd
	if cmd.Dir == "" {
		cmd.Dir = s.workdir
	}
	cmd.Env = os.Environ()
	for k, v := range req.Env {
		if strings.ContainsAny(k, "=\x00") || strings.ContainsRune(v, '\x00') {
			return nil, errors.New("invalid environment")
		}
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	cmd.WaitDelay = 2 * time.Second
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, err
	}
	r := &run{info: commandInfo{ID: "cmd-" + hex.EncodeToString(nonce[:]), Status: "running", StartedAt: time.Now().UTC()}, cmd: cmd, limit: s.outputLimit, notify: make(chan struct{}), done: make(chan struct{})}
	cmd.Stdout = streamWriter{r, "stdout"}
	cmd.Stderr = streamWriter{r, "stderr"}
	r.config = processConfig{Cmd: argv[0], Args: argv[1:], Envs: req.Env, Cwd: cmd.Dir}
	r.tag = req.Tag
	if req.Stdin && req.Pty == nil {
		input, err := cmd.StdinPipe()
		if err != nil {
			return nil, err
		}
		r.input = input
	}
	terminalDone := make(chan struct{})
	s.mu.Lock()
	// Bound memory retained by completed commands and reject excessive concurrency.
	if len(s.runs) >= 512 {
		for id, old := range s.runs {
			if old.snapshot().FinishedAt != nil {
				delete(s.runs, id)
			}
		}
	}
	active := 0
	for _, previous := range s.runs {
		if previous.snapshot().FinishedAt == nil {
			active++
		}
	}
	if active >= 128 {
		s.mu.Unlock()
		return nil, errors.New("too many active commands")
	}
	var startErr error
	if req.Pty != nil {
		cmd.Stdout, cmd.Stderr = nil, nil
		r.terminal, startErr = pty.StartWithSize(cmd, req.Pty)
		if startErr == nil {
			r.input = r.terminal
			go func() { _, _ = io.Copy(streamWriter{r, "pty"}, r.terminal); close(terminalDone) }()
		}
	} else {
		startErr = cmd.Start()
		close(terminalDone)
	}
	if startErr != nil {
		if r.input != nil {
			_ = r.input.Close()
		}
		s.mu.Unlock()
		return nil, startErr
	}
	s.runs[r.info.ID] = r
	s.mu.Unlock()
	go func() {
		var timer *time.Timer
		if req.Timeout > 0 {
			timer = time.AfterFunc(time.Duration(req.Timeout*float64(time.Second)), func() { r.kill("timed_out") })
		}
		err := cmd.Wait()
		if r.terminal != nil {
			select {
			case <-terminalDone:
			case <-time.After(time.Second):
			}
			_ = r.terminal.Close()
			<-terminalDone
		}
		if r.input != nil {
			_ = r.input.Close()
		}
		if timer != nil {
			timer.Stop()
		}
		// Commands own a process group; prevent descendants from outliving a completed command.
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		r.mu.Lock()
		defer r.mu.Unlock()
		code := cmd.ProcessState.ExitCode()
		if code < 0 {
			code = 137
		}
		if r.info.Status == "running" {
			r.info.Status = "exited"
		}
		if err != nil && code == 0 && !errors.Is(err, exec.ErrWaitDelay) {
			message := err.Error()
			r.info.Error = &message
			r.info.Status = "failed"
		}
		r.info.ExitCode = &code
		now := time.Now().UTC()
		r.info.FinishedAt = &now
		r.emitLocked(event{Type: "exit", ExitCode: &code, Status: r.info.Status})
		close(r.done)
	}()
	return r, nil
}
func (s *server) command(w http.ResponseWriter, request *http.Request) {
	if request.URL.Path == "/commands" {
		if request.Method == "GET" {
			s.mu.Lock()
			out := make([]commandInfo, 0, len(s.runs))
			for _, r := range s.runs {
				out = append(out, r.snapshot())
			}
			s.mu.Unlock()
			reply(w, out)
			return
		}
		if request.Method != "POST" {
			w.WriteHeader(405)
			return
		}
		var req commandRequest
		if !decode(w, request, &req) {
			return
		}
		if req.Timeout < 0 || req.Timeout > 604800 {
			failure(w, 400, errors.New("invalid timeout"))
			return
		}
		r, err := s.start(req)
		if err != nil {
			failure(w, 400, err)
			return
		}
		if !req.Background {
			select {
			case <-r.done:
			case <-request.Context().Done():
				return
			}
		}
		reply(w, r.snapshot())
		return
	}
	parts := strings.Split(strings.TrimPrefix(request.URL.Path, "/commands/"), "/")
	s.mu.Lock()
	r := s.runs[parts[0]]
	s.mu.Unlock()
	if r == nil {
		failure(w, 404, errors.New("command not found"))
		return
	}
	if len(parts) == 1 && request.Method == "DELETE" {
		r.kill("killed")
		<-r.done
		reply(w, r.snapshot())
		return
	}
	if len(parts) == 1 && request.Method == "GET" {
		reply(w, r.snapshot())
		return
	}
	if len(parts) != 2 || parts[1] != "events" || request.Method != "GET" {
		w.WriteHeader(404)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	flusher, ok := w.(http.Flusher)
	if !ok {
		return
	}
	next, _ := strconv.Atoi(request.URL.Query().Get("from_seq"))
	for {
		r.mu.Lock()
		events := append([]event(nil), r.events...)
		notify := r.notify
		finished := r.info.FinishedAt != nil
		r.mu.Unlock()
		if len(events) > 0 && next < events[0].Seq {
			payload, _ := json.Marshal(event{Seq: events[0].Seq - 1, Type: "truncated", Text: "Earlier output was discarded"})
			if _, err := fmt.Fprintf(w, "data: %s\n\n", payload); err != nil {
				return
			}
			next = events[0].Seq
		}
		for _, e := range events {
			if e.Seq >= next {
				payload, _ := json.Marshal(e)
				if _, err := fmt.Fprintf(w, "id: %d\ndata: %s\n\n", e.Seq, payload); err != nil {
					return
				}
				next = e.Seq + 1
			}
		}
		flusher.Flush()
		if finished {
			return
		}
		select {
		case <-notify:
		case <-request.Context().Done():
			return
		case <-time.After(15 * time.Second):
			if _, err := io.WriteString(w, ": keepalive\n\n"); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}
func absolute(value string) (string, error) {
	if !filepath.IsAbs(value) || strings.ContainsRune(value, '\x00') {
		return "", errors.New("path must be absolute")
	}
	return filepath.Clean(value), nil
}
func (s *server) files(w http.ResponseWriter, r *http.Request) {
	path, err := absolute(r.URL.Query().Get("path"))
	if err != nil {
		failure(w, 400, err)
		return
	}
	switch r.Method {
	case "GET":
		f, err := os.Open(path)
		if err != nil {
			failure(w, 404, err)
			return
		}
		defer f.Close()
		stat, err := f.Stat()
		if err != nil || !stat.Mode().IsRegular() {
			failure(w, 400, errors.New("path is not a regular file"))
			return
		}
		if stat.Size() > s.uploadLimit {
			failure(w, 413, errors.New("file exceeds transfer limit"))
			return
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		_, _ = io.Copy(w, io.LimitReader(f, s.uploadLimit))
	case "PUT":
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			failure(w, 400, err)
			return
		}
		f, err := os.CreateTemp(filepath.Dir(path), ".opensandbox-write-*")
		if err != nil {
			failure(w, 400, err)
			return
		}
		defer os.Remove(f.Name())
		_, err = io.Copy(f, http.MaxBytesReader(w, r.Body, s.uploadLimit))
		closeErr := f.Close()
		if err != nil {
			failure(w, 413, err)
			return
		}
		if closeErr != nil {
			failure(w, 400, closeErr)
			return
		}
		if err = os.Rename(f.Name(), path); err != nil {
			failure(w, 400, err)
			return
		}
		reply(w, map[string]bool{"ok": true})
	default:
		w.WriteHeader(405)
	}
}
func safeMember(root, name string) (string, error) {
	name = filepath.Clean(name)
	if filepath.IsAbs(name) || name == ".." || strings.HasPrefix(name, ".."+string(os.PathSeparator)) {
		return "", errors.New("archive path escapes destination")
	}
	target := filepath.Join(root, name)
	// Reject pre-existing symlink ancestors. No archive may introduce symlinks.
	current := root
	for _, part := range strings.Split(name, string(os.PathSeparator)) {
		current = filepath.Join(current, part)
		st, err := os.Lstat(current)
		if err == nil && st.Mode()&os.ModeSymlink != 0 {
			return "", errors.New("archive destination contains symlink")
		}
	}
	return target, nil
}
func extract(reader io.Reader, root string, limit int64) error {
	if err := os.MkdirAll(root, 0755); err != nil {
		return err
	}
	actual, err := filepath.EvalSymlinks(root)
	if err != nil {
		return err
	}
	root = actual
	tr := tar.NewReader(reader)
	remaining := limit
	for count := 0; ; count++ {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if count > 100000 {
			return errors.New("too many archive entries")
		}
		target, err := safeMember(root, hdr.Name)
		if err != nil {
			return err
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err = os.MkdirAll(target, 0755); err != nil {
				return err
			}
		case tar.TypeReg, tar.TypeRegA:
			if hdr.Size < 0 || hdr.Size > remaining {
				return errors.New("archive exceeds transfer limit")
			}
			remaining -= hdr.Size
			if err = os.MkdirAll(filepath.Dir(target), 0755); err != nil {
				return err
			}
			f, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, os.FileMode(hdr.Mode)&0777)
			if err != nil {
				return err
			}
			_, err = io.CopyN(f, tr, hdr.Size)
			closeErr := f.Close()
			if err != nil {
				return err
			}
			if closeErr != nil {
				return closeErr
			}
		default:
			return errors.New("only regular files and directories are allowed in archives")
		}
	}
}
func archive(writer io.Writer, path string, limit int64) error {
	tw := tar.NewWriter(writer)
	defer tw.Close()
	parent := filepath.Dir(path)
	remaining := limit
	return filepath.Walk(path, func(name string, stat os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !stat.IsDir() && !stat.Mode().IsRegular() {
			return errors.New("cannot transfer symlinks or special files")
		}
		if stat.Size() > remaining && !stat.IsDir() {
			return errors.New("archive exceeds transfer limit")
		}
		hdr, err := tar.FileInfoHeader(stat, "")
		if err != nil {
			return err
		}
		hdr.Name, err = filepath.Rel(parent, name)
		if err != nil {
			return err
		}
		if err = tw.WriteHeader(hdr); err != nil {
			return err
		}
		if stat.Mode().IsRegular() {
			remaining -= stat.Size()
			f, err := os.Open(name)
			if err != nil {
				return err
			}
			defer f.Close()
			_, err = io.CopyN(tw, f, stat.Size())
			return err
		}
		return nil
	})
}
func (s *server) archives(w http.ResponseWriter, r *http.Request) {
	path, err := absolute(r.URL.Query().Get("path"))
	if err != nil {
		failure(w, 400, err)
		return
	}
	switch r.Method {
	case "PUT":
		if err = extract(http.MaxBytesReader(w, r.Body, s.uploadLimit), path, s.uploadLimit); err != nil {
			failure(w, 400, err)
			return
		}
		reply(w, map[string]bool{"ok": true})
	case "GET":
		if _, err = os.Lstat(path); err != nil {
			failure(w, 404, err)
			return
		}
		w.Header().Set("Content-Type", "application/x-tar")
		if err = archive(w, path, s.uploadLimit); err != nil {
			panic(http.ErrAbortHandler)
		}
	default:
		w.WriteHeader(405)
	}
}
func (s *server) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		reply(w, map[string]any{"ok": true, "gvisor": gvisorKernel(), "pid": os.Getpid(), "expires": s.expires.Load()})
	})
	mux.HandleFunc("/commands", s.command)
	mux.HandleFunc("/commands/", s.command)
	mux.HandleFunc("/files", s.files)
	mux.HandleFunc("/e2b/files", s.e2bFiles)
	mux.HandleFunc("/process.Process/", s.processRPC)
	mux.HandleFunc("/filesystem.Filesystem/", s.filesystemRPC)
	mux.HandleFunc("/archive", s.archives)
	mux.HandleFunc("/timeout", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "PUT" {
			w.WriteHeader(405)
			return
		}
		var req struct {
			Expires int64 `json:"expires"`
		}
		if !decode(w, r, &req) {
			return
		}
		if req.Expires <= time.Now().Unix() || req.Expires > time.Now().Add(7*24*time.Hour).Unix() {
			failure(w, 400, errors.New("invalid expiration"))
			return
		}
		s.expires.Store(req.Expires)
		reply(w, map[string]bool{"ok": true})
	})
	return mux
}
func main() {
	if len(os.Args) < 2 {
		log.Fatal("usage: agent install DEST | health | serve")
	}
	switch os.Args[1] {
	case "verify":
		if !gvisorKernel() {
			log.Fatal("gVisor kernel verification failed")
		}
		fmt.Println("gVisor")
	case "install":
		if len(os.Args) != 3 {
			log.Fatal("install requires destination")
		}
		source, err := os.Executable()
		if err != nil {
			log.Fatal(err)
		}
		content, err := os.ReadFile(source)
		if err != nil {
			log.Fatal(err)
		}
		if err = os.WriteFile(os.Args[2], content, 0755); err != nil {
			log.Fatal(err)
		}
	case "health":
		client := http.Client{Timeout: time.Second}
		resp, err := client.Get("http://127.0.0.1:" + agentPort + "/health")
		if err != nil {
			log.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			os.Exit(1)
		}
	case "serve":
		flags := flag.NewFlagSet("serve", flag.ExitOnError)
		workdir := flags.String("workdir", "/workspace", "command working directory")
		listen := flags.String("listen", "127.0.0.1:"+agentPort, "loopback listen address")
		requireGvisor := flags.Bool("require-gvisor", false, "refuse to start outside gVisor")
		expires := flags.Int64("expires", 0, "Unix expiration time")
		output := flags.Int("output-limit", 2<<20, "retained output per stream")
		upload := flags.Int64("upload-limit", 512<<20, "maximum transfer size")
		_ = flags.Parse(os.Args[2:])
		if *requireGvisor && !gvisorKernel() {
			log.Fatal("gVisor kernel verification failed")
		}
		if *expires <= time.Now().Unix() || *output < 16384 || *upload < 1 {
			log.Fatal("invalid limits or expiration")
		}
		if err := os.MkdirAll(*workdir, 0755); err != nil {
			log.Fatal(err)
		}
		s := newServer(*workdir, *output, *upload)
		s.expires.Store(*expires)
		go func() {
			for range time.Tick(time.Second) {
				if time.Now().Unix() >= s.expires.Load() {
					os.Exit(0)
				}
			}
		}()
		startReaper(s)
		listener, err := net.Listen("tcp", *listen)
		if err != nil {
			log.Fatal(err)
		}
		service := &http.Server{Handler: s.handler(), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 64 << 10}
		log.Fatal(service.Serve(listener))
	default:
		log.Fatal("unknown agent command")
	}
}
