"""Two small readings for the owner's first deployment (Run 28), both about THIS process.

- how much memory the process holds now and at its peak, to be compared with the host's limit
  (512 MB on Render's free plan [T]) while the masker is loaded and a login is being hashed;
- nothing else: no user, no text, no key.

`memory_now_mb` reads /proc on Linux (the host); `memory_peak_mb` uses the resource module
(Linux and macOS). Either is None where the platform does not offer it (a Windows machine).
"""

from __future__ import annotations

import os

_MB = 1024 * 1024


def memory_now_mb() -> float | None:
    """The resident set of this process in megabytes, or None."""
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / _MB, 1)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def memory_peak_mb() -> float | None:
    """The highest resident set this process has reached, in megabytes, or None."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError, AttributeError):
        return None
    # Linux reports kilobytes, macOS bytes
    import sys

    return round(peak / _MB, 1) if sys.platform == "darwin" else round(peak / 1024, 1)
