from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from tkinter import END, LEFT, W, Listbox, StringVar, Tk
from tkinter import messagebox, ttk

import pystray
from PIL import Image

from unistream_client import PLCError, check_opcua, check_plc, reboot_plc, validate_plc


CONFIG_FILE_NAME = "config.json"
VALID_STARTUP_COMMANDS = {"gui", "check", "validate", "reboot", "check-opcua"}
APP_TITLE = "Unistream PLC Reboot"
APP_ID = "lioil.UnistreamPLCReboot"
ICON_FILE_NAME = "lioil.ico"


def get_app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PLCError(f"Invalid {path.name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise PLCError(f"{path.name} must contain a JSON object.")
    return raw


def load_config(base_dir: Path) -> tuple[dict[str, Any], Path]:
    config_path = base_dir / CONFIG_FILE_NAME
    if not config_path.exists():
        resource_path = get_resource_path(CONFIG_FILE_NAME)
        if resource_path.exists():
            config_path = resource_path
        else:
            raise PLCError(f"Missing {CONFIG_FILE_NAME} in {base_dir}.")

    config = read_json_object(config_path)
    validate_config(config)
    return config, config_path


def validate_config(config: dict[str, Any]) -> None:
    try:
        plc = config["plc"]
        startup = config["startup"]
        run_monitor = config["run_monitor"]
    except KeyError as exc:
        raise PLCError(f"config.json is missing required section: {exc.args[0]}") from exc

    for section_name, section in (("plc", plc), ("startup", startup), ("run_monitor", run_monitor)):
        if not isinstance(section, dict):
            raise PLCError(f"config.json section '{section_name}' must be an object.")

    for key in ("ip", "api_port", "opc_ua_port", "password"):
        if key not in plc:
            raise PLCError(f"config.json plc.{key} is required.")
    for key in ("command", "auto_run_monitor", "start_in_tray"):
        if key not in startup:
            raise PLCError(f"config.json startup.{key} is required.")
    for key in ("check_interval_seconds", "cooldown_seconds"):
        if key not in run_monitor:
            raise PLCError(f"config.json run_monitor.{key} is required.")

    api_port = int(plc["api_port"])
    opc_port = int(plc["opc_ua_port"])
    check_interval_seconds = int(run_monitor["check_interval_seconds"])
    cooldown_seconds = int(run_monitor["cooldown_seconds"])
    command = str(startup["command"])

    if not 1 <= api_port <= 65535:
        raise PLCError("config.json plc.api_port must be between 1 and 65535.")
    if not 1 <= opc_port <= 65535:
        raise PLCError("config.json plc.opc_ua_port must be between 1 and 65535.")
    if check_interval_seconds < 1:
        raise PLCError("config.json run_monitor.check_interval_seconds must be >= 1.")
    if cooldown_seconds < 0:
        raise PLCError("config.json run_monitor.cooldown_seconds must be >= 0.")
    if command not in VALID_STARTUP_COMMANDS:
        valid = ", ".join(sorted(VALID_STARTUP_COMMANDS))
        raise PLCError(f"config.json startup.command must be one of: {valid}.")


def get_resource_path(name: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", get_app_base_dir()))
    return base_dir / name


class RebootApp:
    def __init__(self, config: dict[str, Any], *, auto_run: bool = False, start_in_tray: bool = False) -> None:
        self.config = config
        self.app_title = APP_TITLE
        self.app_id = APP_ID
        self.plc_port = int(config["plc"]["api_port"])
        self.opc_ua_port = int(config["plc"]["opc_ua_port"])
        self.default_password = str(config["plc"]["password"])
        self.run_check_interval_seconds = int(config["run_monitor"]["check_interval_seconds"])
        self.run_cooldown_seconds = int(config["run_monitor"]["cooldown_seconds"])
        self.icon_path = get_resource_path(ICON_FILE_NAME)

        set_windows_app_id(self.app_id)
        self.root = Tk()
        self.root.title(self.app_title)
        self.root.geometry("680x300")
        self.root.minsize(660, 260)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_window_icon(self.root, self.icon_path)
        self.root.bind("<Unmap>", self._on_window_state_change)

        self.ip_var = StringVar(value=str(config["plc"]["ip"]))
        self.opc_port_var = StringVar(value=str(self.opc_ua_port))
        self.password_var = StringVar(value=self.default_password)
        self.status_var = StringVar(value="")

        self.password_visible = False
        self.is_busy = False
        self.run_enabled = False
        self.run_stop_event = threading.Event()
        self.run_thread: threading.Thread | None = None
        self.run_config: tuple[str, int, int, str | None] | None = None
        self.cooldown_until = 0.0
        self._closing = False

        self.tray_icon: Any = None
        self.tray_image: Image.Image | None = None
        self.is_tray_visible = False
        self.auto_run_on_start = auto_run
        self.start_in_tray = start_in_tray

        self._build_ui()
        self.root.after(50, self._on_window_ready)

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = ttk.Frame(root, padding=16)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=0)
        top.columnconfigure(1, weight=1)
        top.columnconfigure(2, weight=0)

        ttk.Label(top, text="PLC IP").grid(row=0, column=0, sticky=W, padx=(0, 10), pady=(0, 8))
        ttk.Entry(top, textvariable=self.ip_var).grid(row=0, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(top, text="OPC UA Port").grid(row=1, column=0, sticky=W, padx=(0, 10), pady=(0, 8))
        ttk.Entry(top, textvariable=self.opc_port_var).grid(row=1, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(top, text="PLC Password").grid(row=2, column=0, sticky=W, padx=(0, 10), pady=(0, 8))
        password_row = ttk.Frame(top)
        password_row.grid(row=2, column=1, sticky="ew", pady=(0, 8))
        password_row.columnconfigure(0, weight=1)

        self.password_entry = ttk.Entry(password_row, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=0, column=0, sticky="ew")
        self.password_toggle_button = ttk.Button(
            password_row,
            text="Show",
            width=6,
            command=self.toggle_password_visibility,
        )
        self.password_toggle_button.grid(row=0, column=1, padx=(6, 0))

        buttons = ttk.Frame(top)
        buttons.grid(row=3, column=0, columnspan=3, pady=(6, 0))

        self.check_button = ttk.Button(buttons, text="Check PLC", command=self.on_check)
        self.check_button.pack(side=LEFT)

        self.validate_button = ttk.Button(buttons, text="Validate", command=self.on_validate)
        self.validate_button.pack(side=LEFT, padx=8)

        self.check_opcua_button = ttk.Button(buttons, text="Check OPC UA", command=self.on_check_opcua)
        self.check_opcua_button.pack(side=LEFT, padx=8)

        self.reboot_button = ttk.Button(buttons, text="Reboot PLC", command=self.on_reboot)
        self.reboot_button.pack(side=LEFT, padx=8)

        self.run_button = ttk.Button(buttons, text="RUN", command=self.toggle_run)
        self.run_button.pack(side=LEFT, padx=8)

        ttk.Button(buttons, text="Clear", command=self.clear_log).pack(side=LEFT)

        self.status_label = ttk.Label(top, textvariable=self.status_var, anchor=W)
        self.status_label.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        activity_frame = ttk.Frame(root, padding=(16, 8, 16, 16))
        activity_frame.grid(row=1, column=0, sticky="nsew")
        activity_frame.rowconfigure(0, weight=1)
        activity_frame.columnconfigure(0, weight=1)

        self.activity_list = Listbox(activity_frame, height=6, font=("Consolas", 10))
        self.activity_list.grid(row=0, column=0, sticky="nsew")
        self.refresh_controls()
        self._set_status("")

    def _on_window_ready(self) -> None:
        try:
            if self.start_in_tray:
                self.minimize_to_tray()
            else:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
        except Exception:
            pass

        if self.auto_run_on_start:
            self.root.after(100, self.start_run_monitor)

    def on_close(self) -> None:
        self._closing = True
        self.stop_run_monitor(join=True, notify=False)
        self.hide_tray_icon()
        self.root.destroy()

    def _on_window_state_change(self, _event=None) -> None:
        if self._closing:
            return
        try:
            if self.root.state() == "iconic":
                self.minimize_to_tray()
        except Exception:
            pass

    def minimize_to_tray(self) -> None:
        if self.is_tray_visible:
            return
        self.show_tray_icon()
        self.root.withdraw()
        self.append_log("[UI] Minimized to system tray.")

    def restore_from_tray(self) -> None:
        self.hide_tray_icon()
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def show_tray_icon(self) -> None:
        if self.is_tray_visible:
            return

        if self.icon_path.exists() and self.tray_image is None:
            self.tray_image = Image.open(self.icon_path)

        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show_window),
            pystray.MenuItem("Stop RUN", self._tray_stop_run),
            pystray.MenuItem("Exit", self._tray_exit),
        )
        icon = pystray.Icon("UnistreamPLCReboot", self.tray_image, self.app_title, menu)
        self.tray_icon = icon

        thread = threading.Thread(target=icon.run, daemon=True)
        thread.start()
        self.is_tray_visible = True

    def hide_tray_icon(self) -> None:
        if not self.tray_icon:
            self.is_tray_visible = False
            return

        icon = self.tray_icon
        self.tray_icon = None
        self.is_tray_visible = False
        try:
            icon.stop()
        except Exception:
            pass

    def _tray_show_window(self, _icon, _item) -> None:
        self.root.after(0, self.restore_from_tray)

    def _tray_stop_run(self, _icon, _item) -> None:
        self.root.after(0, lambda: self.stop_run_monitor(join=False, notify=True))

    def _tray_exit(self, _icon, _item) -> None:
        self.root.after(0, self.on_close)

    def append_log(self, text: str) -> None:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            self.add_event(line)

    def clear_log(self) -> None:
        self.activity_list.delete(0, END)
        self._set_status("")

    def add_event(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        entry = f"{timestamp}  {message}"
        self.activity_list.insert(0, entry)
        if self.activity_list.size() > 8:
            self.activity_list.delete(8, END)

    def append_log_threadsafe(self, text: str) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, lambda: self.append_log(text))
        except Exception:
            pass

    def set_status_threadsafe(self, text: str) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, lambda: self._set_status(text))
        except Exception:
            pass

    def set_busy(self, busy: bool, status: str) -> None:
        self.is_busy = busy
        self.refresh_controls()
        self._set_status(status)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        if text:
            self.status_label.grid()
        else:
            self.status_label.grid_remove()

    def refresh_controls(self) -> None:
        manual_state = "normal"
        if self.is_busy or self.run_enabled:
            manual_state = "disabled"

        self.check_button.configure(state=manual_state)
        self.validate_button.configure(state=manual_state)
        self.check_opcua_button.configure(state=manual_state)
        self.reboot_button.configure(state=manual_state)

        self.run_button.configure(text="Stop RUN" if self.run_enabled else "RUN")
        if self.is_busy and not self.run_enabled:
            self.run_button.configure(state="disabled")
        else:
            self.run_button.configure(state="normal")

    def toggle_password_visibility(self) -> None:
        self.password_visible = not self.password_visible
        self.password_entry.configure(show="" if self.password_visible else "*")
        self.password_toggle_button.configure(text="Hide" if self.password_visible else "Show")

    def get_inputs(self) -> tuple[str, int, int, str | None]:
        ip = self.ip_var.get().strip()
        if not ip:
            raise PLCError("PLC IP cannot be empty.")

        try:
            opc_port = int(self.opc_port_var.get().strip())
        except ValueError as exc:
            raise PLCError("OPC UA port must be an integer.") from exc

        if not 1 <= opc_port <= 65535:
            raise PLCError("OPC UA port must be between 1 and 65535.")

        password = self.password_var.get().strip()
        return ip, self.plc_port, opc_port, password or None

    def check_blocking_session(self, ip: str, port: int) -> str | None:
        command = rf"""
$conns = Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
    Where-Object {{ $_.RemoteAddress -eq '{ip}' -and $_.RemotePort -eq {port} }};
$items = foreach ($c in $conns) {{
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue;
    [PSCustomObject]@{{
        pid = $c.OwningProcess
        name = if ($p) {{ $p.ProcessName }} else {{ '' }}
    }}
}};
$items | ConvertTo-Json -Compress
"""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

        if isinstance(data, dict):
            data = [data]

        blockers = []
        for item in data:
            pid = item.get("pid")
            name = item.get("name") or "unknown"
            if pid:
                blockers.append(f"{name} (PID {pid})")

        if blockers:
            return ", ".join(blockers)
        return None

    def run_async(self, title: str, action, *, check_session: bool = True, is_opcua_action: bool = False) -> None:
        try:
            ip, plc_port, opc_port, password = self.get_inputs()
        except PLCError as exc:
            messagebox.showerror("Input Error", str(exc), parent=self.root)
            return

        if check_session:
            blocking_session = self.check_blocking_session(ip, plc_port)
            if blocking_session:
                self.add_event(f"{title} blocked: {blocking_session}")
                messagebox.showwarning(
                    "Session In Use",
                    "Another process is already connected to this PLC.\n\n"
                    f"{blocking_session}\n\n"
                    "Close or disconnect that session first, then try again.",
                    parent=self.root,
                )
                return

        password_mode = "provided" if password else "empty"
        self.set_busy(True, f"{title}...")
        self.add_event(f"{title} started ({ip}, password={password_mode})")

        def worker() -> None:
            try:
                if is_opcua_action:
                    result = action(ip, opc_port)
                else:
                    result = action(ip, plc_port, password)
                self.root.after(
                    0,
                    lambda: self._finish_action(
                        title,
                        result.returncode,
                        result.stdout.strip(),
                        result.stderr.strip(),
                    ),
                )
            except Exception as exc:  # pragma: no cover - UI path
                self.root.after(0, lambda: self._finish_action(title, 99, "", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_action(self, title: str, returncode: int, stdout: str, stderr: str) -> None:
        next_status = (
            f"RUN monitoring OPC UA every {self.run_check_interval_seconds} seconds."
            if self.run_enabled
            else ""
        )
        self.set_busy(False, next_status)

        if stderr:
            self.add_event(f"{title} error: {stderr.splitlines()[0]}")

        if returncode == 0:
            self.add_event(f"{title} success")
            if title == "Check PLC":
                messagebox.showinfo("Check PLC", "PLC communication looks normal.", parent=self.root)
            elif title == "Check OPC UA":
                messagebox.showinfo("Check OPC UA", "OPC UA communication looks normal.", parent=self.root)
            elif title == "Reboot":
                messagebox.showinfo("Reboot Sent", "Reboot command sent successfully.", parent=self.root)
            return

        failure_detail = self._summarize_output(stdout) or f"exit code {returncode}"
        self.add_event(f"{title} failed: {failure_detail}")
        messagebox.showerror(title, f"{title} failed.\nSee log for details.", parent=self.root)

    def _summarize_output(self, output: str) -> str:
        for line in output.splitlines():
            clean = line.strip()
            if not clean:
                continue
            if clean.startswith("Attempting "):
                continue
            if clean in {"OPC UA communication OK", "PLC communication OK", "Validated"}:
                return clean
            if "failed" in clean.lower() or "unauthorized" in clean.lower():
                return clean
            if clean.startswith("reboot="):
                return clean
        return ""

    def on_check(self) -> None:
        self.run_async("Check PLC", check_plc, check_session=False)

    def on_validate(self) -> None:
        self.run_async("Validate", validate_plc)

    def on_reboot(self) -> None:
        if not messagebox.askyesno("Confirm Reboot", "Send reboot command to the PLC now?", parent=self.root):
            return
        self.run_async("Reboot", reboot_plc)

    def on_check_opcua(self) -> None:
        self.run_async("Check OPC UA", check_opcua, check_session=False, is_opcua_action=True)

    def toggle_run(self) -> None:
        if self.run_enabled:
            self.stop_run_monitor(join=False, notify=True)
        else:
            self.start_run_monitor()

    def start_run_monitor(self) -> None:
        if self.run_enabled:
            return

        try:
            self.run_config = self.get_inputs()
        except PLCError as exc:
            messagebox.showerror("Input Error", str(exc), parent=self.root)
            return

        self.run_stop_event.clear()
        self.cooldown_until = 0.0
        self.run_enabled = True
        self.refresh_controls()

        ip, plc_port, opc_port, password = self.run_config
        password_mode = "provided" if password else "empty"
        self.add_event(f"RUN started ({ip}, password={password_mode})")
        self._set_status(f"RUN monitoring OPC UA every {self.run_check_interval_seconds} seconds.")

        self.run_thread = threading.Thread(target=self._run_monitor_loop, daemon=True)
        self.run_thread.start()

    def stop_run_monitor(self, *, join: bool, notify: bool) -> None:
        if not self.run_enabled and not self.run_thread:
            return

        self.run_enabled = False
        self.run_stop_event.set()
        self.refresh_controls()

        if join and self.run_thread and self.run_thread.is_alive():
            self.run_thread.join(timeout=2)
        self.run_thread = None

        if notify:
            self.add_event("RUN stopped")
            self._set_status("")

    def _run_monitor_loop(self) -> None:
        if not self.run_config:
            return

        ip, plc_port, opc_port, password = self.run_config

        while not self.run_stop_event.is_set():
            now = time.time()
            if now < self.cooldown_until:
                remaining = int(self.cooldown_until - now)
                self.set_status_threadsafe(f"RUN cooldown: {remaining}s remaining before next OPC UA check.")
                self.run_stop_event.wait(1)
                continue

            opc_result = check_opcua(ip, opc_port)
            if opc_result.returncode == 0:
                self.set_status_threadsafe(
                    f"RUN monitoring OPC UA every {self.run_check_interval_seconds} seconds."
                )
                self.run_stop_event.wait(self.run_check_interval_seconds)
                continue

            reboot_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            self.append_log_threadsafe(f"AUTO REBOOT at {reboot_time}")

            reboot_result = reboot_plc(ip, plc_port, password)
            if reboot_result.stderr:
                self.append_log_threadsafe(f"Reboot error: {reboot_result.stderr.strip()}")

            if reboot_result.returncode == 0:
                self.append_log_threadsafe(f"Reboot sent at {reboot_time}")
            else:
                detail = self._summarize_output(reboot_result.stdout) or f"exit code {reboot_result.returncode}"
                self.append_log_threadsafe(f"Auto reboot failed: {detail}")

            self.cooldown_until = time.time() + self.run_cooldown_seconds
            self.set_status_threadsafe(
                f"RUN cooldown: {self.run_cooldown_seconds}s remaining before next OPC UA check."
            )

        self.set_status_threadsafe("")

    def run(self) -> None:
        self.root.mainloop()


def run_cli(args: argparse.Namespace) -> int:
    password = args.password.strip() if args.password else None
    if args.command == "check":
        result = check_plc(args.ip, args.port, password)
    elif args.command == "check-opcua":
        result = check_opcua(args.ip, args.opc_port)
    elif args.command == "validate":
        result = validate_plc(args.ip, args.port, password)
    else:
        result = reboot_plc(args.ip, args.port, password)

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result.returncode


def build_parser(config: dict[str, Any]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unistream PLC reboot helper")
    parser.add_argument(
        "-run",
        "--run",
        action="store_true",
        help="Start GUI and automatically enable RUN monitoring.",
    )
    parser.add_argument(
        "-tray",
        "--tray",
        action="store_true",
        help="Start GUI minimized to system tray.",
    )
    sub = parser.add_subparsers(dest="command")

    for name in ("check", "validate", "reboot"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--ip", default=str(config["plc"]["ip"]))
        cmd.add_argument("--port", type=int, default=int(config["plc"]["api_port"]))
        cmd.add_argument("--password", default=str(config["plc"]["password"]))

    opc_cmd = sub.add_parser("check-opcua")
    opc_cmd.add_argument("--ip", default=str(config["plc"]["ip"]))
    opc_cmd.add_argument("--opc-port", type=int, default=int(config["plc"]["opc_ua_port"]))

    return parser


def build_namespace_from_config(config: dict[str, Any]) -> argparse.Namespace:
    startup = config["startup"]
    plc = config["plc"]
    command = str(startup["command"])

    return argparse.Namespace(
        command=command if command != "gui" else None,
        run=bool(startup["auto_run_monitor"]),
        tray=bool(startup["start_in_tray"]),
        ip=str(plc["ip"]),
        port=int(plc["api_port"]),
        opc_port=int(plc["opc_ua_port"]),
        password=str(plc["password"]),
    )


def set_windows_app_id(app_id: str) -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def apply_window_icon(root: Tk, icon_path: Path) -> None:
    if not icon_path.exists():
        return
    try:
        root.iconbitmap(default=str(icon_path))
    except Exception:
        pass


def main() -> int:
    base_dir = get_app_base_dir()
    config, config_path = load_config(base_dir)
    parser = build_parser(config)
    args = parser.parse_args()

    # If no CLI arguments are provided, config.json fully drives startup behavior.
    if len(sys.argv) == 1:
        args = build_namespace_from_config(config)

    if args.command:
        return run_cli(args)

    app = RebootApp(config, auto_run=args.run, start_in_tray=args.tray)
    if len(sys.argv) == 1:
        print(f"Loaded startup settings from {config_path}")
    app.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
