"""本地演示启动器的失败前置检查与就绪协议。"""

import socket

import pytest

import start_demo


def test_start_requires_auth_configuration(monkeypatch):
    monkeypatch.delenv("IA_AUTH_TOKENS", raising=False)
    with pytest.raises(RuntimeError, match="IA_AUTH_TOKENS"):
        start_demo._preflight()


def test_start_rejects_occupied_port_before_spawning(monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", '{"fixture-token":{"actor":"analyst","tenant":"demo","roles":["analyst"]}}')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        occupied = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="已被占用"):
            start_demo._preflight((occupied, 8501))


def test_login_probe_sends_bearer_token(monkeypatch):
    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout):
        captured.append(request.get_header("Authorization"))
        return Response()

    monkeypatch.setattr(start_demo.urllib.request, "urlopen", fake_urlopen)
    assert start_demo._get("http://127.0.0.1:8000/api/me", "fixture-token")
    assert captured == ["Bearer fixture-token"]
