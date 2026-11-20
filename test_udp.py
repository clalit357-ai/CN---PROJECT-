"""Local integration tests for the UDP client/server module.

Run from this folder with: python -m pytest -q
These tests use localhost only and do not require Mininet or a controller.
"""

import csv
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_csv(path: Path):
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def wait_for_server(log_path: Path, process: subprocess.Popen, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if process.poll() is not None:
            raise AssertionError(f"server exited early with code {process.returncode}")
        if log_path.exists():
            try:
                if any(row["event"] == "STARTED" for row in read_csv(log_path)):
                    return
            except (OSError, KeyError, csv.Error):
                pass
        time.sleep(0.05)
    raise AssertionError("server did not become ready in time")


def test_client_recovers_from_simulated_loss(tmp_path):
    port = free_udp_port()
    server_log = tmp_path / "server_events.csv"
    client_log = tmp_path / "client_events.csv"
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--host", "127.0.0.1", "--port", str(port),
         "--drop-every", "2", "--log", str(server_log)],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        wait_for_server(server_log, server)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "client.py"), "--host", "127.0.0.1", "--port", str(port),
             "--count", "6", "--interval", "0", "--timeout", "0.08", "--max-retries", "4",
             "--alert-after", "5", "--log", str(client_log)],
            cwd=tmp_path, capture_output=True, text=True, timeout=10,
        )
        assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
        client_events = read_csv(client_log)
        server_events = read_csv(server_log)
        acked = {int(row["seq"]) for row in client_events if row["event"] == "ACK_RECEIVED"}
        assert acked == set(range(1, 7))
        assert any(row["event"] == "RETRANSMIT" for row in client_events)
        assert any(row["event"] == "SIMULATED_DROP" for row in server_events)
        accepted = {int(row["seq"]) for row in server_events if row["event"] == "RECEIVED"}
        assert accepted == set(range(1, 7))

        # A second client run may restart sequence numbering at 1. Its session ID
        # must prevent those packets from being mistaken for duplicates from run 1.
        second_log = tmp_path / "client_events_second_run.csv"
        second = subprocess.run(
            [sys.executable, str(ROOT / "client.py"), "--host", "127.0.0.1", "--port", str(port),
             "--count", "2", "--interval", "0", "--timeout", "0.08", "--max-retries", "4",
             "--alert-after", "5", "--log", str(second_log)],
            cwd=tmp_path, capture_output=True, text=True, timeout=10,
        )
        assert second.returncode == 0, second.stdout + "\n" + second.stderr
        second_rows = read_csv(second_log)
        first_session = next(row["session_id"] for row in client_events if row["event"] == "STARTED")
        second_session = next(row["session_id"] for row in second_rows if row["event"] == "STARTED")
        assert first_session != second_session
        server_after_second_run = read_csv(server_log)
        seq1_sessions = {row["session_id"] for row in server_after_second_run
                         if row["event"] == "RECEIVED" and row["seq"] == "1"}
        assert first_session in seq1_sessions and second_session in seq1_sessions
    finally:
        server.terminate()
        try:
            server.wait(timeout=2)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=2)


def test_client_sends_alert_after_repeated_timeouts(tmp_path):
    unused_port = free_udp_port()
    client_log = tmp_path / "client_alert_events.csv"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "client.py"), "--host", "127.0.0.1", "--port", str(unused_port),
         "--count", "1", "--interval", "0", "--timeout", "0.03", "--max-retries", "2",
         "--alert-after", "1", "--log", str(client_log)],
        cwd=tmp_path, capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 2  # server intentionally absent
    events = read_csv(client_log)
    assert any(row["event"] == "ALERT_SENT" for row in events)
    assert any(row["event"] == "FAILED" for row in events)
