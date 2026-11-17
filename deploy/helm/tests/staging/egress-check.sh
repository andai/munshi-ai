#!/usr/bin/env bash
# TEST-ONLY egress capture for the staging VM (a Lima VM running single-node k3s). Run on the Mac;
# every step runs inside the VM as root through `limactl shell`.
#
#   egress-check.sh start    record the uplink's gateway and resolvers, turn on the CoreDNS query
#                            log (coredns-custom log.override, when k3s is up), start tcpdump
#   egress-check.sh mark X   append a timestamped window marker (e.g. "pull-window-open")
#   egress-check.sh stop     stop tcpdump, save the CoreDNS log
#   egress-check.sh report   destinations by class, names from captured DNS answers, and the count
#                            of lookups of the SeaweedFS telemetry host; exits 1 when a destination
#                            is outside the allowed classes or the telemetry host was looked up.
#                            EGRESS_SINCE=<UTC ISO time> counts only flows from that time on (DNS
#                            answers from the whole capture still name the addresses)
#
# Captured: outbound TCP SYNs, all UDP except the VM's own loopback, ICMP, and every DNS packet,
# headers only (snaplen 512), never payloads of established connections. Lima's SSH is excluded.
# The report prints classes, names, ports and counts, never the VM's or the Mac's addresses
# (EGRESS_SHOW_IPS=1 prints addresses, for local debugging only).
#
# Allowed classes: the Mac host (the VM's default gateway), DNS to the VM's resolvers, and names
# matching EGRESS_ALLOW (a file of extended regexes, one per line; default: allow-hosts.txt next to
# this script) — the registries and package mirrors of an approved download window.
set -euo pipefail

VM="${MUNSHI_VM:-munshi-staging}"
DIR=/var/lib/munshi-egress
HERE="$(cd "$(dirname "$0")" && pwd)"
ALLOW_FILE="${EGRESS_ALLOW:-$HERE/allow-hosts.txt}"
TELEMETRY_HOST="telemetry.seaweedfs.com"

vm() { limactl shell --workdir / "$VM" sudo "$@"; }

start() {
  vm bash -euo pipefail -c '
    dir='"$DIR"'
    mkdir -p "$dir" && chmod 700 "$dir"
    if [ -f "$dir/tcpdump.pid" ] && kill -0 "$(cat "$dir/tcpdump.pid")" 2>/dev/null; then
      echo "capture already running"; exit 0
    fi
    dev=$(ip -o -4 route show default | awk "{print \$5; exit}")
    ip -o -4 route show default | awk "{print \$3; exit}" > "$dir/gateway"
    { resolvectl dns 2>/dev/null | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}" || true
      grep -E "^nameserver" /run/systemd/resolve/resolv.conf 2>/dev/null | awk "{print \$2}" || true; } | sort -u > "$dir/resolvers"
    ip -o -4 addr show dev "$dev" | awk "{print \$4}" | cut -d/ -f1 > "$dir/self"
    echo "$dev" > "$dir/uplink"
    filter="not port 22 and ((tcp[tcpflags] & (tcp-syn|tcp-ack) = tcp-syn) or udp or icmp or ip6)"
    nohup tcpdump -i "$dev" -n -U -s 512 -w "$dir/uplink-$(date -u +%Y%m%dT%H%M%SZ).pcap" "$filter" > "$dir/tcpdump.log" 2>&1 &
    echo $! > "$dir/tcpdump.pid"
    [ -s "$dir/started" ] || date -u +%FT%TZ > "$dir/started"
    echo "$(date -u +%FT%TZ) start" >> "$dir/marks"
    if command -v kubectl >/dev/null && kubectl get ns kube-system >/dev/null 2>&1; then
      kubectl -n kube-system create configmap coredns-custom --from-literal=log.override=log \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
      kubectl -n kube-system rollout restart deploy/coredns >/dev/null
      echo "coredns query log on"
    fi
    sleep 1; kill -0 "$(cat "$dir/tcpdump.pid")" && echo "capture started on the uplink"
  '
}

mark() {
  vm bash -c "echo \"\$(date -u +%FT%TZ) ${1:?window label}\" >> $DIR/marks"
}

save_coredns() {
  vm bash -c "if command -v kubectl >/dev/null && kubectl -n kube-system get deploy coredns >/dev/null 2>&1; then
      kubectl -n kube-system logs deploy/coredns --since-time=\$(cat $DIR/started) >> $DIR/coredns.log 2>/dev/null || true
      sort -u -o $DIR/coredns.log $DIR/coredns.log; fi"
}

stop() {
  save_coredns
  vm bash -c "if [ -f $DIR/tcpdump.pid ]; then kill \$(cat $DIR/tcpdump.pid) 2>/dev/null || true; rm -f $DIR/tcpdump.pid; fi
              echo \"\$(date -u +%FT%TZ) stop\" >> $DIR/marks; echo stopped"
}

report() {
  save_coredns
  local allow=""
  [ -f "$ALLOW_FILE" ] && allow="$(grep -vE '^\s*(#|$)' "$ALLOW_FILE" | paste -sd'|' -)"
  vm env ALLOW="$allow" SHOW_IPS="${EGRESS_SHOW_IPS:-0}" TELEMETRY="$TELEMETRY_HOST" DIR="$DIR" \
    SINCE="${EGRESS_SINCE:-}" python3 -I -c '
import collections, os, re, subprocess, sys
d = os.environ["DIR"]
read = lambda f: [l.strip() for l in open(os.path.join(d, f)) if l.strip()] if os.path.exists(os.path.join(d, f)) else []
gw, resolvers, me = set(read("gateway")), set(read("resolvers")), set(read("self"))
allow = re.compile(os.environ["ALLOW"]) if os.environ["ALLOW"] else None
show = os.environ["SHOW_IPS"] == "1"
tele = os.environ["TELEMETRY"]
import glob, ipaddress, struct, datetime
since = os.environ["SINCE"]
since_ts = datetime.datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp() if since else 0
def packets():
    for f in sorted(glob.glob(os.path.join(d, "uplink*.pcap"))):
        data = open(f, "rb").read()
        if len(data) < 24:
            continue
        e = "<" if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
        link = struct.unpack(e + "I", data[20:24])[0]
        off = 24
        while off + 16 <= len(data):
            ts = struct.unpack(e + "I", data[off:off + 4])[0]
            incl = struct.unpack(e + "I", data[off + 8:off + 12])[0]
            off += 16
            yield ts, link, data[off:off + incl]
            off += incl
def name_at(b, o, depth=0):
    labels = []
    while o < len(b) and depth < 16:
        n = b[o]
        if n == 0:
            return ".".join(labels), o + 1
        if n & 0xC0 == 0xC0:
            rest, _ = name_at(b, ((n & 0x3F) << 8) | b[o + 1], depth + 1)
            return ".".join(labels + ([rest] if rest else [])), o + 2
        labels.append(b[o + 1:o + 1 + n].decode("ascii", "replace"))
        o += 1 + n
    return ".".join(labels), o
def dns(b, flows_names):
    qd, an = struct.unpack(">HH", b[4:8])
    o, q = 12, None
    for _ in range(qd):
        q, o = name_at(b, o)
        o += 4
    for _ in range(an):
        _, o = name_at(b, o)
        typ, _, _, ln = struct.unpack(">HHIH", b[o:o + 10])
        o += 10
        if q and typ in (1, 28) and ln in (4, 16):
            flows_names.setdefault(str(ipaddress.ip_address(b[o:o + ln])), set()).add(q.lower())
        o += ln
    return q
names, flows, queries = {}, collections.Counter(), collections.Counter()
for ts, link, p in packets():
    try:
        if link == 1:
            et, p = struct.unpack(">H", p[12:14])[0], p[14:]
            if et == 0x8100:
                et, p = struct.unpack(">H", p[2:4])[0], p[4:]
        elif link == 113:
            et, p = struct.unpack(">H", p[14:16])[0], p[16:]
        else:
            continue
        if et == 0x0800:
            ihl, proto = (p[0] & 15) * 4, p[9]
            src, dst, l4 = str(ipaddress.ip_address(p[12:16])), str(ipaddress.ip_address(p[16:20])), p[ihl:]
        elif et == 0x86DD:
            proto = p[6]
            src, dst, l4 = str(ipaddress.ip_address(p[8:24])), str(ipaddress.ip_address(p[24:40])), p[40:]
        else:
            continue
        if proto == 1 or proto == 58:
            if src in me:
                flows[("icmp", dst, "-")] += 1
            continue
        if proto not in (6, 17):
            continue
        sport, dport = struct.unpack(">HH", l4[:4])
        if proto == 17 and sport == 53:
            dns(l4[8:], names)
            continue
        if ts < since_ts:
            continue
        if src not in me and not (et == 0x86DD and not ipaddress.ip_address(src).is_link_local):
            continue
        if proto == 6 and (l4[13] & 0x12) != 0x02:
            continue
        if proto == 17 and dport == 53:
            qn = dns(l4[8:], {})
            if qn:
                queries[qn.lower()] += 1
        flows[("tcp" if proto == 6 else "udp", dst, str(dport))] += 1
    except (IndexError, struct.error, ValueError):
        continue
classes = collections.defaultdict(collections.Counter)
bad = 0
for (proto, dst, dport), n in sorted(flows.items()):
    if dst in gw:
        cls, label = "mac-host", f"{proto}/{dport}" + (" (dns)" if dport == "53" else "")
    elif ipaddress.ip_address(dst).is_multicast or dst == "255.255.255.255":
        cls, label = "local-multicast", f"{proto}/{dport}"
    elif dport == "53" and dst in resolvers:
        cls, label = "dns-resolver", f"{proto}/53"
    else:
        nm = ",".join(sorted(names.get(dst, []))) or "(no name seen)"
        if not show and nm == "(no name seen)":
            nm = "(no name seen; EGRESS_SHOW_IPS=1 for the address)"
        ok = bool(allow) and any(allow.search(x) for x in names.get(dst, []))
        cls = "allowed-window" if ok else "DISALLOWED"
        bad += 0 if ok else n
        label = f"{nm} {proto}/{dport}" + (f" [{dst}]" if show else "")
    classes[cls][label] += n
started = read("started")
print("capture since " + (started[0] if started else "?") + ("; flows counted from " + since if since else "") + "; windows: " + "; ".join(read("marks")))
for cls in ("mac-host", "dns-resolver", "local-multicast", "allowed-window", "DISALLOWED"):
    total = sum(classes[cls].values())
    print(f"{cls}: {total} flow start(s)")
    for label, n in classes[cls].most_common():
        print(f"  {n:6d}  {label}")
print(f"DNS names asked upstream: {len(queries)} distinct")
for q, n in queries.most_common():
    print(f"  {n:6d}  {q}")
core = open(os.path.join(d, "coredns.log")).read() if os.path.exists(os.path.join(d, "coredns.log")) else ""
tq = queries.get(tele, 0) + core.count(tele)
print(f"lookups of {tele}: {tq} (uplink {queries.get(tele, 0)}, coredns {core.count(tele)}; coredns log lines: {core.count(chr(10))})")
sys.exit(1 if bad or tq else 0)
'
}

case "${1:-}" in
  start) start ;;
  mark) mark "${2:-}" ;;
  stop) stop ;;
  report) report ;;
  *) echo "usage: $0 start|mark <label>|stop|report" >&2; exit 2 ;;
esac
