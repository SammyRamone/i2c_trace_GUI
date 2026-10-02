#!/usr/bin/env python3
"""i2c_tracer - parse a kernel i2c ftrace log and visualize it in a web GUI.
Usage: python3 i2c_tracer.py <i2c-log> [-p PORT] [--no-browser]"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EVENT_RE = re.compile(
    r"^\s*(?P<task>[\w.\-]+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\]\s+[-.\*]+\s+"
    r"(?P<sec>\d+)\.(?P<us>\d{6}):\s+"
    r"(?P<kind>i2c_write|i2c_read|i2c_result):\s+"
    r"(?P<bus>i2c-?\d+)"
)


def _parse_segments(rest, kind):
    """Parse 'a=008 f=0000 l=2 [80-80]' / 'a=008 f=0001 l=1'."""
    m = re.search(r"a=(\S+)\s+f=(\S+)\s+l=(\d+)", rest)
    if not m:
        return None
    seg = {
        "op": kind,
        "addr": m.group(1),
        "flags": m.group(2),
        "len": int(m.group(3)),
        "data": [],
    }
    m = re.search(r"\[([0-9a-fA-F\s\-]*)\]", rest)
    if m:
        seg["data"] = [b for b in m.group(1).split("-") if b != ""]
    return seg


def parse_log(text):
    first_ts = None
    pending = None
    last = None
    txns = []
    for line in text.splitlines():
        m = EVENT_RE.match(line)
        if not m:
            continue
        ts = int(m.group("sec")) + int(m.group("us")) / 1_000_000.0
        kind = m["kind"]
        entry = {
            "ts": ts,
            "kind": kind,
            "task": m["task"],
            "pid": m["pid"],
            "cpu": m["cpu"],
            "bus": m["bus"],
        }
        if first_ts is None:
            first_ts = ts
        rest = line[m.end():]
        if kind in ("i2c_write", "i2c_read"):
            seg = _parse_segments(rest, kind)
            if seg is None:
                last = entry
                continue
            seg.update(entry)
            if pending is None:
                pending = {"segs": [], "first_ts": ts, "last_cpu": m["cpu"]}
            else:
                if seg["bus"] != pending["segs"][0]["bus"]:
                    _emit(pending, txns, first_ts)
                    pending = {"segs": [], "first_ts": ts, "last_cpu": m["cpu"]}
                if seg["op"] == "i2c_write" and pending["segs"] and pending["segs"][-1]["op"] == "i2c_read":
                    _emit(pending, txns, first_ts)
                    pending = {"segs": [], "first_ts": ts, "last_cpu": m["cpu"]}
            pending["segs"].append(seg)
            pending["last_cpu"] = m["cpu"]
            last = seg
        elif kind == "i2c_result":
            m2 = re.search(r"n=(\d+)\s+ret=(-?\d+)", rest)
            if pending is not None:
                entry["n"] = int(m2.group(1)) if m2 else None
                entry["ret"] = int(m2.group(2)) if m2 else None
                pending["result"] = entry
                _emit(pending, txns, first_ts)
                pending = None
            else:
                if m2:
                    entry["n"] = int(m2.group(1))
                    entry["ret"] = int(m2.group(2))
            last = entry
    if pending is not None:
        _emit(pending, txns, first_ts)
    return txns


def _emit(pending, txns, first_ts):
    segs = pending["segs"]
    if not segs:
        return
    res = pending.get("result")
    t = {
        "id": len(txns),
        "bus": segs[0]["bus"],
        "addr": segs[0]["addr"],
        "flags": segs[0]["flags"],
        "segs": segs,
        "t0": segs[0]["ts"] - first_ts,
        "t1": (res["ts"] if res else segs[-1]["ts"]) - first_ts,
        "ret": res["ret"] if res else None,
        "n": res["n"] if res else None,
        "task": segs[0]["task"],
        "pid": segs[0]["pid"],
        "cpu": pending["last_cpu"],
        "result_ts": res["ts"] if res else None,
    }
    t["ok"] = t["ret"] is not None and t["ret"] > 0
    t["pending"] = res is None
    txns.append(t)


def load(path):
    text = Path(path).read_text(errors="replace")
    txns = parse_log(text)
    buses = []
    for t in txns:
        if t["bus"] not in buses:
            buses.append(t["bus"])
    summary = {
        "total": len(txns),
        "ok": sum(1 for t in txns if t["ok"]),
        "err": sum(1 for t in txns if (not t["ok"]) and not t["pending"]),
        "pending": sum(1 for t in txns if t["pending"]),
        "buses": buses,
        "addresses": sorted({int(t["addr"], 16) for t in txns}, key=int),
        "tasks": sorted({t["task"] for t in txns}),
        "span_s": txns[-1]["t1"] - txns[0]["t0"] if txns else 0,
    }
    return {"summary": summary, "txns": txns}


class Handler(BaseHTTPRequestHandler):
    html = b""
    trace_json = b""

    def _send_body(self, code, body, ctype):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/index.html"):
            self._send_body(200, self.html, "text/html; charset=utf-8")
        elif p == "/trace":
            self._send_body(200, self.trace_json, "application/json")
        elif p == "/favicon.ico":
            self._send_body(204, b"", None)
        else:
            self._send_body(404, b"not found\n", "text/plain")

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="Visualize an i2c ftrace log in a web GUI")
    ap.add_argument("log", help="path to the i2c ftrace log file")
    ap.add_argument("-p", "--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    log_path = Path(args.log).resolve()
    if not log_path.is_file():
        raise SystemExit(f"log file not found: {log_path}")

    gui = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
    data = load(str(log_path))
    data["source"] = log_path.name

    Handler.html = gui.encode("utf-8")
    Handler.trace_json = json.dumps(data).encode("utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}/"
    print(f"Parsed {data['summary']['total']} transactions from {log_path.name}")
    print(f"GUI: {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
