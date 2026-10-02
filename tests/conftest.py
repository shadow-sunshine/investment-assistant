"""旧 API 回归在明确的本地测试身份下运行；安全攻击测试自行提供凭证。"""
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _legacy_test_identity(monkeypatch, request, tmp_path_factory):
    if request.module.__name__.endswith("test_team_boundaries"):
        return
    from investment_assistant import audit_log, review_publish, research_memory
    state_root = tmp_path_factory.mktemp("team-services")
    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", state_root / "audit-log")
    monkeypatch.setattr(review_publish, "REVIEW_DIR", state_root / "review-records")
    monkeypatch.setattr(review_publish, "PUBLISH_DIR", state_root / "publish-state")
    monkeypatch.setattr(research_memory, "WATCHLIST_PATH", state_root / "watches.json")
    monkeypatch.setattr(research_memory, "MEMORY_PATH", state_root / "memories.json")
    token = "legacy-test-token-" + "x" * 32
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({
        token: {"actor": "test-actor", "tenant": "test-tenant", "roles": ["admin", "analyst", "reviewer", "publisher"]}
    }))
    original = TestClient.request

    def authenticated_request(self, method, url, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("Authorization", f"Bearer {token}")
        kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(TestClient, "request", authenticated_request)
