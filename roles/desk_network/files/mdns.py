#!/usr/bin/env python3
"""Bounded LAN mDNS lookup feeding NetAlertX's native AVAHISCAN name pipeline."""
from contextlib import closing
from pathlib import Path
import fcntl
import ipaddress
import json
import os
import re
import resource
import socket
import sqlite3
import struct
import sys
import time

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.rdataclass
import dns.rdatatype
import dns.reversename
import dns.wire

LAN = ipaddress.ip_network('192.168.1.0/24')
MAX_BATCH, MAX_PACKETS = 32, 256
NEGATIVE_TTL, POSITIVE_TTL = 600, 600
CACHE = Path('/tmp/desk-mdns-cache.json')
STATUS = Path('/tmp/desk-mdns-status.json')
DATABASE = Path('/data/db/app.db')
UNKNOWN = ('', '(unknown)', '(name not found)')


def lan_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
        return str(ip) if ip in LAN and ip not in (LAN.network_address, LAN.broadcast_address) else None
    except (ValueError, TypeError):
        return None


def hostname(name):
    """Only host labels; exclude service-instance names and result-file separators."""
    try:
        labels = [part.decode('ascii') for part in name.labels if part]
    except (AttributeError, UnicodeError):
        return None
    if len(labels) < 2 or labels[-1].lower() != 'local':
        return None
    value = '.'.join(labels)
    return value if len(value) <= 253 and all(re.fullmatch(r'[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?', x) for x in labels) else None


def candidates(database):
    """Read inventory only; names never create presence and MAC remains identity."""
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        rows = db.execute('''SELECT devMac,devLastIP FROM Devices
            WHERE devPresentLastScan=1 AND devIsArchived=0
            AND COALESCE(devName,'') IN ('','(unknown)','(name not found)')
            AND COALESCE(devNameSource,'') NOT IN ('USER','LOCKED')
            ORDER BY devMac LIMIT 512''').fetchall()
    result, conflicts = {}, set()
    for mac, ip in rows:
        if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac) or int(mac[:2],16)&1 or mac.lower() == '00:00:00:00:00:00' or not lan_ip(ip):
            continue
        if ip in result and result[ip] != mac.lower():
            conflicts.add(ip)
        result[ip] = mac.lower()
    return {ip: mac for ip, mac in result.items() if ip not in conflicts}


def read_cache(path, now):
    try:
        if path.stat().st_size > 128 * 1024:
            return {}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or len(raw) > 512:
            return {}
        result = {}
        for key, v in raw.items():
            if not isinstance(key,str) or not isinstance(v,dict) or set(v) != {'name','until'}:
                continue
            until = v['until']
            if isinstance(until,(int,float)) and now < until <= now + 600:
                name = v['name']
                if name == '' or isinstance(name,str) and hostname(dns.name.from_text(name)) == name:
                    result[key] = v
        return result
    except (OSError, ValueError, TypeError, dns.exception.DNSException):
        return {}


def save_json(path, value):
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, separators=(',', ':'))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def interface_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        address = socket.inet_ntoa(fcntl.ioctl(s, 0x8915, struct.pack('256s', b'wlan0'))[20:24])
    if not lan_ip(address):
        raise OSError('No LAN address on wlan0')
    return address


def decode_message(raw):
    # DNS RR class bit 15 means cache-flush in mDNS. Strip it only at class
    # fields using dnspython's bounded compression-aware wire parser, so the
    # ordinary DNS decoder can decode IN A/PTR RDATA correctly.
    parser = dns.wire.Parser(raw)
    _, _, nq, na, ns, nr = parser.get_struct('!6H')
    if nq + na + ns + nr > 256:
        raise dns.exception.FormError()
    cleaned = bytearray(raw)
    for _ in range(nq):
        parser.get_name()
        offset = parser.current
        _, cls = parser.get_struct('!HH')
        struct.pack_into('!H', cleaned, offset + 2, cls & 0x7fff)
    for _ in range(na + ns + nr):
        parser.get_name()
        offset = parser.current
        _, cls, _, size = parser.get_struct('!HHIH')
        struct.pack_into('!H', cleaned, offset + 2, cls & 0x7fff)
        parser.get_bytes(size)
    return dns.message.from_wire(bytes(cleaned))


def answers(raw, source, targets, requests, ptrs, forwards):
    """Accept only solicited, same-device PTR/A answers with positive TTLs."""
    if source[1] != 5353 or source[0] not in targets:
        return
    try:
        message = decode_message(raw)
        if not message.flags & dns.flags.QR or message.flags & dns.flags.TC or message.rcode() != 0 or message.opcode() != 0:
            return
        request = requests.get(message.id)
        if request is None or request[0] != source[0]:
            return
        for rrset in message.answer + message.additional:
            if rrset.ttl <= 0 or int(rrset.rdclass)&0x7fff != 1:
                continue
            ttl = min(rrset.ttl, POSITIVE_TTL)
            if rrset.rdtype == dns.rdatatype.PTR and rrset.name == dns.reversename.from_address(source[0]):
                for record in rrset:
                    name = hostname(record.target)
                    if name:
                        ptrs.setdefault(source[0], {})[name] = ttl
            elif rrset.rdtype == dns.rdatatype.A:
                name = hostname(rrset.name)
                if name and any(record.address == source[0] for record in rrset):
                    forwards[(source[0], name.lower())] = ttl
    except (dns.exception.DNSException, ValueError, IndexError):
        return


def resolve(targets, address):
    """One shared socket, two receive windows; runtime independent of host count."""
    if not targets:
        return {}, {'sent': 0, 'received': 0}
    requests, ptrs, forwards = {}, {}, {}
    sent = received = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((address, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 255)
        def send(ip, name, kind):
            nonlocal sent
            query = dns.message.make_query(name, kind, use_edns=False)
            query.flags = 0
            while query.id in requests:
                query.id = (query.id + 1) % 65536
            requests[query.id] = (ip, name)
            for destination in ('224.0.0.251', ip):
                try:
                    s.sendto(query.to_wire(), (destination, 5353))
                    sent += 1
                except OSError:
                    pass
        def receive(seconds):
            nonlocal received
            end = time.monotonic() + seconds
            while received < MAX_PACKETS:
                left = end - time.monotonic()
                if left <= 0:
                    return
                s.settimeout(left)
                try:
                    raw, source = s.recvfrom(9000)
                except socket.timeout:
                    return
                received += 1
                answers(raw, source, targets, requests, ptrs, forwards)
        for ip in targets:
            send(ip, dns.reversename.from_address(ip), dns.rdatatype.PTR)
        receive(3)
        for ip, names in ptrs.items():
            for name in sorted(names)[:2]:
                send(ip, dns.name.from_text(name), dns.rdatatype.A)
        if ptrs:
            receive(2)
    found = {}
    for ip, names in ptrs.items():
        confirmed = [(name, min(ttl, forwards[(ip, name.lower())])) for name, ttl in names.items() if (ip, name.lower()) in forwards]
        # Ambiguous host labels are left unnamed rather than guessed.
        if len(confirmed) == 1:
            found[ip] = confirmed[0]
    return found, {'sent': sent, 'received': received}


def run(database=DATABASE, cache_path=CACHE, lookup=resolve):
    now = time.time()
    targets = candidates(database)
    cache = read_cache(cache_path, now)
    cache = {mac + '/' + ip: cache[mac + '/' + ip] for ip, mac in targets.items() if mac + '/' + ip in cache}
    pending = {ip:mac for ip,mac in targets.items() if mac + '/' + ip not in cache}
    batch = dict(list(pending.items())[:MAX_BATCH])
    found, stats = lookup(batch, interface_address()) if batch else ({}, {'sent':0,'received':0})
    # Recheck identity/presence/manual-name changes after waiting for packets.
    fresh = candidates(database)
    for ip, mac in batch.items():
        name, ttl = found.get(ip, ('', NEGATIVE_TTL))
        if fresh.get(ip) == mac:
            cache[mac + '/' + ip] = {'name':name, 'until':now + min(ttl,600)}
    cache = {mac + '/' + ip: cache[mac + '/' + ip] for ip,mac in fresh.items() if mac + '/' + ip in cache and cache[mac + '/' + ip]['until'] > time.time()}
    save_json(cache_path, cache)
    result = [(mac, ip, cache[mac + '/' + ip]['name']) for ip,mac in fresh.items() if mac + '/' + ip in cache and cache[mac + '/' + ip]['name']]
    return result, dict(stats, eligible=len(targets), queried=len(batch), resolved=len(result))


def main():
    sys.path[:0] = ['/app/server', '/app/server/plugins']
    from plugin_helper import Plugin_Objects
    from const import logPath
    result = Plugin_Objects(str(Path(logPath) / 'plugins/last_result.AVAHISCAN.log'))
    start, cpu = time.monotonic(), time.process_time()
    with open('/tmp/desk-mdns.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Always clear old results, including when fresh input is unavailable.
        result.write_result_file()
        try:
            names, stats = run()
            for mac, ip, name in names:
                result.add_object(primaryId=mac, secondaryId=ip, watched2=name, foreignKey=mac)
            result.write_result_file()
            stats['state'] = 'ready'
        except (OSError, sqlite3.Error, ValueError) as e:
            stats = {'state':'unavailable', 'error':type(e).__name__}
        stats.update(wall_seconds=round(time.monotonic()-start,3), cpu_seconds=round(time.process_time()-cpu,4), peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        save_json(STATUS, stats)
        print('Desk mDNS ' + json.dumps(stats, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
