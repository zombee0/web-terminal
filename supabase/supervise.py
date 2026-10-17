"""Small management-process supervisor; contains no user-facing services."""

import os
import signal
import subprocess
import time


def main():
    child = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        child = subprocess.Popen(["/opt/web-terminal/venv/bin/python", "-m", "supabase.app"],
                                 cwd="/opt/web-terminal/current", env={
                                     "PATH": "/opt/web-terminal/venv/bin:/usr/local/bin:/usr/bin:/bin",
                                     "HOME": "/root", "WEB_TERMINAL_CONFIG": "/etc/web-terminal/manager.json",
                                     "RUST_LOG": "off", "PYTHONUNBUFFERED": "1",
                                 })
        child.wait()
        if not stopping:
            print("gateway: restarting", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
