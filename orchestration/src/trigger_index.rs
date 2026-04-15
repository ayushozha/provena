use dashmap::DashMap;
use serde::{Deserialize, Serialize};

/// A single trigger hit returned from a lookup.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TriggerHit {
    pub phrase: String,
    pub memory_id: String,
    pub match_score: f64,
}

/// Entry stored in the forward index: phrase -> Vec<(memory_id, tenant_id)>.
#[derive(Debug, Clone)]
struct IndexEntry {
    memory_id: String,
    tenant_id: String,
}

/// Concurrent trigger index backed by DashMap.
///
/// Forward map: normalized phrase -> Vec<IndexEntry>
/// Reverse map: memory_id -> Vec<phrase> (for fast removal)
pub struct TriggerIndex {
    forward: DashMap<String, Vec<IndexEntry>>,
    reverse: DashMap<String, Vec<String>>,
}

impl TriggerIndex {
    pub fn new() -> Self {
        Self {
            forward: DashMap::new(),
            reverse: DashMap::new(),
        }
    }

    /// Index a memory with its trigger phrases for a given tenant.
    pub fn index(&self, memory_id: &str, phrases: &[String], tenant_id: &str) {
        let mut reverse_phrases: Vec<String> = Vec::with_capacity(phrases.len());

        for phrase in phrases {
            let normalized = normalize(phrase);
            reverse_phrases.push(normalized.clone());

            self.forward
                .entry(normalized)
                .or_default()
                .push(IndexEntry {
                    memory_id: memory_id.to_string(),
                    tenant_id: tenant_id.to_string(),
                });
        }

        self.reverse
            .entry(memory_id.to_string())
            .or_default()
            .extend(reverse_phrases);
    }

    /// Remove all entries for a given memory_id.
    pub fn remove(&self, memory_id: &str) {
        if let Some((_, phrases)) = self.reverse.remove(memory_id) {
            for phrase in &phrases {
                if let Some(mut entries) = self.forward.get_mut(phrase) {
                    entries.retain(|e| e.memory_id != memory_id);
                }
            }
            // Clean up empty forward entries.
            for phrase in &phrases {
                self.forward
                    .remove_if(phrase, |_, entries| entries.is_empty());
            }
        }
    }

    /// Look up trigger hits for a user message within a specific tenant.
    ///
    /// Generates unigrams and bigrams from the message and checks the index.
    /// Exact phrase match yields score 1.0, substring match yields 0.7.
    pub fn lookup(&self, user_message: &str, tenant_id: &str) -> Vec<TriggerHit> {
        let normalized_msg = normalize(user_message);
        let tokens = tokenize(&normalized_msg);

        // (indexed_phrase, memory_id) -> best score seen so far.
        let mut best: std::collections::HashMap<(String, String), f64> =
            std::collections::HashMap::new();

        // Generate candidate n-grams: unigrams and bigrams.
        let mut candidates: Vec<String> = Vec::new();
        for token in &tokens {
            candidates.push(token.clone());
        }
        for window in tokens.windows(2) {
            candidates.push(format!("{} {}", window[0], window[1]));
        }

        // Pass 1: exact matches (score = 1.0).
        for candidate in &candidates {
            if let Some(entries) = self.forward.get(candidate) {
                for entry in entries.iter() {
                    if entry.tenant_id == tenant_id {
                        let key = (candidate.clone(), entry.memory_id.clone());
                        best.insert(key, 1.0);
                    }
                }
            }
        }

        // Pass 2: substring matches (score = 0.7, only if no better match exists).
        for candidate in &candidates {
            for entry in self.forward.iter() {
                let indexed_phrase = entry.key();
                if indexed_phrase == candidate {
                    continue; // Already handled as exact match.
                }

                let is_substring = indexed_phrase.contains(candidate.as_str())
                    || candidate.contains(indexed_phrase.as_str());

                if is_substring {
                    for index_entry in entry.value().iter() {
                        if index_entry.tenant_id == tenant_id {
                            let key =
                                (indexed_phrase.clone(), index_entry.memory_id.clone());
                            best.entry(key).or_insert(0.7);
                        }
                    }
                }
            }
        }

        // Collect results.
        best.into_iter()
            .map(|((phrase, memory_id), match_score)| TriggerHit {
                phrase,
                memory_id,
                match_score,
            })
            .collect()
    }
}

impl Default for TriggerIndex {
    fn default() -> Self {
        Self::new()
    }
}

/// Normalize a phrase: lowercase, trim, collapse whitespace.
fn normalize(s: &str) -> String {
    s.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Split normalized text into word tokens.
fn tokenize(s: &str) -> Vec<String> {
    s.split_whitespace().map(|w| w.to_string()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_index_and_lookup() {
        let idx = TriggerIndex::new();
        idx.index(
            "mem-1",
            &["project plan".to_string(), "roadmap".to_string()],
            "tenant-a",
        );

        let hits = idx.lookup("tell me about the project plan", "tenant-a");
        assert!(!hits.is_empty());
        assert!(hits.iter().any(|h| h.phrase == "project plan" && h.match_score == 1.0));
    }

    #[test]
    fn test_remove() {
        let idx = TriggerIndex::new();
        idx.index("mem-1", &["alpha".to_string()], "t1");
        idx.remove("mem-1");
        let hits = idx.lookup("alpha", "t1");
        assert!(hits.is_empty());
    }

    #[test]
    fn test_tenant_isolation() {
        let idx = TriggerIndex::new();
        idx.index("mem-1", &["secret".to_string()], "tenant-a");
        let hits = idx.lookup("secret", "tenant-b");
        assert!(hits.is_empty());
    }
}
