"""Render a complete, high-contrast QR code directly in a terminal."""

import qrcode
from qrcode.exceptions import DataOverflowError
from rich.console import Console
from rich.style import Style
from rich.text import Text


def _qr_lines(content):
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    lines = []
    for row in range(0, len(matrix), 2):
        upper = matrix[row]
        lower = matrix[row + 1] if row + 1 < len(matrix) else [False] * len(upper)
        text = Text()
        for top, bottom in zip(upper, lower):
            # One terminal cell represents two square modules. Explicit colors
            # keep a white quiet zone even when the user's terminal is dark.
            text.append("▀", Style(color="#000000" if top else "#ffffff",
                                   bgcolor="#000000" if bottom else "#ffffff"))
        lines.append(text)
    return lines


def render_qr(content, console=None, *, max_width=None, max_height=None):
    """Print a whole QR code, or a clear fallback message; never clip or wrap it.

    Returns True only when a complete QR was printed. Six terminal rows are
    reserved by default for the title, instructions, and subsequent prompt.
    No image libraries, local files, browser, or network are involved.
    """
    console = console if console is not None else Console()
    if not isinstance(content, str) or not content:
        console.print("二维码内容无效，请重新获取，或选择账号密码登录。", style="yellow")
        return False
    if console.no_color or console.color_system is None:
        console.print("当前终端无法显示黑白二维码，请使用支持颜色的终端，或选择账号密码登录。")
        return False
    try:
        lines = _qr_lines(content)
    except DataOverflowError:
        console.print("二维码内容过长，无法完整展示，请选择账号密码登录。", style="yellow")
        return False
    width = min(console.size.width, max_width) if max_width is not None else console.size.width
    height = min(console.size.height - 6, max_height) if max_height is not None else console.size.height - 6
    required_width, required_height = lines[0].cell_len, len(lines)
    if required_width > width or required_height > height:
        console.print(
            f"窗口太小，二维码需要至少 {required_width} 列、{required_height + 6} 行。"
            "请放大窗口后重试，或选择账号密码登录。", style="yellow",
        )
        return False
    for line in lines:
        console.print(line, soft_wrap=True, crop=False, overflow="ignore", no_wrap=True)
    return True
