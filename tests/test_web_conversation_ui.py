"""Streamlit 界面接线测试（AppTest，离线 fake 后端）。

边界
----
* 这里验证的是**接线与交互**：会话列表、切换、发送、处理中提示、重命名/删除确认、
  画像确认入口是否真的被调用、状态是否落到后端。
* **没有真实浏览器与响应式验证**：未在宽屏/窄屏真实渲染，
  也不能证明真实模型语义能力或第三方来源可用性。
"""

import json
from pathlib import Path

import pytest
import requests

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from investment_assistant.conversation_store import ConversationStore  # noqa: E402

TOKENS = {"a" * 40: {"actor": "alice", "tenant": "desk-a", "roles": ["analyst", "admin"]}}


def _path_of(url: str) -> str:
    """从完整 URL 里取出路径与查询串。"""
    text = str(url)
    for marker in ("127.0.0.1:8000", "localhost:8000"):
        if marker in text:
            return text.split(marker, 1)[1]
    return text


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeAPI:
    """最小后端替身：只实现界面真正调用的端点，并记录调用。

    实现方式是**替换 ``requests`` 的四个动词**，而不是替换 web_app 里的包装函数。
    原因：AppTest 直接执行脚本文件，脚本内并没有 ``import investment_assistant.web_app``，
    所以 monkeypatch 模块属性不会生效；``import requests`` 拿到的��是同一个全局模块，
    替换其动词才对所有引用点生效。
    """

    def __init__(self, monkeypatch, tmp_path):
        from investment_assistant import audit_log
        monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", tmp_path / "audit")
        self.path = tmp_path / "conversation_memory.sqlite3"
        self.conversations: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.profile: dict[str, dict] = {}
        self.candidates: list[dict] = []
        self.turn_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.fail_turns_with: Exception | None = None
        self._next = 0

    def store(self):
        return ConversationStore(self.path)

    def install(self, monkeypatch):
        real = requests

        def fake_get(url, **kwargs):
            path = _path_of(url)
            self.get_calls.append(path)
            return FakeResponse(self._get(path))

        def fake_post(url, **kwargs):
            path = _path_of(url)
            if path.endswith("/turns") and self.fail_turns_with is not None:
                raise self.fail_turns_with
            return FakeResponse(self._post(path, kwargs.get("json") or {}))

        def fake_put(url, **kwargs):
            return FakeResponse(self._put(_path_of(url), kwargs.get("json") or {}))

        def fake_delete(url, **kwargs):
            return FakeResponse(self._delete(_path_of(url)))

        monkeypatch.setattr(real, "get", fake_get)
        monkeypatch.setattr(real, "post", fake_post)
        monkeypatch.setattr(real, "put", fake_put)
        monkeypatch.setattr(real, "delete", fake_delete)
        return self

    def _identity(self):
        return {"actor_id": "alice", "tenant_id": "desk-a", "roles": ["analyst"]}

    def _get(self, path):
        if path.startswith("/api/me"):
            return self._identity()
        if path.startswith("/api/conversations?"):
            items = list(self.conversations.values())
            items.sort(key=lambda item: item["updated_at"], reverse=True)
            return {"conversations": items, "total": len(items), "has_more": False}
        if path.startswith("/api/conversations/") and path.endswith("/messages"):
            conversation_id = path.split("/")[3]
            return {"conversation_id": conversation_id, "messages": self.messages.get(conversation_id, []),
                    "count": len(self.messages.get(conversation_id, [])),
                    "history_notice": "历史消息按原文展示，不代表其中的财务结论仍为当前已核验的官方事实。"}
        if path.startswith("/api/conversations/"):
            conversation_id = path.split("/")[3]
            if conversation_id not in self.conversations:
                raise requests.HTTPError("404")
            detail = dict(self.conversations[conversation_id])
            detail["state"] = detail.get("state", {})
            return detail
        if path == "/api/user-profile":
            return {"profile": self.profile, "fields": [
                {"field": "risk_preference", "label": "风险偏好（自述）"},
                {"field": "focus_market", "label": "关注市场/方向"}],
                "pending_candidates": [c for c in self.candidates if c["status"] == "pending"],
                "note": "画像是用户自述偏好。"}
        if path == "/api/company-evidence-state":
            return {"companies": []}
        if path == "/api/source-health":
            return {"sources": []}
        if path == "/api/research-tools":
            return {"tools": [], "mcp_exposed": False}
        return {}

    def _new_id(self, prefix):
        self._next += 1
        return f"{prefix}_{self._next:04d}"

    def _post(self, path, payload):
        if path == "/api/conversations":
            conversation_id = self._new_id("conv")
            record = {"conversation_id": conversation_id, "title": payload.get("title", ""),
                      "title_source": "manual" if payload.get("title") else "auto",
                      "answer_mode": payload.get("answer_mode", "official"),
                      "state_version": 0, "state": {},
                      "created_at": f"2026-10-06T00:00:{self._next:02d}",
                      "updated_at": f"2026-10-06T00:00:{self._next:02d}"}
            self.conversations[conversation_id] = record
            self.messages[conversation_id] = []
            return record
        if path.endswith("/rename"):
            conversation_id = path.split("/")[3]
            self.conversations[conversation_id]["title"] = payload["title"]
            self.conversations[conversation_id]["title_source"] = "manual"
            return self.conversations[conversation_id]
        if path.endswith("/answer-mode"):
            conversation_id = path.split("/")[3]
            self.conversations[conversation_id]["answer_mode"] = payload["answer_mode"]
            return self.conversations[conversation_id]
        if path.endswith("/turns"):
            conversation_id = path.split("/")[3]
            self.turn_calls.append({"conversation_id": conversation_id, **payload})
            status = "needs_confirmation" if "偏保守" in payload["text"] else "answered"
            answer_text = ("拟保存以下长期偏好：\n- risk_preference：conservative\n回复“确认”保存。"
                           if status == "needs_confirmation" else
                           "0700.HK 2025年营业收入为 751,766 百万元。[S1]")
            candidate = {"candidate_id": self._new_id("cand"), "field": "risk_preference",
                         "proposed_value": "conservative", "previous_value": None,
                         "op": "create", "status": "pending", "version": 1,
                         "conversation_id": conversation_id}
            if status == "needs_confirmation":
                self.candidates.append(candidate)
            record = self.conversations[conversation_id]
            if "收入" in payload["text"]:
                record["state"] = {**record.get("state", {}), "ticker": "0700.HK", "year": "2025"}
            self.messages[conversation_id].extend([
                {"role": "user", "text": payload["text"], "answer": None},
                {"role": "assistant", "text": answer_text,
                 "answer": {"status": "answered", "intent_label": "年报事实", "latency_ms": 5}}])
            return {"status": status, "message": answer_text,
                    "answer": {"status": "answered", "intent_label": "年报事实", "latency_ms": 5},
                    "candidates": [candidate] if status == "needs_confirmation" else [],
                    "effective_question": payload["text"], "conversation_id": conversation_id,
                    "answer_mode": payload.get("answer_mode", "official"), "summary": "ok",
                    "context": {"sections": [{"order": 2, "name": "profile",
                                              "lines": ["风险偏好（用户自述）：conservative"]}],
                                "profile_applied": True}}
        if path.startswith("/api/user-profile/candidates/") and path.endswith(("/confirm", "/reject")):
            candidate_id = path.split("/")[4]
            action = "confirmed" if path.endswith("confirm") else "rejected"
            for candidate in self.candidates:
                if candidate["candidate_id"] == candidate_id:
                    candidate["status"] = action
                    if action == "confirmed":
                        self.profile[candidate["field"]] = {"field": candidate["field"],
                                                            "value": candidate["proposed_value"],
                                                            "confirmed": True}
            return {"status": action}
        return {}

    def _put(self, path, payload):
        field = path.rsplit("/", 1)[-1]
        self.profile[field] = {"field": field, "value": payload["value"], "confirmed": True}
        return {"field": field, "previous_value": None, "entry": self.profile[field]}

    def _delete(self, path):
        if path.startswith("/api/conversations/"):
            conversation_id = path.split("/")[3]
            self.conversations.pop(conversation_id, None)
            self.messages.pop(conversation_id, None)
            return {"deleted": True}
        if path == "/api/user-profile":
            count = len(self.profile)
            self.profile.clear()
            return {"deleted": count}
        field = path.rsplit("/", 1)[-1]
        self.profile.pop(field, None)
        return {"deleted": True, "field": field}


@pytest.fixture()
def app_env(monkeypatch, tmp_path):
    fake = FakeAPI(monkeypatch, tmp_path).install(monkeypatch)
    return fake


def run_app(monkeypatch, fake):
    """运行 AppTest；token 已在 session_state 中，跳过登录表单。

    返回 ``AppTest`` 本身（不是 ``run()`` 的结果），这样测试后续还能用
    ``at.chat_input[0].set_value(...).run()`` 继续交互。首屏若有异常立刻失败。
    """
    app_path = Path(__file__).resolve().parent.parent / "investment_assistant" / "web_app.py"
    at = AppTest.from_file(str(app_path), default_timeout=60)
    at.session_state["auth_token"] = "a" * 40
    at.session_state["identity"] = fake._identity()
    result = at.run()
    assert not result.exception, [str(item.value) for item in result.exception]
    return at


def _collect_text(node, depth: int = 0) -> list[str]:
    """递归收集容器树里的可见文本。

    ``ChatMessage`` 本身没有 ``value``，它是容器；对话文本在其子节点的
    ``markdown`` / ``text`` / ``caption`` 上，所以必须下钻，不能只读顶层。
    容器会回指父节点，因此必须限深，否则无限递归。
    """
    if depth > 6:
        return []
    parts: list[str] = []
    for attribute in ("value", "body", "label"):
        candidate = getattr(node, attribute, None)
        if isinstance(candidate, str) and candidate:
            parts.append(candidate)
    for child_name in ("markdown", "text", "caption"):
        for child in getattr(node, child_name, None) or []:
            parts.extend(_collect_text(child, depth + 1))
    return parts


def chat_texts(at) -> str:
    """把当前对话流的全部可见文本拼起来，便于断言。"""
    parts: list[str] = []
    for container in (at.chat_message, at.markdown, at.caption, at.info, at.warning, at.error):
        for item in container or []:
            parts.extend(_collect_text(item))
    return " ".join(part for part in parts if part)


def sidebar_button(result, label: str):
    """按标签找侧栏按钮；找不到直接失败并列出实际标签。"""
    for button in result.sidebar.button:
        if button.label == label:
            return button
    raise AssertionError(f"侧栏没有按钮 {label}；实际有：{[b.label for b in result.sidebar.button]}")

def test_sidebar_shows_new_conversation_and_list(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    labels = [button.label for button in at.sidebar.button]
    assert "新对话" in labels
    assert any("最近会话" in str(markdown.value) for markdown in at.sidebar.markdown)
    # 首屏应至少建立一个会话，否则用户一进来没有可写入的容器。
    assert at.session_state["conversation_id"]


def test_main_area_has_quick_questions_and_no_technical_context_meter(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    labels = {button.label for button in at.button}
    assert "腾讯最新官方资料有哪些？" in labels
    rendered = chat_texts(at)
    assert "提醒阈值" not in rendered
    assert "第三方 MCP 数据不与官方证据混用" not in rendered


def test_sending_a_message_reaches_backend_and_renders_answer(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    at.chat_input[0].set_value("腾讯 2025 年营业收入是多少？").run()
    assert app_env.turn_calls, "没有向 /turns 提交"
    assert app_env.turn_calls[-1]["text"] == "腾讯 2025 年营业收入是多少？"
    assert app_env.turn_calls[-1]["request_id"], "必须带 request_id 才能防重复提交"
    rendered = chat_texts(at)
    assert "751,766" in rendered


def test_sending_a_message_shows_user_message_immediately(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    at.chat_input[0].set_value("腾讯 2025 年营业收入是多少？").run()
    texts = chat_texts(at)
    # 用户消息与助手回复都必须出现在对话流里。
    assert "腾讯 2025 年营业收入是多少？" in texts
    assert "751,766" in texts


def test_mode_switch_posts_answer_mode_without_clearing_messages(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    at.chat_input[0].set_value("腾讯 2025 年营业收入是多少？").run()
    before = len(app_env.messages[at.session_state["conversation_id"]])
    at.session_state["previous_answer_mode"] = "official"
    at.session_state["answer_mode"] = "mcp"
    at.run()
    conversation_id = at.session_state["conversation_id"]
    # 消息仍在，且会话模式已更新。
    assert len(app_env.messages[conversation_id]) == before
    assert app_env.conversations[conversation_id]["answer_mode"] == "mcp"


def test_profile_command_creates_confirmation_controls(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    at.chat_input[0].set_value("我偏保守，主要看港股").run()
    # 未确认前不得写入画像。
    assert app_env.profile == {}
    labels = [button.label for button in at.button]
    assert "确认保存" in labels and "取消" in labels
    # 点确认后才写画像。
    at.button(key="profile_confirm").click().run()
    assert "risk_preference" in app_env.profile


def test_profile_reject_leaves_archive_untouched(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    at.chat_input[0].set_value("我偏保守").run()
    at.button(key="profile_reject").click().run()
    assert app_env.profile == {}
    assert all(candidate["status"] == "rejected" for candidate in app_env.candidates)


def test_new_conversation_creates_second_session_in_list(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    first = at.session_state["conversation_id"]
    sidebar_button(at, "新对话").click().run()
    second = at.session_state["conversation_id"]
    assert second != first
    assert first in app_env.conversations and second in app_env.conversations


def test_rename_flow_calls_backend(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    conversation_id = at.session_state["conversation_id"]
    sidebar_button(at, "重命名").click().run()
    at.text_input(key="rename_value").set_value("腾讯收入研究").run()
    at.sidebar.button(key="rename_save").click().run()
    assert app_env.conversations[conversation_id]["title"] == "腾讯收入研究"


def test_delete_requires_confirmation(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    conversation_id = at.session_state["conversation_id"]
    sidebar_button(at, "删除会话").click().run()
    # 仅打开确认框时不得删除。
    assert conversation_id in app_env.conversations
    at.sidebar.button(key="delete_confirm").click().run()
    assert conversation_id not in app_env.conversations


def test_switching_conversation_restores_its_messages(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    at.chat_input[0].set_value("腾讯 2025 年营业收入是多少？").run()
    first = at.session_state["conversation_id"]
    sidebar_button(at, "新对话").click().run()
    second = at.session_state["conversation_id"]
    assert second != first
    # 从列表切回第一个会话，消息应恢复。
    at.sidebar.button(key=f"conv_{first}").click().run()
    assert at.session_state["conversation_id"] == first
    assert "腾讯 2025 年营业收入是多少？" in chat_texts(at)


def test_profile_panel_entry_is_available(app_env, monkeypatch):
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    sidebar_button(at, "研究偏好")
    at.sidebar.button(key="profile_toggle").click().run()
    # 打开后应能看到当前偏好或明确的空态说明。
    sidebar_text = " ".join(str(caption.value) for caption in at.sidebar.caption)
    assert "偏好" in sidebar_text


def test_removed_panels_are_not_brought_back(app_env, monkeypatch):
    """不得为了本任务把工作台/研究记忆卡片/报告任务列表加回侧栏。"""
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    labels = {button.label for button in at.sidebar.button}
    for forbidden in ("工作台", "研究记忆", "报告任务", "已发布报告", "公司发现"):
        assert not any(forbidden in label for label in labels), forbidden


def test_backend_failure_shows_readable_chinese_not_stack_trace(app_env, monkeypatch):
    """超时/不可用必须有中文提示与重试入口，且不出现内部堆栈。"""
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    at = run_app(monkeypatch, app_env)
    app_env.fail_turns_with = requests.Timeout("timed out")
    at.chat_input[0].set_value("腾讯 2025 年营业收入是多少？").run()
    rendered = chat_texts(at)
    assert "超时" in rendered or "重试" in rendered
    assert "Traceback" not in rendered
    # 必须提供重试入口，而不是无限转圈。
    assert any(button.label == "重试本轮" for button in at.button)




