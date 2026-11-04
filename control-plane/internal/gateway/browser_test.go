package gateway

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func browserChain(t *testing.T) (http.Handler, *int) {
	t.Helper()
	access, err := NewBrowserAccess([]string{"http://localhost:3000", "http://127.0.0.1:3000"}, []string{"127.0.0.1:8787", "localhost:8787"})
	if err != nil {
		t.Fatalf("NewBrowserAccess: %v", err)
	}
	reached := 0
	return access.Middleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reached++
		w.WriteHeader(http.StatusNoContent)
	})), &reached
}

func browserRequest(method, host, origin string) *http.Request {
	req := httptest.NewRequest(method, "http://"+host+"/v1/memories/search", nil)
	req.Host = host
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	return req
}

func TestBrowserAccessEchoesCORSOnlyForListedOrigins(t *testing.T) {
	handler, reached := browserChain(t)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, browserRequest(http.MethodPost, "127.0.0.1:8787", "http://localhost:3000"))
	if rec.Code != http.StatusNoContent || *reached != 1 {
		t.Fatalf("listed origin: got %d (reached %d), want 204 reaching the handler once", rec.Code, *reached)
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "http://localhost:3000" {
		t.Fatalf("Access-Control-Allow-Origin: got %q, want the exact origin", got)
	}
	if got := rec.Header().Get("Vary"); got != "Origin" {
		t.Fatalf("Vary: got %q, want Origin", got)
	}
	if got := rec.Header().Get("Access-Control-Allow-Credentials"); got != "" {
		t.Fatalf("credentials must never be allowed, got %q", got)
	}

	for _, origin := range []string{
		"null",
		"*",
		"http://localhost:3001",
		"http://localhost:3000/",
		"https://localhost:3000",
		"http://localhost:3000.evil.example",
		"http://evil.example",
	} {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, browserRequest(http.MethodPost, "127.0.0.1:8787", origin))
		if rec.Code != http.StatusForbidden {
			t.Errorf("origin %q: got %d, want 403", origin, rec.Code)
		}
		if rec.Header().Get("Access-Control-Allow-Origin") != "" {
			t.Errorf("origin %q: CORS header leaked", origin)
		}
	}
	if *reached != 1 {
		t.Fatalf("an unlisted origin reached the handler (%d)", *reached)
	}
}

func TestBrowserAccessAnswersPreflightWithoutReachingAuth(t *testing.T) {
	handler, reached := browserChain(t)
	rec := httptest.NewRecorder()
	req := browserRequest(http.MethodOptions, "localhost:8787", "http://127.0.0.1:3000")
	req.Header.Set("Access-Control-Request-Method", "POST")
	req.Header.Set("Access-Control-Request-Headers", "authorization, content-type")
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent || *reached != 0 {
		t.Fatalf("preflight: got %d (reached %d), want 204 without reaching the handler", rec.Code, *reached)
	}
	want := map[string]string{
		"Access-Control-Allow-Origin":  "http://127.0.0.1:3000",
		"Access-Control-Allow-Methods": browserAllowMethods,
		"Access-Control-Allow-Headers": browserAllowHeaders,
		"Access-Control-Max-Age":       browserPreflightTTL,
	}
	for header, value := range want {
		if got := rec.Header().Get(header); got != value {
			t.Errorf("%s: got %q, want %q", header, got, value)
		}
	}

	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, browserRequest(http.MethodOptions, "localhost:8787", "http://evil.example"))
	if rec.Code != http.StatusForbidden || *reached != 0 {
		t.Fatalf("preflight from an unlisted origin: got %d (reached %d), want 403", rec.Code, *reached)
	}
}

func TestBrowserAccessRefusesOtherHostsBeforeAnythingElse(t *testing.T) {
	handler, reached := browserChain(t)
	for _, host := range []string{"evil.example:8787", "127.0.0.1:8788", "127.0.0.1", "localhost", "rebound.example"} {
		for _, origin := range []string{"", "http://localhost:3000"} {
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, browserRequest(http.MethodGet, host, origin))
			if rec.Code != http.StatusMisdirectedRequest {
				t.Errorf("host %q origin %q: got %d, want 421", host, origin, rec.Code)
			}
		}
	}
	if *reached != 0 {
		t.Fatalf("a wrong host reached the handler (%d)", *reached)
	}

	// No Origin header: a CLI client. The Host check still applies; authentication follows.
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, browserRequest(http.MethodGet, "LOCALHOST:8787", ""))
	if rec.Code != http.StatusNoContent || *reached != 1 {
		t.Fatalf("no origin on a listed host: got %d (reached %d), want 204", rec.Code, *reached)
	}
	if rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("CORS headers must not be sent without an Origin")
	}
}

func TestNewBrowserAccessRejectsLooseConfiguration(t *testing.T) {
	hosts := []string{"127.0.0.1:8787"}
	for _, origins := range [][]string{
		nil,
		{"*"},
		{"null"},
		{"http://*.example"},
		{"localhost:3000"},
		{"http://localhost:3000/app"},
		{"http://user@localhost:3000"},
		{"http://localhost:3000?x=1"},
		{"ftp://localhost:3000"},
	} {
		if _, err := NewBrowserAccess(origins, hosts); err == nil {
			t.Errorf("origins %v: want an error", origins)
		}
	}
	for _, badHosts := range [][]string{nil, {""}, {"*"}, {"127.0.0.1:8787/"}, {"a b"}} {
		if _, err := NewBrowserAccess([]string{"http://localhost:3000"}, badHosts); err == nil {
			t.Errorf("hosts %v: want an error", badHosts)
		}
	}
	if _, err := NewBrowserAccess([]string{"https://app.example"}, []string{"127.0.0.1:8787"}); err != nil {
		t.Fatalf("an exact https origin must be accepted: %v", err)
	}
}
