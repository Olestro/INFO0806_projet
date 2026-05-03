"""Worker de scan RFID pour execution en processus separe."""

from __future__ import annotations

import json
import os
import time
import threading
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

from connexion_decodeur import run_inventory


def append_terminal_log(message: str, live_log_path: str, archive_log_path: str) -> None:
    """Ajoute des lignes au log live et a l'archive avec timestamp."""
    live_file = Path(live_log_path)
    archive_file = Path(archive_log_path)
    live_file.parent.mkdir(parents=True, exist_ok=True)
    lines = str(message).splitlines() or [""]
    with open(live_file, "a", encoding="utf-8") as live_f, open(
        archive_file, "a", encoding="utf-8"
    ) as archive_f:
        for line in lines:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            formatted = f"[{timestamp}] {line}\n"
            live_f.write(formatted)
            archive_f.write(formatted)


def _write_scan_state(
    scan_state_path: str,
    status: str,
    session_uid: str,
    preset_name: str,
    pid: int,
) -> None:
    payload = {
        "status": status,
        "session_uid": session_uid,
        "preset_name": preset_name or "",
        "pid": pid,
        "updated_at": time.time(),
    }
    with open(scan_state_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _clear_scan_state(scan_state_path: str) -> None:
    try:
        Path(scan_state_path).unlink()
    except FileNotFoundError:
        return


def _clear_stop_flag(stop_flag_path: str) -> None:
    try:
        Path(stop_flag_path).unlink()
    except FileNotFoundError:
        return


class TerminalLogStream:
    """Flux texte compatible stdout/stderr qui ecrit dans le log en continu."""

    def __init__(self, live_log_path: str, archive_log_path: str) -> None:
        self._buffer = ""
        self._live_log_path = live_log_path
        self._archive_log_path = archive_log_path

    def write(self, text: str) -> int:
        if not text:
            return 0

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            append_terminal_log(line, self._live_log_path, self._archive_log_path)

        return len(text)

    def flush(self) -> None:
        if self._buffer:
            append_terminal_log(self._buffer, self._live_log_path, self._archive_log_path)
            self._buffer = ""


def run_scan_process(
    cfg: dict,
    preset_name: str,
    session_uid: str,
    stop_flag_path: str,
    scan_state_path: str,
    live_log_path: str,
    archive_log_path: str,
    completion_message: str,
) -> None:
    """Execute le scan dans un processus separe avec logs et heartbeat."""

    stop_event = threading.Event()
    stop_flag = Path(stop_flag_path)

    def stop_watcher() -> None:
        while not stop_event.is_set():
            if stop_flag.exists():
                stop_event.set()
                break
            time.sleep(0.2)

    def heartbeat() -> None:
        while not stop_event.is_set():
            _write_scan_state(scan_state_path, "running", session_uid, preset_name, pid)
            time.sleep(1.0)

    pid = os.getpid()
    _write_scan_state(scan_state_path, "running", session_uid, preset_name, pid)
    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()
    watcher_thread = threading.Thread(target=stop_watcher, daemon=True)
    watcher_thread.start()

    append_terminal_log(
        f"Demarrage scan preset='{preset_name}' "
        f"ip={cfg['ip']} port={cfg['port']} antennas={cfg['antennas']} duration={cfg['duration']}",
        live_log_path,
        archive_log_path,
    )

    terminal_stream = TerminalLogStream(live_log_path, archive_log_path)
    try:
        with redirect_stdout(terminal_stream), redirect_stderr(terminal_stream):
            result = run_inventory(
                cfg["ip"],
                cfg["port"],
                cfg["timeout"],
                cfg["antennas"],
                cfg["duration"],
                cfg.get("afficher_antennes", False),
                stop_event=stop_event,
                client_holder=None,
            )
        terminal_stream.flush()

        if result == 0 and not stop_event.is_set():
            append_terminal_log(completion_message, live_log_path, archive_log_path)
        elif result != 0:
            append_terminal_log(f"Erreur scan code={result}", live_log_path, archive_log_path)
    except Exception as exc:
        append_terminal_log(f"Exception scan: {exc}", live_log_path, archive_log_path)
    finally:
        stop_event.set()
        try:
            hb_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            watcher_thread.join(timeout=1.0)
        except Exception:
            pass
        _clear_scan_state(scan_state_path)
        _clear_stop_flag(stop_flag_path)
