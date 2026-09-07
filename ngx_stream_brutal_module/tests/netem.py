#!/usr/bin/env python3
"""Optional isolated IPv4 loopback netem diagnostic, not WAN acceptance.
Requires a --network none container with NET_ADMIN and iproute2.
Only the nginx frontend ports are shaped; backend traffic is unshaped.
"""
import argparse
import concurrent.futures
import json
from pathlib import Path
import socket
import struct
import subprocess
import time
import uuid

from integration import Nginx, hello
from throughput import Source


def tc(*args):
    return subprocess.run(['tc', *args], check=True, capture_output=True, text=True).stdout


def profile(port, rtt, loss, capacity):
    # Unmatched traffic defaults to band 1. Shape download and ACK directions
    # separately; requested loss applies only to server-to-client traffic.
    tc('qdisc', 'add', 'dev', 'lo', 'root', 'handle', '1:', 'prio', 'bands', '3',
       'priomap', *(['0'] * 16))
    tc('qdisc', 'add', 'dev', 'lo', 'parent', '1:2', 'handle', '20:', 'netem',
       'limit', '20000', 'delay', f'{rtt / 2}ms', 'loss', f'{loss}%', 'rate', f'{capacity}mbit')
    tc('qdisc', 'add', 'dev', 'lo', 'parent', '1:3', 'handle', '30:', 'netem',
       'limit', '20000', 'delay', f'{rtt / 2}ms')
    for field, band in [('sport', '1:2'), ('dport', '1:3')]:
        tc('filter', 'add', 'dev', 'lo', 'parent', '1:', 'protocol', 'ip', 'prio', '1',
           'u32', 'match', 'ip', 'protocol', '6', '0xff',
           'match', 'ip', field, str(port), '0xffff', 'flowid', band)


def run(args, gain, loss):
    source = Source()
    n = Nginx(args, source, 'real', workers=2)
    name = f'netem-{uuid.uuid4().hex}.example.com'
    n.write(str(args.mbps), sni=name, gain=str(gain), log_level='warn')
    n.config.write_text(n.config.read_text().replace('proxy_timeout 5s;', 'proxy_timeout 300s;')
                        .replace('preread_timeout 2s;', 'preread_timeout 30s;'))
    try:
        with n:
            try:
                profile(n.port, args.rtt, loss, args.capacity)
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.connections) as pool:
                    # Common future start time gives handshakes a warmup window.
                    start = time.monotonic() + args.warmup
                    end = start + args.seconds
                    def receive(_):
                        with socket.create_connection(('127.0.0.1', n.port), timeout=15) as c:
                            c.sendall(hello(name))
                            c.settimeout(.5)
                            total = 0
                            while time.monotonic() < end:
                                try:
                                    data = c.recv(65536)
                                except socket.timeout:
                                    continue
                                assert data, 'download closed'
                                now = time.monotonic()
                                if start <= now < end:
                                    total += len(data)
                            info = c.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
                            return dict(bytes=total, client_rtt_ms=struct.unpack_from('=I', info, 68)[0] / 1000)
                    results = list(pool.map(receive, range(args.connections)))
                records = [r for r in n.records() if r['action'] == 'set-params']
                assert len(records) == args.connections and all(r['rc'] == 0 for r in records), records
                assert len({r['group'] for r in records}) == 1
                queues = json.loads(tc('-j', '-s', 'qdisc', 'show', 'dev', 'lo'))
                return dict(gain=gain, loss_percent=loss, target_mbps=args.mbps,
                            rtt_ms=args.rtt, capacity_mbps=args.capacity,
                            connections=args.connections, warmup=args.warmup, seconds=args.seconds,
                            delivered_mbps=round(sum(r['bytes'] for r in results) * 8 / args.seconds / 1e6, 3),
                            clients=results, qdiscs=queues)
            finally:
                subprocess.run(['tc', 'qdisc', 'del', 'dev', 'lo', 'root'], capture_output=True)
    finally:
        source.sock.close()


def main():
    p = argparse.ArgumentParser()
    for field in ('nginx', 'module', 'probe', 'output'):
        p.add_argument('--' + field, required=True)
    p.add_argument('--mbps', type=int, default=300)
    p.add_argument('--capacity', type=int, default=500)
    p.add_argument('--rtt', type=int, default=150)
    p.add_argument('--seconds', type=int, default=20)
    p.add_argument('--warmup', type=int, default=10)
    p.add_argument('--connections', type=int, default=4)
    p.add_argument('--gains', type=int, nargs='+', default=[15, 20])
    p.add_argument('--losses', type=float, nargs='+', default=[0, .1])
    args = p.parse_args()
    if (not 1 <= args.mbps <= 1000000 or min(args.capacity, args.rtt, args.seconds,
        args.warmup, args.connections) < 1 or any(not 5 <= g <= 80 for g in args.gains)
        or any(not 0 <= loss <= 100 for loss in args.losses)):
        p.error('invalid bandwidth, timing, connection count, gain or loss')
    # Refuse host networking and containers with an external interface.
    links = json.loads(subprocess.check_output(['ip', '-j', 'link', 'show'], text=True))
    isolated = all(link['ifname'] == 'lo' or (link.get('link_type') == 'sit'
                   and link['ifname'] == 'sit0' and 'UP' not in link['flags']) for link in links)
    if not Path('/.dockerenv').exists() or not isolated:
        p.error('requires a disposable Docker --network none container')
    existing = json.loads(tc('-j', 'qdisc', 'show', 'dev', 'lo'))
    if any(q['kind'] != 'noqueue' for q in existing):
        p.error('refusing to replace an existing loopback qdisc')
    results = []
    for loss in args.losses:
        for gain in args.gains:
            result = run(args, gain, loss)
            results.append(result)
            Path(args.output).write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps({k: v for k, v in result.items() if k not in ('clients', 'qdiscs')}), flush=True)


if __name__ == '__main__':
    main()
