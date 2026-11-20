# UDP Client-Server + Recovery (Person 2)

This module implements the application-level part of the Adaptive Communication Network with Failure Recovery assignment.
It is designed to run on `localhost` first, without Mininet or an SDN controller, and then integrate with Lalit's SDN topology.

## Requirements

- Python 3.10+ recommended
- VS Code or another editor (optional)
- `pytest` for automated tests
- No third-party runtime libraries are required; the client and server use Python standard-library modules only.

Install pytest:

```bash
python -m pip install pytest
```

## Files

- `client.py` - sends numbered UDP data messages, waits for acknowledgements, retries after timeouts, writes CSV events, and sends an SDN alert after repeated timeouts.
- `server.py` - receives messages, accepts unique sequence numbers, acknowledges duplicates, and can simulate loss by dropping the first arrival for every Nth sequence number.
- `test_udp.py` - localhost integration tests for recovery and alert generation.

## 1. Localhost demo (no Mininet required)

Open two terminals in this folder.

Terminal A - start server:

```bash
python server.py --host 127.0.0.1 --port 5005 --drop-every 4 --log server_events.csv
```

Terminal B - run client:

```bash
python client.py --host 127.0.0.1 --port 5005 --count 20 --timeout 0.5 --max-retries 5 --interval 0.1 --log client_events.csv
```

Expected behavior:

- The server intentionally ignores the first arrival for sequence numbers 4, 8, 12, 16 and 20, and does not ACK those first arrivals.
- The client records a timeout, retransmits, receives the ACK, and continues.
- The server accepts each sequence only once and resends ACKs for duplicates.
- Both programs write CSV logs using the same column names.

`--drop-every` simulates loss at the application layer. It is not a claim that a real packet was dropped by the operating system or network hardware. The actual SDN link-failure experiment will be performed after integration with Mininet.

## 2. Run automated tests

From this folder:

```bash
python -m pytest -q
```

The tests start a temporary localhost server, simulate packet loss, verify that all six packets are eventually acknowledged, and verify that an alert is logged after repeated timeouts when no server is listening.

## 3. Integrate with Lalit's Mininet topology

Lalit's topology uses `h1=10.0.0.1`, `h2=10.0.0.2`, with UDP traffic from h1 to h2. The application server should run on h2 and bind to all interfaces so h1 can reach it.

Inside the Mininet CLI, start the server on h2:

```text
h2 python3 server.py --host 0.0.0.0 --port 5005 --drop-every 0 --log /tmp/server_events.csv &
```

Start the client on h1:

```text
h1 python3 client.py --host 10.0.0.2 --port 5005 --count 100 --timeout 0.5 --max-retries 8 --interval 0.1 --alert-host 10.0.0.2 --alert-port 9999 --log /tmp/client_events.csv
```

The controller code installs a high-priority OpenFlow rule that sends UDP packets whose destination port is `9999` to the SDN controller. After `--alert-after` timeouts for one sequence, the client sends a small alert datagram to `10.0.0.2:9999`. The controller uses this packet-in as a request to switch to the backup path. The alert is a separate control signal; normal application data and ACKs use port `5005`.

For a normal test with the server and client running on separate Mininet hosts, set `--drop-every 0` so the application's synthetic loss is disabled. Use link-down experiments to test the real SDN failover behavior.

The client sends an alert only once per run by default. This avoids flooding the controller if the server remains unreachable.

## 4. CSV schema

Both logs use this schema:

```text
time,role,session_id,event,seq,attempt,peer,rtt_ms,detail
```

- `time`: Unix epoch timestamp in seconds, compatible with the controller's event-log timestamps.
- `role`: `client` or `server`.
- `session_id`: unique identifier for one client run, so repeated runs can safely restart sequence numbers at 1.
- `event`: event name such as `SENT`, `RETRANSMIT`, `TIMEOUT`, `ACK_RECEIVED`, `SIMULATED_DROP`, `RECEIVED`, `ACK_SENT`, or `ALERT_SENT`.
- `seq`: application packet sequence number, if applicable.
- `attempt`: send or retry attempt number, if applicable.
- `peer`: remote IP and UDP port.
- `rtt_ms`: client-measured round-trip time for a matching ACK; blank otherwise.
- `detail`: event context.

The controller's existing `controller_events.csv` keeps its own `time,event,detail,path` schema. Joshua can load both formats independently or normalize them in the analysis script.

## Notes / limits

- UDP itself does not guarantee delivery. This application implements a small stop-and-wait ACK/retry protocol for the assignment.
- The client waits for an ACK for each sequence before moving to the next sequence, which makes the behavior easy to measure but limits throughput.
- For a full network test, use a timeout long enough for controller detection and route updates, and record controller and application logs from the same machine clock.
- The alert mechanism is compatible with the port-9999 packet-in behavior documented by the current `controller.py`. It does not require the server to listen on port 9999.
