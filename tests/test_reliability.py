"""调用可靠性与配对报告的离线单测。"""
from types import SimpleNamespace

import pytest

import lme_core
from lme_core import LLMClient, Usage, _is_retryable
from pair_report import compare


class HttpError(Exception):
    def __init__(self, status: int, msg: str = "http error"):
        super().__init__(msg)
        self.status_code = status


class FakeCompletions:
    def __init__(self, exc: Exception | None = None, content: str = "ok"):
        self.exc = exc
        self.content = content
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc
        return SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
        )


def make_client(exc=None, content="ok"):
    c = object.__new__(LLMClient)
    c.model = "fake"
    c.thinking = False
    c.force_reasoning = False
    c.max_retry = 4
    c.usage = Usage()
    c.completions = FakeCompletions(exc, content)
    c.cli = SimpleNamespace(chat=SimpleNamespace(completions=c.completions))
    return c


def test_retryable_classification():
    assert not _is_retryable(HttpError(402))
    assert not _is_retryable(HttpError(400))
    assert not _is_retryable(HttpError(401))
    assert not _is_retryable(HttpError(404))
    assert _is_retryable(HttpError(408))
    assert _is_retryable(HttpError(429))
    assert _is_retryable(HttpError(500))
    assert _is_retryable(RuntimeError("connection reset"))


def test_fatal_4xx_fails_fast(monkeypatch):
    monkeypatch.setattr(lme_core.time, "sleep", lambda *_: None)
    c = make_client(HttpError(402))
    with pytest.raises(RuntimeError, match="不可重试错误 HTTP 402"):
        c.chat("hello")
    assert c.completions.calls == 1
    assert c.usage.fatal_4xx == 1
    assert c.usage.retries == 0


def test_server_error_keeps_backoff_but_no_final_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(lme_core.time, "sleep", lambda s: sleeps.append(s))
    c = make_client(HttpError(500))
    with pytest.raises(RuntimeError, match="调用失败"):
        c.chat("hello")
    assert c.completions.calls == 4
    assert c.usage.retries == 3
    assert sleeps == [1, 2, 4]


def test_empty_response_is_counted_without_exception():
    c = make_client(content="   ")
    assert c.chat("hello") == ""
    assert c.usage.empty_responses == 1


def test_pair_report_counts_fix_break_and_projection():
    base = {
        "a": {"question_id": "a", "question_type": "x", "label": True},
        "b": {"question_id": "b", "question_type": "x", "label": False},
        "c": {"question_id": "c", "question_type": "y", "label": True},
    }
    cand = {
        "a": {"question_id": "a", "question_type": "x", "label": False},
        "b": {"question_id": "b", "question_type": "x", "label": True},
    }
    r = compare(base, cand)
    assert r["n"] == 2 and r["delta"] == 0
    assert r["fix"] == ["b"] and r["break"] == ["a"]
    assert r["projected_full"]["candidate_correct"] == 2
