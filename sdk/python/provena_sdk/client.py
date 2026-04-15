from __future__ import annotations

from typing import Any

import httpx


class ProvenaClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_key = api_key
        self._headers = headers or {}

    def _request_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        response = httpx.request(
            method,
            f"{self._base_url}{path}",
            json=json_body,
            headers=self._request_headers(),
            timeout=self._timeout,
        )
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def create_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/memories", payload)

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/memories/{memory_id}")

    def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/memories/search", payload)

    def create_relation(self, payload: dict[str, Any]) -> None:
        self._request("POST", "/v1/memories/relations", payload)

    def delete_memory(self, memory_id: str, hard_delete: bool = False) -> dict[str, Any]:
        suffix = "?hard_delete=true" if hard_delete else ""
        return self._request("DELETE", f"/v1/memories/{memory_id}{suffix}")

    def erase_scope(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/admin/erase", payload)
