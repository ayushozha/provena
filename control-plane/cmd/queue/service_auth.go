package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/altrixy/provena/control-plane/internal/observability"
	queuepkg "github.com/altrixy/provena/control-plane/internal/queue"
)

const (
	queueKeyID          = "queue-service"
	queueGroups         = "provena-service"
	maxEnqueueBodyBytes = 1 << 20
)

var localEnvironments = map[string]bool{
	"development": true,
	"local":       true,
	"test":        true,
}

type queueServiceAuth struct {
	ingressToken string
	enabled      bool
	token        string
	tenantID     string
	principalID  string
	role         string
}

func loadQueueServiceAuth(getenv func(string) string) (queueServiceAuth, error) {
	ingressToken := strings.TrimSpace(getenv("PROVENA_QUEUE_INGRESS_TOKEN"))
	if ingressToken == "" {
		return queueServiceAuth{}, fmt.Errorf("PROVENA_QUEUE_INGRESS_TOKEN is required")
	}
	environment := strings.ToLower(strings.TrimSpace(getenv("PROVENA_ENVIRONMENT")))
	if environment == "" {
		environment = "development"
	}
	allowLocal := true
	if raw := strings.TrimSpace(getenv("PROVENA_ALLOW_UNAUTHENTICATED_LOCAL")); raw != "" {
		parsed, err := strconv.ParseBool(raw)
		if err != nil {
			return queueServiceAuth{}, fmt.Errorf("PROVENA_ALLOW_UNAUTHENTICATED_LOCAL must be true or false")
		}
		allowLocal = parsed
	}

	token := strings.TrimSpace(getenv("PROVENA_SERVICE_TOKEN"))
	tenantID := strings.TrimSpace(getenv("PROVENA_SERVICE_TENANT_ID"))
	principalID := strings.TrimSpace(getenv("PROVENA_SERVICE_PRINCIPAL_ID"))
	rawRole := strings.TrimSpace(getenv("PROVENA_SERVICE_ROLE"))
	// Principal and role have harmless local defaults in the shared store
	// configuration. A token or tenant opts the queue into service auth; outside
	// explicit local bypass the complete identity is always mandatory.
	hasIdentityValue := token != "" || tenantID != ""
	if !hasIdentityValue && allowLocal && localEnvironments[environment] {
		return queueServiceAuth{ingressToken: ingressToken}, nil
	}
	role := strings.ToLower(rawRole)
	if role == "" {
		role = "editor"
	}
	auth := queueServiceAuth{
		ingressToken: ingressToken,
		enabled:      true,
		token:        token,
		tenantID:     tenantID,
		principalID:  principalID,
		role:         role,
	}
	if err := auth.validate(); err != nil {
		return queueServiceAuth{}, err
	}
	return auth, nil
}

func (auth queueServiceAuth) validate() error {
	if strings.TrimSpace(auth.ingressToken) == "" {
		return fmt.Errorf("PROVENA_QUEUE_INGRESS_TOKEN is required")
	}
	return auth.validateDownstream()
}

func (auth queueServiceAuth) validateDownstream() error {
	if !auth.enabled {
		return nil
	}
	missing := make([]string, 0, 3)
	if strings.TrimSpace(auth.token) == "" {
		missing = append(missing, "PROVENA_SERVICE_TOKEN")
	}
	if strings.TrimSpace(auth.tenantID) == "" {
		missing = append(missing, "PROVENA_SERVICE_TENANT_ID")
	}
	if strings.TrimSpace(auth.principalID) == "" {
		missing = append(missing, "PROVENA_SERVICE_PRINCIPAL_ID")
	}
	if len(missing) > 0 {
		return fmt.Errorf("queue service identity requires %s", strings.Join(missing, ", "))
	}
	if auth.role != "editor" && auth.role != "admin" {
		return fmt.Errorf("PROVENA_SERVICE_ROLE must be editor or admin for queue writes")
	}
	if tokenEqual(auth.ingressToken, auth.token) {
		return fmt.Errorf("PROVENA_QUEUE_INGRESS_TOKEN must differ from PROVENA_SERVICE_TOKEN")
	}
	return nil
}

func tokenEqual(left, right string) bool {
	leftDigest := sha256.Sum256([]byte(left))
	rightDigest := sha256.Sum256([]byte(right))
	return subtle.ConstantTimeCompare(leftDigest[:], rightDigest[:]) == 1
}

func (auth queueServiceAuth) authenticateIngress(request *http.Request) bool {
	scheme, credential, found := strings.Cut(request.Header.Get("Authorization"), " ")
	credential = strings.TrimSpace(credential)
	return found && strings.EqualFold(strings.TrimSpace(scheme), "Bearer") &&
		credential != "" && tokenEqual(credential, auth.ingressToken)
}

func (auth queueServiceAuth) validateTenant(tenantID string) error {
	if !auth.enabled {
		return nil
	}
	if strings.TrimSpace(tenantID) == "" || strings.TrimSpace(tenantID) != auth.tenantID {
		return fmt.Errorf("queue item tenant does not match worker tenant")
	}
	return nil
}

func (auth queueServiceAuth) apply(req *http.Request) error {
	if err := auth.validateDownstream(); err != nil {
		return err
	}
	if !auth.enabled {
		return nil
	}
	req.Header.Set("Authorization", "Bearer "+auth.token)
	req.Header.Set("X-Provena-Tenant-Id", auth.tenantID)
	req.Header.Set("X-Provena-Role", auth.role)
	req.Header.Set("X-Provena-Key-Id", queueKeyID)
	req.Header.Set("X-Provena-Principal-Id", auth.principalID)
	req.Header.Set("X-Provena-Groups", queueGroups)
	return nil
}

func newPipelineProcessFn(client *http.Client, pipelineURL string, auth queueServiceAuth) queuepkg.ProcessFunc {
	baseURL := strings.TrimRight(pipelineURL, "/")
	return func(ctx context.Context, batch []queuepkg.WriteItem) error {
		if err := auth.validateDownstream(); err != nil {
			return err
		}
		for _, item := range batch {
			if err := auth.validateTenant(item.TenantID); err != nil {
				return err
			}
		}
		payload, err := json.Marshal(batch)
		if err != nil {
			return fmt.Errorf("marshal batch: %w", err)
		}
		req, err := http.NewRequestWithContext(
			ctx,
			http.MethodPost,
			baseURL+"/v1/batch-write",
			bytes.NewReader(payload),
		)
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")
		if err := auth.apply(req); err != nil {
			return err
		}
		resp, err := client.Do(req)
		if err != nil {
			return fmt.Errorf("pipeline request: %w", err)
		}
		defer resp.Body.Close()
		if resp.StatusCode >= http.StatusBadRequest {
			return fmt.Errorf("pipeline returned %d", resp.StatusCode)
		}
		return nil
	}
}

func newEnqueueHandler(wq *queuepkg.WriteQueue, auth queueServiceAuth) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !auth.authenticateIngress(r) {
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, `{"error":"valid queue ingress bearer required"}`, http.StatusUnauthorized)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxEnqueueBodyBytes)
		decoder := json.NewDecoder(r.Body)
		var item queuepkg.WriteItem
		if err := decoder.Decode(&item); err != nil {
			writeEnqueueBodyError(w, err)
			return
		}
		var trailing any
		if err := decoder.Decode(&trailing); err != io.EOF {
			writeEnqueueBodyError(w, err)
			return
		}
		if err := auth.validateTenant(item.TenantID); err != nil {
			http.Error(w, `{"error":"queue tenant mismatch"}`, http.StatusForbidden)
			return
		}
		if item.CreatedAt.IsZero() {
			item.CreatedAt = time.Now()
		}
		if err := wq.Enqueue(item); err != nil {
			observability.QueueDepth.Set(int64(wq.Depth()))
			http.Error(w, `{"error":"queue full"}`, http.StatusTooManyRequests)
			return
		}
		observability.QueueDepth.Set(int64(wq.Depth()))
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"status":"accepted"}`))
	}
}

func writeEnqueueBodyError(w http.ResponseWriter, err error) {
	var oversized *http.MaxBytesError
	if errors.As(err, &oversized) {
		http.Error(w, `{"error":"request body exceeds 1 MiB"}`, http.StatusRequestEntityTooLarge)
		return
	}
	http.Error(w, `{"error":"invalid request body"}`, http.StatusBadRequest)
}
