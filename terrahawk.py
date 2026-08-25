#!/usr/bin/env python3
"""Thin shim — delegates to the terrahawk package."""

import os
import sys

# Ensure the package is importable when running directly from repo root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from terrahawk import main  # noqa: E402

if __name__ == "__main__":
    # Force unbuffered stdout for background/CI execution
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
    sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\u26a0\ufe0f  Interrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"\n\u274c Fatal error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
