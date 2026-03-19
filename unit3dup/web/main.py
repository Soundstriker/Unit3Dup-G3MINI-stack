from __future__ import annotations

import contextlib
import html
import io
import json
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from common.command import CommandLine
from common.settings import DEFAULT_JSON_PATH, Load
from unit3dup.bot import Bot

app = FastAPI(title="Unit3Dup Web", docs_url=None, redoc_url=None)
WEB_SETTINGS_PATH = DEFAULT_JSON_PATH.parent / "unit3dup-web.json"


def _fmt_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} B"


def _fmt_time(value: float | None) -> str:
    return "-" if not value else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _checked(value) -> str:
    return " checked" if _truthy(value) else ""


def _secret_state(value: str | None) -> str:
    if not value or str(value).strip().lower() in {"", "no_key", "no_pass", "no_pid"}:
        return "missing"
    return "configured"


def _build_cli_args(argv: list[str]):
    old_argv = sys.argv[:]
    try:
        sys.argv = ["unit3dup", *argv]
        return CommandLine().args
    finally:
        sys.argv = old_argv


def _list_entries(path_str: str | None) -> list[dict]:
    if not path_str:
        return []
    root = Path(path_str)
    if not root.exists() or not root.is_dir():
        return []

    items = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if entry.name.startswith("."):
            continue
        stats = entry.stat()
        items.append(
            {
                "name": entry.name,
                "quoted_name": quote(entry.name, safe=""),
                "type": "folder" if entry.is_dir() else "file",
                "size": _fmt_size(stats.st_size),
                "modified": _fmt_time(stats.st_mtime),
            }
        )
    return items


def _load_raw_config() -> dict:
    with open(DEFAULT_JSON_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=4)


def _reload_config() -> None:
    loaded = Load.load_config()
    if Load._instance:
        Load._instance.config = loaded


def _load_web_settings() -> dict:
    defaults = {
        "local_config_path": os.getenv("UNIT3DUP_LOCAL_CONFIG_PATH", ""),
        "local_watch_path": os.getenv("UNIT3DUP_LOCAL_WATCH_PATH", ""),
        "local_done_path": os.getenv("UNIT3DUP_LOCAL_DONE_PATH", ""),
        "local_data_path": os.getenv("UNIT3DUP_LOCAL_DATA_PATH", ""),
    }
    if not WEB_SETTINGS_PATH.exists():
        return defaults
    with open(WEB_SETTINGS_PATH, "r", encoding="utf-8") as handle:
        saved = json.load(handle)
    for key, value in saved.items():
        if str(value).strip():
            defaults[key] = value
    return defaults


def _coerce_int(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


async def _request_data(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _trackers(config, cli_args) -> list[str]:
    if cli_args.mt:
        return [tracker.upper() for tracker in config.tracker_config.MULTI_TRACKER]
    if cli_args.tracker:
        return [cli_args.tracker.upper()]
    return [config.tracker_config.MULTI_TRACKER[0].upper()]


@dataclass
class JobState:
    label: str
    status: str = "queued"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    logs: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, message: str) -> None:
        with self.lock:
            for line in str(message).replace("\r", "\n").splitlines():
                if line:
                    self.logs.append(line)
            self.logs = self.logs[-500:]

    def text(self) -> str:
        with self.lock:
            return "\n".join(self.logs)


class _Capture(io.TextIOBase):
    def __init__(self, job: JobState):
        self.job = job
        self.buffer = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        self.buffer += data.replace("\r", "\n")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.job.append(line)
        return len(data)

    def flush(self) -> None:
        if self.buffer:
            self.job.append(self.buffer)
            self.buffer = ""


class JobStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.job: JobState | None = None

    def get(self) -> JobState | None:
        with self.lock:
            return self.job

    def start(self, label: str, runner) -> None:
        with self.lock:
            if self.job and self.job.status in {"queued", "running"}:
                raise RuntimeError("A job is already running")
            job = JobState(label=label)
            self.job = job

        def run() -> None:
            capture = _Capture(job)
            job.status = "running"
            job.append(f"[Web] Starting job: {label}")
            try:
                with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                    runner(job)
            except SystemExit as exc:
                job.append(f"[Web] Job stopped with exit code: {exc}")
                job.status = "failed"
            except Exception:
                job.append("[Web] Unexpected error")
                job.append(traceback.format_exc())
                job.status = "failed"
            else:
                job.status = "completed"
                job.append("[Web] Job completed")
            finally:
                capture.flush()
                job.finished_at = time.time()

        threading.Thread(target=run, daemon=True).start()

    def clear(self) -> None:
        with self.lock:
            if self.job and self.job.status in {"queued", "running"}:
                raise RuntimeError("Cannot clear a running job")
            self.job = None


JOB_STORE = JobStore()


def _cli_args_for_mode(mode: str):
    argv = ["-watcher"]
    if mode == "dry-run":
        argv.extend(["-noseed", "-noup"])
    elif mode == "upload-no-seed":
        argv.append("-noseed")
    elif mode == "upload-seed":
        pass
    else:
        raise ValueError(f"Unknown mode: {mode}")
    return _build_cli_args(argv)


def _mode_label(mode: str) -> str:
    labels = {
        "dry-run": "Dry run",
        "upload-no-seed": "Upload no seed",
        "upload-seed": "Upload and seed",
    }
    return labels.get(mode, mode)


def _process_entry(src: Path, mode: str, job: JobState) -> None:
    config = Load.load_config()
    cli_args = _cli_args_for_mode(mode)
    bot = Bot(
        path=str(src),
        cli=cli_args,
        trackers_name_list=_trackers(config, cli_args),
        mode="folder" if src.is_dir() else "man",
        torrent_archive_path=config.user_preferences.TORRENT_ARCHIVE_PATH or ".",
    )
    job.append(f"[Web] Processing: {src.name} ({_mode_label(mode)})")
    result = bot.run()
    if mode == "dry-run":
        job.append(f"[Web] Dry run finished: {src.name}")
        return
    if not result:
        job.append(f"[Web] Upload skipped or failed: {src.name}")
        return
    done_root = Path(config.user_preferences.WATCHER_DESTINATION_PATH)
    done_root.mkdir(parents=True, exist_ok=True)
    moved_to = bot._move_to_destination(src=src, done_root=done_root)
    job.append(f"[Web] Moved to destination: {moved_to}" if moved_to else f"[Web] Move failed: {src.name}")


def _run_watcher_job(job: JobState, mode: str, entry_name: str | None = None) -> None:
    config = Load.load_config()
    watcher_root = Path(config.user_preferences.WATCHER_PATH)
    if not watcher_root.exists() or not watcher_root.is_dir():
        raise FileNotFoundError(f"Watcher path not found: {watcher_root}")
    if entry_name:
        entries = [watcher_root / entry_name]
    else:
        entries = [entry for entry in sorted(watcher_root.iterdir(), key=lambda item: item.name.lower()) if not entry.name.startswith(".")]
    entries = [entry for entry in entries if entry.exists()]
    if not entries:
        job.append("[Web] Watcher folder is empty")
        return
    for entry in entries:
        _process_entry(entry, mode, job)


def _nav(active: str) -> str:
    items = [("Dashboard", "/", "dashboard"), ("Settings", "/settings", "settings")]
    links = []
    for label, href, name in items:
        css = "nav-link active" if active == name else "nav-link"
        links.append(f"<a class='{css}' href='{href}'>{label}</a>")
    return "".join(links)


def _job_panel(job: JobState | None) -> str:
    if not job:
        return "<section class='panel'><div class='empty'>No job yet.</div></section>"
    clear = ""
    if job.status != "running":
        clear = "<form method='post' action='/jobs/clear'><button class='button ghost small' type='submit'>Clear</button></form>"
    return (
        "<section class='panel'>"
        "<div class='panel-head'>"
        "<div><h2>Last job</h2>"
        f"<p>{_esc(job.label)}</p></div>{clear}</div>"
        "<div class='meta-row'>"
        f"<span>Status: {_esc(job.status)}</span>"
        f"<span>Started: {_fmt_time(job.started_at)}</span>"
        f"<span>Finished: {_fmt_time(job.finished_at)}</span>"
        "</div>"
        f"<pre>{_esc(job.text())}</pre>"
        "</section>"
    )


def _layout(title: str, active: str, body: str, message: str = "", refresh: bool = False) -> str:
    meta = "<meta http-equiv='refresh' content='3'>" if refresh else ""
    flash = f"<div class='flash'>{_esc(message)}</div>" if message else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{meta}<title>{_esc(title)}</title>
<style>
:root{{color-scheme:dark;--bg:#0d1117;--panel:#141a23;--panel2:#1b2330;--line:#293241;--text:#e6ebf2;--muted:#95a2b7;--accent:#78a7ff;--shadow:0 18px 40px rgba(0,0,0,.28)}}
*{{box-sizing:border-box}}body{{margin:0;font-family:Segoe UI,Tahoma,sans-serif;background:radial-gradient(circle at top,rgba(120,167,255,.09),transparent 35%),linear-gradient(180deg,#0a0d12,var(--bg));color:var(--text)}}
main{{width:min(1200px,calc(100% - 32px));margin:22px auto 40px}}a{{color:inherit;text-decoration:none}}form{{margin:0}}
.topbar,.panel{{border:1px solid var(--line);border-radius:18px;background:linear-gradient(180deg,rgba(27,35,48,.94),rgba(20,26,35,.96));box-shadow:var(--shadow)}}
.topbar{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:18px 22px}}.brand h1,.panel h2{{margin:0}}.brand p,.panel p{{margin:6px 0 0;color:var(--muted);font-size:14px}}
.nav{{display:flex;gap:8px;flex-wrap:wrap}}.nav-link{{padding:10px 14px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}}.nav-link.active{{background:rgba(120,167,255,.14);border-color:rgba(120,167,255,.35);color:var(--text)}}
.flash{{margin-top:18px;padding:12px 14px;border-radius:14px;border:1px solid rgba(120,167,255,.24);background:rgba(120,167,255,.08)}}.stack{{display:grid;gap:18px;margin-top:18px}}
.dashboard{{display:grid;grid-template-columns:1.2fr .9fr;gap:18px}}.panel{{padding:20px}}.panel-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:16px}}
.stats{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}.stat{{padding:14px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.02)}}.label{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}.value{{margin-top:8px;font-size:15px;line-height:1.45;word-break:break-word}}
.pill-row,.actions,.row-actions,.meta-row{{display:flex;flex-wrap:wrap;gap:10px}}.pill{{padding:7px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted);font-size:12px}}.pill.ok{{color:#7de0c2;border-color:rgba(125,224,194,.3)}}
.button{{border:1px solid rgba(120,167,255,.35);border-radius:12px;padding:10px 14px;background:rgba(120,167,255,.15);color:var(--text);cursor:pointer;font:inherit}}.button.ghost{{background:transparent;border-color:var(--line);color:var(--muted)}}.button.small{{padding:8px 11px;font-size:13px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:12px 8px;border-bottom:1px solid rgba(255,255,255,.06);vertical-align:top}}th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}.empty{{color:var(--muted);padding:8px 0}}
pre{{margin:0;padding:14px;border-radius:14px;border:1px solid rgba(255,255,255,.06);background:#0b1017;color:#d8deea;font-family:Consolas,monospace;font-size:13px;line-height:1.5;white-space:pre-wrap;max-height:420px;overflow:auto}}
.settings{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}}.fields{{display:grid;gap:14px}}.fields.two{{grid-template-columns:repeat(2,minmax(0,1fr))}}.field label{{display:block;margin-bottom:8px;color:var(--muted);font-size:13px}}.field input,.field select{{width:100%;border:1px solid var(--line);border-radius:12px;padding:11px 12px;background:#0f141c;color:var(--text);font:inherit}}.field small{{display:block;margin-top:6px;color:var(--muted);font-size:12px}}
.checks{{display:grid;gap:10px}}.check{{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.02)}}.check input{{width:18px;height:18px;accent-color:var(--accent)}}.muted{{color:var(--muted);font-size:13px;line-height:1.5}}
@media (max-width:1000px){{.dashboard,.settings,.fields.two,.stats{{grid-template-columns:1fr}}.topbar{{flex-direction:column}}}}
</style></head><body><main><header class="topbar"><div class="brand"><h1>Unit3Dup Web</h1><p>Simple control panel for watcher jobs and settings.</p></div><nav class="nav">{_nav(active)}</nav></header>{flash}<div class="stack">{body}</div></main></body></html>"""


def _render_dashboard(message: str = "") -> str:
    config = Load.load_config()
    web_settings = _load_web_settings()
    watcher = _list_entries(config.user_preferences.WATCHER_PATH)
    done = _list_entries(config.user_preferences.WATCHER_DESTINATION_PATH)[:10]
    job = JOB_STORE.get()

    waiting_rows = "".join(
        "<tr>"
        f"<td>{_esc(item['name'])}</td><td>{_esc(item['type'])}</td><td>{_esc(item['size'])}</td><td>{_esc(item['modified'])}</td>"
        "<td><div class='row-actions'>"
        f"<form method='post' action='/jobs/items/{item['quoted_name']}/dry-run'><button class='button ghost small' type='submit'>Dry run</button></form>"
        f"<form method='post' action='/jobs/items/{item['quoted_name']}/upload-no-seed'><button class='button small' type='submit'>No seed</button></form>"
        f"<form method='post' action='/jobs/items/{item['quoted_name']}/upload-seed'><button class='button small' type='submit'>Seed</button></form>"
        "</div></td></tr>"
        for item in watcher
    ) or "<tr><td colspan='5' class='empty'>No entries</td></tr>"

    done_rows = "".join(
        f"<tr><td>{_esc(item['name'])}</td><td>{_esc(item['type'])}</td><td>{_esc(item['size'])}</td><td>{_esc(item['modified'])}</td></tr>"
        for item in done
    ) or "<tr><td colspan='4' class='empty'>No processed entries</td></tr>"

    body = (
        "<section class='panel'><div class='panel-head'><div><h2>Overview</h2><p>Current paths, secrets state and quick actions.</p></div>"
        "<div class='actions'>"
        "<form method='post' action='/jobs/watcher/dry-run'><button class='button ghost' type='submit'>Dry run watcher</button></form>"
        "<form method='post' action='/jobs/watcher/upload-no-seed'><button class='button' type='submit'>Upload no seed</button></form>"
        "<form method='post' action='/jobs/watcher/upload-seed'><button class='button' type='submit'>Upload and seed</button></form>"
        "</div></div>"
        "<div class='stats'>"
        f"<div class='stat'><div class='label'>Config</div><div class='value'>{_esc(DEFAULT_JSON_PATH)}</div></div>"
        f"<div class='stat'><div class='label'>Watcher</div><div class='value'>{_esc(config.user_preferences.WATCHER_PATH)}</div></div>"
        f"<div class='stat'><div class='label'>Destination</div><div class='value'>{_esc(config.user_preferences.WATCHER_DESTINATION_PATH)}</div></div>"
        f"<div class='stat'><div class='label'>Waiting</div><div class='value'>{len(watcher)}</div></div>"
        "</div>"
        "<div class='pill-row' style='margin-top:14px'>"
        f"<span class='pill {'ok' if _secret_state(config.tracker_config.Gemini_APIKEY) == 'configured' else ''}'>Gemini API: {_secret_state(config.tracker_config.Gemini_APIKEY)}</span>"
        f"<span class='pill {'ok' if _secret_state(config.tracker_config.Gemini_PID) == 'configured' else ''}'>Passkey: {_secret_state(config.tracker_config.Gemini_PID)}</span>"
        f"<span class='pill {'ok' if _secret_state(config.tracker_config.TMDB_APIKEY) == 'configured' else ''}'>TMDB: {_secret_state(config.tracker_config.TMDB_APIKEY)}</span>"
        f"<span class='pill {'ok' if _secret_state(config.tracker_config.IMGBB_KEY) == 'configured' else ''}'>IMGBB: {_secret_state(config.tracker_config.IMGBB_KEY)}</span>"
        "</div></section>"
        "<section class='panel'><div class='panel-head'><div><h2>Local mappings</h2><p>Saved for reference only.</p></div></div>"
        "<div class='stats'>"
        f"<div class='stat'><div class='label'>Local config</div><div class='value'>{_esc(web_settings['local_config_path'] or '-')}</div></div>"
        f"<div class='stat'><div class='label'>Local watch</div><div class='value'>{_esc(web_settings['local_watch_path'] or '-')}</div></div>"
        f"<div class='stat'><div class='label'>Local done</div><div class='value'>{_esc(web_settings['local_done_path'] or '-')}</div></div>"
        f"<div class='stat'><div class='label'>Local data</div><div class='value'>{_esc(web_settings['local_data_path'] or '-')}</div></div>"
        "</div></section>"
        "<div class='dashboard'>"
        "<div>"
        "<section class='panel'><div class='panel-head'><div><h2>Watcher queue</h2><p>Top-level items waiting in the watcher folder.</p></div></div><table><thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Modified</th><th>Actions</th></tr></thead><tbody>"
        f"{waiting_rows}</tbody></table></section>"
        "<section class='panel'><div class='panel-head'><div><h2>Recently moved</h2><p>Last entries found in the destination folder.</p></div></div><table><thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Modified</th></tr></thead><tbody>"
        f"{done_rows}</tbody></table></section>"
        "</div>"
        f"{_job_panel(job)}"
        "</div>"
    )
    return _layout("Unit3Dup Web", "dashboard", body, message, bool(job and job.status == "running"))


def _render_settings(message: str = "") -> str:
    raw = _load_raw_config()
    local = _load_web_settings()
    tracker = raw["tracker_config"]
    prefs = raw["user_preferences"]
    torrent = raw["torrent_client_config"]

    body = (
        "<div class='settings'>"
        "<section class='panel'><div class='panel-head'><div><h2>App settings</h2><p>Edit the main Unit3Dbot.json fields.</p></div></div>"
        "<form method='post' action='/settings/app'><div class='fields'>"
        "<div class='fields two'>"
        f"<div class='field'><label>Gemini URL</label><input name='Gemini_URL' value='{_esc(tracker.get('Gemini_URL', ''))}'></div>"
        f"<div class='field'><label>Multi tracker</label><input name='MULTI_TRACKER' value='{_esc(', '.join(tracker.get('MULTI_TRACKER', [])))}'><small>Comma separated</small></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>Gemini API key</label><input type='password' name='Gemini_APIKEY' placeholder='Leave blank to keep current value'><small>Current state: {_esc(_secret_state(tracker.get('Gemini_APIKEY')))}</small></div>"
        f"<div class='field'><label>Gemini passkey</label><input type='password' name='Gemini_PID' placeholder='Leave blank to keep current value'><small>Current state: {_esc(_secret_state(tracker.get('Gemini_PID')))}</small></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>TMDB API key</label><input type='password' name='TMDB_APIKEY' placeholder='Leave blank to keep current value'><small>Current state: {_esc(_secret_state(tracker.get('TMDB_APIKEY')))}</small></div>"
        f"<div class='field'><label>IMGBB key</label><input type='password' name='IMGBB_KEY' placeholder='Leave blank to keep current value'><small>Current state: {_esc(_secret_state(tracker.get('IMGBB_KEY')))}</small></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>Watcher path</label><input name='WATCHER_PATH' value='{_esc(prefs.get('WATCHER_PATH', ''))}'></div>"
        f"<div class='field'><label>Destination path</label><input name='WATCHER_DESTINATION_PATH' value='{_esc(prefs.get('WATCHER_DESTINATION_PATH', ''))}'></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>Torrent archive path</label><input name='TORRENT_ARCHIVE_PATH' value='{_esc(prefs.get('TORRENT_ARCHIVE_PATH', ''))}'></div>"
        f"<div class='field'><label>Cache path</label><input name='CACHE_PATH' value='{_esc(prefs.get('CACHE_PATH', ''))}'></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>Watcher interval (s)</label><input name='WATCHER_INTERVAL' value='{_esc(prefs.get('WATCHER_INTERVAL', 60))}'></div>"
        f"<div class='field'><label>Number of screenshots</label><input name='NUMBER_OF_SCREENSHOTS' value='{_esc(prefs.get('NUMBER_OF_SCREENSHOTS', 4))}'></div>"
        "</div>"
        "<div class='fields two'>"
        "<div class='field'><label>Torrent client</label><select name='TORRENT_CLIENT'>"
        f"<option value='qbittorrent'{' selected' if str(torrent.get('TORRENT_CLIENT', '')).lower() == 'qbittorrent' else ''}>qBittorrent</option>"
        f"<option value='transmission'{' selected' if str(torrent.get('TORRENT_CLIENT', '')).lower() == 'transmission' else ''}>Transmission</option>"
        f"<option value='rtorrent'{' selected' if str(torrent.get('TORRENT_CLIENT', '')).lower() == 'rtorrent' else ''}>rTorrent</option>"
        "</select></div>"
        f"<div class='field'><label>Tag</label><input name='TAG' value='{_esc(torrent.get('TAG', ''))}'></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>qBittorrent host</label><input name='QBIT_HOST' value='{_esc(torrent.get('QBIT_HOST', ''))}'></div>"
        f"<div class='field'><label>qBittorrent port</label><input name='QBIT_PORT' value='{_esc(torrent.get('QBIT_PORT', ''))}'></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>Transmission host</label><input name='TRASM_HOST' value='{_esc(torrent.get('TRASM_HOST', ''))}'></div>"
        f"<div class='field'><label>Transmission port</label><input name='TRASM_PORT' value='{_esc(torrent.get('TRASM_PORT', ''))}'></div>"
        "</div>"
        "<div class='fields two'>"
        f"<div class='field'><label>rTorrent host</label><input name='RTORR_HOST' value='{_esc(torrent.get('RTORR_HOST', ''))}'></div>"
        f"<div class='field'><label>rTorrent port</label><input name='RTORR_PORT' value='{_esc(torrent.get('RTORR_PORT', ''))}'></div>"
        "</div>"
        "<div class='checks'>"
        f"<label class='check'><input type='checkbox' name='DUPLICATE_ON'{_checked(prefs.get('DUPLICATE_ON'))}><span>Check duplicates before upload</span></label>"
        f"<label class='check'><input type='checkbox' name='SKIP_DUPLICATE'{_checked(prefs.get('SKIP_DUPLICATE'))}><span>Skip upload when duplicate is found</span></label>"
        f"<label class='check'><input type='checkbox' name='ANON'{_checked(prefs.get('ANON'))}><span>Upload as anonymous</span></label>"
        f"<label class='check'><input type='checkbox' name='PERSONAL_RELEASE'{_checked(prefs.get('PERSONAL_RELEASE'))}><span>Default personal release</span></label>"
        "</div><div class='actions'><button class='button' type='submit'>Save app settings</button></div></div></form></section>"
        "<section class='panel'><div class='panel-head'><div><h2>Local folders</h2><p>Host-side references saved in unit3dup-web.json.</p></div></div>"
        "<form method='post' action='/settings/local'><div class='fields'>"
        f"<div class='field'><label>Local config folder</label><input name='local_config_path' value='{_esc(local['local_config_path'])}'></div>"
        f"<div class='field'><label>Local watch folder</label><input name='local_watch_path' value='{_esc(local['local_watch_path'])}'></div>"
        f"<div class='field'><label>Local destination folder</label><input name='local_done_path' value='{_esc(local['local_done_path'])}'></div>"
        f"<div class='field'><label>Local data folder</label><input name='local_data_path' value='{_esc(local['local_data_path'])}'></div>"
        "<div class='actions'><button class='button' type='submit'>Save local paths</button></div>"
        "<p class='muted'>These values are informative and do not change Docker bind mounts by themselves.</p>"
        "</div></form></section></div>"
    )
    return _layout("Unit3Dup Settings", "settings", body, message)


def _redirect(path: str, message: str = "") -> RedirectResponse:
    target = path if not message else f"{path}?message={quote(message)}"
    return RedirectResponse(url=target, status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(message: str = "") -> HTMLResponse:
    return HTMLResponse(_render_dashboard(message))


@app.get("/settings", response_class=HTMLResponse)
async def settings(message: str = "") -> HTMLResponse:
    return HTMLResponse(_render_settings(message))


@app.post("/settings/app")
async def save_app_settings(request: Request) -> RedirectResponse:
    form = await _request_data(request)
    current = _load_raw_config()
    backup = json.loads(json.dumps(current))
    tracker = current["tracker_config"]
    prefs = current["user_preferences"]
    torrent = current["torrent_client_config"]

    tracker["Gemini_URL"] = str(form.get("Gemini_URL", tracker.get("Gemini_URL", ""))).strip()
    tracker["MULTI_TRACKER"] = [item.strip().lower() for item in str(form.get("MULTI_TRACKER", "")).split(",") if item.strip()] or tracker.get("MULTI_TRACKER", ["gemini"])
    for key in ("Gemini_APIKEY", "Gemini_PID", "TMDB_APIKEY", "IMGBB_KEY"):
        value = str(form.get(key, "")).strip()
        if value:
            tracker[key] = value

    prefs["WATCHER_PATH"] = str(form.get("WATCHER_PATH", prefs.get("WATCHER_PATH", ""))).strip()
    prefs["WATCHER_DESTINATION_PATH"] = str(form.get("WATCHER_DESTINATION_PATH", prefs.get("WATCHER_DESTINATION_PATH", ""))).strip()
    prefs["TORRENT_ARCHIVE_PATH"] = str(form.get("TORRENT_ARCHIVE_PATH", prefs.get("TORRENT_ARCHIVE_PATH", ""))).strip()
    prefs["CACHE_PATH"] = str(form.get("CACHE_PATH", prefs.get("CACHE_PATH", ""))).strip()
    prefs["WATCHER_INTERVAL"] = _coerce_int(form.get("WATCHER_INTERVAL", prefs.get("WATCHER_INTERVAL", 60)), 60)
    prefs["NUMBER_OF_SCREENSHOTS"] = _coerce_int(form.get("NUMBER_OF_SCREENSHOTS", prefs.get("NUMBER_OF_SCREENSHOTS", 4)), 4)
    prefs["DUPLICATE_ON"] = "true" if form.get("DUPLICATE_ON") else "false"
    prefs["SKIP_DUPLICATE"] = "true" if form.get("SKIP_DUPLICATE") else "false"
    prefs["ANON"] = "true" if form.get("ANON") else "false"
    prefs["PERSONAL_RELEASE"] = "true" if form.get("PERSONAL_RELEASE") else "false"

    torrent["TORRENT_CLIENT"] = str(form.get("TORRENT_CLIENT", torrent.get("TORRENT_CLIENT", "qbittorrent"))).strip()
    torrent["TAG"] = str(form.get("TAG", torrent.get("TAG", ""))).strip()
    for key in ("QBIT_HOST", "QBIT_PORT", "TRASM_HOST", "TRASM_PORT", "RTORR_HOST", "RTORR_PORT"):
        torrent[key] = str(form.get(key, torrent.get(key, ""))).strip()

    try:
        _write_json(DEFAULT_JSON_PATH, current)
        _reload_config()
    except SystemExit:
        _write_json(DEFAULT_JSON_PATH, backup)
        _reload_config()
        return _redirect("/settings", "Invalid configuration. Previous values restored.")
    except Exception:
        _write_json(DEFAULT_JSON_PATH, backup)
        _reload_config()
        return _redirect("/settings", "Failed to save app settings.")
    return _redirect("/settings", "App settings saved.")


@app.post("/settings/local")
async def save_local_settings(request: Request) -> RedirectResponse:
    form = await _request_data(request)
    _write_json(
        WEB_SETTINGS_PATH,
        {
            "local_config_path": str(form.get("local_config_path", "")).strip(),
            "local_watch_path": str(form.get("local_watch_path", "")).strip(),
            "local_done_path": str(form.get("local_done_path", "")).strip(),
            "local_data_path": str(form.get("local_data_path", "")).strip(),
        },
    )
    return _redirect("/settings", "Local paths saved.")


@app.post("/jobs/watcher/dry-run")
async def start_watcher_dry_run() -> RedirectResponse:
    try:
        JOB_STORE.start("Dry run watcher", lambda job: _run_watcher_job(job, mode="dry-run"))
    except RuntimeError as exc:
        return _redirect("/", str(exc))
    return _redirect("/")


@app.post("/jobs/watcher/upload-no-seed")
async def start_watcher_upload_no_seed() -> RedirectResponse:
    try:
        JOB_STORE.start("Upload watcher without seeding", lambda job: _run_watcher_job(job, mode="upload-no-seed"))
    except RuntimeError as exc:
        return _redirect("/", str(exc))
    return _redirect("/")


@app.post("/jobs/watcher/upload-seed")
async def start_watcher_upload_seed() -> RedirectResponse:
    try:
        JOB_STORE.start("Upload watcher with seeding", lambda job: _run_watcher_job(job, mode="upload-seed"))
    except RuntimeError as exc:
        return _redirect("/", str(exc))
    return _redirect("/")


@app.post("/jobs/items/{entry_name}/dry-run")
async def start_entry_dry_run(entry_name: str) -> RedirectResponse:
    try:
        JOB_STORE.start(f"Dry run: {entry_name}", lambda job: _run_watcher_job(job, mode="dry-run", entry_name=entry_name))
    except RuntimeError as exc:
        return _redirect("/", str(exc))
    return _redirect("/")


@app.post("/jobs/items/{entry_name}/upload-no-seed")
async def start_entry_upload_no_seed(entry_name: str) -> RedirectResponse:
    try:
        JOB_STORE.start(f"Upload no seed: {entry_name}", lambda job: _run_watcher_job(job, mode="upload-no-seed", entry_name=entry_name))
    except RuntimeError as exc:
        return _redirect("/", str(exc))
    return _redirect("/")


@app.post("/jobs/items/{entry_name}/upload-seed")
async def start_entry_upload_seed(entry_name: str) -> RedirectResponse:
    try:
        JOB_STORE.start(f"Upload and seed: {entry_name}", lambda job: _run_watcher_job(job, mode="upload-seed", entry_name=entry_name))
    except RuntimeError as exc:
        return _redirect("/", str(exc))
    return _redirect("/")


@app.post("/jobs/clear")
async def clear_job() -> RedirectResponse:
    try:
        JOB_STORE.clear()
    except RuntimeError as exc:
        return _redirect("/", str(exc))
    return _redirect("/")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "unit3dup.web.main:app",
        host=os.getenv("UNIT3DUP_WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("UNIT3DUP_WEB_PORT", "8787")),
        reload=False,
        access_log=False,
    )
