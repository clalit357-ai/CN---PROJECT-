#!/usr/bin/env python3
"""Reliable UDP demo client with ACKs, retries, CSV logs, and SDN failover alerts.

The application protocol is deliberately small and JSON-based so it can run on
localhost first and later on the team's Mininet hosts (h1 -> h2).
"""

import argparse
import csv
import json
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

CSV_FIELDS = ["time", "role", "session_id", "event", "seq", "attempt", "peer", "rtt_ms", "detail"]


class EventLogger:
    """Append events to a CSV file using a shared, analysis-friendly schema."""

    def __init__(self, path: str, role: str = "client", session_id: str = "") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=CSV_FIELDS)
        self.writer.writeheader()
        self.file.flush()
        self.role = role
        self.session_id = session_id

    def log(
        self,
        event: str,
        seq: Optional[int] = None,
        attempt: Optional[int] = None,
        peer: str = "",
        rtt_ms: Optional[float] = None,
        detail: str = "",
    ) -> None:
        row: Dict[str, Any] = {
            "time": f"{time.time():.6f}",
            "role": self.role,
            "session_id": self.session_id,
            "event": event,
            "seq": "" if seq is None else seq,
            "attempt": "" if attempt is None else attempt,
            "peer": peer,
            "rtt_ms": "" if rtt_ms is None else f"{rtt_ms:.3f}",
            "detail": detail,
        }
        self.writer.writerow(row)
        self.file.flush()
        suffix = f" seq={seq}" if seq is not None else ""
        if attempt is not None:
            suffix += f" attempt={attempt}"
        print(f"[{row['time']}] {event}{suffix} {detail}".rstrip(), flush=True)

    def close(self) -> None:
        self.file.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reliable UDP client with retransmission and failover alerts")
    parser.add_argument("--host", default="127.0.0.1", help="UDP server IP/host (localhost default)")
    parser.add_argument("--port", type=int, default=5005, help="UDP server port (default: 5005)")
    parser.add_argument("--count", type=int, default=20, help="Number of application packets to send")
    parser.add_argument("--interval", type=float, default=0.1, help="Delay between completed packets in seconds")
    parser.add_argument("--timeout", type=float, default=0.5, help="ACK wait timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=5, help="Retries after the first attempt")
    parser.add_argument("--payload-prefix", default="hello", help="Text prefix for each packet payload")
    parser.add_argument("--log", default="client_events.csv", help="CSV output path")
    parser.add_argument("--alert-host", default=None, help="SDN alert destination; defaults to --host")
    parser.add_argument("--alert-port", type=int, default=9999, help="SDN controller alert UDP port (default: 9999)")
    parser.add_argument("--alert-after", type=int, default=3,
                        help="Send one SDN alert after this many ACK timeouts for a packet (default: 3)")
    parser.add_argument("--disable-alert", action="store_true", help="Disable SDN alert packets")
    args = parser.parse_args()
    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")
    if args.alert_port < 1 or args.alert_port > 65535:
        parser.error("--alert-port must be between 1 and 65535")
    if args.count < 1:
        parser.error("--count must be at least 1")
    if args.interval < 0:
        parser.error("--interval cannot be negative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.max_retries < 0:
        parser.error("--max-retries cannot be negative")
    if args.alert_after < 1:
        parser.error("--alert-after must be at least 1")
    return args


def send_sdn_alert(
    sock: socket.socket,
    alert_addr: Tuple[str, int],
    logger: EventLogger,
    seq: int,
    timeouts: int,
    session_id: str,
) -> None:
    """Send a small UDP datagram to the controller's configured alert port.

    In Lalit's controller, UDP packets destined for port 9999 are sent to the
    SDN controller by an OpenFlow rule. During localhost tests, no listener is
    needed at this port; UDP send succeeds even if no process is listening.
    """
    message = {
        "type": "ALERT",
        "session_id": session_id,
        "reason": "repeated_ack_timeouts",
        "seq": seq,
        "timeouts": timeouts,
        "time": time.time(),
    }
    sock.sendto(json.dumps(message).encode("utf-8"), alert_addr)
    logger.log(
        "ALERT_SENT",
        seq=seq,
        attempt=timeouts,
        peer=f"{alert_addr[0]}:{alert_addr[1]}",
        detail="repeated ACK timeouts; requested SDN backup path",
    )


def wait_for_ack(
    sock: socket.socket,
    server_addr: Tuple[str, int],
    seq: int,
    session_id: str,
    attempt: int,
    timeout_s: float,
    attempt_started: float,
    logger: EventLogger,
) -> Optional[float]:
    """Wait at most timeout_s for the matching ACK; return RTT in ms or None."""
    deadline = time.perf_counter() + timeout_s
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            raw, peer = sock.recvfrom(65535)
        except socket.timeout:
            return None

        if peer[0] != server_addr[0] or peer[1] != server_addr[1]:
            logger.log("UNEXPECTED_SOURCE", seq=seq, attempt=attempt,
                       peer=f"{peer[0]}:{peer[1]}", detail="ignored datagram from unexpected source")
            continue
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.log("BAD_RESPONSE", seq=seq, attempt=attempt,
                       peer=f"{peer[0]}:{peer[1]}", detail="response was not valid JSON")
            continue
        if (response.get("type") != "ACK" or response.get("seq") != seq
                or response.get("session_id") != session_id):
            logger.log("UNEXPECTED_RESPONSE", seq=seq, attempt=attempt,
                       peer=f"{peer[0]}:{peer[1]}",
                       detail=f"ignored type={response.get('type')} seq={response.get('seq')} session={response.get('session_id')}")
            continue
        return (time.perf_counter() - attempt_started) * 1000.0


def run_client(args: argparse.Namespace) -> int:
    server_ip = socket.gethostbyname(args.host)
    server_addr = (server_ip, args.port)
    alert_ip = socket.gethostbyname(args.alert_host) if args.alert_host else server_ip
    alert_addr = (alert_ip, args.alert_port)
    session_id = uuid.uuid4().hex[:12]
    logger = EventLogger(args.log, role="client", session_id=session_id)
    successes = 0
    failures = 0
    alert_sent = False

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        # Bind explicitly so the same socket is used for data, ACKs and alerts.
        sock.bind(("0.0.0.0", 0))
        local = sock.getsockname()
        logger.log("STARTED", peer=f"{server_addr[0]}:{server_addr[1]}",
                   detail=f"local={local[0]}:{local[1]} count={args.count} session_id={session_id}")
        try:
            for seq in range(1, args.count + 1):
                original_sent_ts = time.time()
                payload = f"{args.payload_prefix}-{seq}"
                acknowledged = False
                timeouts_for_seq = 0

                for attempt in range(1, args.max_retries + 2):
                    message = {
                        "type": "DATA",
                        "session_id": session_id,
                        "seq": seq,
                        "sent_ts": original_sent_ts,
                        "attempt_sent_ts": time.time(),
                        "attempt": attempt,
                        "payload": payload,
                    }
                    event = "SENT" if attempt == 1 else "RETRANSMIT"
                    logger.log(event, seq=seq, attempt=attempt,
                               peer=f"{server_addr[0]}:{server_addr[1]}",
                               detail=f"payload_bytes={len(payload.encode('utf-8'))}")
                    attempt_started = time.perf_counter()
                    sock.sendto(json.dumps(message).encode("utf-8"), server_addr)
                    rtt_ms = wait_for_ack(sock, server_addr, seq, session_id, attempt,
                                          args.timeout, attempt_started, logger)
                    if rtt_ms is not None:
                        logger.log("ACK_RECEIVED", seq=seq, attempt=attempt,
                                   peer=f"{server_addr[0]}:{server_addr[1]}",
                                   rtt_ms=rtt_ms, detail="matching ACK received")
                        acknowledged = True
                        successes += 1
                        break

                    timeouts_for_seq += 1
                    logger.log("TIMEOUT", seq=seq, attempt=attempt,
                               peer=f"{server_addr[0]}:{server_addr[1]}",
                               detail=f"no ACK within {args.timeout:.3f}s")
                    if (not args.disable_alert and not alert_sent
                            and timeouts_for_seq >= args.alert_after):
                        try:
                            send_sdn_alert(sock, alert_addr, logger, seq, timeouts_for_seq, session_id)
                            alert_sent = True
                        except OSError as exc:
                            logger.log("ALERT_ERROR", seq=seq, attempt=attempt,
                                       peer=f"{alert_addr[0]}:{alert_addr[1]}", detail=str(exc))

                if not acknowledged:
                    failures += 1
                    logger.log("FAILED", seq=seq, attempt=args.max_retries + 1,
                               peer=f"{server_addr[0]}:{server_addr[1]}",
                               detail="retry limit reached; packet was not acknowledged")
                if seq < args.count and args.interval:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.log("INTERRUPTED", detail="client stopped by user")
        finally:
            logger.log("SUMMARY", detail=f"total={args.count} acknowledged={successes} failed={failures}")
            logger.close()

    print(f"\nSummary: acknowledged={successes}/{args.count}, failed={failures}; log={args.log}")
    return 0 if failures == 0 else 2


def main() -> int:
    return run_client(parse_args())


if __name__ == "__main__":
    sys.exit(main())
