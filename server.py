#!/usr/bin/env python3
"""UDP server with ACKs, duplicate handling, CSV logs, and deterministic loss simulation."""

import argparse
import csv
import json
import socket
import time
from pathlib import Path
from typing import Any, Dict, Optional

CSV_FIELDS = ["time", "role", "session_id", "event", "seq", "attempt", "peer", "rtt_ms", "detail"]


class EventLogger:
    def __init__(self, path: str, role: str = "server") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=CSV_FIELDS)
        self.writer.writeheader()
        self.file.flush()
        self.role = role

    def log(self, event: str, seq: Optional[int] = None, attempt: Optional[int] = None,
            peer: str = "", rtt_ms: Optional[float] = None, detail: str = "",
            session_id: str = "") -> None:
        row: Dict[str, Any] = {
            "time": f"{time.time():.6f}",
            "role": self.role,
            "session_id": session_id,
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
    parser = argparse.ArgumentParser(description="Reliable UDP server with ACKs and optional simulated loss")
    parser.add_argument("--host", default="127.0.0.1", help="Bind IP; use 0.0.0.0 on Mininet h2")
    parser.add_argument("--port", type=int, default=5005, help="UDP server port (default: 5005)")
    parser.add_argument("--drop-every", type=int, default=0,
                        help="Simulate losing the first arrival of each Nth sequence (0 disables; e.g. 4)")
    parser.add_argument("--log", default="server_events.csv", help="CSV output path")
    args = parser.parse_args()
    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")
    if args.drop_every < 0:
        parser.error("--drop-every cannot be negative")
    return args


def run_server(args: argparse.Namespace) -> int:
    logger = EventLogger(args.log, role="server")
    # Keep sets in memory for this demo run: only unique accepted sequence IDs count as delivered.
    accepted_sequences = set()  # keys are (session_id, sequence_number)
    simulated_drops = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((args.host, args.port))
        sock.settimeout(0.5)
        bound = sock.getsockname()
        logger.log("STARTED", peer=f"{bound[0]}:{bound[1]}",
                   detail=f"drop_every={args.drop_every}")
        print("Server is ready. Press Ctrl+C to stop.", flush=True)

        while True:
            try:
                raw, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            peer = f"{addr[0]}:{addr[1]}"
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.log("BAD_PACKET", peer=peer, detail="ignored malformed JSON")
                continue

            if message.get("type") != "DATA":
                logger.log("UNSUPPORTED_MESSAGE", peer=peer,
                           detail=f"ignored type={message.get('type')}")
                continue
            seq = message.get("seq")
            session_id = message.get("session_id")
            attempt = message.get("attempt", 1)
            if not isinstance(seq, int) or seq < 1:
                logger.log("BAD_PACKET", peer=peer, attempt=attempt, detail="sequence number must be a positive integer")
                continue
            if not isinstance(session_id, str) or not session_id:
                logger.log("BAD_PACKET", seq=seq, peer=peer, attempt=attempt, detail="missing session_id")
                continue
            key = (session_id, seq)
            try:
                attempt = int(attempt)
            except (TypeError, ValueError):
                attempt = 1

            logger.log("DATAGRAM_RECEIVED", seq=seq, attempt=attempt, peer=peer,
                       detail=f"payload={str(message.get('payload', ''))[:80]}", session_id=session_id)

            # Simulate a lost data packet by intentionally not accepting or ACKing
            # the first arrival of every Nth sequence number. Its retransmission
            # will be accepted, demonstrating recovery without Mininet.
            if (args.drop_every > 0 and seq % args.drop_every == 0
                    and key not in simulated_drops and key not in accepted_sequences):
                simulated_drops.add(key)
                logger.log("SIMULATED_DROP", seq=seq, attempt=attempt, peer=peer,
                           detail="first arrival intentionally discarded; no ACK sent", session_id=session_id)
                continue

            if key in accepted_sequences:
                logger.log("DUPLICATE", seq=seq, attempt=attempt, peer=peer,
                           detail="already delivered in this session; ACK resent without processing payload again",
                           session_id=session_id)
            else:
                accepted_sequences.add(key)
                logger.log("RECEIVED", seq=seq, attempt=attempt, peer=peer,
                           detail=f"accepted payload={str(message.get('payload', ''))[:80]}",
                           session_id=session_id)

            ack = {
                "type": "ACK",
                "session_id": session_id,
                "seq": seq,
                "attempt": attempt,
                "server_ts": time.time(),
            }
            sock.sendto(json.dumps(ack).encode("utf-8"), addr)
            logger.log("ACK_SENT", seq=seq, attempt=attempt, peer=peer,
                       detail="ACK transmitted", session_id=session_id)
    except KeyboardInterrupt:
        logger.log("STOPPED", detail="server stopped by user")
    except OSError as exc:
        logger.log("ERROR", detail=str(exc))
        raise
    finally:
        sock.close()
        logger.close()
    return 0


def main() -> int:
    return run_server(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
