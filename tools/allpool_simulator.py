#!/usr/bin/env python3
"""
Einfacher ALLPOOL-Simulator für Tests ohne echte Steuerung.

Legt ein virtuelles serielles Gerät (pty) an und beantwortet das ASCII-Protokoll
mit Prüfsumme, Bereichsprüfung und Fehlercodes. Der Pfad des virtuellen Ports
wird ausgegeben bzw. als Symlink angelegt (Standard: /tmp/allpool-sim).

  python3 tools/allpool_simulator.py --link /tmp/allpool-sim
  # in config.yaml:  serial.port: /tmp/allpool-sim
  # kill -USR1 <pid>  -> Simulator antwortet nicht mehr / wieder
"""
import argparse
import os
import pty
import signal
import random
import time
import tty

# id: [wert, min, max, step, writable]
REG = {
    110: ["10", 10, 10, 1, False], 120: ["413", 100, 9999, 1, False],
    1010: ["1", 0, 1, 1, False], 1012: ["4.2", 0, 16.3, 0.1, False],
    1020: ["0", 0, 1, 1, False], 1030: ["1", 0, 1, 1, False],
    1040: ["0", 0, 1, 1, False], 1050: ["0", 0, 1, 1, False],
    1100: ["0", 0, 3, 1, False], 1101: ["1", 0, 1, 1, False],
    1510: ["0", 0, 1, 1, True], 1520: ["0", 0, 1, 1, "r"], 1530: ["0", 0, 1, 1, "r"],
    1540: ["0", 0, 1, 1, "r"], 1550: ["0", 0, 1, 1, "r"],
    2010: ["24.3", -39.5, 150, 0.1, False], 2020: ["41.7", -39.5, 150, 0.1, False],
    2050: ["0110", 0, 1111, 1, False],
    2510: ["0", 0, 1, 1, False], 2512: ["h", 0, 0, 0, False],
    2520: ["0", 0, 1, 1, False], 2530: ["1", 0, 1, 1, False], 2532: ["h", 0, 0, 0, False],
    30010: ["1", 1, 2, 1, True], 30030: ["10.0", 5, 45, 0.5, True], 30031: ["15.0", 5, 45, 0.5, True],
    30035: ["02.01.00", None, None, None, True], 30036: ["02.01.00", None, None, None, True],
    33010: ["1", 1, 3, 1, True], 33011: ["2", 1, 3, 1, True], 39010: ["1", 1, 3, 1, True],
    42010: ["1", 1, 3, 1, True], 42020: ["26.5", 5, 40, 0.5, True], 42025: ["35.0", 5, 40, 0.5, True],
    45010: ["1", 1, 3, 1, True], 45020: ["30.0", 15, 45, 0.5, True], 45025: ["38.0", 15, 45, 0.5, True],
    48010: ["2", 1, 2, 1, True],
}


QUIRKS = set()  # IDs, die Schreiben bestätigen, den Wert aber nicht übernehmen


def cs(s):
    c = 0
    for b in s.encode():
        c ^= b
    return f"{c:02X}"


def answer(line):
    line = line.strip()
    if not line or line[0] not in "#%":
        return None
    body = line[1:]
    with_cs = "$" in body
    if with_cs:
        body, c = body.rsplit("$", 1)
        if cs(body) != c.upper():
            return "Xc", True
    if "?" in body:
        sid, q = body.split("?", 1)
        op, val = "?", q
    elif "=" in body:
        sid, val = body.split("=", 1)
        op = "="
    else:
        return "Xi", with_cs
    try:
        vid = int(sid)
    except ValueError:
        return "Xi", with_cs
    if vid not in REG:
        return "Xu", with_cs
    r = REG[vid]
    if op == "?":
        if val.lower() == "l":
            return f">{r[1]}", with_cs
        if val.lower() == "h":
            return f">{r[2]}", with_cs
        if r[0] in ("l", "h"):
            return "X" + r[0], with_cs
        if vid == 2020:  # Kollektor schwankt
            r[0] = f"{41.7 + random.uniform(-2, 2):.1f}"
        return ">" + r[0], with_cs
    if r[4] is not True:
        return "Xr", with_cs
    if r[1] is not None:
        try:
            f = float(val)
        except ValueError:
            return "Xi", with_cs
        if f < r[1]:
            return "Xl", with_cs
        if f > r[2]:
            return "Xh", with_cs
        if abs(round((f - r[1]) / r[3]) * r[3] + r[1] - f) > 1e-6:
            return "Xs", with_cs
    if vid not in QUIRKS:
        r[0] = val
    return ">" + val, with_cs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", default="/tmp/allpool-sim")
    ap.add_argument("--delay", type=float, default=0.02, help="Antwortzeit in s")
    ap.add_argument("--min-gap", type=float, default=0.0,
                    help="Befehle, die früher als so viele s nach der letzten Antwort kommen, "
                         "werden ignoriert (langsames ALLPOOL)")
    ap.add_argument("--quirks", action="store_true",
                    help="33011 (Whirlpool) bestätigt Schreiben, behält aber den alten Wert")
    a = ap.parse_args()
    if a.quirks:
        QUIRKS.add(33011)
    master, slave = pty.openpty()
    tty.setraw(slave)
    name = os.ttyname(slave)
    try:
        os.unlink(a.link)
    except FileNotFoundError:
        pass
    os.symlink(name, a.link)
    print(f"ALLPOOL-Simulator auf {name} (Link: {a.link})", flush=True)
    muted = [False]

    def toggle(*_):  # kill -USR1 <pid>: ALLPOOL "stromlos" ein/aus
        muted[0] = not muted[0]
        print("stumm" if muted[0] else "antwortet wieder", flush=True)
    signal.signal(signal.SIGUSR1, toggle)
    buf = b""
    last_reply = 0.0
    while True:
        data = os.read(master, 256)
        buf += data
        while b"\n" in buf or b"\r" in buf:
            idx = min(i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0)
            line, buf = buf[:idx].decode(errors="replace"), buf[idx + 1:]
            res = answer(line)
            if res is None or muted[0]:
                continue
            if time.monotonic() - last_reply < a.min_gap:
                print(f"  {line!r:24} -> (zu schnell, ignoriert)", flush=True)
                continue
            resp, with_cs = res
            out = resp + (f"${cs(resp[1:])}" if with_cs else "") + "\r\n"
            time.sleep(a.delay)
            os.write(master, out.encode())
            last_reply = time.monotonic()
            print(f"  {line!r:24} -> {out.strip()!r}", flush=True)


if __name__ == "__main__":
    main()
