#!/usr/bin/env python3
"""Optional 24-hour churn/long-connection/reload test in the test container.
Use --seconds for a shorter diagnostic run; it is not 24-hour acceptance.
"""
import argparse
import concurrent.futures
import json
from pathlib import Path
import signal
import socket
import time

from integration import Backend, Nginx, hello


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nginx", required=True)
    p.add_argument("--module", required=True)
    p.add_argument("--probe", required=True)
    p.add_argument("--seconds", type=int, default=86400)
    p.add_argument("--reload-every", type=int, default=300)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    for field in ("nginx", "module", "probe"):
        setattr(args, field, str(Path(getattr(args, field)).resolve()))
    backend = Backend()
    count = reloads = 0
    samples = []
    start = time.monotonic()
    try:
        n = Nginx(args, backend, "real", workers=2)
        n.config.write_text(n.config.read_text().replace(" debug;", " warn;"))
        with n:
            # Keep output bounded: warn logs; clear tracing on later generations.
            with socket.create_connection(("127.0.0.1", n.port), timeout=5) as held:
                data = hello()
                held.sendall(data)
                got = b""
                while len(got) < len(data):
                    part = held.recv(len(data) - len(got))
                    assert part
                    got += part
                assert got == data
                assert any(r["action"] == "set-params" and r["rc"] == 0 for r in n.records())
                # Subsequent generations need no probe; production module remains.
                n.write()
                n.config.write_text(n.config.read_text().replace(" debug;", " warn;")
                                    .replace("env BRUTAL_TEST_TRACE;", "env BRUTAL_TEST_TRACE=;"))
                n.proc.send_signal(signal.SIGHUP)
                last_reload = last_sample = start
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    while time.monotonic() - start < args.seconds:
                        list(pool.map(lambda i: n.exchange("target.example.com" if i % 2 else "other.example.com",
                                                         ipv6=bool(i % 3 == 0)), range(8)))
                        count += 8
                        held.sendall(b"ping")
                        received = b""
                        while len(received) < 4:
                            part = held.recv(4 - len(received))
                            assert part, "held connection closed"
                            received += part
                        assert received == b"ping"
                        assert n.proc.poll() is None
                        now = time.monotonic()
                        if now - last_reload >= args.reload_every:
                            n.proc.send_signal(signal.SIGHUP)
                            reloads += 1
                            last_reload = now
                        if now - last_sample >= 10:
                            children = Path(f"/proc/{n.proc.pid}/task/{n.proc.pid}/children").read_text().split()
                            resources = []
                            for pid in children:
                                try:
                                    status = Path(f"/proc/{pid}/status").read_text().splitlines()
                                    rss = next(line for line in status if line.startswith("VmRSS:"))
                                    try:
                                        fds = len(list(Path(f"/proc/{pid}/fd").iterdir()))
                                    except PermissionError:
                                        fds = None
                                    resources.append(dict(pid=int(pid), rss=rss, fds=fds))
                                except (FileNotFoundError, StopIteration):
                                    pass
                            row = dict(elapsed=round(now - start), connections=count, reloads=reloads, workers=resources)
                            samples.append(row)
                            print(json.dumps(row), flush=True)
                            last_sample = now
                        time.sleep(.2)
                logs = n.log.read_text()
                assert "[alert]" not in logs and "[emerg]" not in logs and "exited on signal" not in logs
                assert "brutal: step=" not in logs
        result = dict(passed=True, seconds=round(time.monotonic() - start), connections=count,
                      reloads=reloads, samples=samples, full_24h=args.seconds >= 86400)
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k: v for k, v in result.items() if k != "samples"}), flush=True)
    finally:
        backend.sock.close()


if __name__ == "__main__":
    main()
