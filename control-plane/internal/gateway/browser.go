package gateway

import (
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

// BrowserAccess admits browser traffic from an exact list of origins and request hosts, and nothing else.
// It is for a gateway bound to loopback that a web app on the same machine calls directly: the Host check
// refuses requests that reached this port under another name (DNS rebinding), the Origin check is exact
// (no wildcard, no "null", no prefix or suffix match) and CORS headers are only ever echoed for a listed
// origin. It runs before authentication because a CORS preflight carries no bearer token.
type BrowserAccess struct {
	origins map[string]bool
	hosts   map[string]bool
}

const (
	browserAllowMethods = "GET, POST, PUT, DELETE, OPTIONS"
	browserAllowHeaders = "Authorization, Content-Type"
	browserPreflightTTL = "600"
)

// NewBrowserAccess validates both lists. An origin is exactly scheme://host[:port]; a host is host[:port].
// Both lists must be non-empty so a misconfigured gateway admits no browser at all.
func NewBrowserAccess(origins, hosts []string) (*BrowserAccess, error) {
	access := &BrowserAccess{origins: map[string]bool{}, hosts: map[string]bool{}}
	for _, origin := range origins {
		origin = strings.TrimSpace(origin)
		parsed, err := url.Parse(origin)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
			parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.User != nil ||
			strings.ContainsAny(origin, "* ") {
			return nil, fmt.Errorf("browser origin %q must be an exact origin such as http://localhost:3000", origin)
		}
		access.origins[origin] = true
	}
	for _, host := range hosts {
		host = strings.ToLower(strings.TrimSpace(host))
		if host == "" || strings.ContainsAny(host, "/*@ ") {
			return nil, fmt.Errorf("browser host %q must be host[:port] such as 127.0.0.1:8787", host)
		}
		access.hosts[host] = true
	}
	if len(access.origins) == 0 || len(access.hosts) == 0 {
		return nil, fmt.Errorf("browser access needs at least one allowed origin and one allowed host")
	}
	return access, nil
}

// Middleware refuses a wrong Host (421) or an unlisted Origin (403), answers preflights for listed origins
// and echoes CORS headers only for them. A request without an Origin header (a command-line client) passes
// through to authentication on the Host check alone.
func (b *BrowserAccess) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !b.hosts[strings.ToLower(r.Host)] {
			http.Error(w, `{"error":"request host is not allowed"}`, http.StatusMisdirectedRequest)
			return
		}
		origin := r.Header.Get("Origin")
		if origin == "" {
			next.ServeHTTP(w, r)
			return
		}
		if !b.origins[origin] {
			http.Error(w, `{"error":"origin is not allowed"}`, http.StatusForbidden)
			return
		}
		headers := w.Header()
		headers.Set("Access-Control-Allow-Origin", origin)
		headers.Add("Vary", "Origin")
		if r.Method == http.MethodOptions {
			headers.Set("Access-Control-Allow-Methods", browserAllowMethods)
			headers.Set("Access-Control-Allow-Headers", browserAllowHeaders)
			headers.Set("Access-Control-Max-Age", browserPreflightTTL)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
