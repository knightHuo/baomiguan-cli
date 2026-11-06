"""Guided terminal behavior using only local fakes and scripted answers."""

import copy
import signal
from collections import deque
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from baomiguan.cli import TerminalApp
from baomiguan.client import ApiError


class FakeClient:
    def __init__(self):
        self.token = "private-cli-token"

    def set_token(self, token):
        self.token = token

    def check_login(self):
        return {"loggedIn": bool(self.token), "nickname": "测试用户"}


class FakeCatalog:
    def __init__(self):
        self.calls = 0
        self.items = [{"id": "current", "title": "2027年度培训", "year": 2027,
                       "status": "active", "startDate": "2027-01-01", "endDate": "2027-12-31"}]

    def list_courses(self):
        self.calls += 1
        return copy.deepcopy(self.items)

    def resolve(self, year, courses=None):
        return [course for course in courses if course["year"] == year]


class FakeService:
    def __init__(self):
        self.calls = []
        self.study_results = deque()
        self.exam_results = deque()

    def study(self, course_id, *, emit, cancel):
        self.calls.append(("study", course_id))
        if self.study_results:
            return self.study_results.popleft()
        return {"success": True, "progress": {"progressRate": 1}, "message": "学习完成"}

    def exam(self, course_id, *, emit, cancel, allow_retake=False):
        self.calls.append(("exam", course_id, allow_retake))
        if self.exam_results:
            return self.exam_results.popleft()
        return {"success": True, "submitted": True, "score": 80, "message": "考试已提交"}

    def progress(self, course_id):
        return {"progressRate": 1}


@pytest.fixture
def terminal_app():
    output = StringIO()
    console = Console(file=output, width=100, force_terminal=False, color_system=None,
                      highlight=False, no_color=True)
    client, catalog, service = FakeClient(), FakeCatalog(), FakeService()
    vault = SimpleNamespace(load=lambda: None, save=lambda token: True,
                            clear=lambda: None, last_error="")
    updater = SimpleNamespace(check=lambda: {"available": False}, download=lambda: {})
    app = TerminalApp(console=console, client=client, catalog=catalog, service=service,
                      vault=vault, updater=updater, year=2027)
    app.selected = copy.deepcopy(catalog.items[0])
    app.nickname = "测试用户"
    return SimpleNamespace(app=app, output=output, client=client, catalog=catalog, service=service)


def test_one_key_flow_runs_exam_only_after_confirmed_study_success(terminal_app):
    current = terminal_app
    current.app.execute("all")
    assert current.service.calls == [("study", "current"), ("exam", "current", False)]
    assert current.app.progress == {"progressRate": 1}
    assert "current" in current.app.exam_submitted


@pytest.mark.parametrize("result", [
    {"success": False, "message": "学习未完成"},
    {"success": False, "cancelled": True, "message": "已停止"},
    {"success": True, "cancelled": True, "message": "已停止"},
    {"pending": True, "message": "尚待确认"},
])
def test_failed_cancelled_or_unconfirmed_study_never_starts_exam(terminal_app, result):
    current = terminal_app
    current.service.study_results.append(result)
    current.app.execute("all")
    assert current.service.calls == [("study", "current")]
    assert current.app.exam_submitted == set()


def test_ctrl_c_at_end_of_study_does_not_start_exam_even_if_service_just_returned_success(terminal_app):
    current = terminal_app

    def interrupt_after_work(course_id, *, emit, cancel):
        current.service.calls.append(("study", course_id))
        signal.raise_signal(signal.SIGINT)
        assert cancel() is True
        return {"success": True, "message": "刚收到平台成功结果"}

    current.service.study = interrupt_after_work
    current.app.execute("all")
    assert current.service.calls == [("study", "current")]


def test_study_only_does_not_continue_to_exam(terminal_app):
    current = terminal_app
    current.app.execute("study")
    assert current.service.calls == [("study", "current")]


@pytest.mark.parametrize("first_result", [
    {"success": False, "pending": True, "submitted": True, "message": "成绩待确认"},
    {"success": False, "uncertain": True, "message": "提交结果未知"},
    {"success": True, "submitted": True, "score": 100, "message": "平台已确认"},
])
def test_exam_submission_is_not_repeated_in_current_session(terminal_app, first_result):
    current = terminal_app
    current.service.exam_results.append(first_result)
    current.app.execute("exam")
    current.app.execute("exam")
    assert current.service.calls == [("exam", "current", False)]
    assert "未重复提交" in current.output.getvalue()


@pytest.mark.parametrize("score", [0, 60, 100])
def test_existing_grade_including_zero_defaults_to_no_retake(terminal_app, monkeypatch, score):
    current = terminal_app
    current.service.exam_results.append({"success": False, "existing": True, "score": score,
                                        "message": f"已有历史成绩 {score} 分"})
    defaults = []

    def confirm(_prompt, *, default, console):
        defaults.append(default)
        return default

    monkeypatch.setattr("baomiguan.cli.Confirm.ask", confirm)
    current.app.execute("exam")
    assert defaults == [False]
    assert current.service.calls == [("exam", "current", False)]
    assert current.app.exam_submitted == set()


def test_only_explicit_retake_confirmation_allows_second_exam_call(terminal_app, monkeypatch):
    current = terminal_app
    current.service.exam_results.append({"existing": True, "score": 0, "message": "已有历史成绩 0 分"})
    monkeypatch.setattr("baomiguan.cli.Confirm.ask", lambda *args, **kwargs: True)
    current.app.execute("exam")
    assert current.service.calls == [("exam", "current", False), ("exam", "current", True)]
    assert "current" in current.app.exam_submitted


@pytest.mark.parametrize("status", ["ended", "upcoming", "unknown"])
def test_activity_state_is_refetched_before_any_study_or_exam(terminal_app, status):
    current = terminal_app
    current.catalog.items[0]["status"] = status
    current.app.execute("all")
    assert current.catalog.calls == 1
    assert current.service.calls == []
    assert "开放状态已变化" in current.output.getvalue()


def test_missing_activity_does_not_fall_back_to_another_year(terminal_app):
    current = terminal_app
    current.catalog.items = [{"id": "old", "title": "2026年度培训", "year": 2026, "status": "active"}]
    current.app.execute("all")
    assert current.app.selected is None
    assert current.service.calls == []


def test_plain_messages_and_result_panels_both_redact_official_token(terminal_app):
    current = terminal_app
    secret = current.client.token
    current.app.say("error " + secret)
    current.app.show_result({"success": False, "message": "failed " + secret,
                             "errors": ["details " + secret]})
    output = current.output.getvalue()
    assert secret not in output
    assert output.count("已隐藏") == 3


def test_enter_on_active_menu_maps_to_one_key_study_and_exam(terminal_app, monkeypatch):
    current = terminal_app
    monkeypatch.setattr(current.app.console, "input", lambda *args, **kwargs: "")
    assert current.app.menu() == "1"
    assert "一键学习并考试" in current.output.getvalue()


def test_enter_on_inactive_menu_maps_to_refresh_instead_of_business_action(terminal_app, monkeypatch):
    current = terminal_app
    current.app.selected["status"] = "ended"
    monkeypatch.setattr(current.app.console, "input", lambda *args, **kwargs: "")
    assert current.app.menu() == "1"
    assert "刷新活动" in current.output.getvalue()
    assert current.service.calls == []


def test_default_run_path_executes_one_key_operation_then_can_exit(terminal_app, monkeypatch):
    current = terminal_app
    answers = iter(["", "0"])
    monkeypatch.setattr(current.app.console, "input", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(current.app, "pause", lambda: None)
    current.app.run()
    assert current.service.calls == [("study", "current"), ("exam", "current", False)]
    assert "已退出" in current.output.getvalue()


def test_default_run_path_for_ended_activity_only_refreshes_catalog(terminal_app, monkeypatch):
    current = terminal_app
    current.catalog.items[0]["status"] = "ended"
    answers = iter(["", "0"])
    monkeypatch.setattr(current.app.console, "input", lambda *args, **kwargs: next(answers))
    current.app.run()
    assert current.catalog.calls == 2
    assert current.service.calls == []


def test_expired_cached_login_returns_to_login_choices_without_starting_business(terminal_app, monkeypatch):
    current = terminal_app
    checked = []

    def expired_login():
        checked.append(current.client.token)
        return {"loggedIn": False, "nickname": ""}

    monkeypatch.setattr(current.client, "check_login", expired_login)
    monkeypatch.setattr(current.app.console, "input", lambda *args, **kwargs: "0")
    current.app.execute("all")
    assert checked == ["private-cli-token"]
    assert current.client.token == ""
    assert current.app.nickname == ""
    assert current.service.calls == []
    assert "登录状态已过期" in current.output.getvalue()
    assert "扫码登录" in current.output.getvalue()


def test_login_check_network_failure_preserves_token_and_run_only_reports_error(terminal_app, monkeypatch):
    current = terminal_app
    original = current.client.token

    def unavailable_login_check():
        raise ApiError("登录状态检查网络不可用")

    answers = iter(["", "0"])
    monkeypatch.setattr(current.client, "check_login", unavailable_login_check)
    monkeypatch.setattr(current.app.console, "input", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(current.app, "restore_login", lambda: None)
    monkeypatch.setattr(current.app, "pause", lambda: None)
    current.app.run()
    assert current.client.token == original
    assert current.app.nickname == "测试用户"
    assert current.service.calls == []
    assert "登录状态检查网络不可用" in current.output.getvalue()
