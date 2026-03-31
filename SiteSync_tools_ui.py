#!/usr/bin/env python3
"""
SiteSync_tools_ui.py — SiteSync 配置管理工具 TUI 界面

使用 Python curses 实现终端用户界面，支持所有 SiteSync 功能。
无需额外依赖，仅使用 Python 标准库。

用法:
  python SiteSync_tools_ui.py
  python SiteSync_tools_ui.py --task 卸车标图
"""

import curses
import curses.textpad
import sys
import os
import io
import contextlib
import json
import difflib
import unicodedata
from pathlib import Path

# 导入 SiteSync_tools 的所有功能
sys.path.insert(0, str(Path(__file__).parent))
from SiteSync_tools import (
    DEFAULT_TASK_NAME, default_config_dir,
    cmd_extract, cmd_update, cmd_apply,
    cmd_diff, cmd_backup, cmd_restore,
    cmd_list_backups, cmd_remove_backups,
    _compute_apply_changes, _execute_apply,
    _compute_restore_python_changes,
    get_backup_dir, get_task_path, SCRIPT_DIR,
)

# ─── 颜色常量 ───────────────────────────────────────────
COLOR_TITLE      = 1   # 标题栏：蓝底白字
COLOR_MENU       = 2   # 普通菜单项：默认
COLOR_SELECT     = 3   # 选中项：青底黑字
COLOR_SUCCESS    = 4   # 成功：绿字
COLOR_WARN       = 5   # 警告：黄字
COLOR_ERROR      = 6   # 错误：红字
COLOR_BORDER     = 7   # 边框：蓝字
COLOR_DIM        = 8   # 暗淡：灰字
COLOR_KEY        = 9   # 按键提示：青字
COLOR_DIFF_OLD   = 10  # diff 旧值高亮：红底白字
COLOR_DIFF_NEW   = 11  # diff 新值高亮：绿底黑字


def _char_width(c: str) -> int:
    """单字符终端显示宽度：全角/宽字符=2，其余=1"""
    eaw = unicodedata.east_asian_width(c)
    return 2 if eaw in ('W', 'F') else 1


def _str_width(s: str) -> int:
    """字符串终端显示宽度"""
    return sum(_char_width(c) for c in s)


def _soft_wrap_lines(text: str, width: int) -> list[str]:
    """将文本按 width 显示宽度软换行，返回行列表（宽度不足时保持原行）"""
    result = []
    for raw in text.splitlines():
        if _str_width(raw) <= width:
            result.append(raw)
            continue
        while raw:
            chunk: list[str] = []
            w = 0
            for c in raw:
                cw = _char_width(c)
                if w + cw > width:
                    break
                chunk.append(c)
                w += cw
            if not chunk:           # 单字符宽于屏幕，强制塞入
                chunk = [raw[0]]
            result.append("".join(chunk))
            raw = raw[len(chunk):]
    return result


def _truncate_str(s: str, max_width: int, suffix: str = "...") -> str:
    """将字符串截断到 max_width 显示宽度，超出时追加 suffix"""
    if _str_width(s) <= max_width:
        return s
    sw = _str_width(suffix)
    result = []
    width = 0
    for c in s:
        cw = _char_width(c)
        if width + cw + sw > max_width:
            break
        result.append(c)
        width += cw
    return "".join(result) + suffix


def _truncate_path_tail(s: str, max_width: int, prefix: str = "...") -> str:
    """路径过长时从末尾保留，头部加 prefix，保证用户看到的是当前位置"""
    if _str_width(s) <= max_width:
        return s
    pw = _str_width(prefix)
    result = []
    w = 0
    for c in reversed(s):
        cw = _char_width(c)
        if w + cw + pw > max_width:
            break
        result.append(c)
        w += cw
    return prefix + "".join(reversed(result))


# ─── 状态持久化 ───────────────────────────────────────────

_STATE_FILE = SCRIPT_DIR / ".sitesync_ui_state.json"


def _load_ui_state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ui_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _repr_short(v, max_len: int = 80) -> str:
    """将值 repr 后截短到 max_len 字符"""
    s = repr(v)
    if len(s) > max_len:
        return s[:max_len - 3] + "..."
    return s


def _diff_segments(a: str, b: str):
    """
    字符级 diff，返回 (a_segs, b_segs)。
    每段为 (text, is_changed)：is_changed=True 表示该段是差异部分。
    """
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    a_segs: list[tuple[str, bool]] = []
    b_segs: list[tuple[str, bool]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            a_segs.append((a[i1:i2], False))
            b_segs.append((b[j1:j2], False))
        elif tag == "replace":
            a_segs.append((a[i1:i2], True))
            b_segs.append((b[j1:j2], True))
        elif tag == "delete":
            a_segs.append((a[i1:i2], True))
        elif tag == "insert":
            b_segs.append((b[j1:j2], True))
    return a_segs, b_segs


def _wrap_segments(segments: list[tuple[str, bool]], width: int) -> list[list[tuple[str, bool]]]:
    """
    将带标记的分段文字按显示宽度换行。
    返回行列表，每行是 [(text, is_changed), ...] 列表。
    """
    lines: list[list[tuple[str, bool]]] = []
    current_line: list[tuple[str, bool]] = []
    current_w = 0

    for text, is_changed in segments:
        pos = 0
        while pos < len(text):
            remaining = width - current_w
            if remaining <= 0:
                lines.append(current_line)
                current_line = []
                current_w = 0
                remaining = width
            chunk: list[str] = []
            chunk_w = 0
            i = pos
            while i < len(text):
                c = text[i]
                cw = _char_width(c)
                if chunk_w + cw > remaining:
                    break
                chunk.append(c)
                chunk_w += cw
                i += 1
            if chunk:
                current_line.append(("".join(chunk), is_changed))
                current_w += chunk_w
                pos = i
            else:
                # 单字符超宽（宽字符在行首），强制换行后重试
                if current_line:
                    lines.append(current_line)
                    current_line = []
                    current_w = 0
                else:
                    # 极端情况：width 极小，直接塞入
                    c = text[pos]
                    current_line.append((c, is_changed))
                    current_w += _char_width(c)
                    pos += 1

    if current_line:
        lines.append(current_line)

    return lines if lines else [[("", False)]]


def run_command_capture(func, *args, **kwargs) -> str:
    """捕获命令函数的 stdout 输出为字符串"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            func(*args, **kwargs)
        except SystemExit:
            pass  # 忽略 sys.exit() 调用
    return buf.getvalue()


def _init_colors():
    """初始化 curses 颜色对"""
    curses.start_color()
    curses.use_default_colors()
    # 标题栏：蓝底白字
    curses.init_pair(COLOR_TITLE,   curses.COLOR_WHITE,   curses.COLOR_BLUE)
    # 普通文本：默认色
    curses.init_pair(COLOR_MENU,    -1,                   -1)
    # 选中项：青底黑字
    curses.init_pair(COLOR_SELECT,  curses.COLOR_BLACK,   curses.COLOR_CYAN)
    # 成功提示：绿字
    curses.init_pair(COLOR_SUCCESS, curses.COLOR_GREEN,   -1)
    # 警告提示：黄字
    curses.init_pair(COLOR_WARN,    curses.COLOR_YELLOW,  -1)
    # 错误提示：红字
    curses.init_pair(COLOR_ERROR,   curses.COLOR_RED,     -1)
    # 边框：蓝字
    curses.init_pair(COLOR_BORDER,  curses.COLOR_BLUE,    -1)
    # 暗淡：默认色（某些终端支持 A_DIM）
    curses.init_pair(COLOR_DIM,     -1,                   -1)
    # 按键提示：青字
    curses.init_pair(COLOR_KEY,      curses.COLOR_CYAN,    -1)
    # diff 高亮：旧值红底白字，新值绿底黑字
    curses.init_pair(COLOR_DIFF_OLD, curses.COLOR_WHITE,   curses.COLOR_RED)
    curses.init_pair(COLOR_DIFF_NEW, curses.COLOR_BLACK,   curses.COLOR_GREEN)


# ─── 绘制工具 ────────────────────────────────────────────

def _safe_addstr(win, y, x, text, attr=0):
    """安全添加字符串，忽略越界错误"""
    try:
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        # 裁剪到屏幕宽度
        available = w - x - 1
        if available <= 0:
            return
        # 按显示宽度裁剪
        result = []
        cur_w = 0
        for c in text:
            cw = _char_width(c)
            if cur_w + cw > available:
                break
            result.append(c)
            cur_w += cw
        win.addstr(y, x, "".join(result), attr)
    except curses.error:
        pass


def draw_box(win, y, x, h, w, color_pair=COLOR_BORDER):
    """用 box-drawing 字符绘制边框"""
    attr = curses.color_pair(color_pair)
    try:
        # 角
        win.addch(y,     x,     "┌", attr)
        win.addch(y,     x+w-1, "┐", attr)
        win.addch(y+h-1, x,     "└", attr)
        win.addch(y+h-1, x+w-1, "┘", attr)
        # 横线
        for col in range(x+1, x+w-1):
            win.addch(y,     col, "─", attr)
            win.addch(y+h-1, col, "─", attr)
        # 竖线
        for row in range(y+1, y+h-1):
            win.addch(row, x,     "│", attr)
            win.addch(row, x+w-1, "│", attr)
    except curses.error:
        pass


def draw_hline(win, y, x, w, color_pair=COLOR_BORDER):
    """绘制一条水平分隔线"""
    attr = curses.color_pair(color_pair)
    try:
        win.addch(y, x,     "├", attr)
        win.addch(y, x+w-1, "┤", attr)
        for col in range(x+1, x+w-1):
            win.addch(y, col, "─", attr)
    except curses.error:
        pass


def draw_header(stdscr, task_name: str):
    """绘制顶部标题栏"""
    h, w = stdscr.getmaxyx()
    title = f"  SiteSync Tools  │  Task: {task_name}  "
    stdscr.attron(curses.color_pair(COLOR_TITLE))
    stdscr.addstr(0, 0, " " * w)
    _safe_addstr(stdscr, 0, 0, title, curses.color_pair(COLOR_TITLE))
    stdscr.attroff(curses.color_pair(COLOR_TITLE))


def draw_footer(stdscr, hints: list[tuple[str, str]]):
    """
    绘制底部按键提示栏。
    hints: [(键名, 说明), ...]，例如 [("↑↓", "移动"), ("Enter", "确认")]
    """
    h, w = stdscr.getmaxyx()
    stdscr.attron(curses.color_pair(COLOR_TITLE))
    stdscr.addstr(h-1, 0, " " * (w-1))
    stdscr.attroff(curses.color_pair(COLOR_TITLE))
    x = 1
    for i, (key, desc) in enumerate(hints):
        if x >= w - 2:
            break
        try:
            # 格式：描述[按键]，组间一个空格，用 _str_width 计算中文宽度
            if desc:
                stdscr.addstr(h-1, x, desc, curses.color_pair(COLOR_TITLE))
                x += _str_width(desc)
            stdscr.addstr(h-1, x, "[", curses.color_pair(COLOR_TITLE))
            x += 1
            stdscr.addstr(h-1, x, key, curses.color_pair(COLOR_TITLE) | curses.A_BOLD)
            x += _str_width(key)
            stdscr.addstr(h-1, x, "]", curses.color_pair(COLOR_TITLE))
            x += 1
            if i < len(hints) - 1:
                stdscr.addstr(h-1, x, " ", curses.color_pair(COLOR_TITLE))
                x += 1
        except curses.error:
            break


def draw_menu(stdscr, items: list[dict], selected: int, start_y: int, start_x: int, inner_w: int):
    """
    绘制菜单列表。
    items: [{"key": "1", "label": "Extract", "desc": "从 task 文件提取 YAML"}, ...]
    selected: 当前选中行的索引
    """
    h, _ = stdscr.getmaxyx()
    # 固定列宽（显示列数）：[3]arrow [1]key [2]sep [12]label [rest]desc
    # 各列使用绝对 x 位置绘制，避免 ▶ 等宽度不确定字符影响后续列对齐
    COL_KEY   = start_x + 3
    COL_LABEL = start_x + 6    # 3+1+2
    COL_DESC  = start_x + 18   # 3+1+2+12
    LABEL_W   = 12

    for i, item in enumerate(items):
        y = start_y + i
        if y >= h - 1:
            break
        key   = item.get("key", "")
        label = item.get("label", "")
        desc  = item.get("desc", "")

        if i == selected:
            attr = curses.color_pair(COLOR_SELECT) | curses.A_BOLD
            arrow = " > "
        else:
            attr = curses.color_pair(COLOR_MENU)
            arrow = "   "

        # 清空该行
        stdscr.addstr(y, start_x, " " * inner_w, attr)
        # 各列独立绘制，固定列位置不受前列字符宽度影响
        _safe_addstr(stdscr, y, start_x,  arrow, attr)
        _safe_addstr(stdscr, y, COL_KEY,  key,   attr)
        _safe_addstr(stdscr, y, COL_LABEL, label, attr)
        desc_max = inner_w - (COL_DESC - start_x)
        if desc_max > 0:
            _safe_addstr(stdscr, y, COL_DESC, _truncate_str(desc, desc_max), attr)


# ─── 输入框 ──────────────────────────────────────────────

def input_box(stdscr, prompt: str, default: str = "") -> str | None:
    """
    在屏幕中央弹出输入框，支持中文输入。
    返回用户输入的字符串；Esc 取消返回 None。
    """
    sh, sw = stdscr.getmaxyx()
    box_w   = min(sw - 4, 70)
    box_h   = 5
    by      = (sh - box_h) // 2
    bx      = (sw - box_w) // 2
    avail   = box_w - 4
    input_y = 3
    input_x = 2

    popup = curses.newwin(box_h, box_w, by, bx)
    draw_box(popup, 0, 0, box_h, box_w)
    _safe_addstr(popup, 1, 2, _truncate_str(prompt, box_w - 4), curses.color_pair(COLOR_WARN))
    popup.refresh()

    val    = list(default)
    cursor = len(val)

    while True:
        # ── 计算显示窗口（以光标为中心，保证光标可见）
        # 从 cursor 向左尽量多放，再向右填满
        show_indices: list[int] = []
        w_used = 0
        for i in range(cursor - 1, -1, -1):
            cw = _char_width(val[i])
            if w_used + cw > avail:
                break
            show_indices.insert(0, i)
            w_used += cw
        for i in range(cursor, len(val)):
            cw = _char_width(val[i])
            if w_used + cw > avail:
                break
            show_indices.append(i)
            w_used += cw

        scroll_start = show_indices[0] if show_indices else cursor
        disp_str     = "".join(val[i] for i in show_indices)
        cur_x        = input_x + _str_width("".join(val[scroll_start:cursor]))

        popup.addstr(input_y, input_x, " " * avail)
        _safe_addstr(popup, input_y, input_x, disp_str,
                     curses.color_pair(COLOR_MENU) | curses.A_UNDERLINE)
        try:
            popup.move(input_y, min(cur_x, input_x + avail - 1))
        except curses.error:
            pass
        popup.refresh()

        curses.curs_set(1)
        try:
            ch = stdscr.get_wch()   # 支持 Unicode/中文
        except curses.error:
            continue
        finally:
            curses.curs_set(0)

        # 归一化：str → (code, ch_char)；int → (code, None)
        if isinstance(ch, str):
            code    = ord(ch) if len(ch) == 1 else -1
            ch_char = ch if code >= 32 else None   # 可打印字符保留原 str
        else:
            code    = ch
            ch_char = None

        if code == 27:                              # Esc
            return None
        elif code in (10, 13):                      # Enter
            return "".join(val)
        elif ch_char is not None:                   # 可打印字符（含中文）
            val.insert(cursor, ch_char)
            cursor += 1
        elif code in (curses.KEY_BACKSPACE, 127, 8):  # Backspace
            if cursor > 0:
                val.pop(cursor - 1)
                cursor -= 1
        elif code == curses.KEY_DC:                 # Delete
            if cursor < len(val):
                val.pop(cursor)
        elif code == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif code == curses.KEY_RIGHT:
            cursor = min(len(val), cursor + 1)
        elif code == curses.KEY_HOME:
            cursor = 0
        elif code == curses.KEY_END:
            cursor = len(val)


# ─── 结果显示界面 ────────────────────────────────────────

def show_result(stdscr, text: str, title: str = "执行结果"):
    """滚动显示结果文本，任意键返回"""
    raw_text  = text
    lines: list[str] = []
    last_sw   = -1
    offset    = 0
    while True:
        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        # 宽度变化时重新软换行（保证长路径能完整展示）
        if sw != last_sw:
            lines   = _soft_wrap_lines(raw_text, sw - 2)
            last_sw = sw
            offset  = min(offset, max(0, len(lines) - (sh - 2)))
        draw_header(stdscr, title)
        draw_footer(stdscr, [("↑↓/PgUp/PgDn", "滚动"), ("任意键", "关闭")])
        # 内容区
        inner_h = sh - 2
        for i in range(inner_h):
            li = offset + i
            if li >= len(lines):
                break
            line = lines[li]
            # 选择颜色（颜色判断基于原始行内容，软换行不影响关键字匹配）
            attr = curses.color_pair(COLOR_MENU)
            if "[apply]" in line or "[backup]" in line or "[restore]" in line:
                if "错误" in line or "error" in line.lower():
                    attr = curses.color_pair(COLOR_ERROR)
                elif "完成" in line or "成功" in line:
                    attr = curses.color_pair(COLOR_SUCCESS)
                else:
                    attr = curses.color_pair(COLOR_WARN)
            elif "无字段改动" in line or "无改动" in line:
                attr = curses.color_pair(COLOR_DIM)
            _safe_addstr(stdscr, 1 + i, 1, line, attr)
        # 滚动提示
        if len(lines) > inner_h:
            pct = int(100 * offset / max(1, len(lines) - inner_h))
            _safe_addstr(stdscr, sh-1, sw-8, f" {pct:3d}%", curses.color_pair(COLOR_TITLE))
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif ch == curses.KEY_DOWN:
            offset = min(max(0, len(lines) - inner_h), offset + 1)
        elif ch == curses.KEY_PPAGE:
            offset = max(0, offset - inner_h)
        elif ch == curses.KEY_NPAGE:
            offset = min(max(0, len(lines) - inner_h), offset + inner_h)
        else:
            break


# ─── 通用表单 ────────────────────────────────────────────

def file_browser(stdscr, start_path=None, mode: str = "dir",
                  extensions: list[str] | None = None) -> Path | None:
    """
    文件/目录浏览器。

    mode="dir"  — 选择目录（←→进入/退出，s/Space 确认选中当前目录）
    mode="file" — 选择文件（Enter 选中文件，进目录用 → 或 Enter）
    extensions  — 文件模式下只显示指定后缀（如 [".yaml"]），目录始终显示

    返回选中的 Path（绝对路径），或 None（取消）。
    """
    # ── 确定起始目录 ──────────────────────────────────────
    cur_dir = SCRIPT_DIR
    if start_path:
        p = Path(str(start_path))
        # 绝对路径 or SCRIPT_DIR 相对路径都尝试
        for candidate in (p, SCRIPT_DIR / p):
            if candidate.is_file():
                cur_dir = candidate.parent.resolve()
                break
            if candidate.is_dir():
                cur_dir = candidate.resolve()
                break
    cur_dir = cur_dir.resolve()

    sel_idx = 0
    scroll  = 0

    while True:
        stdscr.clear()
        sh, sw = stdscr.getmaxyx()

        # 标题栏和底部提示
        header_title = "选择目录" if mode == "dir" else "选择文件"
        draw_header(stdscr, header_title)
        if mode == "dir":
            draw_footer(stdscr, [
                ("↑↓",     "移动"),
                ("→/Enter", "进入目录"),
                ("←",      "返回上级"),
                ("s/Space", "选择此目录"),
                ("Esc",    "取消"),
            ])
        else:
            draw_footer(stdscr, [
                ("↑↓",     "移动"),
                ("→/Enter", "进入目录/选择文件"),
                ("←",      "返回上级"),
                ("Esc",    "取消"),
            ])

        # ── 主边框 ────────────────────────────────────────
        box_w = min(sw - 2, 72)
        # 底部留出状态行：box_h = sh - 2（1行header + 1行footer）
        box_h = max(8, sh - 2)
        by = 1
        bx = max(0, (sw - box_w) // 2)
        draw_box(stdscr, by, bx, box_h, box_w)

        # 顶部路径行（标题位置）
        path_display = _truncate_path_tail(str(cur_dir), box_w - 4)
        _safe_addstr(stdscr, by, bx + 2, f" {path_display} ", curses.color_pair(COLOR_TITLE))
        draw_hline(stdscr, by + 1, bx, box_w)

        # ── 构建目录条目列表 ──────────────────────────────
        entries = []  # (label_prefix, name, is_dir, full_path)
        try:
            items = sorted(
                cur_dir.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
            for item in items:
                if item.is_dir():
                    entries.append(("[/]", item.name, True, item))
                elif mode == "file":
                    if extensions is None or item.suffix.lower() in extensions:
                        entries.append(("[-]", item.name, False, item))
        except PermissionError:
            pass

        # ".." 上级目录放最前
        display: list[tuple] = []
        if cur_dir.parent != cur_dir:
            display.append(("[↑]", "..", True, cur_dir.parent))
        display.extend(entries)

        # 内容区高度（顶部2行 + 底部分隔+状态共2行 + 底边1行 = 5行）
        inner_h = box_h - 5
        if not display:
            inner_h = 0

        # 安全限制 sel_idx
        if sel_idx >= len(display):
            sel_idx = max(0, len(display) - 1)

        # 自动滚动
        if sel_idx < scroll:
            scroll = sel_idx
        elif inner_h > 0 and sel_idx >= scroll + inner_h:
            scroll = sel_idx - inner_h + 1

        inner_x = bx + 2
        inner_w = box_w - 4

        # ── 绘制条目 ──────────────────────────────────────
        for i in range(inner_h):
            ry = by + 2 + i
            li = scroll + i
            if li >= len(display):
                break
            prefix, name, is_dir, fpath = display[li]
            is_sel = (li == sel_idx)

            if is_sel:
                attr = curses.color_pair(COLOR_SELECT) | curses.A_BOLD
            elif is_dir:
                attr = curses.color_pair(COLOR_WARN)     # 目录用警告色（黄）
            elif name.endswith(".yaml") or name.endswith(".yml"):
                attr = curses.color_pair(COLOR_SUCCESS)  # yaml 用绿色
            else:
                attr = curses.color_pair(COLOR_MENU)

            label = f" {prefix} {name}"
            _safe_addstr(stdscr, ry, inner_x, " " * inner_w, attr)
            _safe_addstr(stdscr, ry, inner_x, _truncate_str(label, inner_w), attr)

        # ── 底部状态行 ────────────────────────────────────
        status_y = by + box_h - 3
        draw_hline(stdscr, status_y, bx, box_w)
        if mode == "dir":
            status_label = "当前目录: "
            status_val   = str(cur_dir)
        elif display and sel_idx < len(display):
            _, _, _, sel_path = display[sel_idx]
            status_label = "选中: "
            status_val   = str(sel_path)
        else:
            status_label, status_val = "", ""
        label_w  = _str_width(status_label)
        val_avail = inner_w - label_w
        status_str = status_label + _truncate_path_tail(status_val, val_avail)
        _safe_addstr(stdscr, status_y + 1, inner_x, status_str, curses.color_pair(COLOR_DIM))

        stdscr.refresh()
        ch = stdscr.getch()

        # ── 按键处理 ──────────────────────────────────────
        if ch == 27:  # Esc：取消
            return None

        elif ch == curses.KEY_UP:
            sel_idx = max(0, sel_idx - 1)

        elif ch == curses.KEY_DOWN:
            sel_idx = min(len(display) - 1, sel_idx + 1)

        elif ch == curses.KEY_PPAGE:  # PgUp
            sel_idx = max(0, sel_idx - inner_h)

        elif ch == curses.KEY_NPAGE:  # PgDn
            sel_idx = min(len(display) - 1, sel_idx + inner_h)

        elif ch == curses.KEY_LEFT:   # ← 退回上级目录
            if cur_dir.parent != cur_dir:
                cur_dir = cur_dir.parent.resolve()
                sel_idx = 0
                scroll  = 0

        elif ch in (curses.KEY_RIGHT, 10, 13):  # → / Enter
            if not display:
                continue
            prefix, name, is_dir, fpath = display[sel_idx]
            if is_dir:
                cur_dir = fpath.resolve()
                sel_idx = 0
                scroll  = 0
            else:
                # 文件模式：选中文件
                if mode == "file":
                    return fpath.resolve()

        elif ch in (ord("s"), ord("S"), ord(" ")):  # s / Space：选中当前目录
            if mode == "dir":
                return cur_dir


def show_form(stdscr, title: str, fields: list[dict], task_name: str) -> dict | None:
    """
    通用参数表单界面。
    fields 每项:
      {"key": "k", "label": "标签", "value": "默认值", "type": "text"|"toggle"|"button"|"dirpath"|"filepath"}
      type=toggle:   value 为 True/False，←→/Space 切换
      type=button:   显示为操作按钮行，Enter 触发返回
      type=dirpath:  目录路径，Enter 打开文件浏览器，e 手动输入
      type=filepath: 文件路径，Enter 打开文件浏览器，e 手动输入；
                     支持 "extensions": [".yaml"] 过滤文件类型
    返回填写后的 {key: value} 字典（value 为字符串/布尔）；Esc 返回 None。
    """
    selected = 0
    # 默认选中第一个非 button 字段
    for i, f in enumerate(fields):
        if f.get("type") != "button":
            selected = i
            break

    def _footer_for(ftype: str) -> list[tuple]:
        """根据当前字段类型动态生成底部提示"""
        if ftype in ("dirpath", "filepath"):
            return [
                ("↑↓",    "移动"),
                ("Enter", "打开路径浏览器"),
                ("e",     "手动输入路径"),
                ("F5/r",  "执行"),
                ("Esc",   "返回"),
            ]
        elif ftype == "toggle":
            return [
                ("↑↓",       "移动"),
                ("←→/Space", "开/关切换"),
                ("F5/r",     "执行"),
                ("Esc",      "返回"),
            ]
        else:
            return [
                ("↑↓",    "移动"),
                ("Enter", "编辑"),
                ("F5/r",  "执行"),
                ("Esc",   "返回"),
            ]

    while True:
        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        draw_header(stdscr, task_name)
        cur_ftype = fields[selected].get("type", "text")
        draw_footer(stdscr, _footer_for(cur_ftype))

        # 绘制表单框
        form_h = len(fields) + 4
        form_w = min(sw - 4, 72)
        fy     = max(1, (sh - form_h) // 2)
        fx     = max(0, (sw - form_w) // 2)
        draw_box(stdscr, fy, fx, form_h, form_w)
        title_str = f"  {title}  "
        _safe_addstr(stdscr, fy, fx + 2, title_str, curses.color_pair(COLOR_TITLE))
        draw_hline(stdscr, fy + 1, fx, form_w)

        inner_w = form_w - 4
        label_w = 16

        for i, field in enumerate(fields):
            ry = fy + 2 + i
            if ry >= sh - 1:
                break
            ftype  = field.get("type", "text")
            label  = field.get("label", "")
            val    = field.get("value", "")
            is_sel = (i == selected)
            val_w  = inner_w - label_w - 3

            if is_sel:
                attr = curses.color_pair(COLOR_SELECT) | curses.A_BOLD
            else:
                attr = curses.color_pair(COLOR_MENU)

            _safe_addstr(stdscr, ry, fx + 2, " " * inner_w, attr)

            if ftype == "toggle":
                toggle_str = "[是]" if val else "[否]"
                line = f"{label:<{label_w}}: {toggle_str}"
            elif ftype == "button":
                line = f"  [ {label} ]"
                attr = (curses.color_pair(COLOR_SELECT) | curses.A_BOLD) if is_sel \
                       else (curses.color_pair(COLOR_SUCCESS) | curses.A_BOLD)
            elif ftype == "dirpath":
                # 目录路径：用 [目录] 前缀标示，优先显示路径末尾
                val_disp = _truncate_path_tail(str(val), val_w - 5)
                line = f"{label:<{label_w}}: [目录] {val_disp}"
            elif ftype == "filepath":
                # 文件路径：用 [文件] 前缀标示，优先显示路径末尾
                val_disp = _truncate_path_tail(str(val), val_w - 5)
                line = f"{label:<{label_w}}: [文件] {val_disp}"
            else:
                val_disp = _truncate_str(str(val), val_w)
                line = f"{label:<{label_w}}: {val_disp}"

            _safe_addstr(stdscr, ry, fx + 2, line, attr)

        stdscr.refresh()
        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue
        # get_wch 返回 str 时转为 ord，统一用 int 处理快捷键
        if isinstance(ch, str):
            ch = ord(ch) if len(ch) == 1 else -1
        ftype = fields[selected].get("type", "text")

        if ch == 27:  # Esc
            return None

        elif ch == curses.KEY_UP:
            selected = (selected - 1) % len(fields)

        elif ch == curses.KEY_DOWN:
            selected = (selected + 1) % len(fields)

        elif ch in (ord(" "), curses.KEY_LEFT, curses.KEY_RIGHT):
            if ftype == "toggle":
                fields[selected]["value"] = not fields[selected]["value"]

        elif ch in (ord("e"), ord("E")):
            # e 键：对路径类字段手动输入
            if ftype in ("dirpath", "filepath", "text"):
                new_val = input_box(stdscr, f"手动输入 {fields[selected]['label']}:",
                                    str(fields[selected]["value"]))
                if new_val is not None:
                    fields[selected]["value"] = new_val
                    if fields[selected]["key"] == "task":
                        for f in fields:
                            if f["key"] == "output_dir":
                                f["value"] = default_config_dir(new_val)
                                break

        elif ch in (10, 13):  # Enter
            if ftype == "button":
                return {f["key"]: f["value"] for f in fields}

            elif ftype == "text":
                new_val = input_box(stdscr, f"编辑 {fields[selected]['label']}:",
                                    str(fields[selected]["value"]))
                if new_val is not None:
                    fields[selected]["value"] = new_val
                    # task 字段变更时自动同步 output_dir
                    if fields[selected]["key"] == "task":
                        for f in fields:
                            if f["key"] == "output_dir":
                                f["value"] = default_config_dir(new_val)
                                break

            elif ftype == "dirpath":
                # 打开目录浏览器
                chosen = file_browser(stdscr, fields[selected]["value"], mode="dir")
                if chosen is not None:
                    fields[selected]["value"] = str(chosen)

            elif ftype == "filepath":
                # 打开文件浏览器，支持 extensions 过滤
                exts = fields[selected].get("extensions")
                chosen = file_browser(stdscr, fields[selected]["value"],
                                      mode="file", extensions=exts)
                if chosen is not None:
                    fields[selected]["value"] = str(chosen)

        elif ch in (curses.KEY_F5, ord("r"), ord("R")):
            return {f["key"]: f["value"] for f in fields}


# ─── Apply 交互确认界面 ──────────────────────────────────

def interactive_confirm_changes(stdscr, changed_details: list[tuple]) -> list[tuple]:
    """
    逐条展示改动，用户逐一确认是否接受。
    返回用户接受的改动子列表。

    按键:
      Enter      - 接受当前改动，下一条
      Esc        - 跳过当前改动，下一条
      a          - 接受剩余所有改动
      A          - 跳过剩余所有改动（取消）
      ↑/↓        - 滚动内容
      PgUp/PgDn  - 快速翻页
    """
    approved = []
    total    = len(changed_details)

    for idx, (cls, uid, field, old_val, new_val) in enumerate(changed_details):
        # 预计算差异分段（不依赖宽度，只算一次）
        old_repr = repr(old_val)
        new_repr = repr(new_val)
        old_segs, new_segs = _diff_segments(old_repr, new_repr)
        scroll_top = 0  # 每条改动重置滚动位置

        while True:
            stdscr.clear()
            sh, sw = stdscr.getmaxyx()

            # ── 布局参数
            box_w     = min(sw - 4, 84)
            bx        = max(0, (sw - box_w) // 2)
            inner_w   = box_w - 4        # 内框总显示宽（含内框边线）
            content_w = inner_w - 4      # │  <content>│ 中内容实际宽度
            box_h     = sh - 2           # 始终占满屏幕高度
            by        = 1

            # ── 固定区域行数（节点类型/UUID/字段/空行）
            FIXED_ROWS = 4
            # 可滚动区域高度 = box_h - 顶边(1) - hline(1) - 固定区(4) - 底边(1)
            avail_h = max(1, box_h - 7)

            # ── 换行（依赖 content_w，需在 while 内）
            old_lines = _wrap_segments(old_segs, content_w)
            new_lines = _wrap_segments(new_segs, content_w)

            # ── 构建虚拟行列表（可滚动内容）
            border_attr = curses.color_pair(COLOR_BORDER)
            # 未变化部分：暗色，变化部分：背景色高亮（红底白字 / 绿底黑字）
            dim_attr    = curses.color_pair(COLOR_DIM)  | curses.A_DIM
            old_base    = dim_attr
            old_hl      = curses.color_pair(COLOR_DIFF_OLD) | curses.A_BOLD
            new_base    = dim_attr
            new_hl      = curses.color_pair(COLOR_DIFF_NEW) | curses.A_BOLD

            virtual: list[tuple] = []
            old_title_attr = curses.color_pair(COLOR_ERROR)   | curses.A_BOLD
            new_title_attr = curses.color_pair(COLOR_SUCCESS) | curses.A_BOLD
            virtual.append(("text", "┌──── 旧值 " + "─" * (inner_w - 11) + "┐", old_title_attr))
            for ls in old_lines:
                virtual.append(("segs", ls, old_base, old_hl))
            virtual.append(("text", "└" + "─" * (inner_w - 2) + "┘", old_title_attr))
            virtual.append(("text", "", curses.color_pair(COLOR_MENU)))
            virtual.append(("text", "┌──── 新值 " + "─" * (inner_w - 11) + "┐", new_title_attr))
            for ls in new_lines:
                virtual.append(("segs", ls, new_base, new_hl))
            virtual.append(("text", "└" + "─" * (inner_w - 2) + "┘", new_title_attr))

            total_v   = len(virtual)
            max_scroll = max(0, total_v - avail_h)
            scroll_top = max(0, min(scroll_top, max_scroll))
            can_scroll = max_scroll > 0

            # ── 绘制
            draw_header(stdscr, "Apply 改动确认")
            footer_hints = [("Enter", "接受"), ("Esc", "跳过"),
                            ("a", "全部接受"), ("s", "全部跳过"), ("q", "退出")]
            if can_scroll:
                footer_hints += [("↑↓", "滚动"), ("PgUp/Dn", "翻页")]
            draw_footer(stdscr, footer_hints)
            draw_box(stdscr, by, bx, box_h, box_w)

            # 进度标题
            prog_str = f"  Apply 改动确认  │  进度: {idx+1}/{total}  │  已接受: {len(approved)}  "
            _safe_addstr(stdscr, by, bx + 2, prog_str, curses.color_pair(COLOR_TITLE))
            draw_hline(stdscr, by + 1, bx, box_w)

            inner_x = bx + 2
            ry = by + 2

            # 固定信息区
            _safe_addstr(stdscr, ry, inner_x,
                         f"节点类型:  {cls}",
                         curses.color_pair(COLOR_WARN));  ry += 1
            _safe_addstr(stdscr, ry, inner_x,
                         f"节点 UUID: {_truncate_str(uid, inner_w - 12)}"); ry += 1
            _safe_addstr(stdscr, ry, inner_x,
                         _truncate_path_tail(f"字段:      {field}", inner_w),
                         curses.color_pair(COLOR_WARN));  ry += 1
            ry += 1  # 空行

            # 滚动条：在外框右边线上绘制 ▲/▼
            if can_scroll:
                scroll_start_y = ry      # 滚动区域起始行
                scroll_end_y   = by + box_h - 2
                bar_x          = bx + box_w - 1
                if scroll_top > 0:
                    _safe_addstr(stdscr, scroll_start_y, bar_x, "▲",
                                 curses.color_pair(COLOR_WARN) | curses.A_BOLD)
                if scroll_top < max_scroll:
                    _safe_addstr(stdscr, scroll_end_y, bar_x, "▼",
                                 curses.color_pair(COLOR_WARN) | curses.A_BOLD)
                # 百分比位置
                pct_str = f"{scroll_top * 100 // max_scroll:3d}%"
                pct_y   = scroll_start_y + 1
                if pct_y < scroll_end_y:
                    _safe_addstr(stdscr, pct_y, bar_x - 3, pct_str,
                                 curses.color_pair(COLOR_DIM))

            # 可滚动内容区渲染
            visible = virtual[scroll_top: scroll_top + avail_h]
            for row in visible:
                if ry >= by + box_h - 1:
                    break
                if row[0] == "text":
                    _, text, attr = row
                    _safe_addstr(stdscr, ry, inner_x, text, attr)
                else:
                    _, line_segs, base_attr, hl_attr = row
                    _safe_addstr(stdscr, ry, inner_x,     "│",  border_attr)
                    _safe_addstr(stdscr, ry, inner_x + 1, "  ", base_attr)
                    x = inner_x + 3
                    used = 0
                    for seg_text, is_changed in line_segs:
                        attr = hl_attr if is_changed else base_attr
                        _safe_addstr(stdscr, ry, x, seg_text, attr)
                        w     = _str_width(seg_text)
                        x    += w
                        used += w
                    pad = max(0, content_w - used)
                    if pad:
                        _safe_addstr(stdscr, ry, x, " " * pad, base_attr)
                    _safe_addstr(stdscr, ry, inner_x + inner_w - 1, "│", border_attr)
                ry += 1

            stdscr.refresh()
            ch = stdscr.getch()

            if ch in (10, 13):                     # Enter：接受
                approved.append((cls, uid, field, old_val, new_val))
                break
            elif ch == 27:                         # Esc：跳过
                break
            elif ch in (ord("a"), ord("A")):        # 接受剩余所有
                approved.append((cls, uid, field, old_val, new_val))
                approved.extend(changed_details[idx + 1:])
                return approved
            elif ch in (ord("s"), ord("S")):        # 跳过剩余所有
                return approved
            elif ch in (ord("q"), ord("Q")):        # 退出，放弃所有
                return []
            elif ch == curses.KEY_UP:
                scroll_top = max(0, scroll_top - 1)
            elif ch == curses.KEY_DOWN:
                scroll_top = min(max_scroll, scroll_top + 1)
            elif ch in (curses.KEY_PPAGE,):        # PgUp
                scroll_top = max(0, scroll_top - avail_h)
            elif ch in (curses.KEY_NPAGE,):        # PgDn
                scroll_top = min(max_scroll, scroll_top + avail_h)

    return approved


# ─── 备份槽公共工具 ──────────────────────────────────────

def _load_slots(backup_base, task_name: str) -> list:
    """返回按时间倒序排列的备份槽目录列表（不含 _latest）"""
    if not backup_base.exists():
        return []
    return sorted(
        [d for d in backup_base.iterdir()
         if d.is_dir() and d.name.startswith(task_name + "_")
         and not d.name.endswith("_latest")],
        reverse=True,
    )


def _slot_meta(slot_dir) -> tuple[str, str]:
    """返回 (backed_at[:16], backup_type)，读不到则返回空串"""
    meta_path = slot_dir / "backup_meta.json"
    if not meta_path.exists():
        return "", ""
    try:
        with open(meta_path) as f:
            m = json.load(f)
        return m.get("backed_up_at", "")[:16], m.get("backup_type", "")
    except Exception:
        return "", ""


def _rename_slot(backup_base, task_name: str, old_name: str, new_name: str) -> tuple[bool, str]:
    """
    将备份槽目录从 old_name 重命名为 new_name。
    若 _latest 软链指向旧名，同步更新。
    返回 (success, error_msg)。
    """
    new_name = new_name.strip()
    if not new_name:
        return False, "名称不能为空"
    if new_name == old_name:
        return False, "名称未变化"
    old_path = backup_base / old_name
    new_path = backup_base / new_name
    if new_path.exists():
        return False, f"名称已存在: {new_name}"
    # 检查 _latest 是否指向旧槽
    latest_link = backup_base / f"{task_name}_latest"
    was_latest = (latest_link.is_symlink()
                  and latest_link.resolve().name == old_name)
    old_path.rename(new_path)
    if was_latest:
        latest_link.unlink(missing_ok=True)
        latest_link.symlink_to(new_path)
    return True, ""


# ─── 备份槽选择器 ────────────────────────────────────────

def backup_slot_selector(stdscr, task_name: str) -> str | None:
    """
    列出可用备份槽，上下键选择，Enter 确认，r 重命名，Esc 取消。
    返回槽名字符串（或 None）。
    """
    backup_base = get_backup_dir()
    if not backup_base.exists():
        show_result(stdscr, "暂无备份记录。", "备份槽选择")
        return None

    slots = _load_slots(backup_base, task_name)
    if not slots:
        show_result(stdscr, f"任务 '{task_name}' 暂无备份。", "备份槽选择")
        return None

    latest_link = backup_base / f"{task_name}_latest"
    selected    = 0

    while True:
        # _latest 每次重新读（重命名后可能变）
        latest_name = latest_link.resolve().name if latest_link.exists() else None

        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        draw_header(stdscr, task_name)
        draw_footer(stdscr, [("↑↓", "移动"), ("Enter", "选择此槽"), ("r", "重命名"), ("Esc", "取消")])

        box_w     = min(sw - 4, 76)
        box_h     = min(len(slots) + 4, sh - 2)
        by        = max(1, (sh - box_h) // 2)
        bx        = max(0, (sw - box_w) // 2)
        inner_w   = box_w - 4
        draw_box(stdscr, by, bx, box_h, box_w)
        _safe_addstr(stdscr, by, bx + 2, "  选择备份槽  ", curses.color_pair(COLOR_TITLE))
        draw_hline(stdscr, by + 1, bx, box_w)

        visible_h  = box_h - 4
        scroll_off = max(0, selected - visible_h + 1)

        for i in range(visible_h):
            si = i + scroll_off
            if si >= len(slots):
                break
            slot_dir  = slots[si]
            backed_at, btype = _slot_meta(slot_dir)
            is_latest = (slot_dir.name == latest_name)
            flag      = " ★" if is_latest else ""
            line      = f"{slot_dir.name:<42} {backed_at:<16} {btype}{flag}"

            ry = by + 2 + i
            if si == selected:
                attr = curses.color_pair(COLOR_SELECT) | curses.A_BOLD
            elif is_latest:
                attr = curses.color_pair(COLOR_SUCCESS)
            else:
                attr = curses.color_pair(COLOR_MENU)
            stdscr.addstr(ry, bx + 2, " " * inner_w, attr)
            _safe_addstr(stdscr, ry, bx + 2, _truncate_str(line, inner_w), attr)

        stdscr.refresh()
        ch = stdscr.getch()

        if ch == 27:
            return None
        elif ch == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif ch == curses.KEY_DOWN:
            selected = min(len(slots) - 1, selected + 1)
        elif ch in (10, 13):
            return slots[selected].name
        elif ch in (ord("r"), ord("R")):
            old_name = slots[selected].name
            new_name = input_box(stdscr, "重命名备份槽:", old_name)
            if new_name and new_name != old_name:
                ok, err = _rename_slot(backup_base, task_name, old_name, new_name)
                if ok:
                    slots = _load_slots(backup_base, task_name)
                    # 定位到重命名后的槽
                    new_names = [s.name for s in slots]
                    selected  = new_names.index(new_name) if new_name in new_names else min(selected, len(slots) - 1)
                else:
                    show_result(stdscr, f"重命名失败: {err}", "错误")


# ─── 备份列表 Diff 结果展示 ──────────────────────────────

def show_diff_result(stdscr, text: str):
    """带颜色的 diff 结果展示"""
    lines = text.splitlines()
    offset = 0

    while True:
        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        draw_header(stdscr, "Diff 对比结果")
        draw_footer(stdscr, [("↑↓/PgUp/PgDn", "滚动"), ("任意键", "关闭")])
        inner_h = sh - 2
        for i in range(inner_h):
            li = offset + i
            if li >= len(lines):
                break
            line = lines[li]
            # 按内容选颜色
            if line.startswith("      A:"):
                attr = curses.color_pair(COLOR_ERROR)
            elif line.startswith("      B:"):
                attr = curses.color_pair(COLOR_SUCCESS)
            elif line.startswith("  ["):
                attr = curses.color_pair(COLOR_WARN) | curses.A_BOLD
            elif line.startswith("==="):
                attr = curses.color_pair(COLOR_TITLE)
            elif "仅在 A" in line:
                attr = curses.color_pair(COLOR_ERROR)
            elif "仅在 B" in line:
                attr = curses.color_pair(COLOR_SUCCESS)
            elif "无差异" in line:
                attr = curses.color_pair(COLOR_DIM)
            else:
                attr = curses.color_pair(COLOR_MENU)
            _safe_addstr(stdscr, 1 + i, 1, line, attr)
        if len(lines) > inner_h:
            pct = int(100 * offset / max(1, len(lines) - inner_h))
            _safe_addstr(stdscr, sh-1, sw-8, f" {pct:3d}%", curses.color_pair(COLOR_TITLE))
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif ch == curses.KEY_DOWN:
            offset = min(max(0, len(lines) - inner_h), offset + 1)
        elif ch == curses.KEY_PPAGE:
            offset = max(0, offset - inner_h)
        elif ch == curses.KEY_NPAGE:
            offset = min(max(0, len(lines) - inner_h), offset + inner_h)
        else:
            break


# ─── 备份列表界面 ────────────────────────────────────────

def show_backups_table(stdscr, task_name: str):
    """以表格形式展示备份列表，支持选中 + r 重命名"""
    backup_base = get_backup_dir()
    latest_link = backup_base / f"{task_name}_latest"

    slots    = _load_slots(backup_base, task_name)
    selected = 0

    while True:
        latest_name = latest_link.resolve().name if latest_link.exists() else None

        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        draw_header(stdscr, task_name)
        draw_footer(stdscr, [("↑↓", "移动"), ("r", "重命名槽"), ("Esc/q", "关闭")])

        if not slots:
            _safe_addstr(stdscr, sh // 2, sw // 2 - 8, "暂无备份记录",
                         curses.color_pair(COLOR_DIM))
            stdscr.refresh()
            stdscr.getch()
            return

        # 表头
        header = f"  {'备份槽名称':<42} {'时间':<16} {'类型':<14} {'标记'}"
        _safe_addstr(stdscr, 1, 0, header, curses.color_pair(COLOR_BORDER) | curses.A_BOLD)
        _safe_addstr(stdscr, 2, 0, "─" * (sw - 1), curses.color_pair(COLOR_BORDER))

        list_top   = 3
        visible_h  = sh - list_top - 1
        scroll_off = max(0, selected - visible_h + 1)

        for i in range(visible_h):
            si = i + scroll_off
            if si >= len(slots):
                break
            slot_dir  = slots[si]
            backed_at, btype = _slot_meta(slot_dir)
            is_latest = (slot_dir.name == latest_name)
            flag      = "★ latest" if is_latest else ""

            col_name  = f"{slot_dir.name:<42}"
            col_time  = f"{backed_at:<16}"
            col_type  = f"{btype:<14}"
            line      = f"  {col_name} {col_time} {col_type} {flag}"

            ry = list_top + i
            if si == selected:
                base_attr = curses.color_pair(COLOR_SELECT) | curses.A_BOLD
                stdscr.addstr(ry, 0, " " * (sw - 1), base_attr)
                _safe_addstr(stdscr, ry, 0, _truncate_str(line, sw - 1), base_attr)
            elif is_latest:
                _safe_addstr(stdscr, ry, 0, _truncate_str(line, sw - 1),
                             curses.color_pair(COLOR_SUCCESS) | curses.A_BOLD)
            elif btype == "task_only":
                _safe_addstr(stdscr, ry, 0, _truncate_str(line, sw - 1),
                             curses.color_pair(COLOR_WARN))
            elif btype == "overrides_only":
                _safe_addstr(stdscr, ry, 0, _truncate_str(line, sw - 1),
                             curses.color_pair(COLOR_KEY))
            else:
                _safe_addstr(stdscr, ry, 0, _truncate_str(line, sw - 1),
                             curses.color_pair(COLOR_MENU))

        stdscr.refresh()
        ch = stdscr.getch()

        if ch in (27, ord("q"), ord("Q")):
            return
        elif ch == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif ch == curses.KEY_DOWN:
            selected = min(len(slots) - 1, selected + 1)
        elif ch in (ord("r"), ord("R")):
            old_name = slots[selected].name
            new_name = input_box(stdscr, "重命名备份槽:", old_name)
            if new_name and new_name != old_name:
                ok, err = _rename_slot(backup_base, task_name, old_name, new_name)
                if ok:
                    slots = _load_slots(backup_base, task_name)
                    new_names = [s.name for s in slots]
                    selected  = new_names.index(new_name) if new_name in new_names else min(selected, len(slots) - 1)
                else:
                    show_result(stdscr, f"重命名失败: {err}", "错误")


# ─── 各命令操作界面 ──────────────────────────────────────

def do_extract(stdscr, task_name: str):
    """Extract 参数表单 + 执行"""
    fields = [
        {"key": "task",       "label": "Task 名称",  "value": task_name,                        "type": "text"},
        {"key": "output_dir", "label": "输出目录",   "value": default_config_dir(task_name),    "type": "dirpath"},
        {"key": "run",        "label": "执行 Extract", "value": None,                           "type": "button"},
    ]
    result = show_form(stdscr, "Extract — 提取 YAML", fields, task_name)
    if result is None:
        return
    out_text = run_command_capture(
        cmd_extract,
        result["task"],
        result["output_dir"],
    )
    show_result(stdscr, out_text, "Extract 结果")


def do_update(stdscr, task_name: str):
    """Update 参数表单 + 执行"""
    fields = [
        {"key": "task",       "label": "Task 名称",  "value": task_name,                        "type": "text"},
        {"key": "input_dir",  "label": "输入目录",   "value": default_config_dir(task_name),    "type": "dirpath"},
        {"key": "output_dir", "label": "输出目录",   "value": default_config_dir(task_name),    "type": "dirpath"},
        {"key": "run",        "label": "执行 Update", "value": None,                            "type": "button"},
    ]
    result = show_form(stdscr, "Update — 追加改动到 overrides", fields, task_name)
    if result is None:
        return
    out_text = run_command_capture(
        cmd_update,
        result["task"],
        result["input_dir"],
        result["output_dir"],
    )
    show_result(stdscr, out_text, "Update 结果")


def do_apply(stdscr, task_name: str):
    """Apply 参数表单 + 可选交互式确认 + 执行"""
    fields = [
        {"key": "task",             "label": "Task 名称",   "value": task_name,                     "type": "text"},
        {"key": "input_dir",        "label": "输入目录",    "value": default_config_dir(task_name), "type": "dirpath"},
        {"key": "trust_task",       "label": "Trust Task",  "value": True,                          "type": "toggle"},
        {"key": "overwrite_python", "label": "写回 .py 文件","value": False,                        "type": "toggle"},
        {"key": "interactive",      "label": "交互确认模式", "value": True,                         "type": "toggle"},
        {"key": "run",              "label": "执行 Apply",   "value": None,                         "type": "button"},
    ]
    result = show_form(stdscr, "Apply — 应用 YAML 到 task", fields, task_name)
    if result is None:
        return

    task_nm    = result["task"]
    input_dir  = result["input_dir"]
    trust_task = result["trust_task"]
    ow_python  = result["overwrite_python"]
    interactive = result["interactive"]

    if interactive:
        # ── 交互模式：dry-run → 逐条确认 → 执行 ──
        # 显示等待提示
        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        draw_header(stdscr, task_nm)
        _safe_addstr(stdscr, sh // 2, sw // 2 - 12, "正在计算改动，请稍候...", curses.color_pair(COLOR_WARN))
        stdscr.refresh()

        try:
            task_data, all_path, changed_details = _compute_apply_changes(
                task_nm, input_dir, trust_task=trust_task
            )
        except Exception as e:
            show_result(stdscr, f"计算改动时出错:\n{e}", "Apply 错误")
            return

        if not changed_details:
            show_result(stdscr, "无字段改动。YAML 与 task 已完全一致，无需写入。", "Apply 结果")
            return

        # 逐条确认
        approved = interactive_confirm_changes(stdscr, changed_details)

        # 汇总确认界面
        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        draw_header(stdscr, task_nm)
        draw_footer(stdscr, [("Enter", "确认执行"), ("Esc", "取消操作")])
        total_str = (f"共 {len(changed_details)} 条改动，"
                     f"已接受 {len(approved)} 条，"
                     f"跳过 {len(changed_details) - len(approved)} 条。")
        _safe_addstr(stdscr, sh // 2 - 1, 2, total_str, curses.color_pair(COLOR_WARN) | curses.A_BOLD)
        if approved:
            _safe_addstr(stdscr, sh // 2 + 1, 2, "按 Enter 执行写入，Esc 取消。", curses.color_pair(COLOR_MENU))
        else:
            _safe_addstr(stdscr, sh // 2 + 1, 2, "没有接受任何改动，无需写入。", curses.color_pair(COLOR_DIM))
        stdscr.refresh()

        ch = stdscr.getch()
        if not approved or ch == 27:
            show_result(stdscr, "已取消，未写入任何改动。", "Apply 已取消")
            return

        # 执行写入
        try:
            out_text = _execute_apply(
                task_nm, task_data, all_path, input_dir,
                approved_changes=approved,
                trust_task=trust_task,
                overwrite_python=ow_python,
            )
        except Exception as e:
            show_result(stdscr, f"写入时出错:\n{e}", "Apply 错误")
            return
        show_result(stdscr, out_text, "Apply 结果")

    else:
        # ── 直接模式：调用 cmd_apply，捕获输出 ──
        out_text = run_command_capture(
            cmd_apply,
            task_nm, input_dir,
            overwrite_python=ow_python,
            trust_task=trust_task,
        )
        show_result(stdscr, out_text, "Apply 结果")


def do_diff(stdscr, task_name: str):
    """Diff 参数表单 + 执行"""
    cfg = default_config_dir(task_name)
    fields = [
        {"key": "yaml_a",  "label": "YAML 文件 A", "value": str(SCRIPT_DIR / cfg / "nodes_overrides.yaml"), "type": "filepath", "extensions": [".yaml", ".yml"]},
        {"key": "yaml_b",  "label": "YAML 文件 B", "value": "",                                              "type": "filepath", "extensions": [".yaml", ".yml"]},
        {"key": "keys",    "label": "过滤关键词",   "value": "",                                              "type": "text"},
        {"key": "run",     "label": "执行 Diff",    "value": None,                                           "type": "button"},
    ]
    result = show_form(stdscr, "Diff — 对比两个 YAML", fields, task_name)
    if result is None:
        return
    filter_keys = [k.strip() for k in result["keys"].split(",") if k.strip()] if result["keys"] else None
    out_text = run_command_capture(
        cmd_diff,
        result["yaml_a"],
        result["yaml_b"],
        filter_keys,
    )
    show_diff_result(stdscr, out_text)


def do_backup(stdscr, task_name: str):
    """Backup 参数表单 + 执行"""
    fields = [
        {"key": "task",      "label": "Task 名称", "value": task_name,                        "type": "text"},
        {"key": "input_dir", "label": "配置目录",  "value": default_config_dir(task_name),    "type": "dirpath"},
        {"key": "run",       "label": "执行 Backup","value": None,                            "type": "button"},
    ]
    result = show_form(stdscr, "Backup — 全量备份", fields, task_name)
    if result is None:
        return
    out_text = run_command_capture(
        cmd_backup,
        result["task"],
        result["input_dir"],
    )
    show_result(stdscr, out_text, "Backup 结果")


def do_restore(stdscr, task_name: str):
    """Restore 参数表单 + 备份槽选择 + 执行（含 Python 脚本逐条确认）"""
    import json as _json

    # 1. 选备份槽
    slot = backup_slot_selector(stdscr, task_name)
    if slot is None:
        slot = "latest"

    # 2. 预检：计算备份槽与当前 task 的 Python 脚本差异
    backup_base = get_backup_dir()
    task_path   = get_task_path(task_name)
    if slot == "latest":
        latest_link = backup_base / f"{task_name}_latest"
        slot_dir = latest_link.resolve() if latest_link.exists() else None
    else:
        slot_dir = backup_base / slot
    task_backup = (slot_dir / f"{task_name}.task") if slot_dir else None

    py_changes: list[tuple] = []
    if task_backup and task_backup.exists() and task_path.exists():
        py_changes = _compute_restore_python_changes(task_backup, task_path)

    # 3. 表单
    fields = [
        {"key": "task",         "label": "Task 名称",  "value": task_name,                     "type": "text"},
        {"key": "output_dir",   "label": "输出目录",   "value": default_config_dir(task_name), "type": "dirpath"},
        {"key": "slot",         "label": "备份槽",     "value": slot,                          "type": "text"},
        {"key": "restore_task", "label": "还原 Task",  "value": True,                          "type": "toggle"},
        {"key": "restore_yaml", "label": "还原 YAML",  "value": True,                          "type": "toggle"},
        {"key": "run",          "label": "执行 Restore","value": None,                         "type": "button"},
    ]

    result = show_form(stdscr, "Restore — 从备份还原", fields, task_name)
    if result is None:
        return

    restore_task = result["restore_task"]
    restore_yaml = result["restore_yaml"]

    # 4. 还原 Task 开启且有 Python 脚本变化时，逐条交互确认
    approved_py: list[tuple] = []
    if restore_task and py_changes:
        confirm_details = [
            ("PythonNodeModel", uid, f"script  [{file_path}]", cur, bak)
            for uid, file_path, cur, bak in py_changes
        ]
        approved_details = interactive_confirm_changes(stdscr, confirm_details)
        approved_uids = {item[1] for item in approved_details}
        approved_py = [(uid, fp, cur, bak)
                       for uid, fp, cur, bak in py_changes
                       if uid in approved_uids]

    # 5. 执行还原
    out_text = run_command_capture(
        cmd_restore,
        result["task"],
        result["output_dir"],
        result["slot"],
        only_yaml=not restore_yaml,
        only_task=not restore_task,
        overwrite_python=False,   # python 脚本由下方手动写
    )

    # 6. 写用户确认的 Python 脚本
    if approved_py:
        project_root = task_path.parent.parent
        py_lines = []
        for uid, file_path_str, _cur, backup_script in approved_py:
            from pathlib import Path as _Path
            py_path = project_root / file_path_str
            py_path.parent.mkdir(parents=True, exist_ok=True)
            py_path.write_text(backup_script, encoding="utf-8")
            py_lines.append(f"  python ← {file_path_str}")
        out_text += "\n已还原 Python 脚本:\n" + "\n".join(py_lines)

    show_result(stdscr, out_text, "Restore 结果")


def do_list_backups(stdscr, task_name: str):
    """直接显示备份列表"""
    show_backups_table(stdscr, task_name)


def do_remove_backups(stdscr, task_name: str):
    """
    Remove Backups — 顶部模式选择器（←→切换），下方动态显示对应参数。

    四种模式：
      days  — 删除超过 N 天的旧备份（默认）
      count — 删除最近 N 个备份
      slot  — 删除指定备份槽（支持弹出选择器）
      all   — 删除全部备份
    """
    # (mode_key, 显示标签, 参数说明, 默认参数值)
    MODES = [
        ("days",  "超过N天  (days)",  "天数",   "7"),
        ("count", "最近N个  (count)", "数量",   "1"),
        ("slot",  "指定槽名 (slot)",  "备份槽", ""),
        ("all",   "全部删除 (all)",   None,     None),
    ]
    mode_idx  = 0        # 默认 days
    task_val  = task_name
    param_val = MODES[0][3]  # 参数值

    # 可选中的行索引：0=task, 1=mode选择器, 2=参数行(可选), last=button
    selected = 1  # 默认选中模式选择器

    while True:
        stdscr.clear()
        sh, sw = stdscr.getmaxyx()
        draw_header(stdscr, task_val)
        draw_footer(stdscr, [
            ("↑↓",    "移动"),
            ("←→",    "切换删除模式"),
            ("Enter", "编辑参数"),
            ("F5/r",  "执行"),
            ("Esc",   "返回"),
        ])

        mode_key, mode_label, param_label, _ = MODES[mode_idx]
        has_param = param_label is not None  # all 模式无参数行

        # 构建行列表（动态）
        # 每行: (row_key, display_label, rtype)
        rows = [
            ("task",  "Task 名称", "text"),
            ("mode",  "删除模式",  "mode"),
        ]
        if has_param:
            rows.append(("param", param_label, "slot_select" if mode_key == "slot" else "text"))
        rows.append(("run", "执行 Remove", "button"))

        # 安全限制 selected
        selected = max(0, min(selected, len(rows) - 1))

        # 绘制边框
        box_w = min(sw - 4, 62)
        box_h = len(rows) + 4   # 标题行 + 分隔线 + 各行 + 底边
        by = max(1, (sh - box_h) // 2)
        bx = max(0, (sw - box_w) // 2)
        draw_box(stdscr, by, bx, box_h, box_w)
        title_str = " Remove Backups — 删除旧备份 "
        _safe_addstr(stdscr, by, bx + 2, title_str, curses.color_pair(COLOR_TITLE))
        draw_hline(stdscr, by + 1, bx, box_w)

        label_w = 9  # 标签列宽（字符数）
        inner_x = bx + 2
        inner_w = box_w - 4

        for i, (rkey, rlabel, rtype) in enumerate(rows):
            ry  = by + 2 + i
            sel = (i == selected)
            bg  = curses.color_pair(COLOR_SELECT) if sel else curses.color_pair(COLOR_MENU)

            # 整行底色
            _safe_addstr(stdscr, ry, bx + 1, " " * (box_w - 2), bg)

            if rtype == "mode":
                # 左右箭头 + 当前模式标签居中
                left_arrow  = "◀"
                right_arrow = "▶"
                mode_display = f"  {rlabel:<{label_w}}: {left_arrow}  {mode_label}  {right_arrow}"
                _safe_addstr(stdscr, ry, inner_x, mode_display, bg)

            elif rtype == "button":
                btn_text = f"  [ {rlabel} ]"
                btn_attr = (curses.color_pair(COLOR_SELECT) | curses.A_BOLD) if sel \
                           else (curses.color_pair(COLOR_SUCCESS) | curses.A_BOLD)
                _safe_addstr(stdscr, ry, inner_x, btn_text, btn_attr)

            else:
                # text / slot_select
                if rkey == "task":
                    val_str = task_val
                else:
                    val_str = param_val or ""
                line = f"  {rlabel:<{label_w}}: {_truncate_path_tail(val_str, inner_w - label_w - 5)}"
                _safe_addstr(stdscr, ry, inner_x, line, bg)

        stdscr.refresh()
        ch = stdscr.getch()

        cur_rkey  = rows[selected][0]
        cur_rtype = rows[selected][2]

        if ch == 27:  # Esc：返回
            return

        elif ch == curses.KEY_UP:
            selected = (selected - 1) % len(rows)

        elif ch == curses.KEY_DOWN:
            selected = (selected + 1) % len(rows)

        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            if cur_rtype == "mode":
                # 切换模式
                delta = -1 if ch == curses.KEY_LEFT else 1
                mode_idx  = (mode_idx + delta) % len(MODES)
                param_val = MODES[mode_idx][3] or ""  # 重置为新模式默认值
                # 若新模式无参数行，selected 超出则移到 button
                if not MODES[mode_idx][2] and selected >= len(rows) - 1:
                    selected = len(rows) - 1

        elif ch in (10, 13):  # Enter
            if cur_rtype == "text":
                label_name = rows[selected][1]
                if cur_rkey == "task":
                    new_val = input_box(stdscr, f"编辑 Task 名称:", task_val)
                    if new_val is not None:
                        task_val = new_val
                else:
                    new_val = input_box(stdscr, f"编辑 {label_name}:", param_val)
                    if new_val is not None:
                        param_val = new_val

            elif cur_rtype == "slot_select":
                # 弹出备份槽选择器
                chosen = backup_slot_selector(stdscr, task_val)
                if chosen:
                    param_val = chosen

            elif cur_rtype == "mode":
                # Enter 在模式行：向右循环
                mode_idx  = (mode_idx + 1) % len(MODES)
                param_val = MODES[mode_idx][3] or ""

            elif cur_rtype == "button":
                break  # 执行

        elif ch in (ord("r"), ord("R"), curses.KEY_F5):
            break  # 执行

    # ── 执行删除 ──────────────────────────────────────────
    mode_key = MODES[mode_idx][0]
    rm_all    = (mode_key == "all")
    slot_val  = param_val.strip() if mode_key == "slot"  else None
    try:
        count_val = int(param_val) if mode_key == "count" else None
    except (ValueError, TypeError):
        count_val = 1
    try:
        days_val = int(param_val) if mode_key == "days" else None
    except (ValueError, TypeError):
        days_val = 7

    out_text = run_command_capture(
        cmd_remove_backups,
        task_val,
        slot=slot_val,
        days=days_val,
        count=count_val,
        remove_all=rm_all,
    )
    show_result(stdscr, out_text, "Remove Backups 结果")


# ─── 主菜单 ──────────────────────────────────────────────

MENU_ITEMS = [
    {"key": "1", "label": "Extract",  "desc": "从 task 文件提取 YAML"},
    {"key": "2", "label": "Update",   "desc": "追加 task 改动到 overrides"},
    {"key": "3", "label": "Apply",    "desc": "将 YAML 配置应用到 task"},
    {"key": "4", "label": "Diff",     "desc": "对比两个 YAML 文件"},
    {"key": "5", "label": "Backup",   "desc": "全量备份"},
    {"key": "6", "label": "Restore",  "desc": "从备份还原"},
    {"key": "7", "label": "Backups",  "desc": "查看备份列表"},
    {"key": "8", "label": "Remove",   "desc": "删除旧备份"},
    {"key": "q", "label": "Quit",     "desc": "退出"},
]

MENU_HANDLERS = {
    "1": do_extract,
    "2": do_update,
    "3": do_apply,
    "4": do_diff,
    "5": do_backup,
    "6": do_restore,
    "7": do_list_backups,
    "8": do_remove_backups,
}


class SiteSyncUI:
    """SiteSync TUI 主类"""

    def __init__(self, task_name: str = DEFAULT_TASK_NAME):
        # 若未通过 CLI 指定 task（仍是默认值），优先读上次记录
        if task_name == DEFAULT_TASK_NAME:
            saved = _load_ui_state().get("last_task", "")
            if saved:
                task_name = saved
        self.task_name = task_name
        self.selected  = 0  # 当前选中菜单项索引

    def run(self, stdscr):
        """主循环"""
        curses.curs_set(0)
        stdscr.keypad(True)
        _init_colors()
        self.show_main_menu(stdscr)

    def show_main_menu(self, stdscr):
        """主菜单循环"""
        while True:
            stdscr.clear()
            sh, sw = stdscr.getmaxyx()
            draw_header(stdscr, self.task_name)
            draw_footer(stdscr, [
                ("↑↓",    "移动"),
                ("Enter", "进入"),
                ("1-8",   "快速进入"),
                ("t",     "切换Task"),
                ("q",     "退出"),
            ])

            # 菜单框
            menu_h = len(MENU_ITEMS) + 4
            menu_w = min(sw - 4, 58)
            my     = max(1, (sh - menu_h) // 2)
            mx     = max(0, (sw - menu_w) // 2)
            draw_box(stdscr, my, mx, menu_h, menu_w)
            title_str = "  SiteSync Tools  "
            _safe_addstr(stdscr, my, mx + 2, title_str, curses.color_pair(COLOR_TITLE))
            draw_hline(stdscr, my + 1, mx, menu_w)
            draw_hline(stdscr, my + menu_h - 2, mx, menu_w)

            inner_w = menu_w - 4
            draw_menu(stdscr, MENU_ITEMS, self.selected, my + 2, mx + 2, inner_w)

            stdscr.refresh()
            ch = stdscr.getch()

            if ch == curses.KEY_UP:
                self.selected = (self.selected - 1) % len(MENU_ITEMS)
            elif ch == curses.KEY_DOWN:
                self.selected = (self.selected + 1) % len(MENU_ITEMS)
            elif ch in (ord("q"), ord("Q")):
                break
            elif ch in (ord("t"), ord("T")):
                # 切换 task 名称，并持久化
                new_task = input_box(stdscr, "输入 Task 名称:", self.task_name)
                if new_task:
                    self.task_name = new_task
                    state = _load_ui_state()
                    state["last_task"] = new_task
                    _save_ui_state(state)
            elif ch in (10, 13):  # Enter
                item = MENU_ITEMS[self.selected]
                key  = item["key"]
                if key == "q":
                    break
                handler = MENU_HANDLERS.get(key)
                if handler:
                    try:
                        handler(stdscr, self.task_name)
                    except Exception as e:
                        show_result(stdscr, f"操作出错:\n{e}", "错误")
            else:
                # 数字键快速跳转
                try:
                    pressed = chr(ch)
                except (ValueError, OverflowError):
                    pressed = ""
                for i, item in enumerate(MENU_ITEMS):
                    if item["key"] == pressed:
                        self.selected = i
                        # 直接触发
                        if pressed != "q":
                            handler = MENU_HANDLERS.get(pressed)
                            if handler:
                                try:
                                    handler(stdscr, self.task_name)
                                except Exception as e:
                                    show_result(stdscr, f"操作出错:\n{e}", "错误")
                        else:
                            return
                        break


# ─── 入口 ────────────────────────────────────────────────

def main():
    import argparse

    # Windows: 切换控制台为 UTF-8，并导入 windows-curses（若已安装）
    if sys.platform == "win32":
        # os.system("chcp ...") 只影响子进程，无法改变当前进程的代码页。
        # 必须同时修改两处：
        #   1. Windows 控制台代码页（SetConsoleOutputCP/SetConsoleCP）
        #   2. C 运行时 locale（msvcrt.setlocale）
        # PDCurses 的 wctomb() 依赖 C 运行时 locale 做 Unicode→多字节转换，
        # 若 locale 仍为 GBK，输出 GBK 字节到 UTF-8 控制台就会显示乱码汉字。
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.cdll.msvcrt.setlocale(0, ".UTF-8")   # LC_ALL=0，C 运行时切到 UTF-8
        except Exception:
            pass
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        try:
            import windows_curses  # noqa: F401
        except ImportError:
            pass

    parser = argparse.ArgumentParser(description="SiteSync Tools TUI — 站点配置管理终端界面")
    parser.add_argument("--task", default=DEFAULT_TASK_NAME,
                        help=f"task 名称（默认: {DEFAULT_TASK_NAME}）")
    args = parser.parse_args()

    curses.wrapper(lambda stdscr: SiteSyncUI(args.task).run(stdscr))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("\n[ERROR] 程序崩溃：")
        traceback.print_exc()
        input("\n按 Enter 键退出...")
