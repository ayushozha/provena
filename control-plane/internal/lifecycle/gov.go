// Package lifecycle provides retention policy enforcement, legal hold management,
// and right-to-be-forgotten (RTBF) support for memory governance.
package lifecycle

import (
	"fmt"
	"sync"
	"time"
)

// ---------- Retention ----------

// RetentionAction specifies what happens when a policy triggers.
type RetentionAction string

const (
	ActionArchive RetentionAction = "archive"
	ActionDelete  RetentionAction = "delete"
)

// RetentionPolicy defines how long memories of a certain kind are kept.
type RetentionPolicy struct {
	PolicyID   string          `json:"policy_id"`
	TenantID   string          `json:"tenant_id"`
	Kind       string          `json:"kind"`
	MaxAgeDays int             `json:"max_age_days"`
	Action     RetentionAction `json:"action"`
	CreatedAt  time.Time       `json:"created_at"`
}

// ---------- Legal Hold ----------

// LegalHold prevents deletion of specific memories.
type LegalHold struct {
	HoldID    string    `json:"hold_id"`
	TenantID  string    `json:"tenant_id"`
	MemoryIDs []string  `json:"memory_ids"`
	Reason    string    `json:"reason"`
	HoldUntil time.Time `json:"hold_until"`
	CreatedAt time.Time `json:"created_at"`
}

// ---------- MemoryRecord (for enforcement) ----------

// MemoryRecord is a minimal representation of a memory for governance purposes.
type MemoryRecord struct {
	ID        string
	TenantID  string
	Kind      string
	CreatedAt time.Time
}

// ---------- RTBF Result ----------

// RTBFResult reports the outcome of a right-to-be-forgotten operation.
type RTBFResult struct {
	Deleted []string `json:"deleted"`
	Held    []string `json:"held"`
}

// ---------- GovernanceService ----------

// GovernanceService manages retention policies and legal holds in memory.
type GovernanceService struct {
	mu       sync.RWMutex
	policies map[string]RetentionPolicy // keyed by PolicyID
	holds    map[string]LegalHold       // keyed by HoldID

	// For enforcement simulation: in-memory set of memory records
	memories map[string]MemoryRecord // keyed by memory ID
}

// NewGovernanceService creates a new governance service.
func NewGovernanceService() *GovernanceService {
	return &GovernanceService{
		policies: make(map[string]RetentionPolicy),
		holds:    make(map[string]LegalHold),
		memories: make(map[string]MemoryRecord),
	}
}

// --- Retention Policy ---

// AddPolicy adds or updates a retention policy.
func (gs *GovernanceService) AddPolicy(p RetentionPolicy) {
	gs.mu.Lock()
	defer gs.mu.Unlock()
	if p.CreatedAt.IsZero() {
		p.CreatedAt = time.Now()
	}
	gs.policies[p.PolicyID] = p
}

// ListPolicies returns all retention policies, optionally filtered by tenant.
func (gs *GovernanceService) ListPolicies(tenantID string) []RetentionPolicy {
	gs.mu.RLock()
	defer gs.mu.RUnlock()
	var result []RetentionPolicy
	for _, p := range gs.policies {
		if tenantID == "" || p.TenantID == tenantID {
			result = append(result, p)
		}
	}
	return result
}

// EnforceRetention checks all memories against retention policies and returns
// the IDs of memories that should be acted upon. It respects legal holds.
func (gs *GovernanceService) EnforceRetention(now time.Time) []string {
	gs.mu.RLock()
	defer gs.mu.RUnlock()

	var expired []string
	for _, m := range gs.memories {
		for _, p := range gs.policies {
			if p.TenantID != m.TenantID {
				continue
			}
			if p.Kind != "" && p.Kind != m.Kind {
				continue
			}
			maxAge := time.Duration(p.MaxAgeDays) * 24 * time.Hour
			if now.Sub(m.CreatedAt) > maxAge {
				if !gs.isHeldLocked(m.ID) {
					expired = append(expired, m.ID)
				}
			}
		}
	}
	return expired
}

// --- Legal Hold ---

// PlaceHold creates a legal hold on specified memories.
func (gs *GovernanceService) PlaceHold(h LegalHold) {
	gs.mu.Lock()
	defer gs.mu.Unlock()
	if h.CreatedAt.IsZero() {
		h.CreatedAt = time.Now()
	}
	gs.holds[h.HoldID] = h
}

// ReleaseHold removes a legal hold by ID.
func (gs *GovernanceService) ReleaseHold(holdID string) error {
	gs.mu.Lock()
	defer gs.mu.Unlock()
	if _, ok := gs.holds[holdID]; !ok {
		return fmt.Errorf("hold not found: %s", holdID)
	}
	delete(gs.holds, holdID)
	return nil
}

// IsHeld checks whether a memory is under any active legal hold.
func (gs *GovernanceService) IsHeld(memoryID string) bool {
	gs.mu.RLock()
	defer gs.mu.RUnlock()
	return gs.isHeldLocked(memoryID)
}

func (gs *GovernanceService) isHeldLocked(memoryID string) bool {
	now := time.Now()
	for _, h := range gs.holds {
		if !h.HoldUntil.IsZero() && now.After(h.HoldUntil) {
			continue // expired hold
		}
		for _, id := range h.MemoryIDs {
			if id == memoryID {
				return true
			}
		}
	}
	return false
}

// --- RTBF ---

// RTBF performs a right-to-be-forgotten erasure for a tenant.
// It deletes all memories for the tenant that are not under legal hold.
func (gs *GovernanceService) RTBF(tenantID string) RTBFResult {
	gs.mu.Lock()
	defer gs.mu.Unlock()

	var result RTBFResult
	for id, m := range gs.memories {
		if m.TenantID != tenantID {
			continue
		}
		if gs.isHeldLocked(id) {
			result.Held = append(result.Held, id)
		} else {
			result.Deleted = append(result.Deleted, id)
			delete(gs.memories, id)
		}
	}
	return result
}

// --- Memory record management (for enforcement) ---

// AddMemory registers a memory record for governance tracking.
func (gs *GovernanceService) AddMemory(m MemoryRecord) {
	gs.mu.Lock()
	defer gs.mu.Unlock()
	gs.memories[m.ID] = m
}

// RemoveMemory removes a memory record from governance tracking.
func (gs *GovernanceService) RemoveMemory(id string) {
	gs.mu.Lock()
	defer gs.mu.Unlock()
	delete(gs.memories, id)
}
