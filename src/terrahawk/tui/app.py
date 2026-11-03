"""TUI main loop and entry point (`terrahawk view`)."""

import curses
import sys

from .common import SORT_MODES, _ALL_ACTIONS, _find_report, _init_colors, _load_report, _safe
from .diagram import _build_diagram_lines, _open_diagram_browser
from .detail import _draw_detail, _format_detail
from .listing import (
    _apply_filters,
    _build_rows,
    _draw_list,
    _get_subscriptions,
    _get_tag_keys,
    _snap_cursor,
)
from .module_info import _format_module_info
from .plan import _build_plan_rows, _draw_plan, _get_plan_types
from .prompt import _input_prompt, _tag_prompt


# ── Main TUI ──────────────────────────────────────────────────────

def _tui_main(stdscr, all_units, report_path):
    _init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)

    # Enable mouse (scroll wheel + click)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    # Filters
    filter_status = None
    filter_sub = None
    filter_tag = None
    search_query = ""
    sort_mode = "status"

    # Derived
    subscriptions = _get_subscriptions(all_units)
    tag_keys = _get_tag_keys(all_units)

    filtered = _apply_filters(all_units, filter_status, filter_sub, filter_tag, search_query, sort_mode)
    rows = _build_rows(filtered)
    cursor = _snap_cursor(rows, 0, 1)
    scroll = 0

    mode = "list"
    detail_lines = []
    detail_scroll = 0
    detail_hscroll = 0
    detail_unit = None
    wrap = False
    # Plan state
    plan_rows = []
    plan_cursor = 0
    plan_scroll = 0
    plan_hscroll = 0
    plan_action_filter = None
    plan_type_filter = None
    plan_display_lines = []

    def rebuild():
        nonlocal filtered, rows, cursor, scroll
        filtered = _apply_filters(all_units, filter_status, filter_sub, filter_tag, search_query, sort_mode)
        rows = _build_rows(filtered)
        cursor = _snap_cursor(rows, 0, 1)
        scroll = 0

    def enter_detail(unit):
        nonlocal detail_unit, detail_lines, detail_scroll, detail_hscroll, mode
        detail_unit = unit
        detail_lines = _format_detail(unit)
        detail_scroll = 0
        detail_hscroll = 0
        mode = "detail"

    def enter_module(unit):
        nonlocal detail_unit, detail_lines, detail_scroll, detail_hscroll, mode
        detail_unit = unit
        detail_lines = _format_module_info(unit)
        detail_scroll = 0
        detail_hscroll = 0
        mode = "detail"

    def enter_diagram(unit):
        nonlocal detail_unit, detail_lines, detail_scroll, detail_hscroll, mode
        dl = _build_diagram_lines(unit)
        if dl:
            detail_unit = unit
            detail_lines = dl
            detail_scroll = 0
            detail_hscroll = 0
            mode = "detail"

    def enter_plan(unit):
        nonlocal detail_unit, plan_rows, plan_cursor, plan_scroll, plan_hscroll
        nonlocal plan_action_filter, plan_type_filter, plan_display_lines, mode
        if not unit.get("planResources"):
            return
        detail_unit = unit
        plan_action_filter = None
        plan_type_filter = None
        plan_rows = _build_plan_rows(unit.get("planResources", []), None, None)
        plan_cursor = 0
        plan_scroll = 0
        plan_hscroll = 0
        plan_display_lines = []
        mode = "plan"

    while True:
        try:
            h, w = stdscr.getmaxyx()
            if h < 5 or w < 20:
                stdscr.erase()
                _safe(stdscr, 0, 0, "Terminal too small", 18, curses.A_DIM)
                stdscr.refresh()
                stdscr.getch()
                continue
        except curses.error:
            continue

        if mode == "list":
            if not rows:
                stdscr.erase()
                _safe(stdscr, h // 2, max(0, (w - 30) // 2),
                      "No units match current filters.", w - 1, curses.color_pair(4))
                _safe(stdscr, h - 1, 0, " f status  s sub  t tag  / search  c clear  q quit",
                      w - 1, curses.A_DIM)
                stdscr.refresh()
            else:
                list_h = h - 4
                cursor = max(0, min(cursor, len(rows) - 1))
                if cursor < scroll:
                    scroll = cursor
                if cursor >= scroll + list_h:
                    scroll = cursor - list_h + 1
                scroll = max(0, scroll)
                _draw_list(stdscr, rows, cursor, scroll,
                           (filter_status, filter_sub, filter_tag, search_query),
                           filtered, sort_mode)

            key = stdscr.getch()

            # Resize
            if key == curses.KEY_RESIZE:
                continue

            # Mouse
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON1_CLICKED:
                        clicked_row = scroll + (my - 3)
                        if 0 <= clicked_row < len(rows) and rows[clicked_row][0] == "unit":
                            cursor = clicked_row
                    elif bstate & curses.BUTTON4_PRESSED:  # scroll up
                        if cursor > 0:
                            cursor = _snap_cursor(rows, cursor - 3, -1)
                    elif bstate & (curses.BUTTON5_PRESSED if hasattr(curses, 'BUTTON5_PRESSED') else 0):
                        if rows and cursor < len(rows) - 1:
                            cursor = _snap_cursor(rows, cursor + 3, 1)
                    # Also handle scroll down via REPORT_MOUSE_POSITION on some terminals
                    elif bstate & 0x00200000:  # scroll down fallback
                        if rows and cursor < len(rows) - 1:
                            cursor = _snap_cursor(rows, cursor + 3, 1)
                except curses.error:
                    pass
                continue

            if key == ord("q"):
                break
            elif key == curses.KEY_DOWN or key == ord("j"):
                if rows and cursor < len(rows) - 1:
                    cursor = _snap_cursor(rows, cursor + 1, 1)
            elif key == curses.KEY_UP or key == ord("k"):
                if cursor > 0:
                    cursor = _snap_cursor(rows, cursor - 1, -1)
            elif key == curses.KEY_NPAGE:
                if rows:
                    cursor = _snap_cursor(rows, min(cursor + (h - 4), len(rows) - 1), -1)
            elif key == curses.KEY_PPAGE:
                if rows:
                    cursor = _snap_cursor(rows, max(cursor - (h - 4), 0), 1)
            elif key == ord("g"):
                cursor = _snap_cursor(rows, 0, 1)
                scroll = 0
            elif key == ord("G"):
                if rows:
                    cursor = _snap_cursor(rows, len(rows) - 1, -1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if rows and rows[cursor][0] == "unit":
                    enter_detail(rows[cursor][1])
            elif key == ord("d"):
                if rows and rows[cursor][0] == "unit":
                    enter_diagram(rows[cursor][1])
            elif key == ord("D"):
                if rows and rows[cursor][0] == "unit":
                    _open_diagram_browser(rows[cursor][1])
            elif key == ord("p"):
                if rows and rows[cursor][0] == "unit":
                    enter_plan(rows[cursor][1])
            elif key == ord("m"):
                if rows and rows[cursor][0] == "unit":
                    enter_module(rows[cursor][1])
            elif key == ord("f"):
                cycle = [None, "drift", "error", "timeout", "clean"]
                idx = cycle.index(filter_status) if filter_status in cycle else 0
                filter_status = cycle[(idx + 1) % len(cycle)]
                rebuild()
            elif key == ord("s"):
                if subscriptions:
                    cycle = [None] + subscriptions
                    idx = cycle.index(filter_sub) if filter_sub in cycle else 0
                    filter_sub = cycle[(idx + 1) % len(cycle)]
                    rebuild()
            elif key == ord("t"):
                filter_tag = _tag_prompt(stdscr, all_units, filter_tag or "")
                rebuild()
            elif key == ord("o"):
                idx = SORT_MODES.index(sort_mode)
                sort_mode = SORT_MODES[(idx + 1) % len(SORT_MODES)]
                rebuild()
            elif key == ord("/"):
                search_query = _input_prompt(stdscr, "/ ", search_query)
                rebuild()
            elif key == ord("c"):
                filter_status = None
                filter_sub = None
                filter_tag = None
                search_query = ""
                sort_mode = "status"
                rebuild()

        elif mode == "detail":
            detail_total = _draw_detail(stdscr, detail_lines, detail_scroll, detail_hscroll, wrap)
            max_scroll = max(0, detail_total - (h - 1))
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key in (27, ord("q"), ord("h"), curses.KEY_LEFT) and detail_hscroll == 0:
                mode = "list"
            elif key == curses.KEY_DOWN or key == ord("j"):
                if detail_scroll < max_scroll:
                    detail_scroll += 1
            elif key == curses.KEY_UP or key == ord("k"):
                if detail_scroll > 0:
                    detail_scroll -= 1
            elif key == curses.KEY_NPAGE:
                detail_scroll = min(detail_scroll + (h - 2), max_scroll)
            elif key == curses.KEY_PPAGE:
                detail_scroll = max(detail_scroll - (h - 2), 0)
            elif key == curses.KEY_RIGHT or key == ord("l"):
                if not wrap:
                    detail_hscroll += 8
            elif key == curses.KEY_LEFT:
                if not wrap:
                    detail_hscroll = max(0, detail_hscroll - 8)
            elif key == ord("0"):
                detail_hscroll = 0
            elif key == ord("w"):
                wrap = not wrap
                detail_scroll = 0
                detail_hscroll = 0
            elif key == ord("g"):
                detail_scroll = 0
            elif key == ord("G"):
                detail_scroll = max_scroll
            elif key == ord("d"):
                if detail_unit:
                    enter_diagram(detail_unit)
            elif key == ord("D"):
                if detail_unit:
                    _open_diagram_browser(detail_unit)
            elif key == ord("p"):
                if detail_unit:
                    enter_plan(detail_unit)
            elif key == ord("m"):
                if detail_unit:
                    enter_module(detail_unit)

            # Mouse scroll in detail
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON4_PRESSED:
                        detail_scroll = max(0, detail_scroll - 3)
                    elif bstate & (getattr(curses, 'BUTTON5_PRESSED', 0) | 0x00200000):
                        detail_scroll = min(detail_scroll + 3, max(0, len(detail_lines) - (h - 1)))
                except curses.error:
                    pass

        elif mode == "plan":
            plan_display_lines = _draw_plan(
                stdscr, detail_unit, plan_rows, plan_cursor, plan_scroll,
                plan_action_filter, plan_type_filter, plan_hscroll, wrap,
            )
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key in (27, ord("q"), ord("h")) or (key == curses.KEY_LEFT and plan_hscroll == 0):
                mode = "list"
            elif key == curses.KEY_DOWN or key == ord("j"):
                if plan_display_lines and plan_cursor < len(plan_display_lines) - 1:
                    plan_cursor += 1
            elif key == curses.KEY_UP or key == ord("k"):
                if plan_cursor > 0:
                    plan_cursor -= 1
            elif key == curses.KEY_NPAGE:
                if plan_display_lines:
                    plan_cursor = min(plan_cursor + (h - 4), len(plan_display_lines) - 1)
            elif key == curses.KEY_PPAGE:
                plan_cursor = max(plan_cursor - (h - 4), 0)
            elif key == curses.KEY_RIGHT or key == ord("l"):
                if not wrap:
                    plan_hscroll += 8
            elif key == curses.KEY_LEFT:
                if not wrap:
                    plan_hscroll = max(0, plan_hscroll - 8)
            elif key == ord("0"):
                plan_hscroll = 0
            elif key == ord("w"):
                wrap = not wrap
                plan_scroll = 0
                plan_hscroll = 0
            elif key == ord("g"):
                plan_cursor = 0
                plan_scroll = 0
            elif key == ord("G"):
                if plan_display_lines:
                    plan_cursor = len(plan_display_lines) - 1
            elif key in (curses.KEY_ENTER, 10, 13):
                if plan_display_lines and plan_cursor < len(plan_display_lines):
                    ri, ltype, _, _ = plan_display_lines[plan_cursor]
                    if ltype == "header":
                        plan_rows[ri]["expanded"] = not plan_rows[ri]["expanded"]
                    elif ltype == "body":
                        plan_rows[ri]["expanded"] = False
            elif key == ord("f"):
                cycle = [None] + list(_ALL_ACTIONS)
                idx = cycle.index(plan_action_filter) if plan_action_filter in cycle else 0
                plan_action_filter = cycle[(idx + 1) % len(cycle)]
                plan_rows = _build_plan_rows(detail_unit.get("planResources", []), plan_action_filter, plan_type_filter)
                plan_cursor = 0
                plan_scroll = 0
            elif key == ord("t"):
                types = _get_plan_types(detail_unit.get("planResources", []))
                if types:
                    cycle = [None] + types
                    idx = cycle.index(plan_type_filter) if plan_type_filter in cycle else 0
                    plan_type_filter = cycle[(idx + 1) % len(cycle)]
                    plan_rows = _build_plan_rows(detail_unit.get("planResources", []), plan_action_filter, plan_type_filter)
                    plan_cursor = 0
                    plan_scroll = 0
            elif key == ord("d"):
                if detail_unit:
                    enter_diagram(detail_unit)
            elif key == ord("D"):
                if detail_unit:
                    _open_diagram_browser(detail_unit)

            # Mouse scroll in plan
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON4_PRESSED:
                        plan_cursor = max(0, plan_cursor - 3)
                    elif bstate & (getattr(curses, 'BUTTON5_PRESSED', 0) | 0x00200000):
                        if plan_display_lines:
                            plan_cursor = min(plan_cursor + 3, len(plan_display_lines) - 1)
                except curses.error:
                    pass

            # Scroll management
            if plan_display_lines:
                plan_h = h - 4
                plan_cursor = max(0, min(plan_cursor, len(plan_display_lines) - 1))
                if plan_cursor < plan_scroll:
                    plan_scroll = plan_cursor
                if plan_cursor >= plan_scroll + plan_h:
                    plan_scroll = plan_cursor - plan_h + 1
                plan_scroll = max(0, plan_scroll)


def run_tui(report_name=None, root_dir=None):
    report_path = _find_report(report_name, root_dir)
    print(f"Loading {report_path.name}...")
    data = _load_report(report_path)
    if not data:
        print("\u274c Report is empty.")
        sys.exit(1)
    try:
        curses.wrapper(lambda stdscr: _tui_main(stdscr, data, report_path))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Ensure terminal is restored before printing
        print(f"\n\u274c TUI error: {e}", file=sys.stderr)
        sys.exit(1)
