from __future__ import annotations

import os
import signal
import subprocess


def stop_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def file_tail(stream, limit: int) -> str:
    stream.flush()
    size = stream.tell()
    stream.seek(max(0, size - limit))
    return stream.read().decode("utf-8", errors="replace")
