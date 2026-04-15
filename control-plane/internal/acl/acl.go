// Package acl provides access control list management for memory resources.
package acl

// PrincipalType identifies the kind of principal (user, service, group).
type PrincipalType string

const (
	PrincipalUser    PrincipalType = "user"
	PrincipalService PrincipalType = "service"
	PrincipalGroup   PrincipalType = "group"
)

// Permission represents a granular ACL permission.
type Permission string

const (
	PermRead   Permission = "read"
	PermWrite  Permission = "write"
	PermDelete Permission = "delete"
	PermShare  Permission = "share"
	PermAdmin  Permission = "admin"
)

// ACLEntry represents a single access control entry.
type ACLEntry struct {
	PrincipalID   string        `json:"principal_id"`
	PrincipalType PrincipalType `json:"principal_type"`
	Permissions   []Permission  `json:"permissions"`
}

// DefaultACL returns the default ACL where the owner has all permissions.
func DefaultACL(ownerID string) []ACLEntry {
	return []ACLEntry{
		{
			PrincipalID:   ownerID,
			PrincipalType: PrincipalUser,
			Permissions:   []Permission{PermRead, PermWrite, PermDelete, PermShare, PermAdmin},
		},
	}
}

// CheckAccess determines whether a principal has the required permission
// in the given ACL entries.
func CheckAccess(entries []ACLEntry, principalID string, required Permission) bool {
	for _, e := range entries {
		if e.PrincipalID != principalID {
			continue
		}
		for _, p := range e.Permissions {
			if p == required || p == PermAdmin {
				return true
			}
		}
	}
	return false
}

// MergeACL merges two ACL lists, producing a union of permissions per principal.
func MergeACL(a, b []ACLEntry) []ACLEntry {
	index := make(map[string]*ACLEntry)

	addEntries := func(entries []ACLEntry) {
		for _, e := range entries {
			key := string(e.PrincipalType) + ":" + e.PrincipalID
			existing, ok := index[key]
			if !ok {
				entry := ACLEntry{
					PrincipalID:   e.PrincipalID,
					PrincipalType: e.PrincipalType,
					Permissions:   make([]Permission, 0, len(e.Permissions)),
				}
				for _, p := range e.Permissions {
					entry.Permissions = append(entry.Permissions, p)
				}
				index[key] = &entry
			} else {
				permSet := make(map[Permission]bool)
				for _, p := range existing.Permissions {
					permSet[p] = true
				}
				for _, p := range e.Permissions {
					if !permSet[p] {
						existing.Permissions = append(existing.Permissions, p)
						permSet[p] = true
					}
				}
			}
		}
	}

	addEntries(a)
	addEntries(b)

	result := make([]ACLEntry, 0, len(index))
	for _, e := range index {
		result = append(result, *e)
	}
	return result
}
