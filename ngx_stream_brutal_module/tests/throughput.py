#!/usr/bin/env python3
"""Real-kernel aggregate download test. Runs only on isolated loopback ports.
Three independent SNI groups can run concurrently; connections within each
trial must share a single target rate. Never alters qdiscs, routes or defaults.
"""
import argparse
import concurrent.futures
import json
from pathlib import Path
import socket
import threading
import time

from integration import Nginx, hello


class Source:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.send, args=(conn,), daemon=True).start()

    def send(self, conn):
        with conn:
            conn.settimeout(120)
            try:
                if conn.recv(16384):
                    block = bytes(65536)
                    while True:
                        conn.sendall(block)
            except OSError:
                pass


def trial(args, count, repeat):
    source = Source()
    sni = f"bench-{count}-{repeat}.example.com"
    n = Nginx(args, source, "real", workers=2, rate=str(args.rate))
    n.write(str(args.rate), sni=sni)
    n.config.write_text(n.config.read_text().replace("proxy_timeout 5s;", "proxy_timeout 300s;"))
    try:
        with n:
            start = time.monotonic() + args.warmup
            end = start + args.seconds

            def receive(index):
                family = socket.AF_INET6 if index % 2 else socket.AF_INET
                with socket.socket(family) as c:
                    c.settimeout(5)
                    c.connect(("::1", n.port6) if index % 2 else ("127.0.0.1", n.port))
                    c.sendall(hello(sni))
                    c.settimeout(.5)
                    total = 0
                    while True:
                        if time.monotonic() >= end:
                            return total
                        try:
                            data = c.recv(65536)
                        except socket.timeout:
                            continue
                        now = time.monotonic()
                        if not data:
                            raise AssertionError("premature EOF")
                        if now >= end:
                            return total
                        if now >= start:
                            total += len(data)

            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                totals = list(pool.map(receive, range(count)))
            records = n.records()
            params = [r for r in records if r["action"] == "set-params"]
            assert len(params) == count and all(r["rc"] == 0 for r in params), records
            assert len({r["group"] for r in params}) == 1
            assert all(r["rate"] == round(args.rate * 1000000) for r in params)
            rate = sum(totals) / args.seconds / 1000000
            result = dict(connections=count, repeat=repeat, target_MBps=args.rate,
                          actual_MBps=round(rate, 6), seconds=args.seconds,
                          per_connection_MBps=[round(x / args.seconds / 1000000, 6) for x in totals],
                          warmup=args.warmup, group=params[0]["group"],
                          workers=sorted({r["pid"] for r in params}),
                          passed=abs(rate / args.rate - 1) <= .10)
            print(json.dumps(result), flush=True)
            return result
    finally:
        source.sock.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nginx", required=True)
    p.add_argument("--module", required=True)
    p.add_argument("--probe", required=True)
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--warmup", type=int, default=10)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--rate", type=float, default=1)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    for field in ("nginx", "module", "probe"):
        setattr(args, field, str(Path(getattr(args, field)).resolve()))
    results = []
    for count in (1, 4, 16):
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.repeats) as pool:
            results.extend(pool.map(lambda repeat: trial(args, count, repeat), range(args.repeats)))
    Path(args.output).write_text(json.dumps(results, indent=2) + "\n")
    assert all(r["passed"] for r in results), "aggregate throughput outside target +/-10%"


if __name__ == "__main__":
    main()
