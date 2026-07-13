package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"fmt"
	"net/http"
	"strconv"
	"strings"
)

const (
	lifecycleKeyID  = "lifecycle-service"
	lifecycleRole   = "admin"
	lifecycleGroups = "provena-service"
)

var lifecycleLocalEnvironments = map[string]bool{
	"development": true,
	"local":       true,
	"test":        true,
}

type lifecycleServiceAuth struct {
	ingressToken string
	enabled      bool
	token        string
	tenantID     string
	principalID  string
}

func loadLifecycleServiceAuth(getenv func(string) string) (lifecycleServiceAuth, error) {
	environment := strings.ToLower(strings.TrimSpace(getenv("PROVENA_ENVIRONMENT")))
	if environment == "" {
		environment = "development"
	}
	allowLocal := true
	if raw := strings.TrimSpace(getenv("PROVENA_ALLOW_UNAUTHENTICATED_LOCAL")); raw != "" {
		parsed, err := strconv.ParseBool(raw)
		if err != nil {
			return lifecycleServiceAuth{}, fmt.Errorf("PROVENA_ALLOW_UNAUTHENTICATED_LOCAL must be true or false")
		}
		allowLocal = parsed
	}

	auth := lifecycleServiceAuth{
		ingressToken: strings.TrimSpace(getenv("PROVENA_GATEWAY_SERVICE_TOKEN")),
		token:        strings.TrimSpace(getenv("PROVENA_LIFECYCLE_SERVICE_TOKEN")),
		tenantID:     strings.TrimSpace(getenv("PROVENA_LIFECYCLE_TENANT_ID")),
		principalID:  strings.TrimSpace(getenv("PROVENA_LIFECYCLE_PRINCIPAL_ID")),
	}
	// Compose supplies a harmless local principal default. Only a token or tenant
	// opts local development into the service-identity path.
	hasIdentityValue := auth.ingressToken != "" || auth.token != "" || auth.tenantID != ""
	if !hasIdentityValue && allowLocal && lifecycleLocalEnvironments[environment] {
		return auth, nil
	}
	auth.enabled = true
	if err := auth.validate(); err != nil {
		return lifecycleServiceAuth{}, err
	}
	return auth, nil
}

func (auth lifecycleServiceAuth) validate() error {
	if !auth.enabled {
		return nil
	}
	missing := make([]string, 0, 4)
	if auth.ingressToken == "" {
		missing = append(missing, "PROVENA_GATEWAY_SERVICE_TOKEN")
	}
	if auth.token == "" {
		missing = append(missing, "PROVENA_LIFECYCLE_SERVICE_TOKEN")
	}
	if auth.tenantID == "" {
		missing = append(missing, "PROVENA_LIFECYCLE_TENANT_ID")
	}
	if auth.principalID == "" {
		missing = append(missing, "PROVENA_LIFECYCLE_PRINCIPAL_ID")
	}
	if len(missing) > 0 {
		return fmt.Errorf("lifecycle service identity requires %s", strings.Join(missing, ", "))
	}
	if lifecycleTokenEqual(auth.ingressToken, auth.token) {
		return fmt.Errorf("PROVENA_GATEWAY_SERVICE_TOKEN must differ from PROVENA_LIFECYCLE_SERVICE_TOKEN")
	}
	return nil
}

func lifecycleTokenEqual(left, right string) bool {
	leftDigest := sha256.Sum256([]byte(left))
	rightDigest := sha256.Sum256([]byte(right))
	return subtle.ConstantTimeCompare(leftDigest[:], rightDigest[:]) == 1
}

func (auth lifecycleServiceAuth) authenticateIngress(request *http.Request) bool {
	if !auth.enabled {
		return true
	}
	scheme, credential, found := strings.Cut(request.Header.Get("Authorization"), " ")
	credential = strings.TrimSpace(credential)
	return found && strings.EqualFold(strings.TrimSpace(scheme), "Bearer") &&
		credential != "" && lifecycleTokenEqual(credential, auth.ingressToken)
}

func (auth lifecycleServiceAuth) authorizeIngressCaller(request *http.Request) bool {
	if !auth.enabled {
		return true
	}
	if strings.TrimSpace(request.Header.Get("X-Provena-Key-Id")) == "" ||
		strings.TrimSpace(request.Header.Get("X-Provena-Principal-Id")) == "" {
		return false
	}
	role := strings.ToLower(strings.TrimSpace(request.Header.Get("X-Provena-Role")))
	if role == "superadmin" {
		return true
	}
	return role == lifecycleRole &&
		strings.TrimSpace(request.Header.Get("X-Provena-Tenant-Id")) == auth.tenantID
}

func (auth lifecycleServiceAuth) apply(request *http.Request) error {
	if err := auth.validate(); err != nil {
		return err
	}
	if !auth.enabled {
		return nil
	}
	request.Header.Set("Authorization", "Bearer "+auth.token)
	request.Header.Set("X-Provena-Tenant-Id", auth.tenantID)
	request.Header.Set("X-Provena-Role", lifecycleRole)
	request.Header.Set("X-Provena-Key-Id", lifecycleKeyID)
	request.Header.Set("X-Provena-Principal-Id", auth.principalID)
	request.Header.Set("X-Provena-Groups", lifecycleGroups)
	return nil
}
