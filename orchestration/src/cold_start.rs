use dashmap::DashMap;
use serde::{Deserialize, Serialize};

/// Cached project overview for cold-start scenarios.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CachedOverview {
    pub summary: String,
    pub key_entities: Vec<String>,
    pub recent_decisions: Vec<String>,
    pub active_memory_count: u64,
    pub generated_at_epoch: u64,
    pub ttl_seconds: u64,
}

/// Cold-start primer: caches project overviews by "{tenant_id}:{project_id}".
pub struct ColdStartPrimer {
    cache: DashMap<String, CachedOverview>,
}

impl ColdStartPrimer {
    pub fn new() -> Self {
        Self {
            cache: DashMap::new(),
        }
    }

    /// Build the cache key from tenant and project identifiers.
    fn key(tenant_id: &str, project_id: &str) -> String {
        format!("{}:{}", tenant_id, project_id)
    }

    /// Get the cached overview if it exists and has not expired.
    /// Returns `None` if missing or if `now_epoch > generated_at + ttl`.
    pub fn get(&self, tenant_id: &str, project_id: &str, now_epoch: u64) -> Option<CachedOverview> {
        let k = Self::key(tenant_id, project_id);
        self.cache.get(&k).and_then(|entry| {
            let expires_at = entry.generated_at_epoch.saturating_add(entry.ttl_seconds);
            if now_epoch > expires_at {
                None
            } else {
                Some(entry.clone())
            }
        })
    }

    /// Get the cached overview or return a sensible default if missing/expired.
    pub fn get_or_default(
        &self,
        tenant_id: &str,
        project_id: &str,
        now_epoch: u64,
    ) -> CachedOverview {
        self.get(tenant_id, project_id, now_epoch)
            .unwrap_or_else(|| CachedOverview {
                summary: format!(
                    "No cached overview available for project '{}'.",
                    project_id
                ),
                key_entities: Vec::new(),
                recent_decisions: Vec::new(),
                active_memory_count: 0,
                generated_at_epoch: now_epoch,
                ttl_seconds: 300,
            })
    }

    /// Store or replace a cached overview.
    pub fn set(&self, tenant_id: &str, project_id: &str, overview: CachedOverview) {
        let k = Self::key(tenant_id, project_id);
        self.cache.insert(k, overview);
    }

    /// Invalidate (remove) a cached overview.
    pub fn invalidate(&self, tenant_id: &str, project_id: &str) {
        let k = Self::key(tenant_id, project_id);
        self.cache.remove(&k);
    }
}

impl Default for ColdStartPrimer {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_set_and_get() {
        let primer = ColdStartPrimer::new();
        primer.set(
            "t1",
            "p1",
            CachedOverview {
                summary: "Overview".into(),
                key_entities: vec!["Entity A".into()],
                recent_decisions: vec!["Decision 1".into()],
                active_memory_count: 42,
                generated_at_epoch: 1000,
                ttl_seconds: 300,
            },
        );

        let result = primer.get("t1", "p1", 1200);
        assert!(result.is_some());
        assert_eq!(result.unwrap().summary, "Overview");
    }

    #[test]
    fn test_expiry() {
        let primer = ColdStartPrimer::new();
        primer.set(
            "t1",
            "p1",
            CachedOverview {
                summary: "Old".into(),
                key_entities: vec![],
                recent_decisions: vec![],
                active_memory_count: 0,
                generated_at_epoch: 1000,
                ttl_seconds: 100,
            },
        );

        // now_epoch = 1101 > 1000 + 100 = 1100 => expired
        assert!(primer.get("t1", "p1", 1101).is_none());
    }

    #[test]
    fn test_get_or_default() {
        let primer = ColdStartPrimer::new();
        let overview = primer.get_or_default("t1", "missing", 5000);
        assert!(overview.summary.contains("missing"));
        assert_eq!(overview.active_memory_count, 0);
    }

    #[test]
    fn test_invalidate() {
        let primer = ColdStartPrimer::new();
        primer.set(
            "t1",
            "p1",
            CachedOverview {
                summary: "X".into(),
                key_entities: vec![],
                recent_decisions: vec![],
                active_memory_count: 1,
                generated_at_epoch: 1000,
                ttl_seconds: 9999,
            },
        );
        primer.invalidate("t1", "p1");
        assert!(primer.get("t1", "p1", 1000).is_none());
    }
}
