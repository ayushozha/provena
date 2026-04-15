use serde::{Deserialize, Serialize};

/// A candidate memory fragment to consider for inclusion.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryCandidate {
    pub memory_id: String,
    pub token_count: u32,
    pub score: f64,
    pub importance: f64,
}

/// Result of the budget allocation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BudgetAllocation {
    pub total_budget: u32,
    pub system_tokens: u32,
    pub user_tokens: u32,
    pub memory_tokens: u32,
    pub remaining_tokens: u32,
    pub selected_memory_ids: Vec<String>,
}

/// Greedy knapsack: sort candidates by weighted_score descending and pack
/// greedily until the memory_tokens budget is exhausted.
///
/// weighted_score = score * 0.6 + importance * 0.4
pub fn allocate(
    candidates: &[MemoryCandidate],
    total_budget: u32,
    system_tokens: u32,
    user_tokens: u32,
) -> BudgetAllocation {
    let reserved = system_tokens.saturating_add(user_tokens);
    let memory_budget = total_budget.saturating_sub(reserved);

    // Sort candidates by weighted score (descending).
    let mut sorted: Vec<&MemoryCandidate> = candidates.iter().collect();
    sorted.sort_by(|a, b| {
        let wa = a.score * 0.6 + a.importance * 0.4;
        let wb = b.score * 0.6 + b.importance * 0.4;
        wb.partial_cmp(&wa).unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut used: u32 = 0;
    let mut selected: Vec<String> = Vec::new();

    for candidate in sorted {
        if used.saturating_add(candidate.token_count) <= memory_budget {
            used += candidate.token_count;
            selected.push(candidate.memory_id.clone());
        }
    }

    let remaining = total_budget.saturating_sub(reserved).saturating_sub(used);

    BudgetAllocation {
        total_budget,
        system_tokens,
        user_tokens,
        memory_tokens: used,
        remaining_tokens: remaining,
        selected_memory_ids: selected,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_greedy_allocation() {
        let candidates = vec![
            MemoryCandidate {
                memory_id: "a".into(),
                token_count: 100,
                score: 0.9,
                importance: 0.8,
            },
            MemoryCandidate {
                memory_id: "b".into(),
                token_count: 200,
                score: 0.5,
                importance: 0.3,
            },
            MemoryCandidate {
                memory_id: "c".into(),
                token_count: 50,
                score: 0.7,
                importance: 0.9,
            },
        ];

        let alloc = allocate(&candidates, 1000, 300, 200);
        // memory_budget = 1000 - 300 - 200 = 500
        // All three fit: 100 + 200 + 50 = 350 <= 500
        assert_eq!(alloc.selected_memory_ids.len(), 3);
        assert_eq!(alloc.memory_tokens, 350);
        assert_eq!(alloc.remaining_tokens, 150);
    }

    #[test]
    fn test_budget_overflow_protection() {
        let candidates = vec![MemoryCandidate {
            memory_id: "big".into(),
            token_count: 5000,
            score: 1.0,
            importance: 1.0,
        }];

        let alloc = allocate(&candidates, 1000, 500, 400);
        // memory_budget = 1000 - 500 - 400 = 100
        // 5000 > 100, so nothing selected.
        assert!(alloc.selected_memory_ids.is_empty());
        assert_eq!(alloc.memory_tokens, 0);
    }
}
