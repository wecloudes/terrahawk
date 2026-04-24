"""Allow running as `python -m terrahawk`."""

import os
import sys

from . import main

if __name__ == "__main__":
    # Force unbuffered stdout for background/CI execution
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
    sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)
    try:
        main()
    except KeyboardInterrupt:
        print("\n\u26a0\ufe0f  Interrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"\n\u274c Fatal error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
