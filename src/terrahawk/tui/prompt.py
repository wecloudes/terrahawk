"""Input prompts: free-text search and tag filter with completion."""

import curses

from .common import _safe
from .listing import _get_tag_completions


# ── Search prompt ─────────────────────────────────────────────────

def _input_prompt(stdscr, prompt_str, initial=""):
    h, w = stdscr.getmaxyx()
    curses.curs_set(1)
    query = list(initial)
    while True:
        display = f" {prompt_str}" + "".join(query)
        _safe(stdscr, h - 1, 0, display + " " * max(0, w - len(display) - 1), w - 1, curses.color_pair(5))
        stdscr.move(h - 1, min(len(f" {prompt_str}") + len(query), w - 1))
        stdscr.refresh()
        k = stdscr.getch()
        if k in (curses.KEY_ENTER, 10, 13):
            break
        elif k == 27:
            query = list(initial)
            break
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            if query:
                query.pop()
        elif 32 <= k <= 126:
            query.append(chr(k))
    curses.curs_set(0)
    return "".join(query)


def _tag_prompt(stdscr, all_units, initial=""):
    """Tag filter prompt with Tab autocomplete and inline suggestions."""
    h, w = stdscr.getmaxyx()
    tag_keys, tag_pairs = _get_tag_completions(all_units)
    all_completions = tag_keys + tag_pairs  # keys first, then key=value pairs

    if not all_completions:
        # No tags in report — show message and return
        _safe(stdscr, h - 1, 0, " No tags found in this report." + " " * (w - 32),
              w - 1, curses.color_pair(3))
        stdscr.refresh()
        stdscr.getch()  # wait for any key to dismiss
        return None

    curses.curs_set(1)
    query = list(initial)
    suggestion = ""  # ghost text shown after cursor

    def _find_suggestion(text):
        """Find best completion matching the current input."""
        if not text:
            return ""
        t = text.lower()
        for c in all_completions:
            if c.lower().startswith(t) and c != text:
                return c[len(text):]
        return ""

    def _find_matches(text):
        """Find all completions matching the current input."""
        if not text:
            return all_completions[:10]
        t = text.lower()
        return [c for c in all_completions if t in c.lower()][:10]

    prompt_str = "tag filter: "

    while True:
        text = "".join(query)
        suggestion = _find_suggestion(text)
        matches = _find_matches(text)

        # Draw prompt line
        prefix = f" {prompt_str}"
        display = prefix + text
        _safe(stdscr, h - 1, 0, " " * (w - 1), w - 1, curses.color_pair(5))
        _safe(stdscr, h - 1, 0, display, w - 1, curses.color_pair(5))

        # Ghost suggestion after typed text
        if suggestion:
            ghost_x = len(display)
            if ghost_x < w - 1:
                _safe(stdscr, h - 1, ghost_x, suggestion[:w - ghost_x - 1],
                      w - ghost_x - 1, curses.A_DIM)

        # Show matches on line h-2 if there are any and input is non-empty
        if text and matches:
            match_line = " matches: " + "  ".join(matches)
            _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)
            _safe(stdscr, h - 2, 0, match_line[:w - 1], w - 1, curses.A_DIM)
        else:
            _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)

        stdscr.move(h - 1, min(len(display), w - 1))
        stdscr.refresh()

        k = stdscr.getch()
        if k in (curses.KEY_ENTER, 10, 13):
            # Validate the input
            final = "".join(query)
            if final:
                # Check if it's a valid key or key=value
                if "=" in final:
                    tk, tv = final.split("=", 1)
                    valid = any(u.get("tags", {}).get(tk) == tv for u in all_units)
                else:
                    valid = final in tag_keys
                if not valid:
                    _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)
                    msg = f" No units match tag '{final}'. Press any key."
                    _safe(stdscr, h - 2, 0, msg[:w - 1], w - 1, curses.color_pair(3))
                    stdscr.refresh()
                    stdscr.getch()
                    continue
            break
        elif k == 27:
            query = list(initial)
            break
        elif k == 9:  # Tab — accept suggestion
            if suggestion:
                query = list(text + suggestion)
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            if query:
                query.pop()
        elif 32 <= k <= 126:
            query.append(chr(k))

    # Clean up the match line
    _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)
    curses.curs_set(0)
    return "".join(query) or None
