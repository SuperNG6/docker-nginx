#!/usr/bin/env python3
"""Real-kernel group isolation, reload overlap and rollback side effects.
Only loopback sockets; no sysctls, routes, destination rules or qdiscs.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import time
import uuid

from integration import Backend, Nginx as BaseNginx, hello
from throughput import Source


class Nginx(BaseNginx):
    def write(self, *args, **kwargs):
        super().write(*args, **kwargs)
        # Pin worker UID to the observer process in this isolated test only.
        # An accepted socket gets the accepting process's UID, not necessarily
        # the listener creator's UID.
        if os.geteuid() == 0:
            self.config.write_text('user root;\n' + self.config.read_text())


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('condition timed out')


def group_id(sni):
    value = 14695981039346656037
    for byte in ('ngx_stream_brutal:v1:' + sni.lower()).encode():
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return value or 1


def params(sock):
    return struct.unpack('=QIQ', sock.getsockopt(socket.IPPROTO_TCP, 23301, 20))


@contextlib.contextmanager
def observer(group, rate=1000000, gain=20):
    # Socket UID/netns must match nginx's accepted sockets; this test pins
    # nginx workers to the observer user's UID, without changing production.
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        accepted, _ = listener.accept()
        with accepted:
            accepted.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, b'brutal')
            accepted.setsockopt(socket.IPPROTO_TCP, 23301, struct.pack('=QIQ', rate, gain, group))
            assert params(accepted) == (rate, gain, group)
            yield accepted


def reload(n):
    assert n.check().returncode == 0
    before = n.log.read_text().count('brutal: module=')
    stopping = n.log.read_text().count('gracefully shutting down')
    n.proc.send_signal(signal.SIGHUP)
    wait_for(lambda: n.log.read_text().count('brutal: module=') >= before + n.workers)
    wait_for(lambda: n.log.read_text().count('gracefully shutting down') >= stopping + n.workers)


def rollback_effect(args):
    name = f'rollback-{uuid.uuid4().hex}.example.com'
    group = group_id(name)
    backend = Backend()
    try:
        with observer(group) as held:
            n = Nginx(args, backend, 'real-verify-fail')
            n.write('16', sni=name, gain='15')
            with n:
                n.exchange(name)
                rows = n.wait_access(lambda rows: any(row[0] == name for row in rows))
                row = next(row for row in rows if row[0] == name)
                assert row[1] == 'failed' and row[8:] == [
                    'verify', 'original-restored', 'may-have-changed'], row
                assert params(held) == (2000000, 15, group), params(held)
                records = n.records()
                assert all(r['local'] in (n.port, n.port6) for r in records)
                assert sum(r['action'] == 'set-params' for r in records) == 1
                reads = [r for r in records if r['action'] == 'get-cc' and r['rc'] == 0]
                assert reads[0]['cc'] == reads[-1]['cc'] != 'brutal', reads
        return dict(test='rollback', passed=True, other_member_rate_Bps=2000000,
                    other_member_gain=15, socket_restored=True, group_restored=False)
    finally:
        backend.sock.close()


def reload_overlap(args):
    name = f'overlap-{uuid.uuid4().hex}.example.com'
    group = group_id(name)
    backend = Backend()
    try:
        with observer(group) as held:
            n = Nginx(args, backend, 'real', workers=2)
            n.write('8', sni=name)
            n.config.write_text(n.config.read_text().replace('preread_timeout 2s;', 'preread_timeout 30s;'))
            with n, socket.create_connection(('127.0.0.1', n.port), timeout=5) as pending:
                data = hello(name)
                pending.sendall(data[:3])
                peer = pending.getsockname()[1]
                # Wait until the old worker has accepted this particular client.
                wait_for(lambda: f'127.0.0.1:{peer} connected to' in n.log.read_text())
                n.write('16', sni=name, gain='15')
                reload(n)
                n.exchange(name)
                assert params(held) == (2000000, 15, group)
                pending.sendall(data[3:])
                received = b''
                while len(received) < len(data):
                    part = pending.recv(len(data) - len(received))
                    assert part, 'old preread connection closed on reload'
                    received += part
                assert received == data
                assert params(held) == (2000000, 15, group), params(held)
                assert not [r for r in n.records() if r['peer'] == peer], 'old socket touched'
                pending.close()
                rows = n.wait_access(lambda rows: any(row[1] == 'skipped-worker-exiting' for row in rows))
                skipped = next(row for row in rows if row[1] == 'skipped-worker-exiting')
                assert skipped[2:] == ['0', '-', '-', '-', '-', '-', 'none', 'not-modified', 'untouched'], skipped
                writes = [r for r in n.records() if r['action'] == 'set-params']
                assert [r['rate'] for r in writes] == [2000000], writes
                n.exchange(name)
                assert params(held) == (2000000, 15, group)
        return dict(test='reload_overlap', passed=True, rates_Bps=[2000000, 2000000, 2000000],
                    old_preread_connection_survived=True)
    finally:
        backend.sock.close()


def measure(clients, duration):
    totals = [0, 0]
    with selectors.DefaultSelector() as selector:
        for sock, index in clients:
            selector.register(sock, selectors.EVENT_READ, index)
        start = time.monotonic()
        deadline = start + duration
        while time.monotonic() < deadline:
            for key, _ in selector.select(min(.2, max(0, deadline - time.monotonic()))):
                if time.monotonic() >= deadline:
                    break
                data = key.fileobj.recv(65536)
                assert data, 'held download closed'
                totals[key.data] += len(data)
    elapsed = time.monotonic() - start
    return [value / elapsed for value in totals]


def isolation(args):
    suffix = uuid.uuid4().hex
    names = [f'a-{suffix}.example.com', f'b-{suffix}.example.com']
    source = Source()
    samples = []
    try:
        n = Nginx(args, source, 'real', workers=2)
        def configure(rate='8', enabled=True):
            n.write(rate, enabled=enabled, sni=names[0], gain='15' if rate == '16' else '20',
                    extra=f'brutal_sni {names[1]} download_mbps=16 gain=20;')
            n.config.write_text(n.config.read_text().replace('proxy_timeout 5s;', 'proxy_timeout 300s;'))
        configure()
        with n, contextlib.ExitStack() as stack:
            clients = []
            for index, name in enumerate(names):
                for ip, port in [('127.0.0.1', n.port), ('::1', n.port6)]:
                    c = stack.enter_context(socket.create_connection((ip, port), timeout=5))
                    c.sendall(hello(name))
                    clients.append((c, index))
            for stage, expected in [('initial', [1000000, 2000000]),
                                    ('change_a', [2000000, 2000000]),
                                    ('remove_a', [2000000, 2000000])]:
                if stage != 'initial':
                    configure('16', enabled=stage != 'remove_a')
                    reload(n)
                    before = len(n.records())
                    with socket.create_connection(('127.0.0.1', n.port), timeout=5) as trigger:
                        trigger.sendall(hello(names[0]))
                        assert trigger.recv(1)
                    writes = [r for r in n.records()[before:] if r['action'] == 'set-params']
                    assert bool(writes) == (stage == 'change_a'), writes
                measure(clients, args.warmup)
                actual = measure(clients, args.seconds)
                assert all(abs(a / e - 1) <= .1 for a, e in zip(actual, expected)), (stage, actual)
                samples.append(dict(stage=stage, expected_Bps=expected,
                                    actual_Bps=[round(v) for v in actual]))
            writes = [r for r in n.records() if r['action'] == 'set-params']
            assert len(writes) == 5, writes
            assert {r['group'] for r in writes} == {group_id(name) for name in names}
        return dict(test='multi_sni_isolation', passed=True, seconds=args.seconds,
                    warmup=args.warmup, samples=samples)
    finally:
        source.sock.close()


def main():
    p = argparse.ArgumentParser()
    for field in ('nginx', 'module', 'probe', 'output'):
        p.add_argument('--' + field, required=True)
    p.add_argument('--seconds', type=int, default=30)
    p.add_argument('--warmup', type=int, default=10)
    args = p.parse_args()
    if args.seconds < 1 or args.warmup < 1:
        p.error('measurement and warmup must be positive')
    results = []
    for test in (rollback_effect, reload_overlap, isolation):
        result = test(args)
        results.append(result)
        Path(args.output).write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
