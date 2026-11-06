from io import StringIO
import re

import qrcode
from rich.console import Console

from baomiguan.qr import _qr_lines, render_qr


def terminal(width=120, height=80, **options):
    output = StringIO()
    console = Console(file=output, width=width, height=height, force_terminal=True,
                      color_system="truecolor", no_color=options.pop("no_color", False), **options)
    return console, output


def test_half_blocks_preserve_every_qr_module_and_four_module_quiet_zone():
    content = '{"params":{"qrToken":"sample-token"}}'
    expected = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    expected.add_data(content)
    expected.make(fit=True)
    matrix = expected.get_matrix()
    rendered = _qr_lines(content)
    pixels = []
    for line in rendered:
        upper, lower = [], []
        for column in range(line.cell_len):
            style = line.get_style_at_offset(Console(), column)
            upper.append(style.color.triplet == (0, 0, 0))
            lower.append(style.bgcolor.triplet == (0, 0, 0))
        pixels.extend([upper, lower])
    assert pixels[:len(matrix)] == matrix
    assert all(not any(row) for row in pixels[:4])
    assert all(not any(row) for row in pixels[len(matrix) - 4:len(matrix)])
    assert all(not any(row[:4]) and not any(row[-4:]) for row in pixels)


def test_fitting_terminal_prints_complete_qr_without_wrapping():
    console, output = terminal()
    content = "test-payload"
    lines = _qr_lines(content)
    assert render_qr(content, console) is True
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
    printed = clean.splitlines()
    assert len(printed) == len(lines)
    assert all(len(row) == lines[0].cell_len for row in printed)


def test_small_terminal_explains_fallback_instead_of_printing_a_partial_qr():
    console, output = terminal(width=24, height=12)
    assert render_qr("test-payload", console) is False
    assert "▀" not in output.getvalue()
    assert "放大窗口" in output.getvalue()
    assert "账号密码登录" in output.getvalue()


def test_explicit_size_limit_does_not_clip_qr():
    console, output = terminal()
    assert render_qr("test-payload", console, max_width=4, max_height=4) is False
    assert "▀" not in output.getvalue()


def test_invalid_payload_does_not_render_a_misleading_code():
    console, output = terminal()
    assert render_qr("", console) is False
    assert "▀" not in output.getvalue()
    assert "重新获取" in output.getvalue()


def test_no_color_terminal_fails_clearly_instead_of_printing_solid_blocks():
    console, output = terminal(no_color=True)
    assert render_qr("test-payload", console) is False
    assert "▀" not in output.getvalue()
    assert "账号密码登录" in output.getvalue()
