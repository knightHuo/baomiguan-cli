"""A small, guided terminal flow; the common path needs no configuration."""

from __future__ import annotations

import signal
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from . import __version__
from .catalog import Catalog
from .client import BaomiClient
from .credentials import CredentialVault
from .qr import render_qr
from .updates import UpdateService
from .workflows import CourseService

ACCENT = "#79b39a"
GOLD = "#c8ae7b"
STATUS = {"active": "进行中", "ended": "已结束", "upcoming": "尚未开始", "unknown": "待确认"}
OFFICIAL_COURSES = "https://www.baomi.org.cn/bmCourse"


class TerminalApp:
    def __init__(self, *, console=None, client=None, catalog=None, vault=None, updater=None,
                 service=None, year=None):
        self.console = console or Console(highlight=False)
        self.client = client or BaomiClient()
        self.catalog = catalog or Catalog(self.client)
        self.vault = vault or CredentialVault()
        self.updater = updater or UpdateService()
        self.service = service or CourseService(self.client)
        self.year = year or datetime.now(timezone(timedelta(hours=8))).year
        self.courses = []
        self.selected = None
        self.nickname = ""
        self.progress = None
        self.exam_submitted = set()
        self.latest_update = None
        self.update_announced = False

    def say(self, message, style=None):
        token = self.client.token
        safe = str(message).replace(token, "[已隐藏]") if token else str(message)
        self.console.print(Text("  " + safe, style=style))

    def ask(self, label, choices, default=None):
        return Prompt.ask("  " + label, console=self.console, choices=choices, default=default,
                          show_choices=False, show_default=False)

    def pause(self):
        self.console.input("\n  按回车返回。")

    def header(self):
        self.console.print()
        heading = Text("  保密观", style=f"bold {ACCENT}")
        heading.append("  /  年度学习", style="dim")
        header = Table.grid(expand=True)
        header.add_column()
        header.add_column(justify="right")
        header.add_row(heading, Text(f"v{__version__}  ", style="dim"))
        self.console.print(header)
        self.console.print(Text("  自动发现年度活动 · 扫码登录 · 回车开始", style="dim"))
        self.console.print()

    def home(self, demo=False):
        if not demo and self.console.is_terminal:
            self.console.clear()
        self.header()
        if demo:
            self.say("演示界面 · 示例数据，不连接真实账户、不提交记录。", GOLD)
            self.console.print()
        table = Table.grid(padding=(0, 2))
        table.add_column(width=8, style="dim", no_wrap=True)
        table.add_column()
        table.add_row("年度", Text(str(self.year), style="bold"))
        table.add_row("账号", Text(self.nickname or "尚未登录", style=ACCENT if self.nickname else "dim"))
        if self.selected:
            table.add_row("活动", Text(self.selected["title"]))
            status = self.selected.get("status", "unknown")
            table.add_row("状态", Text(STATUS.get(status, "待确认"), style=ACCENT if status == "active" else GOLD))
            start, end = self.selected.get("startDate"), self.selected.get("endDate")
            if start and end:
                table.add_row("日期", Text(f"{str(start)[:10]} — {str(end)[:10]}", style="dim"))
        else:
            table.add_row("活动", Text("尚未找到本年度活动", style=GOLD))
        if self.progress and self.progress.get("progressRate") is not None:
            try:
                rate = max(0.0, min(1.0, float(self.progress["progressRate"])))
                table.add_row("进度", Text(f"{round(rate * 100)}%", style=ACCENT))
            except (TypeError, ValueError):
                pass
        self.console.print(Panel(table, border_style="#527b68", box=box.ROUNDED,
                                 padding=(1, 2), width=min(94, self.console.width)))
        if self.latest_update and self.latest_update.get("available") and not self.update_announced:
            self.say(f"有新版本 {self.latest_update['latestVersion']}，可在「更多选项」中下载。", GOLD)
            self.update_announced = True

    def menu(self):
        self.console.print()
        if self.selected and self.selected.get("status") == "active":
            primary = Text("  [回车] ", style=f"bold {ACCENT}")
            primary.append("一键学习并考试", style="bold")
            primary.append("   学习完成后，提交一次本期考试", style="dim")
            self.console.print(primary)
            self.say("[2] 仅学习     [3] 仅考试     [4] 更多选项     [0] 退出", "dim")
            self.console.print()
            return self.ask("请选择", ["1", "2", "3", "4", "0"], "1")
        self.say("本期未开放，暂不启动学习或考试。", GOLD)
        self.say("[回车] 刷新活动     [2] 选择年度     [3] 检查更新     [0] 退出", "dim")
        self.console.print()
        return self.ask("请选择", ["1", "2", "3", "0"], "1")

    def load_catalog(self):
        with self.console.status("  正在查找年度活动…", spinner="dots", spinner_style=ACCENT):
            self.courses = self.catalog.list_courses()
        matches = self.catalog.resolve(self.year, self.courses)
        previous = self.selected["id"] if self.selected else None
        self.selected = next((item for item in matches if item["id"] == previous), None)
        if not self.selected and len(matches) == 1:
            self.selected = matches[0]
        elif not self.selected and len(matches) > 1:
            self.choose_course(matches)
        self.progress = None

    def choose_course(self, matches):
        table = Table(box=box.SIMPLE, show_edge=False, padding=(0, 1))
        table.add_column("", style=ACCENT, width=3)
        table.add_column("培训活动")
        table.add_column("状态", style="dim")
        for index, item in enumerate(matches, 1):
            table.add_row(str(index), Text(item["title"]), STATUS.get(item.get("status"), "待确认"))
        self.console.print(table)
        value = self.ask("请选择本次活动（0 返回）", [str(i) for i in range(len(matches) + 1)])
        self.selected = matches[int(value) - 1] if value != "0" else None

    def change_year(self):
        self.say("可用年度：" + "、".join(str(value) for value in sorted(
            {item["year"] for item in self.courses if item.get("year")}, reverse=True)), "dim")
        while True:
            value = Prompt.ask("  查看哪一年", default=str(self.year), console=self.console)
            if value.isdigit() and 2000 <= int(value) <= 2100:
                self.year = int(value)
                self.selected = None
                self.load_catalog()
                return
            self.say("请输入四位年度，例如 2026。", GOLD)

    def restore_login(self):
        token = self.vault.load()
        if not token:
            return
        self.client.set_token(token)
        try:
            with self.console.status("  正在恢复登录…", spinner_style=ACCENT):
                state = self.client.check_login()
            if state.get("loggedIn"):
                self.nickname = state.get("nickname") or "已登录用户"
                return
        except Exception:
            pass
        self.client.set_token("")
        self.say("保存的登录状态暂不可用，开始时会请你重新登录。", "dim")

    def save_login(self, result):
        self.nickname = result.get("nickname") or "已登录用户"
        if not self.vault.save(self.client.token):
            self.say("本次登录有效，系统凭据库暂不可用，退出后需重新登录。", GOLD)
        self.say(f"已连接：{self.nickname}", ACCENT)

    def qr_login(self):
        with self.console.status("  正在生成登录二维码…", spinner_style=ACCENT):
            code = self.client.create_qr()
        if not render_qr(code["content"], console=self.console):
            return False
        self.say("用保密观 App 扫码并确认；按 Ctrl+C 返回其他登录方式。", "dim")
        deadline, failures = time.monotonic() + 300, 0
        try:
            with self.console.status("  等待扫码确认…", spinner_style=ACCENT):
                while time.monotonic() < deadline:
                    time.sleep(3)
                    try:
                        result = self.client.poll_qr(code["qrToken"])
                        failures = 0
                    except Exception:
                        failures += 1
                        if failures >= 3:
                            raise ValueError("扫码连接暂不稳定，请重试或切换账号密码登录。") from None
                        continue
                    if result.get("state") == "confirmed":
                        self.save_login(result)
                        return True
                    if result.get("state") == "expired":
                        self.say("二维码已过期，请重新生成。", GOLD)
                        return False
            self.say("等待扫码超时，请重新登录。", GOLD)
        except KeyboardInterrupt:
            self.say("已返回登录选项。", "dim")
        return False

    def login(self):
        if self.client.token and self.nickname:
            with self.console.status("  正在确认登录状态…", spinner_style=ACCENT):
                state = self.client.check_login()
            if state.get("loggedIn"):
                self.nickname = state.get("nickname") or self.nickname
                return True
            self.client.set_token("")
            self.nickname = ""
            self.say("登录状态已过期，请重新连接。", GOLD)
        while True:
            self.console.print()
            self.say("连接保密观", f"bold {ACCENT}")
            self.say("[回车] 扫码登录（推荐）     [2] 账号密码     [0] 返回", "dim")
            self.say("仅保存登录状态到系统凭据库，不保存密码。", "dim")
            choice = self.ask("登录方式", ["1", "2", "0"], "1")
            if choice == "0":
                return False
            try:
                if choice == "1":
                    if self.qr_login():
                        return True
                else:
                    username = Prompt.ask("  账号", console=self.console).strip()
                    password = Prompt.ask("  密码", password=True, console=self.console)
                    try:
                        with self.console.status("  正在登录…", spinner_style=ACCENT):
                            result = self.client.login_password(username, password)
                    finally:
                        password = None
                    self.save_login(result)
                    return True
            except Exception as exc:
                self.say(exc, GOLD)

    def refresh_progress(self):
        if self.selected and self.nickname:
            with self.console.status("  正在读取平台进度…", spinner_style=ACCENT):
                self.progress = self.service.progress(self.selected["id"])

    def _perform(self, title, action):
        cancelled = Event()
        old_handler = signal.getsignal(signal.SIGINT)

        def stop(_signum, _frame):
            cancelled.set()

        signal.signal(signal.SIGINT, stop)
        latest = Text("正在准备…", style="dim")
        try:
            with Live(Group(Text("  " + title, style=f"bold {ACCENT}"),
                            Spinner("dots", text=latest, style=ACCENT)),
                      console=self.console, refresh_per_second=5, transient=True) as live:
                def emit(message):
                    safe = str(message).replace(self.client.token, "[已隐藏]") if self.client.token else str(message)
                    latest = Text(safe, style=GOLD if cancelled.is_set() else "dim")
                    if cancelled.is_set():
                        latest.append(" · 正在停止后续请求", style=GOLD)
                    live.update(Group(Text("  " + title, style=f"bold {ACCENT}"),
                                      Spinner("dots", text=latest, style=ACCENT)))
                result = action(emit, cancelled.is_set)
        finally:
            signal.signal(signal.SIGINT, old_handler)
        if cancelled.is_set():
            result = {**result, "cancelled": True, "success": False,
                      "message": "已停止后续处理。" + str(result.get("message") or "请核对平台记录。")}
        return result

    def show_result(self, result):
        success = result.get("success")
        title = "已确认完成" if success else "等待平台确认" if result.get("pending") else "处理结果"
        token = self.client.token

        def safe(value):
            value = str(value)
            return value.replace(token, "[已隐藏]") if token else value

        lines = [Text(safe(result.get("message") or "处理结束，请核对平台记录。"))]
        for error in result.get("errors", [])[:6]:
            lines.append(Text("· " + safe(error), style="dim"))
        self.console.print(Panel(Group(*lines), title=title, title_align="left",
                                 border_style=ACCENT if success else GOLD,
                                 box=box.ROUNDED, width=min(94, self.console.width), padding=(1, 2)))

    def execute(self, mode):
        if not self.selected:
            return
        course_id = self.selected["id"]
        # Refresh state before any business request, even after a long idle menu.
        self.load_catalog()
        if not self.selected or self.selected["id"] != course_id or self.selected.get("status") != "active":
            self.say("活动开放状态已变化，请重新选择。", GOLD)
            return
        if not self.login():
            return
        self.console.print()
        if mode in {"all", "study"}:
            study = self._perform("正在学习课程 · Ctrl+C 可停止后续处理", lambda emit, cancel:
                self.service.study(course_id, emit=emit, cancel=cancel))
            self.progress = study.get("progress", self.progress)
            self.show_result(study)
            if (not study.get("success") or study.get("cancelled") or study.get("pending")
                    or study.get("uncertain") or mode == "study"):
                return
        if course_id in self.exam_submitted:
            self.say("本次已提交过该活动的考试，请先到官网核对记录，未重复提交。", GOLD)
            return

        def take_exam(allow_retake):
            return self._perform("正在处理本期考试 · Ctrl+C 可停止后续处理", lambda emit, cancel:
                self.service.exam(course_id, emit=emit, cancel=cancel, allow_retake=allow_retake))

        result = take_exam(False)
        if result.get("existing"):
            self.say(result["message"], GOLD)
            if not Confirm.ask("  是否重新提交一次考试？", default=False, console=self.console):
                return
            result = take_exam(True)
        if result.get("submitted") or result.get("uncertain") or result.get("pending"):
            self.exam_submitted.add(course_id)
        self.show_result(result)

    def update(self):
        with self.console.status("  正在检查新版本…", spinner_style=ACCENT):
            update = self.updater.check()
        self.latest_update = update
        if not update.get("available"):
            self.say(update.get("message") or f"当前版本 v{__version__}，暂无新版本。", "dim")
            return
        self.say(f"发现新版本 {update['latestVersion']}", ACCENT)
        if Confirm.ask("  现在下载并校验？", default=True, console=self.console):
            with self.console.status("  正在下载并校验…", spinner_style=ACCENT):
                result = self.updater.download()
            self.say(result["message"], ACCENT)
            self.say("保存位置：" + str(result["path"]))
            self.say("退出当前程序后，双击新版即可；登录状态会继续保留。", "dim")
            if Confirm.ask("  打开下载文件夹？", default=True, console=self.console):
                import os
                from pathlib import Path
                directory = str(Path(result["path"]).parent)
                if os.name == "nt":
                    os.startfile(directory)
                else:
                    webbrowser.open(Path(directory).as_uri())

    def more(self):
        self.console.print()
        self.say("[1] 切换年度 / 活动     [2] 刷新进度     [3] 检查更新", "dim")
        self.say("[4] 切换账号     [5] 去官网查看记录     [0] 返回", "dim")
        choice = self.ask("更多选项", ["1", "2", "3", "4", "5", "0"], "0")
        if choice == "1":
            self.change_year()
        elif choice == "2":
            if self.login():
                self.refresh_progress()
        elif choice == "3":
            self.update()
            self.pause()
        elif choice == "4":
            self.client.set_token("")
            self.nickname, self.progress = "", None
            self.vault.clear()
            if self.vault.last_error:
                self.say(self.vault.last_error, GOLD)
            self.exam_submitted.clear()
            self.login()
        elif choice == "5":
            webbrowser.open(self.selected.get("url", OFFICIAL_COURSES) if self.selected else OFFICIAL_COURSES)

    def run(self):
        self.header()

        def background_update():
            try:
                self.latest_update = self.updater.check()
            except Exception:
                pass

        Thread(target=background_update, daemon=True, name="version-check").start()
        try:
            self.load_catalog()
        except Exception as exc:
            self.say(exc, GOLD)
        self.restore_login()
        while True:
            self.home()
            active = self.selected and self.selected.get("status") == "active"
            choice = self.menu()
            if choice == "0":
                self.say("已退出，下次打开即可继续。", "dim")
                return
            try:
                if active:
                    if choice == "4":
                        self.more()
                    else:
                        self.execute({"1": "all", "2": "study", "3": "exam"}[choice])
                        self.pause()
                elif choice == "1":
                    self.load_catalog()
                elif choice == "2":
                    self.change_year()
                elif choice == "3":
                    self.update()
                    self.pause()
            except Exception as exc:
                self.say(exc, GOLD)
                self.pause()


def show_demo(console):
    """Render the exact terminal components with explicitly marked sample data."""
    app = TerminalApp(console=console)
    app.nickname = "演示用户"
    app.selected = {"id": "demo", "title": f"{app.year}年度全国保密教育线上培训",
                    "year": app.year, "status": "active", "startDate": f"{app.year}-01-01",
                    "endDate": f"{app.year}-12-31"}
    app.progress = {"progressRate": 0.625}
    app.home(demo=True)
    console.print()
    app.say("[回车] 一键学习并考试", f"bold {ACCENT}")
    app.say("       学习完成后，提交一次本期考试", "dim")
    app.say("[2] 仅学习   [3] 仅考试   [4] 更多选项   [0] 退出", "dim")
    console.print()
    app.show_result({"success": True, "message": "示例：平台已确认课程完成，本次完成 3 项，跳过 5 项。"})
