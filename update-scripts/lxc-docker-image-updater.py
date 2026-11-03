#!/usr/bin/env python3
"""
lxc-docker-image-updater.py - pull new Docker images in every running LXC and
recreate only the containers whose image actually changed, with a live TUI.

Everything goes through the Proxmox nodes by IP (`pct exec` on whichever node
hosts the CT), so it keeps working while DNS/reverse-proxy CTs restart and DNS is down.

  1. Discover - every running LXC; Compose projects inside the ones with Docker
  2. Pull     - in parallel; nothing running is touched
  3. Update   - `docker compose up -d` ONLY for projects where a running container's
                image differs from the local image; LXC_RUN_LAST ones go last
  4. Prune    - dangling images on hosts where something was recreated

Settings come from .env next to this script (copy .env.example and fill it in).

Pinned images (tag with a dotted version like 1.25 / v2.3.4, or an @sha256 digest)
are highlighted - those won't move on their own.

Keys: up/down scroll, q = stop after the current step, Ctrl-C twice = abort.

Usage:
  ./lxc-docker-image-updater.py                # pull + update
  ./lxc-docker-image-updater.py --pull-only    # pull and report, no restarts
  ./lxc-docker-image-updater.py --only jellyfin immich
  ./lxc-docker-image-updater.py --no-prune
"""
from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import locale
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# -- Configuration (.env next to this script, see .env.example) ----------------
ENV_FILE = Path(__file__).resolve().parent / ".env"


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines; blank lines, # comments, `export` and quotes are allowed."""
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            key, sep, val = line.partition("=")
            if not sep or line.startswith("#"):
                continue
            val = val.strip()
            if len(val) > 1 and val[0] in "'\"" and val[-1] == val[0]:
                val = val[1:-1]
            else:
                val = val.split(" #", 1)[0].strip()
            out[key.strip()] = val
    return out


DOTENV = read_env(ENV_FILE)


def env(key: str, default: str = "") -> str:
    """Real environment variables win over .env."""
    return os.environ.get(key, DOTENV.get(key, default)).strip()


def env_list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in env(key, default).split(",") if x.strip()]


def env_int(key: str, default: int) -> int:
    v = env(key)
    try:
        return int(v) if v else default
    except ValueError:
        sys.exit(f"{key} must be a whole number, got {v!r} ({ENV_FILE})")


NODES = env_list("PVE_NODES")                    # any reachable one is used
SSH_USER = env("SSH_USER")
SSH_KEY = Path(env("SSH_KEY")).expanduser()
SUDO = env("SSH_SUDO", "" if SSH_USER == "root" else "sudo -n")
SSH_EXTRA = ((["-p", env("SSH_PORT")] if env("SSH_PORT") else [])
             + (["-o", f"StrictHostKeyChecking={env('SSH_STRICT_HOST_KEY_CHECKING')}"]
                if env("SSH_STRICT_HOST_KEY_CHECKING") else []))
LOG_DIR = Path(env("UPDATER_LOG_DIR") or Path(__file__).resolve().parent / "logs").expanduser()

EXCLUDE = set(env_list("DOCKER_EXCLUDE"))        # compose project or CT names to never touch
RUN_LAST = tuple(x.lower() for x in env_list("LXC_RUN_LAST"))  # substring of project/CT name, updated last in this order
PARALLEL = env_int("DOCKER_PARALLEL", 4)         # concurrent pull jobs
DISCOVER_PARALLEL = env_int("DOCKER_DISCOVER_PARALLEL", 2)  # pct exec is slow under load
DISCOVER_TIMEOUT = env_int("DOCKER_DISCOVER_TIMEOUT", 90)   # per CT, retried once on timeout

PULL_TIMEOUT = env_int("DOCKER_PULL_TIMEOUT", 30 * 60)
UP_TIMEOUT = env_int("DOCKER_UP_TIMEOUT", 10 * 60)
HEALTH_TIMEOUT = env_int("DOCKER_HEALTH_TIMEOUT", 3 * 60)
INSPECT_TIMEOUT = env_int("DOCKER_INSPECT_TIMEOUT", 3 * 60)
POLL = 5
LOG_LINES = 6


# -- Logging / helpers ---------------------------------------------------------
class Log:
    fh = None
    lock = threading.Lock()

    @classmethod
    def write(cls, msg: str) -> None:
        if cls.fh:
            with cls.lock:
                cls.fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
                cls.fh.flush()


class StepError(Exception):
    pass


def fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def sudo(cmd: str) -> str:
    return f"{SUDO} {cmd}".strip()


def ssh(host: str, cmd: str, timeout: int = 60, check: bool = True,
        log_output: bool = True) -> subprocess.CompletedProcess:
    argv = ["ssh", "-i", str(SSH_KEY), *SSH_EXTRA,
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            # one shared connection per node instead of a new login per command
            "-o", "ControlMaster=auto", "-o", "ControlPersist=120",
            "-o", "ControlPath=~/.ssh/cm-docker-upd-%r@%h:%p",
            f"{SSH_USER}@{host}", cmd]
    Log.write(f"[{host}] $ {cmd}")
    for attempt in (1, 2):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            Log.write(f"[{host}] timed out after {timeout}s")
            if check:
                raise StepError(f"timed out after {fmt(timeout)}")
            return subprocess.CompletedProcess(argv, 124, "", "timeout")
        # 255 + these messages = ssh never got the command to the node; safe to retry
        if attempt == 1 and r.returncode == 255 and re.search(
                r"Connection (reset|refused|closed)|kex_exchange|banner exchange|timed out",
                r.stderr):
            Log.write(f"[{host}] ssh connection failed, retrying: {r.stderr.strip()}")
            time.sleep(3)
            continue
        break
    if log_output:
        for line in (r.stdout + r.stderr).strip().splitlines():
            Log.write(f"[{host}]   {line}")
    Log.write(f"[{host}] exit {r.returncode}")
    if check and r.returncode != 0:
        lines = (r.stderr.strip() or r.stdout.strip()).splitlines()
        raise StepError(f"exit {r.returncode}" + (f": {lines[-1]}" if lines else ""))
    return r


def pvesh(host: str, path: str, *args: str):
    r = ssh(host, sudo(f"pvesh get {path} {' '.join(args)} --output-format json"),
            log_output=False)
    return json.loads(r.stdout or "null")


def parse_json_multi(text: str) -> list[dict]:
    """Compose prints a JSON array or one object per line, depending on version."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def is_pinned(ref: str) -> bool:
    if "@sha256:" in ref:
        return True
    last = ref.rsplit("/", 1)[-1]
    tag = last.split(":", 1)[1] if ":" in last else "latest"
    return bool(re.search(r"\d+\.\d+", tag))


def short(ref: str) -> str:
    ref = ref.rsplit("/", 1)[-1]
    return re.sub(r"@sha256:(\w{8})\w+", r"@\1", ref)


# -- Model ---------------------------------------------------------------------
DISCOVER = r'''
command -v docker >/dev/null 2>&1 || exit 3
docker compose version >/dev/null 2>&1 || exit 4
docker compose ls -a --format json
echo "@@@"
docker ps --format '{{.Names}}|{{.Label "com.docker.compose.project"}}'
'''

# per container: service, image ref, image ID it runs, image ID the ref points at now
INSPECT = r''' | while read -r c; do
  set -- $(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}} {{.Config.Image}} {{.Image}}' "$c")
  [ -n "$2" ] && echo "$1 $2 $3 $(docker image inspect --format '{{.Id}}' "$2" 2>/dev/null)"
done'''


@dataclass
class Host:
    vmid: int
    name: str
    node: str
    ip: str

    def run(self, inner: str, timeout: int = 60, check: bool = True,
            log_output: bool = True) -> subprocess.CompletedProcess:
        return ssh(self.ip, sudo(f"pct exec {self.vmid} -- sh -c {shlex.quote(inner)}"),
                   timeout=timeout, check=check, log_output=log_output)


@dataclass
class Row:
    host: Host
    project: str = ""
    files: list[str] = field(default_factory=list)
    status: str = "pending"
    detail: str = ""
    pinned: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    since: float = field(default_factory=time.time)

    @property
    def tag(self) -> str:
        return f"{self.host.name}/{self.project or '-'}"

    def compose(self, args: str) -> str:
        files = " ".join(f"-f {shlex.quote(f)}" for f in self.files)
        return f"docker compose -p {shlex.quote(self.project)} {files} {args}"

    def inspect(self) -> list[tuple[str, str, str, str]]:
        r = self.host.run(self.compose("ps -aq") + INSPECT, timeout=INSPECT_TIMEOUT,
                          log_output=False)
        out = []
        for line in r.stdout.splitlines():
            p = line.split()
            if len(p) >= 3:
                out.append((p[0], p[1], p[2], p[3] if len(p) > 3 else ""))
        return out

    def containers(self) -> list[dict]:
        return parse_json_multi(self.host.run(self.compose("ps -a --format json"),
                                              log_output=False).stdout)


class Board:
    """Shared state between the worker thread and the UI."""

    def __init__(self, plain: bool):
        self.rows: list[Row] = []
        self.log: deque[str] = deque(maxlen=200)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.plain = plain
        self.phase = "Starting"
        self.done = False
        self.error = ""
        self.t0 = time.time()

    def set(self, row: Row, status: str | None = None, detail: str | None = None) -> None:
        with self.lock:
            changed = bool(status) and status != row.status
            if changed:
                row.status, row.since = status, time.time()
            if detail is not None:
                row.detail = detail
        if changed:
            line = f"{row.tag}: {row.status}" + (f" - {row.detail}" if row.detail else "")
            Log.write(f"STATE {line}")
            if self.plain:
                print(f"  {line}", flush=True)

    def note(self, msg: str) -> None:
        with self.lock:
            self.log.append(msg)
        Log.write(f"NOTE  {msg}")
        if self.plain:
            print(f"  ! {msg}", flush=True)

    def replace(self, old: Row, new: list[Row]) -> None:
        with self.lock:
            i = self.rows.index(old)
            self.rows[i:i + 1] = new


def matches(name: str, patterns) -> bool:
    return any(p.lower() in name.lower() for p in patterns)


# -- Phases --------------------------------------------------------------------
def discover(board: Board, only: list[str] | None) -> None:
    board.phase = "Discovering"
    entry = next((ip for ip in NODES if ssh(ip, "true", timeout=10, check=False).returncode == 0),
                 None)
    if not entry:
        raise StepError("couldn't reach any Proxmox node")
    board.note(f"Using Proxmox node {entry}")
    node_ip = {s["name"]: s.get("ip") for s in (pvesh(entry, "/cluster/status") or [])
               if s.get("type") == "node" and s.get("online")}
    cts = sorted((r for r in (pvesh(entry, "/cluster/resources", "--type", "vm") or [])
                  if r.get("type") == "lxc" and r.get("status") == "running"),
                 key=lambda r: r["vmid"])

    placeholders = []
    for ct in cts:
        ip = node_ip.get(ct.get("node"))
        name = ct.get("name") or str(ct["vmid"])
        if not ip:
            board.note(f"{name}: no IP for node {ct.get('node')} - skipped")
            continue
        placeholders.append(Row(Host(ct["vmid"], name, ct["node"], ip)))
    with board.lock:
        board.rows = list(placeholders)

    def probe(row: Row) -> None:
        board.set(row, "checking")
        r = row.host.run(DISCOVER, timeout=DISCOVER_TIMEOUT, check=False, log_output=False)
        if r.returncode == 124:
            board.set(row, detail="slow to respond, retrying")
            r = row.host.run(DISCOVER, timeout=DISCOVER_TIMEOUT, check=False, log_output=False)
        if r.returncode == 124:
            board.set(row, "failed", f"no answer from pct exec in {fmt(DISCOVER_TIMEOUT)} (tried twice)")
            return
        if r.returncode == 3:
            board.set(row, "no docker")
            return
        if r.returncode == 4:
            board.set(row, "skipped", "Docker but no Compose v2")
            return
        if r.returncode != 0:
            board.set(row, "failed", (r.stderr.strip().splitlines() or ["query failed"])[-1])
            return
        ls_part, _, ps_part = r.stdout.partition("@@@")
        found = []
        for p in parse_json_multi(ls_part):
            pname = p.get("Name", "")
            if pname in EXCLUDE or row.host.name in EXCLUDE:
                continue
            if only and not (matches(pname, only) or matches(row.host.name, only)):
                continue
            files = [f.strip() for f in p.get("ConfigFiles", "").split(",") if f.strip()]
            nr = Row(row.host, pname, files)
            if "running" not in p.get("Status", ""):
                nr.status, nr.detail = "stopped", "project not running - left alone"
            found.append(nr)
        loose = [ln.split("|")[0] for ln in ps_part.strip().splitlines()
                 if ln and not ln.partition("|")[2]]
        if loose:
            board.note(f"{row.host.name}: not managed by Compose, skipped: {', '.join(loose)}")
        if found:
            board.replace(row, found)
        elif only:
            with board.lock:
                board.rows.remove(row)
        else:
            board.set(row, "no projects", "Docker but no Compose projects")

    with ThreadPoolExecutor(DISCOVER_PARALLEL) as ex:
        list(ex.map(probe, placeholders))


def pull_phase(board: Board, rows: list[Row]) -> None:
    board.phase = "Pulling images"

    def pull(row: Row) -> None:
        if board.stop.is_set():
            board.set(row, "skipped", "stopped by user")
            return
        try:
            step = "check images"
            board.set(row, "checking", "reading current images")
            before = row.inspect()
            row.pinned = sorted({ref for _, ref, *_ in before if is_pinned(ref)})
            step = "pull"
            board.set(row, "pulling", "")
            row.host.run(row.compose("pull -q"), timeout=PULL_TIMEOUT)
            step = "check images after pull"
            after = row.inspect()
            old = {ref: local for _, ref, _, local in before}
            new = sorted({short(ref) for _, ref, _, local in after if local and local != old.get(ref)})
            row.stale = sorted({svc for svc, _, running, local in after if local and running != local})
            if row.stale:
                board.set(row, "update ready", "new: " + (", ".join(new) or ", ".join(row.stale)))
            else:
                board.set(row, "up to date", "")
        except StepError as e:
            board.set(row, "failed", f"{step}: {e}")

    with ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(pull, rows))


def wait_ready(board: Board, row: Row) -> str:
    deadline, streak = time.time() + HEALTH_TIMEOUT, 0
    while True:
        try:
            waiting, cs = [], row.containers()
            for c in cs:
                state, health = c.get("State", ""), c.get("Health", "")
                if state == "exited" and c.get("ExitCode", 1) == 0:
                    continue                                   # one-shot/init container
                if state != "running" or health not in ("", "healthy"):
                    waiting.append(f"{c.get('Service')} {health or state}")
            done = not waiting
            detail = f"{len(cs)} container(s) up" if done else ", ".join(waiting[:3])
        except (StepError, json.JSONDecodeError):
            done, detail = False, "waiting for docker"
        board.set(row, detail=detail)
        streak = streak + 1 if done else 0
        if streak >= 2:
            return detail
        if time.time() > deadline:
            raise StepError(f"not healthy after {fmt(HEALTH_TIMEOUT)} ({detail})")
        time.sleep(POLL)


def update_phase(board: Board, rows: list[Row]) -> list[Host]:
    board.phase = "Updating containers"

    def rank(r: Row) -> int:
        key = (r.project + r.host.name).lower()
        return next((i + 1 for i, k in enumerate(RUN_LAST) if k in key), 0)

    todo = sorted((r for r in rows if r.status == "update ready"), key=rank)
    for r in todo:
        board.set(r, "queued")
    updated: dict[int, Host] = {}
    for r in todo:
        if board.stop.is_set():
            board.set(r, "update ready", "stopped by user - not restarted")
            continue
        if rank(r):
            board.note(f"Restarting {r.project} - DNS may drop briefly (nodes are hit by IP)")
        try:
            board.set(r, "restarting", ", ".join(r.stale))
            r.host.run(r.compose("up -d"), timeout=UP_TIMEOUT)
            board.set(r, "starting")
            detail = wait_ready(board, r)
            left = sorted({s for s, _, running, local in r.inspect() if local and running != local})
            if left:
                raise StepError(f"still on old image: {', '.join(left)}")
            board.set(r, "updated", f"recreated {', '.join(r.stale)}; {detail}")
            updated[r.host.vmid] = r.host
        except StepError as e:
            board.set(r, "failed", str(e))
    return list(updated.values())


def prune_phase(board: Board, hosts: list[Host]) -> None:
    board.phase = "Removing old images"
    for h in hosts:
        r = h.run("docker image prune -f", timeout=300, check=False)
        m = re.search(r"Total reclaimed space:\s*(\S+)", r.stdout)
        if m and not m.group(1).startswith("0"):
            board.note(f"{h.name}: reclaimed {m.group(1)}")


def work(board: Board, args) -> None:
    try:
        discover(board, args.only)
        rows = [r for r in board.rows if r.project and r.status == "pending"]
        if rows:
            pull_phase(board, rows)
            if args.pull_only:
                for r in rows:
                    if r.status == "update ready":
                        board.set(r, detail=r.detail + " (run without --pull-only)")
            else:
                hosts = update_phase(board, rows)
                if hosts and not args.no_prune and not board.stop.is_set():
                    prune_phase(board, hosts)
        board.phase = "Stopped" if board.stop.is_set() else "Done"
    except StepError as e:
        board.error, board.phase = str(e), "Failed"
        board.note(f"FAILED: {e}")
    except Exception as e:                                     # keep the UI alive, log it
        board.error, board.phase = repr(e), "Crashed"
        Log.write(traceback.format_exc())
        board.note(f"CRASHED: {e!r} (see log)")
    finally:
        board.done = True


# -- TUI -----------------------------------------------------------------------
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE = {"checking", "pulling", "restarting", "starting"}
PAIR = {"checking": 4, "pulling": 4, "up to date": 1, "update ready": 3, "queued": 3,
        "restarting": 5, "starting": 5, "updated": 1, "failed": 2}
BOLD_STATES = {"updated", "failed", "update ready"}


def status_attr(s: str) -> int:
    if s not in PAIR:
        return curses.A_DIM
    return curses.color_pair(PAIR[s]) | (curses.A_BOLD if s in BOLD_STATES else 0)


def put(scr, y: int, x: int, text: str, attr: int = 0) -> int:
    h, w = scr.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            scr.addstr(y, x, text[:w - 1 - x], attr)
        except curses.error:
            pass
    return x + len(text)


def counts_line(rows: list[Row]) -> str:
    c = Counter(r.status for r in rows if r.project)
    pinned = sum(1 for r in rows if r.pinned)
    parts = [f"{n} {s}" for s, n in c.items() if s in PAIR]
    return " · ".join(parts + ([f"{pinned} pinned"] if pinned else []))


def draw(scr, board: Board, offset: int, frame: int) -> int:
    scr.erase()
    h, w = scr.getmaxyx()
    with board.lock:
        rows, logs = list(board.rows), list(board.log)
    put(scr, 0, 0, f" LXC Docker updater │ {board.phase} │ {fmt(time.time() - board.t0)} ".ljust(w),
        curses.A_REVERSE | curses.A_BOLD)
    put(scr, 1, 1, counts_line(rows), curses.A_DIM)
    put(scr, 2, 0, f"{'CT':>5}  {'LXC':<18} {'PROJECT':<16} {'STATUS':<15} {'PINNED':<26} DETAIL",
        curses.A_UNDERLINE | curses.A_BOLD)

    log_h = min(LOG_LINES, max(0, h - 10))
    sep = h - log_h - 2
    body_h = max(0, sep - 3)
    offset = max(0, min(offset, len(rows) - body_h))
    for i, r in enumerate(rows[offset:offset + body_h]):
        y = 3 + i
        x = put(scr, y, 0, f"{r.host.vmid:>5}  ")
        x = put(scr, y, x, f"{r.host.name:<18.18} ",
                curses.color_pair(5) | curses.A_BOLD if r.pinned else 0)
        x = put(scr, y, x, f"{r.project or '-':<16.16} ", 0 if r.project else curses.A_DIM)
        st = (SPIN[frame % len(SPIN)] if r.status in ACTIVE else " ") + " " + r.status
        x = put(scr, y, x, f"{st:<15.15} ", status_attr(r.status))
        x = put(scr, y, x, f"{', '.join(short(p) for p in r.pinned):<26.26} ", curses.color_pair(5))
        extra = f" ({fmt(time.time() - r.since)})" if r.status in ACTIVE else ""
        put(scr, y, x, r.detail + extra, curses.A_DIM)
    if len(rows) > body_h > 0:
        put(scr, sep, w - 22, f" rows {offset + 1}-{min(len(rows), offset + body_h)}/{len(rows)} ",
            curses.A_DIM)

    put(scr, sep, 0, "─ log " + "─" * (w - 30), curses.A_DIM)
    for i, line in enumerate(logs[-log_h:] if log_h else []):
        put(scr, sep + 1 + i, 1, line, curses.A_DIM)
    if board.done:
        foot, attr = " done - press any key to exit", curses.A_REVERSE
    elif board.stop.is_set():
        foot, attr = " stopping after the current step… (Ctrl-C again to abort)", curses.color_pair(3)
    else:
        foot, attr = " ↑/↓ scroll   q stop after current step", curses.A_DIM
    put(scr, h - 1, 0, foot.ljust(w), attr)
    scr.refresh()
    return offset


def tui(scr, board: Board) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW,
                           curses.COLOR_CYAN, curses.COLOR_MAGENTA), 1):
        curses.init_pair(i, c, -1)
    scr.timeout(150)
    offset = frame = 0
    while True:
        frame += 1
        offset = draw(scr, board, offset, frame)
        try:
            ch = scr.getch()
        except KeyboardInterrupt:
            if board.stop.is_set():
                raise
            board.stop.set()
            continue
        if board.done and ch != -1 and ch != curses.KEY_RESIZE:
            return
        if ch in (ord("q"), ord("Q")):
            board.stop.set()
        elif ch == curses.KEY_UP:
            offset -= 1
        elif ch == curses.KEY_DOWN:
            offset += 1
        elif ch == curses.KEY_PPAGE:
            offset -= 10
        elif ch == curses.KEY_NPAGE:
            offset += 10


# -- Summary / main ------------------------------------------------------------
def summary(board: Board, log_path: Path) -> None:
    tty = sys.stdout.isatty()
    c = (lambda code: f"\033[{code}m" if tty else "")
    ansi = {1: "32", 2: "31", 3: "33", 4: "36", 5: "35"}
    print(f"\n{c('1')}{board.phase} in {fmt(time.time() - board.t0)} - {counts_line(board.rows)}{c('0')}")
    for r in board.rows:
        if r.status in ("no docker",) and not r.pinned:
            continue
        col = c(ansi[PAIR[r.status]]) if r.status in PAIR else c("2")
        pin = f" {c('35')}pinned: {', '.join(short(p) for p in r.pinned)}{c('0')}" if r.pinned else ""
        print(f"{r.host.vmid:>5}  {r.host.name:<18.18} {r.project or '-':<16.16} "
              f"{col}{r.status:<13}{c('0')} {c('2')}{r.detail}{c('0')}{pin}")
    for line in board.log:
        print(f"  {c('2')}! {line}{c('0')}")
    print(f"{c('2')}Full log: {log_path}{c('0')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull new Docker images in LXCs and recreate changed containers.")
    ap.add_argument("--only", nargs="+", metavar="NAME",
                    help="only projects or CTs whose name contains one of these")
    ap.add_argument("--pull-only", action="store_true", help="pull and report, don't restart anything")
    ap.add_argument("--no-prune", action="store_true", help="keep old images")
    args = ap.parse_args()

    missing = [k for k in ("PVE_NODES", "SSH_USER", "SSH_KEY") if not env(k)]
    if missing:
        print(f"Missing {', '.join(missing)} in {ENV_FILE} - copy .env.example to .env and fill it in")
        return 1
    if not SSH_KEY.is_file():
        print(f"SSH key not found: {SSH_KEY}")
        return 1
    log_path = LOG_DIR / f"docker-update-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    Log.fh = log_path.open("w")

    board = Board(plain=not (sys.stdout.isatty() and sys.stdin.isatty()))
    try:
        if board.plain:
            work(board, args)
        else:
            locale.setlocale(locale.LC_ALL, "")
            threading.Thread(target=work, args=(board, args), daemon=True).start()
            curses.wrapper(tui, board)
    except KeyboardInterrupt:
        board.phase = "Aborted"
        summary(board, log_path)
        return 130
    summary(board, log_path)
    return 1 if board.error or any(r.status == "failed" for r in board.rows) else 0


if __name__ == "__main__":
    sys.exit(main())
