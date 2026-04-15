use std::error::Error;
use std::fmt::{Display, Formatter};
use std::io::{Read, Write};
use std::net::TcpStream;

#[derive(Debug)]
pub struct ProvenaError(String);

impl Display for ProvenaError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl Error for ProvenaError {}

pub struct ProvenaClient {
    host: String,
    port: u16,
    base_path: String,
    api_key: Option<String>,
    headers: Vec<(String, String)>,
}

impl ProvenaClient {
    pub fn new(base_url: &str) -> Result<Self, ProvenaError> {
        let trimmed = base_url.trim_end_matches('/');
        let without_scheme = trimmed
            .strip_prefix("http://")
            .ok_or_else(|| ProvenaError("only http:// URLs are supported in the std Rust SDK".into()))?;
        let mut parts = without_scheme.splitn(2, '/');
        let authority = parts.next().unwrap_or_default();
        let base_path = format!("/{}", parts.next().unwrap_or_default()).trim_end_matches('/').to_string();
        let mut authority_parts = authority.splitn(2, ':');
        let host = authority_parts
            .next()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ProvenaError("missing host".into()))?
            .to_string();
        let port = authority_parts
            .next()
            .unwrap_or("80")
            .parse::<u16>()
            .map_err(|_| ProvenaError("invalid port".into()))?;

        Ok(Self {
            host,
            port,
            base_path,
            api_key: None,
            headers: Vec::new(),
        })
    }

    pub fn with_api_key(mut self, api_key: impl Into<String>) -> Self {
        self.api_key = Some(api_key.into());
        self
    }

    pub fn with_header(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((key.into(), value.into()));
        self
    }

    pub fn health(&self) -> Result<String, ProvenaError> {
        self.request("GET", "/healthz", None)
    }

    pub fn create_memory(&self, payload: &str) -> Result<String, ProvenaError> {
        self.request("POST", "/v1/memories", Some(payload))
    }

    pub fn get_memory(&self, memory_id: &str) -> Result<String, ProvenaError> {
        self.request("GET", &format!("/v1/memories/{memory_id}"), None)
    }

    pub fn search_memories(&self, payload: &str) -> Result<String, ProvenaError> {
        self.request("POST", "/v1/memories/search", Some(payload))
    }

    pub fn create_relation(&self, payload: &str) -> Result<(), ProvenaError> {
        self.request("POST", "/v1/memories/relations", Some(payload))?;
        Ok(())
    }

    pub fn delete_memory(&self, memory_id: &str, hard_delete: bool) -> Result<String, ProvenaError> {
        let suffix = if hard_delete { "?hard_delete=true" } else { "" };
        self.request("DELETE", &format!("/v1/memories/{memory_id}{suffix}"), None)
    }

    pub fn erase_scope(&self, payload: &str) -> Result<String, ProvenaError> {
        self.request("POST", "/v1/admin/erase", Some(payload))
    }

    fn request(&self, method: &str, path: &str, body: Option<&str>) -> Result<String, ProvenaError> {
        let target = format!("{}{}", self.base_path, path);
        let mut stream = TcpStream::connect((self.host.as_str(), self.port))
            .map_err(|error| ProvenaError(format!("connect failed: {error}")))?;

        let payload = body.unwrap_or("");
        let mut extra_headers = String::new();
        if let Some(api_key) = &self.api_key {
            extra_headers.push_str(&format!("Authorization: Bearer {api_key}\r\n"));
        }
        for (key, value) in &self.headers {
            extra_headers.push_str(&format!("{key}: {value}\r\n"));
        }
        let request = if body.is_some() {
            format!(
                "{method} {target} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n{extra_headers}Connection: close\r\n\r\n{payload}",
                host = self.host,
                length = payload.len()
            )
        } else {
            format!(
                "{method} {target} HTTP/1.1\r\nHost: {host}\r\n{extra_headers}Connection: close\r\n\r\n",
                host = self.host
            )
        };

        stream
            .write_all(request.as_bytes())
            .map_err(|error| ProvenaError(format!("write failed: {error}")))?;

        let mut response = String::new();
        stream
            .read_to_string(&mut response)
            .map_err(|error| ProvenaError(format!("read failed: {error}")))?;

        let (headers, body) = response
            .split_once("\r\n\r\n")
            .ok_or_else(|| ProvenaError("invalid HTTP response".into()))?;
        let status_line = headers.lines().next().unwrap_or_default();
        if !status_line.contains(" 200 ") && !status_line.contains(" 204 ") {
            return Err(ProvenaError(format!("request failed: {status_line}")));
        }
        Ok(body.to_string())
    }
}

pub fn extract_json_string(body: &str, key: &str) -> Option<String> {
    let patterns = [format!("\"{key}\":\""), format!("\"{key}\": \"")];
    for pattern in patterns {
        if let Some(start) = body.find(&pattern) {
            let rest = &body[start + pattern.len()..];
            if let Some(end) = rest.find('"') {
                return Some(rest[..end].to_string());
            }
        }
    }
    None
}
