#!/usr/bin/env python3
"""netsch — планировщик сетевых интерфейсов.

by Aleksey KRIVOSHEIN aka Dr.Slon
https://github.com/A-Krivoshen/netsch

Полноэкранное меню (curses) или нумерованные экраны через input().
Сервисные аргументы только для автозапуска:

  netsch.py                 меню
  netsch.py apply           один проход
  netsch.py apply --dry-run
  netsch.py run             демон (без cron)
  netsch.py --config PATH
"""
from __future__ import annotations

import argparse
import datetime as dt
import locale
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "1.0"
AUTHOR = "Aleksey KRIVOSHEIN aka Dr.Slon"

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

@dataclass
class Window:
    start: str
    end: str


@dataclass
class IfaceSpec:
    enabled: bool = True
    windows: list[Window] = field(default_factory=list)


@dataclass
class Config:
    iface: dict[str, IfaceSpec] = field(default_factory=dict)
    check_every_sec: int = 30
    force: bool = False


@dataclass
class Iface:
    name: str
    state: str  # UP / DOWN
    ipv4: str | None
    default_route: bool
    current_ssh: bool


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

def stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"{stamp()}  {msg}", flush=True)


# ---------------------------------------------------------------------------
# time windows
# ---------------------------------------------------------------------------

_HHMM = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def parse_hhmm(raw: str) -> tuple[int, int] | None:
    m = _HHMM.match(raw.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h, mi


def fmt_hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def parse_window(raw: str) -> Window | str:
    s = re.sub(r"\s+", "", raw.strip())
    parts = re.split(r"[–—-]", s)
    if len(parts) != 2:
        return "Нужен формат HH:MM-HH:MM, например 09:00-21:00"
    a, b = parse_hhmm(parts[0]), parse_hhmm(parts[1])
    if not a or not b:
        return "Время 00:00–23:59, минуты две цифры. Пример: 22:00-07:00"
    if a == b:
        return "Начало и конец совпадают — задайте ненулевой интервал"
    return Window(fmt_hhmm(*a), fmt_hhmm(*b))


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now: dt.datetime, w: Window) -> bool:
    t = now.hour * 60 + now.minute
    s, e = _mins(w.start), _mins(w.end)
    if s < e:
        return s <= t < e
    return t >= s or t < e


def desired_up(spec: IfaceSpec, now: dt.datetime) -> bool:
    if not spec.enabled or not spec.windows:
        return False
    return any(in_window(now, w) for w in spec.windows)


# ---------------------------------------------------------------------------
# yaml
# ---------------------------------------------------------------------------

def dump_config(cfg: Config) -> str:
    if yaml is not None:
        data: dict[str, Any] = {
            "iface": {
                name: {
                    "enabled": spec.enabled,
                    "windows": [{"start": w.start, "end": w.end} for w in spec.windows],
                }
                for name, spec in cfg.iface.items()
            },
            "check_every_sec": cfg.check_every_sec,
            "force": cfg.force,
        }
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    lines = ["iface:"]
    if not cfg.iface:
        lines.append("  {}")
    for name, spec in cfg.iface.items():
        lines.append(f"  {name}:")
        lines.append(f"    enabled: {'true' if spec.enabled else 'false'}")
        lines.append("    windows:")
        if not spec.windows:
            lines.append("      []")
        else:
            for w in spec.windows:
                lines.append(f'      - start: "{w.start}"')
                lines.append(f'        end: "{w.end}"')
    lines.append(f"check_every_sec: {cfg.check_every_sec}")
    lines.append(f"force: {'true' if cfg.force else 'false'}")
    return "\n".join(lines) + "\n"


def _spec_from_mapping(raw: Any) -> IfaceSpec:
    spec = IfaceSpec()
    if not isinstance(raw, dict):
        return spec
    spec.enabled = bool(raw.get("enabled", True))
    windows = raw.get("windows") or []
    for item in windows:
        if not isinstance(item, dict):
            continue
        parsed = parse_window(f"{item.get('start', '')}-{item.get('end', '')}")
        if isinstance(parsed, Window):
            spec.windows.append(parsed)
    return spec


def load_config_text(text: str) -> Config:
    cfg = Config()
    if yaml is not None:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return cfg
        iface = data.get("iface") or {}
        if isinstance(iface, dict):
            for name, raw in iface.items():
                cfg.iface[str(name)] = _spec_from_mapping(raw)
        try:
            cfg.check_every_sec = max(1, int(data.get("check_every_sec", 30)))
        except (TypeError, ValueError):
            cfg.check_every_sec = 30
        cfg.force = bool(data.get("force", False))
        return cfg
    current: str | None = None
    pending: str | None = None
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.replace("\t", "  ")
        m = re.match(r"^\s*check_every_sec:\s*(\d+)\s*$", line)
        if m:
            cfg.check_every_sec = max(1, int(m.group(1)))
            continue
        m = re.match(r"^\s*force:\s*(true|false)\s*$", line, re.I)
        if m:
            cfg.force = m.group(1).lower() == "true"
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m and not line.startswith("    "):
            current = m.group(1)
            cfg.iface[current] = IfaceSpec()
            pending = None
            continue
        if not current:
            continue
        m = re.match(r"^\s+enabled:\s*(true|false)\s*$", line, re.I)
        if m:
            cfg.iface[current].enabled = m.group(1).lower() == "true"
            continue
        m = re.match(r'^\s+- start:\s*"?(\d{1,2}[:.]\d{2})"?\s*$', line)
        if m:
            pending = m.group(1)
            continue
        m = re.match(r'^\s+end:\s*"?(\d{1,2}[:.]\d{2})"?\s*$', line)
        if m and pending:
            parsed = parse_window(f"{pending}-{m.group(1)}")
            if isinstance(parsed, Window):
                cfg.iface[current].windows.append(parsed)
            pending = None
    return cfg


def default_config_path() -> Path:
    etc = Path("/etc/netsch/config.yaml")
    cwd = Path("netsch.yaml")
    if os.geteuid() == 0:
        return etc
    if etc.is_file() and os.access(etc, os.R_OK):
        return etc
    return cwd


def load_config(path: Path) -> Config:
    if not path.is_file():
        return Config()
    return load_config_text(path.read_text(encoding="utf-8"))


def save_config(path: Path, cfg: Config) -> Path:
    text = dump_config(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        fallback = Path("netsch.yaml")
        fallback.write_text(text, encoding="utf-8")
        log(f"нет прав на {path}, записано в {fallback}")
        return fallback


# ---------------------------------------------------------------------------
# scan (только ip / SSH_CONNECTION)
# ---------------------------------------------------------------------------

def _run_ip(args: list[str]) -> str | None:
    for bin_ in ("ip", "/sbin/ip", "/usr/sbin/ip", "/bin/ip"):
        try:
            out = subprocess.run(
                [bin_, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if out.returncode == 0:
                return out.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def _first_ipv4(tokens: list[str]) -> str | None:
    for tok in tokens:
        m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3})(?:/\d+)?$", tok)
        if m and not m.group(1).startswith("127."):
            return m.group(1)
    return None


def ssh_server_ip() -> str | None:
    raw = os.environ.get("SSH_CONNECTION", "").strip().split()
    if len(raw) >= 3:
        return raw[2]
    return None


def scan_ifaces() -> list[Iface]:
    link = _run_ip(["-br", "link"])
    addr = _run_ip(["-br", "addr"])
    route = _run_ip(["route", "show", "default"])
    if link is None:
        raise RuntimeError("команда ip не найдена (пакет iproute2)")

    default_devs: set[str] = set()
    for line in (route or "").splitlines():
        if "default" not in line:
            continue
        m = re.search(r"\bdev\s+(\S+)", line)
        if m:
            default_devs.add(m.group(1))

    ipv4: dict[str, str] = {}
    for line in (addr or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ip4 = _first_ipv4(parts[2:])
        if ip4:
            ipv4[parts[0].split("@")[0]] = ip4

    ssh_ip = ssh_server_ip()
    out: list[Iface] = []
    for line in link.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].rstrip(":").split("@")[0]
        if not name or name == "lo":
            continue
        ip4 = ipv4.get(name)
        state = "UP" if parts[1].upper() == "UP" else "DOWN"
        out.append(
            Iface(
                name=name,
                state=state,
                ipv4=ip4,
                default_route=name in default_devs,
                current_ssh=bool(ssh_ip and ip4 and ip4 == ssh_ip),
            )
        )
    return out


def is_protected(iface: Iface) -> bool:
    return iface.default_route or iface.current_ssh


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def set_link(name: str, up: bool, dry_run: bool) -> None:
    action = "up" if up else "down"
    if dry_run:
        log(f"{name}: ip link set {name} {action} (dry-run)")
        return
    result = subprocess.run(
        ["ip", "link", "set", name, action],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        log(f"{name}: ошибка ip link set {action}: {err}")
        return
    log(f"{name}: {action}")


def apply_once(
    cfg: Config,
    dry_run: bool = False,
    interactive: bool = False,
    allow_protected: set[str] | None = None,
) -> None:
    allow = allow_protected or set()
    try:
        ifaces = scan_ifaces()
    except RuntimeError as exc:
        log(str(exc))
        return
    now = dt.datetime.now()
    for iface in ifaces:
        if iface.name == "lo":
            continue
        spec = cfg.iface.get(iface.name)
        if spec is None or not spec.enabled:
            continue
        want = "UP" if desired_up(spec, now) else "DOWN"
        if want == iface.state:
            log(f"{iface.name}: уже {want}, пропуск")
            continue
        if want == "DOWN" and is_protected(iface) and not cfg.force and iface.name not in allow:
            why = []
            if iface.default_route:
                why.append("default route")
            if iface.current_ssh:
                why.append("текущий SSH")
            log(f"{iface.name}: защищён ({', '.join(why)}), force=false — не гашу")
            if interactive:
                ans = input(f"Погасить {iface.name} ({', '.join(why)})? [y/N]: ").strip().lower()
                if ans in ("y", "yes", "д", "да"):
                    set_link(iface.name, False, dry_run)
            continue
        set_link(iface.name, want == "UP", dry_run)


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

def unit_text(config_path: Path) -> str:
    script = Path(__file__).resolve()
    python = sys.executable
    return f"""[Unit]
Description=netsch — планировщик сетевых интерфейсов
After=network-pre.target
Wants=network-pre.target

[Service]
Type=simple
ExecStart={python} {script} run --config {config_path}
Restart=always
RestartSec=5
WorkingDirectory=/

[Install]
WantedBy=multi-user.target
"""


def do_install(config_path: Path) -> None:
    text = unit_text(config_path)
    dest = Path("/etc/systemd/system/netsch.service")
    print(text)
    try:
        dest.write_text(text, encoding="utf-8")
        print(f"записано: {dest}")
    except OSError as exc:
        print(f"не записано ({exc}). Нужны права root.")
    print("systemctl сам не запускаю. Дальше:")
    print("  systemctl daemon-reload")
    print("  systemctl enable --now netsch.service")
    print("  systemctl status netsch")


# ---------------------------------------------------------------------------
# numbered menus (fallback)
# ---------------------------------------------------------------------------

def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def print_table(ifaces: list[Iface]) -> None:
    print(f"{'#':>3}  {'имя':<12} {'state':<6} {'IPv4':<16} {'default':<8} SSH")
    for i, iface in enumerate(ifaces, 1):
        print(
            f"{i:3d}  {iface.name:<12} {iface.state:<6} {(iface.ipv4 or '—'):<16} "
            f"{'да' if iface.default_route else 'нет':<8} "
            f"{'да' if iface.current_ssh else 'нет'}"
        )


class App:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.ifaces: list[Iface] = []
        self.selected: list[str] = list(self.cfg.iface.keys())

    def reload_cfg(self) -> None:
        self.cfg = load_config(self.config_path)

    def persist(self) -> None:
        self.config_path = save_config(self.config_path, self.cfg)

    def scan(self) -> None:
        try:
            self.ifaces = scan_ifaces()
        except RuntimeError as exc:
            print(exc)
            self.ifaces = []
            return
        print_table(self.ifaces)

    def choose(self) -> None:
        if not self.ifaces:
            self.scan()
        if not self.ifaces:
            return
        print("Номера через пробел, пустой ввод — подтвердить текущий выбор.")
        print("Текущие:", ", ".join(self.selected) or "—")
        raw = ask("> ").strip()
        if raw:
            picked: list[str] = []
            for tok in raw.replace(",", " ").split():
                if tok.isdigit():
                    idx = int(tok) - 1
                    if 0 <= idx < len(self.ifaces):
                        picked.append(self.ifaces[idx].name)
                elif any(i.name == tok for i in self.ifaces):
                    picked.append(tok)
            self.selected = list(dict.fromkeys(picked))
        iface: dict[str, IfaceSpec] = {}
        for name in self.selected:
            iface[name] = self.cfg.iface.get(name, IfaceSpec())
            iface[name].enabled = True
        self.cfg.iface = iface
        print("выбрано:", ", ".join(self.selected) or "—")

    def schedule(self) -> None:
        if not self.selected:
            print("Сначала выберите интерфейсы.")
            return
        for name in self.selected:
            spec = self.cfg.iface.setdefault(name, IfaceSpec())
            print(f"\n{name}: текущие окна:")
            for w in spec.windows:
                extra = " (через полночь)" if _mins(w.start) > _mins(w.end) else ""
                print(f"  {w.start}-{w.end}{extra}")
            print("Добавьте окна HH:MM-HH:MM. Пустая строка — дальше. 'c' — очистить.")
            while True:
                raw = ask(f"{name}> ").strip()
                if raw == "":
                    break
                if raw.lower() in ("c", "clear", "очистить"):
                    spec.windows = []
                    print("очищено")
                    continue
                parsed = parse_window(raw)
                if isinstance(parsed, str):
                    print(parsed)
                    continue
                spec.windows.append(parsed)
                print(f"  + {parsed.start}-{parsed.end}")
        print("\nИтог:")
        for name in self.selected:
            spec = self.cfg.iface[name]
            wins = ", ".join(f"{w.start}-{w.end}" for w in spec.windows) or "нет окон → DOWN"
            print(f"  {name}: {wins}")
        ans = ask("Записать? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes", "д", "да"):
            self.persist()
            print(f"записано: {self.config_path}")
        else:
            print("не записано")

    def status(self) -> None:
        now = dt.datetime.now()
        try:
            ifaces = scan_ifaces()
        except RuntimeError as exc:
            print(exc)
            ifaces = self.ifaces
        by_name = {i.name: i for i in ifaces}
        print(f"сейчас {stamp()}  force={self.cfg.force}  check_every_sec={self.cfg.check_every_sec}")
        print(f"конфиг {self.config_path}")
        for name, spec in self.cfg.iface.items():
            live = by_name.get(name)
            want = "UP" if desired_up(spec, now) else "DOWN"
            fact = live.state if live else "—"
            flags = []
            if live and live.default_route:
                flags.append("default")
            if live and live.current_ssh:
                flags.append("ssh")
            wins = ", ".join(f"{w.start}-{w.end}" for w in spec.windows) or "—"
            print(f"  {name:12} факт {fact:4} желаемо {want:4}  {wins}  {' '.join(flags)}")
        print(dump_config(self.cfg))

    def apply(self, dry_run: bool = False) -> None:
        self.reload_cfg()
        apply_once(self.cfg, dry_run=dry_run, interactive=True)

    def daemon(self) -> None:
        print("демон в этом процессе, Ctrl+C стоп. cron не используется.")
        run_daemon(self.config_path)

    def install(self) -> None:
        do_install(self.config_path)

    def main_menu(self) -> None:
        while True:
            print()
            print("netsch")
            print(f"by {AUTHOR}")
            print("1. Сканировать интерфейсы")
            print("2. Выбрать интерфейсы для расписания")
            print("3. Задать окна работы")
            print("4. Показать статус")
            print("5. Применить сейчас")
            print("6. Режим демона")
            print("7. Установить systemd-сервис")
            print("8. Выход")
            print(f"конфиг: {self.config_path}")
            choice = ask("Выбор [1-8]: ").strip()
            if choice == "1":
                self.scan()
            elif choice == "2":
                self.choose()
            elif choice == "3":
                self.schedule()
            elif choice == "4":
                self.status()
            elif choice == "5":
                dry = ask("dry-run? [y/N]: ").strip().lower() in ("y", "yes", "д", "да")
                self.apply(dry_run=dry)
            elif choice == "6":
                self.daemon()
            elif choice == "7":
                self.install()
            elif choice in ("8", "q", "й"):
                return
            else:
                print("введите число 1–8")


# ---------------------------------------------------------------------------
# curses
# ---------------------------------------------------------------------------

def curses_available() -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    try:
        size = os.get_terminal_size()
    except OSError:
        return False
    if size.columns < 60 or size.lines < 18:
        return False
    try:
        import curses  # noqa: F401
    except Exception:
        return False
    return True


def run_curses(app: App) -> None:
    import curses

    locale.setlocale(locale.LC_ALL, "")

    menu = [
        "Сканировать интерфейсы",
        "Выбрать интерфейсы для расписания",
        "Задать окна работы",
        "Показать статус",
        "Применить сейчас",
        "Режим демона",
        "Установить systemd-сервис",
        "Выход",
    ]

    def draw_box(stdscr: Any, title: str) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        try:
            stdscr.border()
            stdscr.addnstr(0, 2, f" netsch {title} ", min(len(title) + 12, w - 4), curses.A_BOLD)
        except curses.error:
            pass

    def pause(stdscr: Any, msg: str = "Enter — назад") -> None:
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addnstr(h - 1, 2, msg, w - 4)
        except curses.error:
            pass
        stdscr.getch()

    def screen_scan(stdscr: Any) -> None:
        draw_box(stdscr, "скан")
        try:
            app.ifaces = scan_ifaces()
            err = None
        except RuntimeError as exc:
            app.ifaces = []
            err = str(exc)
        h, w = stdscr.getmaxyx()
        row = 2
        if err:
            stdscr.addnstr(row, 2, err, w - 4)
        else:
            hdr = f"{'#':>3}  {'имя':<12} {'st':<6} {'IPv4':<15} def  ssh"
            stdscr.addnstr(row, 2, hdr, w - 4, curses.A_DIM)
            row += 1
            for i, iface in enumerate(app.ifaces, 1):
                line = (
                    f"{i:3d}  {iface.name:<12} {iface.state:<6} "
                    f"{(iface.ipv4 or '—'):<15} "
                    f"{'да' if iface.default_route else 'нет':<4} "
                    f"{'да' if iface.current_ssh else 'нет'}"
                )
                attr = curses.A_BOLD if iface.state == "UP" else curses.A_NORMAL
                stdscr.addnstr(row, 2, line, w - 4, attr)
                row += 1
                if row >= h - 2:
                    break
        pause(stdscr)

    def screen_select(stdscr: Any) -> None:
        if not app.ifaces:
            try:
                app.ifaces = scan_ifaces()
            except RuntimeError:
                app.ifaces = []
        if not app.ifaces:
            draw_box(stdscr, "выбор")
            stdscr.addnstr(2, 2, "Нет интерфейсов. Сначала скан.")
            pause(stdscr)
            return
        marked = set(app.selected)
        idx = 0
        while True:
            draw_box(stdscr, "выбор  пробел — отметить  Enter — ок")
            h, w = stdscr.getmaxyx()
            for i, iface in enumerate(app.ifaces):
                mark = "[x]" if iface.name in marked else "[ ]"
                line = f"{mark} {iface.name:<12} {iface.state}  {iface.ipv4 or '—'}"
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                stdscr.addnstr(2 + i, 2, line, w - 4, attr)
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(app.ifaces)
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(app.ifaces)
            elif key in (ord(" "),):
                name = app.ifaces[idx].name
                if name in marked:
                    marked.remove(name)
                else:
                    marked.add(name)
            elif key in (10, 13, curses.KEY_ENTER):
                app.selected = [i.name for i in app.ifaces if i.name in marked]
                iface: dict[str, IfaceSpec] = {}
                for name in app.selected:
                    iface[name] = app.cfg.iface.get(name, IfaceSpec())
                    iface[name].enabled = True
                app.cfg.iface = iface
                return
            elif key in (27, ord("q")):
                return

    def screen_schedule(stdscr: Any) -> None:
        curses.echo()
        curses.curs_set(1)
        if not app.selected:
            draw_box(stdscr, "окна")
            stdscr.addnstr(2, 2, "Сначала выберите интерфейсы.")
            pause(stdscr)
            curses.noecho()
            curses.curs_set(0)
            return
        for name in app.selected:
            spec = app.cfg.iface.setdefault(name, IfaceSpec())
            while True:
                draw_box(stdscr, f"окна {name}")
                h, w = stdscr.getmaxyx()
                stdscr.addnstr(2, 2, "Текущие окна (через полночь можно 22:00-07:00):", w - 4)
                row = 3
                for wdw in spec.windows:
                    stdscr.addnstr(row, 4, f"{wdw.start}-{wdw.end}", w - 6)
                    row += 1
                stdscr.addnstr(row + 1, 2, "HH:MM-HH:MM  Enter пустой — дальше  c — очистить", w - 4)
                stdscr.move(row + 2, 2)
                try:
                    raw = stdscr.getstr(row + 2, 2, 20).decode("utf-8", "ignore").strip()
                except curses.error:
                    raw = ""
                if raw == "":
                    break
                if raw.lower() in ("c", "clear"):
                    spec.windows = []
                    continue
                parsed = parse_window(raw)
                if isinstance(parsed, Window):
                    spec.windows.append(parsed)
                else:
                    stdscr.addnstr(row + 4, 2, parsed, w - 4)
                    stdscr.getch()
        curses.noecho()
        curses.curs_set(0)
        draw_box(stdscr, "итог")
        h, w = stdscr.getmaxyx()
        row = 2
        for name in app.selected:
            spec = app.cfg.iface[name]
            wins = ", ".join(f"{w.start}-{w.end}" for w in spec.windows) or "нет окон"
            stdscr.addnstr(row, 2, f"{name}: {wins}", w - 4)
            row += 1
        stdscr.addnstr(row + 1, 2, "Записать? y/n", w - 4)
        key = stdscr.getch()
        if key in (ord("y"), ord("Y"), ord("д"), ord("д".encode()[0]) if False else -1, 10, 13):
            app.persist()
            stdscr.addnstr(row + 3, 2, f"записано {app.config_path}", w - 4)
            pause(stdscr)

    def screen_status(stdscr: Any) -> None:
        draw_box(stdscr, "статус")
        h, w = stdscr.getmaxyx()
        now = dt.datetime.now()
        try:
            live = {i.name: i for i in scan_ifaces()}
        except RuntimeError:
            live = {i.name: i for i in app.ifaces}
        stdscr.addnstr(2, 2, f"{stamp()}  force={app.cfg.force}  {app.config_path}", w - 4)
        row = 4
        for name, spec in app.cfg.iface.items():
            fact = live[name].state if name in live else "—"
            want = "UP" if desired_up(spec, now) else "DOWN"
            wins = ", ".join(f"{x.start}-{x.end}" for x in spec.windows) or "—"
            stdscr.addnstr(row, 2, f"{name:12} факт {fact:4} желаемо {want:4}  {wins}", w - 4)
            row += 1
            if row >= h - 2:
                break
        pause(stdscr)

    def screen_apply(stdscr: Any) -> None:
        draw_box(stdscr, "apply")
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(2, 2, "Enter — применить  d — dry-run  Esc — назад", w - 4)
        key = stdscr.getch()
        if key in (27, ord("q")):
            return
        dry = key in (ord("d"), ord("D"))
        curses.endwin()
        apply_once(app.cfg, dry_run=dry, interactive=True)
        input("Enter — меню ")
        stdscr = curses.initscr()
        curses.cbreak()
        curses.noecho()
        stdscr.keypad(True)

    def screen_install(stdscr: Any) -> None:
        curses.endwin()
        do_install(app.config_path)
        input("Enter — меню ")
        stdscr = curses.initscr()
        curses.cbreak()
        curses.noecho()
        stdscr.keypad(True)

    def main(stdscr: Any) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        idx = 0
        while True:
            h, w = stdscr.getmaxyx()
            if w < 60 or h < 18:
                curses.endwin()
                app.main_menu()
                return
            draw_box(stdscr, "меню")
            stdscr.addnstr(1, 2, f"by {AUTHOR}", w - 4, curses.A_DIM)
            stdscr.addnstr(2, 2, "VPS: не гасите SSH/default без force", w - 4, curses.A_DIM)
            for i, label in enumerate(menu):
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                stdscr.addnstr(4 + i, 4, f"{i + 1}. {label}", w - 6, attr)
            stdscr.addnstr(h - 2, 2, f"конфиг {app.config_path}", w - 4, curses.A_DIM)
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % 8
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % 8
            elif key in (10, 13, curses.KEY_ENTER) or (ord("1") <= key <= ord("8")):
                if ord("1") <= key <= ord("8"):
                    idx = key - ord("1")
                if idx == 0:
                    screen_scan(stdscr)
                elif idx == 1:
                    screen_select(stdscr)
                elif idx == 2:
                    screen_schedule(stdscr)
                elif idx == 3:
                    screen_status(stdscr)
                elif idx == 4:
                    screen_apply(stdscr)
                elif idx == 5:
                    curses.endwin()
                    run_daemon(app.config_path)
                    return
                elif idx == 6:
                    screen_install(stdscr)
                elif idx == 7:
                    return
            elif key in (27, ord("q")):
                return

    curses.wrapper(main)


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

_STOP = False


def _handle_stop(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    log(f"сигнал {signum}, останавливаюсь")


def run_daemon(config_path: Path) -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    log(f"netsch daemon start config={config_path}")
    while not _STOP:
        cfg = load_config(config_path)
        apply_once(cfg, dry_run=False, interactive=False)
        every = max(1, cfg.check_every_sec)
        for _ in range(every):
            if _STOP:
                break
            time.sleep(1)
    log("netsch daemon stop")


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="netsch", add_help=True)
    p.add_argument("--config", help="путь к yaml")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("cmd", nargs="?", choices=["run", "apply", "install"])
    p.add_argument("--version", action="version", version=f"netsch {VERSION} — by {AUTHOR}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    path = Path(args.config) if args.config else default_config_path()
    if args.cmd == "apply":
        cfg = load_config(path)
        apply_once(cfg, dry_run=args.dry_run, interactive=False)
        return 0
    if args.cmd == "run":
        run_daemon(path)
        return 0
    if args.cmd == "install":
        do_install(path)
        return 0
    app = App(path)
    if curses_available():
        try:
            run_curses(app)
            return 0
        except Exception as exc:
            log(f"curses недоступен ({exc}), нумерованное меню")
    app.main_menu()
    return 0


if __name__ == "__main__":
    sys.exit(main())
