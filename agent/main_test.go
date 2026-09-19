package main

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCommandOutputAndExit(t *testing.T) {
	s := newServer(t.TempDir(), 32768, 1<<20)
	r, err := s.start(commandRequest{Command: json.RawMessage(`"printf hello; printf problem >&2; exit 7"`)})
	if err != nil {
		t.Fatal(err)
	}
	<-r.done
	info := r.snapshot()
	if info.Stdout != "hello" || info.Stderr != "problem" || *info.ExitCode != 7 || info.Status != "exited" {
		t.Fatalf("bad result: %+v", info)
	}
}
func TestBackgroundKillAndTimeout(t *testing.T) {
	for _, timeout := range []float64{0, 0.1} {
		s := newServer(t.TempDir(), 32768, 1<<20)
		r, err := s.start(commandRequest{Command: json.RawMessage(`"sleep 30 & wait"`), Timeout: timeout, Background: true})
		if err != nil {
			t.Fatal(err)
		}
		if timeout == 0 {
			r.kill("killed")
		}
		select {
		case <-r.done:
		case <-time.After(3 * time.Second):
			t.Fatal("process group did not terminate")
		}
		expected := "killed"
		if timeout > 0 {
			expected = "timed_out"
		}
		if r.snapshot().Status != expected {
			t.Fatalf("wrong status: %+v", r.snapshot())
		}
	}
}
func TestBoundedOutputAndReplay(t *testing.T) {
	s := newServer(t.TempDir(), 32768, 1<<20)
	r, err := s.start(commandRequest{Command: json.RawMessage(`"head -c 200000 /dev/zero"`)})
	if err != nil {
		t.Fatal(err)
	}
	<-r.done
	info := r.snapshot()
	if len(info.Stdout) > 32768 || !info.Truncated {
		t.Fatal("unbounded output")
	}
	req := httptest.NewRequest("GET", "/commands/"+info.ID+"/events", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)
	if !strings.Contains(rec.Body.String(), `"type":"exit"`) || !strings.Contains(rec.Body.String(), `"type":"truncated"`) {
		t.Fatal("stream must report discarded history and final status")
	}
}
func makeTar(t *testing.T, name string, kind byte, data string) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	hdr := &tar.Header{Name: name, Mode: 0644, Typeflag: kind, Size: int64(len(data))}
	if kind == tar.TypeSymlink {
		hdr.Linkname = "/etc/passwd"
		hdr.Size = 0
		data = ""
	}
	if err := tw.WriteHeader(hdr); err != nil {
		t.Fatal(err)
	}
	_, _ = tw.Write([]byte(data))
	_ = tw.Close()
	return buf.Bytes()
}
func TestArchiveTraversalAndSymlinks(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"../escape", "/escape", "a/../../escape"} {
		if err := extract(bytes.NewReader(makeTar(t, name, tar.TypeReg, "bad")), root, 1024); err == nil {
			t.Fatal("accepted traversal", name)
		}
	}
	if err := extract(bytes.NewReader(makeTar(t, "link", tar.TypeSymlink, "")), root, 1024); err == nil {
		t.Fatal("accepted symlink")
	}
	if err := os.Symlink(t.TempDir(), filepath.Join(root, "existing")); err != nil {
		t.Fatal(err)
	}
	if err := extract(bytes.NewReader(makeTar(t, "existing/data", tar.TypeReg, "bad")), root, 1024); err == nil {
		t.Fatal("followed preexisting symlink")
	}
}
func TestFileAndArchiveRoundTrip(t *testing.T) {
	root := t.TempDir()
	s := newServer(root, 32768, 1<<20)
	ts := httptest.NewServer(s.handler())
	defer ts.Close()
	path := filepath.Join(root, "project", "binary.dat")
	data := []byte{0, 255, 10, 13, 0}
	req, _ := http.NewRequest("PUT", ts.URL+"/files?path="+path, bytes.NewReader(data))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal(resp.Status)
	}
	resp, err = http.Get(ts.URL + "/files?path=" + path)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(data, body) {
		t.Fatal("binary content changed")
	}
	resp, err = http.Get(ts.URL + "/archive?path=" + filepath.Dir(path))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	dest := t.TempDir()
	if err = extract(resp.Body, dest, 1<<20); err != nil {
		t.Fatal(err)
	}
	roundtrip, err := os.ReadFile(filepath.Join(dest, "project", "binary.dat"))
	if err != nil || !bytes.Equal(data, roundtrip) {
		t.Fatal("archive roundtrip failed", err)
	}
}
func TestUploadLimitDoesNotReplaceExistingFile(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "data")
	_ = os.WriteFile(path, []byte("old"), 0600)
	s := newServer(root, 32768, 4)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("PUT", "/files?path="+path, strings.NewReader("too long"))
	s.handler().ServeHTTP(rec, req)
	if rec.Code != 413 {
		t.Fatal("expected transfer limit", rec.Code)
	}
	data, _ := os.ReadFile(path)
	if string(data) != "old" {
		t.Fatal("failed transfer replaced existing file")
	}
}
