/// Approximate token count by splitting on whitespace and applying a 1.3x
/// multiplier to account for sub-word tokenization.
pub fn count_tokens(text: &str) -> u32 {
    let word_count = text.split_whitespace().count() as f64;
    (word_count * 1.3).ceil() as u32
}

/// Fast byte-based token estimate: ceil(byte_len / 3.5).
pub fn count_tokens_fast(text: &str) -> u32 {
    (text.len() as f64 / 3.5).ceil() as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_count_tokens_empty() {
        assert_eq!(count_tokens(""), 0);
        assert_eq!(count_tokens_fast(""), 0);
    }

    #[test]
    fn test_count_tokens_basic() {
        // 5 words * 1.3 = 6.5 -> ceil = 7
        assert_eq!(count_tokens("hello world how are you"), 7);
    }

    #[test]
    fn test_count_tokens_fast_basic() {
        // "hello" = 5 bytes -> 5 / 3.5 = 1.428 -> ceil = 2
        assert_eq!(count_tokens_fast("hello"), 2);
    }
}
