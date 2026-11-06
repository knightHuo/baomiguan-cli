"""One executable, one terminal, no configuration required."""

import argparse
import sys

from rich.console import Console

from baomiguan import __version__


def main():
    parser = argparse.ArgumentParser(description="保密观 · 简洁的年度学习终端")
    parser.add_argument("--year", type=int, help="选择年度，默认当前年度")
    parser.add_argument("--demo", action="store_true", help="展示终端界面，不连接真实账户")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    if args.year is not None and not 2000 <= args.year <= 2100:
        parser.error("年度应在 2000–2100 之间")
    console = Console(highlight=False)
    from baomiguan.cli import TerminalApp, show_demo
    try:
        if args.demo:
            show_demo(console)
            if sys.stdin.isatty():
                console.input("\n  按回车退出演示。")
        else:
            if not sys.stdin.isatty():
                console.print("请在终端中运行，或直接双击 Windows 程序。", style="yellow")
                return
            TerminalApp(console=console, year=args.year).run()
    except (KeyboardInterrupt, EOFError):
        console.print("\n  已退出。已发出的请求请以平台记录为准。", style="dim")
    except Exception as exc:
        from baomiguan.client import ApiError
        message = str(exc) if isinstance(exc, (ApiError, ValueError)) else "程序未能继续，请重启后重试。"
        console.print("\n  " + message, style="yellow", markup=False)
        if sys.stdin.isatty():
            console.input("\n  按回车关闭。")
