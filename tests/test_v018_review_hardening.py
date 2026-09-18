from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from core import ai_client
from core.wcl_client import GRAPHQL_URL, TOKEN_URL, WCLClient


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_wcl_forks_share_token_refresh_without_refresh_storm():
    parent = WCLClient("client", "secret")
    a = parent.fork()
    b = parent.fork()
    calls = 0
    lock = threading.Lock()

    def post(url, **kwargs):
        nonlocal calls
        assert url == TOKEN_URL
        with lock:
            calls += 1
        time.sleep(0.04)
        return FakeResponse(200, {"access_token": "shared-token", "expires_in": 3600})

    a._session.post = post
    b._session.post = post
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda c: c._get_token(), (a, b)))

    assert tokens == ["shared-token", "shared-token"]
    assert calls == 1
    assert parent._auth_state["token"] == "shared-token"
    assert parent.diagnostics()["token_refreshes"] == 1


def test_wcl_singleflight_deduplicates_same_event_download(monkeypatch):
    parent = WCLClient("client", "secret")
    child = parent.fork()
    calls = 0
    lock = threading.Lock()

    def fake_page(self, *args, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.05)
        return {
            "reportData": {
                "report": {
                    "events": {
                        "data": [{"timestamp": 1, "type": "cast", "abilityGameID": 123}],
                        "nextPageTimestamp": None,
                    }
                }
            }
        }

    monkeypatch.setattr(WCLClient, "report_events_page", fake_page)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(c.report_events, "ABC", 1, "Casts", 99)
            for c in (parent, child)
        ]
        rows = [f.result() for f in futures]

    assert rows[0] == rows[1]
    assert calls == 1
    assert parent.diagnostics()["singleflight_waits"] >= 1


def test_wcl_query_reauthenticates_once_after_401():
    client = WCLClient("client", "secret")
    client._auth_state.update({"token": "old-token", "expires_at": time.time() + 3600})
    client._token = "old-token"
    client._expires_at = time.time() + 3600
    calls: list[str] = []

    def post(url, **kwargs):
        calls.append(url)
        if url == GRAPHQL_URL and calls.count(GRAPHQL_URL) == 1:
            return FakeResponse(401, {"message": "expired"})
        if url == TOKEN_URL:
            return FakeResponse(200, {"access_token": "new-token", "expires_in": 3600})
        if url == GRAPHQL_URL:
            return FakeResponse(200, {"data": {"ok": True}})
        raise AssertionError(url)

    client._session.post = post
    assert client.query("query { ok }") == {"ok": True}
    assert calls == [GRAPHQL_URL, TOKEN_URL, GRAPHQL_URL]
    diag = client.diagnostics()
    assert diag["auth_retries"] == 1
    assert diag["token_refreshes"] == 1
    assert diag["network_queries"] == 2


def test_coach_pipeline_reuses_identical_result(monkeypatch):
    ai_client._COACH_CACHE.clear()
    calls = {"analysis": 0, "editor": 0}

    def fake_analysis(*args, **kwargs):
        calls["analysis"] += 1
        return {"executive_summary": ["测试结论"], "overall_confidence": "high"}

    def fake_editor(*args, **kwargs):
        calls["editor"] += 1
        return "# 战斗教练报告\n\n测试结论"

    monkeypatch.setattr(ai_client, "analyze_with_deepseek_json", fake_analysis)
    monkeypatch.setattr(ai_client, "write_player_report_with_deepseek", fake_editor)

    kwargs = dict(
        api_key="secret-key",
        payload={"fight": 1, "skills": [{"name": "A", "casts": 3}]},
        model="deepseek-flash",
        source_summary={"samples": 5},
        memory_context={"history": 2},
        knowledge_context={"cohort": 4},
        speed_mode="fast",
    )
    first = ai_client.run_coach_pipeline(**kwargs)
    second = ai_client.run_coach_pipeline(**kwargs)

    assert calls == {"analysis": 1, "editor": 1}
    assert first[1] == second[1]
    assert first[2]["cache_hit"] is False
    assert second[2]["cache_hit"] is True
    assert second[2]["total_seconds"] == 0.0


def test_coach_pipeline_singleflight_deduplicates_concurrent_calls(monkeypatch):
    ai_client._COACH_CACHE.clear()
    ai_client._COACH_INFLIGHT.clear()
    calls = {"analysis": 0, "editor": 0}
    lock = threading.Lock()

    def fake_analysis(*args, **kwargs):
        with lock:
            calls["analysis"] += 1
        time.sleep(0.05)
        return {"executive_summary": ["并发测试"], "overall_confidence": "high"}

    def fake_editor(*args, **kwargs):
        with lock:
            calls["editor"] += 1
        time.sleep(0.05)
        return "# 战斗教练报告\n\n并发测试"

    monkeypatch.setattr(ai_client, "analyze_with_deepseek_json", fake_analysis)
    monkeypatch.setattr(ai_client, "write_player_report_with_deepseek", fake_editor)
    kwargs = dict(
        api_key="secret-key",
        payload={"fight": 9, "skills": [{"name": "A", "casts": 5}]},
        model="deepseek-flash",
        source_summary={"samples": 8},
        memory_context={"history": 3},
        knowledge_context={"cohort": 6},
        speed_mode="fast",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ai_client.run_coach_pipeline(**kwargs), range(2)))

    assert calls == {"analysis": 1, "editor": 1}
    assert results[0][1] == results[1][1]
    assert sorted(bool(r[2]["cache_hit"]) for r in results) == [False, True]


def test_concurrent_401s_share_one_reauthentication():
    parent = WCLClient("client", "secret")
    a = parent.fork()
    b = parent.fork()
    expires = time.time() + 3600
    parent._auth_state.update({"token": "stale", "expires_at": expires})
    for c in (parent, a, b):
        c._token = "stale"
        c._expires_at = expires

    token_calls = 0
    graphql_calls = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def make_post(worker_name):
        first_graphql = True

        def post(url, **kwargs):
            nonlocal token_calls, graphql_calls, first_graphql
            if url == TOKEN_URL:
                with lock:
                    token_calls += 1
                time.sleep(0.03)
                return FakeResponse(200, {"access_token": "fresh", "expires_in": 3600})
            if url == GRAPHQL_URL:
                with lock:
                    graphql_calls += 1
                auth = (kwargs.get("headers") or {}).get("Authorization")
                if first_graphql and auth == "Bearer stale":
                    first_graphql = False
                    barrier.wait(timeout=2)
                    return FakeResponse(401, {"message": "expired"})
                assert auth == "Bearer fresh"
                return FakeResponse(200, {"data": {"worker": worker_name}})
            raise AssertionError(url)

        return post

    a._session.post = make_post("a")
    b._session.post = make_post("b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: c.query("query { ok }"), (a, b)))

    assert {row["worker"] for row in results} == {"a", "b"}
    assert token_calls == 1
    assert graphql_calls == 4
    assert parent.diagnostics()["token_refreshes"] == 1
    assert parent.diagnostics()["auth_retries"] == 2


def test_desktop_build_workflows_use_shared_hardened_scripts():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    release = (root / ".github" / "workflows" / "build-desktop.yml").read_text(encoding="utf-8")
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    win = (root / "build_windows.ps1").read_text(encoding="utf-8")
    mac = (root / "build_macos_app.sh").read_text(encoding="utf-8")

    assert "./build_windows.ps1" in release
    assert "./build_macos_app.sh" in release
    assert "--onefile" not in release
    assert "--self-test" in win
    assert "function Invoke-Checked" in win
    assert "PyInstaller 构建" in win
    assert "--self-test" in mac
    assert "windows-latest" in ci
    assert "upload-artifact@v4" in ci
