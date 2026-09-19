// E2B runtime adapter. Wire contracts: e2b-dev/E2B spec/envd at SDK 2.51.0.
// Connect's JSON encoding keeps the domain process runner independent of protobuf.
package main

import (
	"compress/gzip"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/creack/pty"
)

type processConfig struct {
	Cmd  string            `json:"cmd"`
	Args []string          `json:"args"`
	Envs map[string]string `json:"envs"`
	Cwd  string            `json:"cwd,omitempty"`
}
type processSelector struct {
	Pid int    `json:"pid"`
	Tag string `json:"tag"`
}
type ptyConfig struct {
	Size struct {
		Cols uint16 `json:"cols"`
		Rows uint16 `json:"rows"`
	} `json:"size"`
}
type processRequest struct {
	Process json.RawMessage `json:"process"`
	Pty     *ptyConfig      `json:"pty"`
	Stdin   *bool           `json:"stdin"`
	Tag     string          `json:"tag"`
	Input   struct {
		Stdin []byte `json:"stdin"`
		Pty   []byte `json:"pty"`
	} `json:"input"`
	Signal json.RawMessage `json:"signal"`
}

func rpcFailure(w http.ResponseWriter, code string, err error) {
	status := map[string]int{"invalid_argument": 400, "not_found": 404, "already_exists": 409, "permission_denied": 403, "resource_exhausted": 429, "unimplemented": 501}[code]
	if status == 0 {
		status = 500
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"code": code, "message": err.Error()})
}
func rpcError(w http.ResponseWriter, err error) {
	code := "invalid_argument"
	if errors.Is(err, os.ErrNotExist) {
		code = "not_found"
	}
	if errors.Is(err, os.ErrExist) {
		code = "already_exists"
	}
	if errors.Is(err, os.ErrPermission) {
		code = "permission_denied"
	}
	rpcFailure(w, code, err)
}
func rpcDecode(w http.ResponseWriter, r *http.Request, value any) bool {
	if r.Method != "POST" {
		w.WriteHeader(405)
		return false
	}
	var reader io.Reader = http.MaxBytesReader(w, r.Body, 2<<20)
	if strings.HasPrefix(r.Header.Get("Content-Type"), "application/connect+json") {
		var header [5]byte
		if _, err := io.ReadFull(reader, header[:]); err != nil {
			rpcError(w, err)
			return false
		}
		size := binary.BigEndian.Uint32(header[1:])
		if header[0] != 0 || size > 2<<20 {
			rpcFailure(w, "invalid_argument", errors.New("invalid Connect envelope"))
			return false
		}
		reader = io.LimitReader(reader, int64(size))
	} else if !strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
		rpcFailure(w, "unimplemented", errors.New("runtime supports Connect JSON"))
		return false
	}
	if err := json.NewDecoder(reader).Decode(value); err != nil {
		rpcError(w, err)
		return false
	}
	return true
}
func frame(w http.ResponseWriter, flag byte, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	header := make([]byte, 5)
	header[0] = flag
	binary.BigEndian.PutUint32(header[1:], uint32(len(data)))
	if _, err = w.Write(append(header, data...)); err != nil {
		return err
	}
	return http.NewResponseController(w).Flush()
}
func currentUser(r *http.Request) error {
	name, _, present := r.BasicAuth()
	if !present {
		name = r.URL.Query().Get("username")
	}
	if name == "" {
		return nil
	}
	u, err := user.Lookup(name)
	if err != nil {
		return fmt.Errorf("unknown user %q: %w", name, os.ErrNotExist)
	}
	if u.Uid != strconv.Itoa(os.Geteuid()) {
		return fmt.Errorf("user switching is unavailable in this restricted runtime: %w", os.ErrPermission)
	}
	return nil
}
func (s *server) findProcess(raw json.RawMessage) (*run, error) {
	var selector processSelector
	if err := json.Unmarshal(raw, &selector); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var found *run
	for _, r := range s.runs {
		if (selector.Pid > 0 && r.cmd.Process.Pid == selector.Pid) || (selector.Tag != "" && r.tag == selector.Tag) {
			if found == nil || r.info.StartedAt.After(found.info.StartedAt) {
				found = r
			}
		}
	}
	if found == nil {
		return nil, os.ErrNotExist
	}
	return found, nil
}
func (s *server) processRPC(w http.ResponseWriter, req *http.Request) {
	var body processRequest
	if !rpcDecode(w, req, &body) {
		return
	}
	method := strings.TrimPrefix(req.URL.Path, "/process.Process/")
	if method == "List" {
		processes := []any{}
		s.mu.Lock()
		for _, r := range s.runs {
			if r.snapshot().FinishedAt == nil {
				processes = append(processes, map[string]any{"pid": r.cmd.Process.Pid, "config": r.config, "tag": r.tag})
			}
		}
		s.mu.Unlock()
		reply(w, map[string]any{"processes": processes})
		return
	}
	var r *run
	var err error
	if method == "Start" {
		if err = currentUser(req); err != nil {
			rpcError(w, err)
			return
		}
		var config processConfig
		if err = json.Unmarshal(body.Process, &config); err != nil {
			rpcError(w, err)
			return
		}
		argv, _ := json.Marshal(append([]string{config.Cmd}, config.Args...))
		command := commandRequest{Command: argv, Cwd: config.Cwd, Env: config.Envs, Tag: body.Tag, Stdin: body.Stdin == nil || *body.Stdin}
		if body.Pty != nil {
			if body.Pty.Size.Rows == 0 || body.Pty.Size.Cols == 0 {
				rpcError(w, errors.New("terminal dimensions must be positive"))
				return
			}
			command.Pty = &pty.Winsize{Rows: body.Pty.Size.Rows, Cols: body.Pty.Size.Cols}
		}
		if value := req.Header.Get("Connect-Timeout-Ms"); value != "" {
			ms, parseErr := strconv.ParseUint(value, 10, 32)
			if parseErr != nil {
				rpcError(w, parseErr)
				return
			}
			command.Timeout = float64(ms) / 1000
		}
		r, err = s.start(command)
	} else {
		r, err = s.findProcess(body.Process)
	}
	if err != nil {
		rpcError(w, err)
		return
	}
	switch method {
	case "Start", "Connect":
		s.streamProcess(w, req, r)
	case "SendInput":
		data := body.Input.Stdin
		if body.Input.Pty != nil {
			data = body.Input.Pty
		}
		if r.input == nil {
			rpcError(w, errors.New("stdin is closed"))
			return
		}
		if _, err = r.input.Write(data); err != nil {
			rpcError(w, err)
			return
		}
		reply(w, map[string]any{})
	case "CloseStdin":
		if r.terminal != nil {
			rpcError(w, errors.New("send Ctrl+D to a terminal"))
			return
		}
		if r.input != nil {
			_ = r.input.Close()
		}
		reply(w, map[string]any{})
	case "SendSignal":
		var name string
		var number int
		_ = json.Unmarshal(body.Signal, &name)
		_ = json.Unmarshal(body.Signal, &number)
		if name == "SIGNAL_SIGKILL" {
			number = 9
		}
		if name == "SIGNAL_SIGTERM" {
			number = 15
		}
		if number != 9 && number != 15 {
			rpcError(w, errors.New("unsupported signal"))
			return
		}
		r.mu.Lock()
		if r.info.FinishedAt != nil {
			err = os.ErrNotExist
		} else {
			err = syscall.Kill(-r.cmd.Process.Pid, syscall.Signal(number))
		}
		r.mu.Unlock()
		if err != nil {
			rpcError(w, err)
			return
		}
		reply(w, map[string]any{})
	case "Update":
		if r.terminal == nil || body.Pty == nil || body.Pty.Size.Rows == 0 || body.Pty.Size.Cols == 0 {
			rpcError(w, errors.New("valid terminal dimensions are required"))
			return
		}
		if err = pty.Setsize(r.terminal, &pty.Winsize{Rows: body.Pty.Size.Rows, Cols: body.Pty.Size.Cols}); err != nil {
			rpcError(w, err)
			return
		}
		reply(w, map[string]any{})
	default:
		rpcFailure(w, "unimplemented", errors.New("unsupported runtime operation"))
	}
}
func (s *server) streamProcess(w http.ResponseWriter, req *http.Request, r *run) {
	w.Header().Set("Content-Type", "application/connect+json")
	w.Header().Set("Cache-Control", "no-store")
	if frame(w, 0, map[string]any{"event": map[string]any{"start": map[string]int{"pid": r.cmd.Process.Pid}}}) != nil {
		return
	}
	next := 0
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		events := append([]event(nil), r.events...)
		notify := r.notify
		finished := r.info.FinishedAt != nil
		r.mu.Unlock()
		if len(events) > 0 && next < events[0].Seq {
			_ = frame(w, 2, map[string]any{"error": map[string]string{"code": "resource_exhausted", "message": "process output exceeded replay buffer"}})
			return
		}
		for _, e := range events {
			if e.Seq < next {
				continue
			}
			next = e.Seq + 1
			var item map[string]any
			if e.Type == "exit" {
				item = map[string]any{"end": map[string]any{"exitCode": *e.ExitCode, "exited": true, "status": e.Status}}
			} else {
				item = map[string]any{"data": map[string]string{e.Type: base64.StdEncoding.EncodeToString([]byte(e.Text))}}
			}
			if frame(w, 0, map[string]any{"event": item}) != nil {
				return
			}
		}
		if finished {
			_ = frame(w, 2, map[string]any{})
			return
		}
		select {
		case <-req.Context().Done():
			return // A disconnected stream must not kill background work.
		case <-notify:
		case <-ticker.C:
			if frame(w, 0, map[string]any{"event": map[string]any{"keepalive": map[string]any{}}}) != nil {
				return
			}
		}
	}
}

func (s *server) resolve(path string) (string, error) {
	if path == "" || strings.ContainsRune(path, 0) {
		return "", errors.New("path is required")
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(s.workdir, path)
	}
	return filepath.Clean(path), nil
}
func entry(path string) (map[string]any, error) {
	st, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	kind := "FILE_TYPE_FILE"
	if st.IsDir() {
		kind = "FILE_TYPE_DIRECTORY"
	}
	if st.Mode()&os.ModeSymlink != 0 {
		kind = "FILE_TYPE_SYMLINK"
	}
	result := map[string]any{"name": st.Name(), "path": path, "type": kind, "size": strconv.FormatInt(st.Size(), 10), "mode": uint32(st.Mode().Perm()), "permissions": st.Mode().String(), "modifiedTime": st.ModTime().UTC().Format(time.RFC3339Nano), "owner": "", "group": ""}
	if stat, ok := st.Sys().(*syscall.Stat_t); ok {
		result["owner"] = strconv.Itoa(int(stat.Uid))
		result["group"] = strconv.Itoa(int(stat.Gid))
		if u, e := user.LookupId(result["owner"].(string)); e == nil {
			result["owner"] = u.Username
		}
		if g, e := user.LookupGroupId(result["group"].(string)); e == nil {
			result["group"] = g.Name
		}
	}
	if kind == "FILE_TYPE_SYMLINK" {
		result["symlinkTarget"], _ = os.Readlink(path)
	}
	return result, nil
}
func (s *server) filesystemRPC(w http.ResponseWriter, req *http.Request) {
	var body struct {
		Path        string `json:"path"`
		Source      string `json:"source"`
		Destination string `json:"destination"`
		Depth       uint32 `json:"depth"`
	}
	if !rpcDecode(w, req, &body) {
		return
	}
	if err := currentUser(req); err != nil {
		rpcError(w, err)
		return
	}
	method := strings.TrimPrefix(req.URL.Path, "/filesystem.Filesystem/")
	switch method {
	case "Stat", "MakeDir", "Move", "Remove", "ListDir":
	default:
		rpcFailure(w, "unimplemented", errors.New("unsupported filesystem operation"))
		return
	}
	if method == "Move" {
		body.Path = body.Source
	}
	path, err := s.resolve(body.Path)
	if err != nil {
		rpcError(w, err)
		return
	}
	var result any = map[string]any{}
	switch method {
	case "Stat":
		var info map[string]any
		info, err = entry(path)
		result = map[string]any{"entry": info}
	case "MakeDir":
		if _, e := os.Stat(path); e == nil {
			err = os.ErrExist
		} else {
			err = os.MkdirAll(path, 0755)
		}
		if err == nil {
			info, e := entry(path)
			err = e
			result = map[string]any{"entry": info}
		}
	case "Move":
		var dest string
		dest, err = s.resolve(body.Destination)
		if err == nil {
			err = os.Rename(path, dest)
		}
		if err == nil {
			info, e := entry(dest)
			err = e
			result = map[string]any{"entry": info}
		}
	case "Remove":
		if path == "/" {
			err = errors.New("cannot remove filesystem root")
		} else if _, err = os.Lstat(path); err == nil {
			err = os.RemoveAll(path)
		}
	case "ListDir":
		if body.Depth == 0 {
			body.Depth = 1
		}
		if body.Depth > 100 {
			rpcError(w, errors.New("directory depth exceeds 100"))
			return
		}
		entries := []any{}
		var walk func(string, uint32) error
		walk = func(dir string, depth uint32) error {
			children, e := os.ReadDir(dir)
			if e != nil {
				return e
			}
			for _, child := range children {
				if len(entries) >= 100000 {
					return errors.New("directory exceeds 100000 entries")
				}
				p := filepath.Join(dir, child.Name())
				item, e := entry(p)
				if e != nil {
					return e
				}
				entries = append(entries, item)
				if child.IsDir() && depth > 1 {
					if e = walk(p, depth-1); e != nil {
						return e
					}
				}
			}
			return nil
		}
		err = walk(path, body.Depth)
		result = map[string]any{"entries": entries}
	default:
		rpcFailure(w, "unimplemented", errors.New("filesystem watchers are not supported"))
		return
	}
	if err != nil {
		rpcError(w, err)
		return
	}
	reply(w, result)
}

func e2bFileError(w http.ResponseWriter, err error) {
	status := 400
	if errors.Is(err, os.ErrNotExist) {
		status = 404
	}
	if errors.Is(err, os.ErrPermission) {
		status = 403
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"code": status, "message": err.Error()})
}
func (s *server) e2bFiles(w http.ResponseWriter, req *http.Request) {
	if err := currentUser(req); err != nil {
		e2bFileError(w, err)
		return
	}
	if req.Method == "GET" {
		path, err := s.resolve(req.URL.Query().Get("path"))
		if err != nil {
			e2bFileError(w, err)
			return
		}
		file, err := os.Open(path)
		if err != nil {
			e2bFileError(w, err)
			return
		}
		defer file.Close()
		st, err := file.Stat()
		if err != nil {
			e2bFileError(w, err)
			return
		}
		if !st.Mode().IsRegular() || st.Size() > s.uploadLimit {
			e2bFileError(w, errors.New("file is not regular or exceeds transfer limit"))
			return
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Content-Length", strconv.FormatInt(st.Size(), 10))
		_, _ = io.Copy(w, io.LimitReader(file, s.uploadLimit))
		return
	}
	if req.Method != "POST" {
		w.WriteHeader(405)
		return
	}
	req.Body = http.MaxBytesReader(w, req.Body, s.uploadLimit)
	var reader io.Reader = req.Body
	if req.Header.Get("Content-Encoding") == "gzip" {
		gz, err := gzip.NewReader(req.Body)
		if err != nil {
			e2bFileError(w, err)
			return
		}
		defer gz.Close()
		reader = gz
	}
	remaining := s.uploadLimit
	written := []any{}
	write := func(name string, data io.Reader) error {
		path, err := s.resolve(name)
		if err != nil {
			return err
		}
		if err = os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			return err
		}
		temp, err := os.CreateTemp(filepath.Dir(path), ".opensandbox-upload-*")
		if err != nil {
			return err
		}
		defer os.Remove(temp.Name())
		defer temp.Close()
		n, err := io.Copy(temp, io.LimitReader(data, remaining+1))
		remaining -= n
		if err != nil {
			return err
		}
		if remaining < 0 {
			return errors.New("upload exceeds transfer limit")
		}
		if err = temp.Chmod(0644); err != nil {
			return err
		}
		if err = temp.Close(); err != nil {
			return err
		}
		if err = os.Rename(temp.Name(), path); err != nil {
			return err
		}
		info, err := entry(path)
		if err != nil {
			return err
		}
		written = append(written, map[string]any{"name": info["name"], "path": path, "type": "file"})
		return nil
	}
	contentType, _, err := mime.ParseMediaType(req.Header.Get("Content-Type"))
	if err != nil {
		e2bFileError(w, err)
		return
	}
	if contentType == "application/octet-stream" {
		err = write(req.URL.Query().Get("path"), reader)
	} else if contentType == "multipart/form-data" {
		mr, e := req.MultipartReader()
		if e != nil {
			e2bFileError(w, e)
			return
		}
		for count := 0; ; count++ {
			part, e := mr.NextPart()
			if e == io.EOF {
				break
			}
			if e != nil {
				err = e
				break
			}
			if count >= 1000 {
				err = errors.New("too many upload files")
				part.Close()
				break
			}
			_, params, e := mime.ParseMediaType(part.Header.Get("Content-Disposition"))
			if e != nil {
				err = e
				part.Close()
				break
			}
			name := req.URL.Query().Get("path")
			if name == "" {
				name = params["filename"]
			}
			if params["name"] != "file" {
				part.Close()
				continue
			}
			err = write(name, part)
			part.Close()
			if err != nil {
				break
			}
		}
	} else {
		err = errors.New("expected multipart/form-data or application/octet-stream")
	}
	if err != nil {
		e2bFileError(w, err)
		return
	}
	reply(w, written)
}
