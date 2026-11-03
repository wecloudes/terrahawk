"""Terminal UI for viewing Terrahawk scan results (`terrahawk view`).

Split by view: `listing` (unit list), `detail`, `plan`, `module_info`,
`diagram`, `prompt` (search/tag input), shared helpers in `common`, and the
curses main loop in `app`.
"""

from .app import run_tui

__all__ = ["run_tui"]
