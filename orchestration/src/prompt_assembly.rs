use serde::{Deserialize, Serialize};

use crate::token_counter::count_tokens;

/// A block of memory content to include in the assembled prompt.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryBlock {
    pub memory_id: String,
    pub kind: String,
    pub title: String,
    pub content: String,
    pub score: f64,
}

/// A citation reference appended at the end of the prompt.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CitationBlock {
    pub memory_id: String,
    pub excerpt: String,
    pub source: String,
}

/// The fully assembled prompt with token count.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssembledPrompt {
    pub text: String,
    pub total_tokens: u32,
}

/// Assemble a structured prompt from its constituent parts.
///
/// Format:
/// ```text
/// <system>
/// {system_prompt}
/// </system>
///
/// <overview>
/// {overview}
/// </overview>
///
/// <memories>
/// [#{index}] ({kind}, score={score:.2}) {title}
/// {content}
///
/// ...
/// </memories>
///
/// <user>
/// {user_message}
/// </user>
///
/// <citations>
/// - [{memory_id}] "{excerpt}" — {source}
/// ...
/// </citations>
/// ```
pub fn assemble(
    system_prompt: &str,
    overview: &str,
    memories: &[MemoryBlock],
    user_message: &str,
    citations: &[CitationBlock],
) -> AssembledPrompt {
    let mut parts: Vec<String> = Vec::new();

    // System prompt section.
    parts.push(format!("<system>\n{}\n</system>", system_prompt));

    // Overview section (only if non-empty).
    if !overview.is_empty() {
        parts.push(format!("<overview>\n{}\n</overview>", overview));
    }

    // Memory blocks section.
    if !memories.is_empty() {
        let mut mem_section = String::from("<memories>");
        for (i, block) in memories.iter().enumerate() {
            mem_section.push_str(&format!(
                "\n[#{}] ({}, score={:.2}) {}\n{}",
                i + 1,
                block.kind,
                block.score,
                block.title,
                block.content,
            ));
        }
        mem_section.push_str("\n</memories>");
        parts.push(mem_section);
    }

    // User message section.
    parts.push(format!("<user>\n{}\n</user>", user_message));

    // Citations section.
    if !citations.is_empty() {
        let mut cite_section = String::from("<citations>");
        for cite in citations {
            cite_section.push_str(&format!(
                "\n- [{}] \"{}\" — {}",
                cite.memory_id, cite.excerpt, cite.source,
            ));
        }
        cite_section.push_str("\n</citations>");
        parts.push(cite_section);
    }

    let text = parts.join("\n\n");
    let total_tokens = count_tokens(&text);

    AssembledPrompt { text, total_tokens }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_assemble_basic() {
        let prompt = assemble(
            "You are an assistant.",
            "Project overview here.",
            &[MemoryBlock {
                memory_id: "m1".into(),
                kind: "note".into(),
                title: "Meeting Notes".into(),
                content: "Discussed roadmap.".into(),
                score: 0.95,
            }],
            "What did we discuss?",
            &[CitationBlock {
                memory_id: "m1".into(),
                excerpt: "Discussed roadmap".into(),
                source: "meeting-notes.md".into(),
            }],
        );

        assert!(prompt.text.contains("<system>"));
        assert!(prompt.text.contains("You are an assistant."));
        assert!(prompt.text.contains("<memories>"));
        assert!(prompt.text.contains("[#1] (note, score=0.95) Meeting Notes"));
        assert!(prompt.text.contains("<user>"));
        assert!(prompt.text.contains("<citations>"));
        assert!(prompt.total_tokens > 0);
    }

    #[test]
    fn test_assemble_empty_memories() {
        let prompt = assemble("sys", "", &[], "hello", &[]);
        assert!(!prompt.text.contains("<memories>"));
        assert!(!prompt.text.contains("<overview>"));
        assert!(!prompt.text.contains("<citations>"));
    }
}
