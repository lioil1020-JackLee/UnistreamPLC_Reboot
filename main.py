from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from tkinter import END, LEFT, W, StringVar, Tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import pystray
from PIL import Image

from unistream_client import PLCError, check_opcua, check_plc, reboot_plc, validate_plc


DEFAULT_PLC_IP = "10.80.1.10"
DEFAULT_PLC_PORT = 8001
DEFAULT_OPCUA_PORT = 48484
DEFAULT_PLC_PASSWORD = "Blue0324!"
RUN_CHECK_INTERVAL_SECONDS = 10
RUN_COOLDOWN_SECONDS = 5 * 60
APP_ID = "lioil.UnistreamPLCReboot"
ICON_PATH = Path(__file__).with_name("lioil.ico")


class RebootApp:
    def __init__(self, *, auto_run: bool = False, start_in_tray: bool = False) -> None:
        set_windows_app_id(APP_ID)
        self.root = Tk()
        self.root.title("Unistream PLC Reboot")
        self.root.geometry("980x620")
        self.root.minsize(920, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_window_icon(self.root, ICON_PATH)
        self.root.bind("<Unmap>", self._on_window_state_change)

        self.ip_var = StringVar(value=DEFAULT_PLC_IP)
        self.opc_port_var = StringVar(value=str(DEFAULT_OPCUA_PORT))
        self.password_var = StringVar(value=DEFAULT_PLC_PASSWORD)
        self.status_var = StringVar(value="Ready. Enter the PLC password, or leave it blank only if the PLC has no password.")

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
        top.columnconfigure(1, weight=1)

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
        self.password_toggle_button = ttk.Button(password_row, text="👁", width=3, command=self.toggle_password_visibility)
        self.password_toggle_button.grid(row=0, column=1, padx=(6, 0))

        hint = "PLC HTTPS port is fixed at 8001. Blank password means: send an empty PLC password. This clean-room build does not use UniLogic's saved password store."
        ttk.Label(top, text=hint, foreground="#555", wraplength=900).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(top)
        buttons.grid(row=4, column=0, columnspan=3, sticky=W)

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

        ttk.Button(buttons, text="Clear Log", command=self.clear_log).pack(side=LEFT)

        ttk.Label(top, textvariable=self.status_var, anchor=W).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        log_frame = ttk.Frame(root, padding=(16, 12, 16, 16))
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log = ScrolledText(log_frame, wrap="word", font=("Consolas", 10))
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.insert(END, "Press Check PLC to verify basic HTTPS communication with the PLC.\n")
        self.log.insert(END, "Press Validate to test the HTTPS login + WebSocket flow.\n")
        self.log.insert(END, "Press Check OPC UA to verify OPC UA communication with the PLC.\n")
        self.log.insert(END, "Press RUN to monitor OPC UA every 10 seconds and auto-reboot on failure.\n")
        self.log.insert(END, "Press Reboot PLC when you're ready.\n")
        self.log.configure(state="disabled")
        self.refresh_controls()

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

        if ICON_PATH.exists() and self.tray_image is None:
            self.tray_image = Image.open(ICON_PATH)

        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show_window),
            pystray.MenuItem("Stop RUN", self._tray_stop_run),
            pystray.MenuItem("Exit", self._tray_exit),
        )
        icon = pystray.Icon("UnistreamPLCReboot", self.tray_image, "Unistream PLC Reboot", menu)
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
        self.log.configure(state="normal")
        self.log.insert(END, text.rstrip() + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", END)
        self.log.configure(state="disabled")

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
            self.root.after(0, lambda: self.status_var.set(text))
        except Exception:
            pass

    def set_busy(self, busy: bool, status: str) -> None:
        self.is_busy = busy
        self.refresh_controls()
        self.status_var.set(status)

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
        return ip, DEFAULT_PLC_PORT, opc_port, password or None

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
                self.append_log(f"\n[{title}] blocked: active PLC session detected: {blocking_session}")
                messagebox.showwarning(
                    "Session In Use",
                    "Another process is already connected to this PLC.\n\n"
                    f"{blocking_session}\n\n"
                    "Close or disconnect that session first, then try again.",
                    parent=self.root,
                )
                return

        password_mode = "provided" if password else "empty"
        self.append_log(f"\n[{title}] ip={ip} plc_port={plc_port} opc_ua_port={opc_port} password={password_mode}")
        self.set_busy(True, f"{title}...")

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
        next_status = "RUN monitoring OPC UA every 10 seconds." if self.run_enabled else "Ready."
        self.set_busy(False, next_status)

        if stdout:
            self.append_log(stdout)
        if stderr:
            self.append_log(f"[stderr]\n{stderr}")

        if returncode == 0:
            self.append_log(f"[{title}] success")
            if title == "Check PLC":
                messagebox.showinfo("Check PLC", "PLC communication looks normal.", parent=self.root)
            elif title == "Check OPC UA":
                messagebox.showinfo("Check OPC UA", "OPC UA communication looks normal.", parent=self.root)
            elif title == "Reboot":
                messagebox.showinfo("Reboot Sent", "Reboot command sent successfully.", parent=self.root)
            return

        self.append_log(f"[{title}] failed with exit code {returncode}")
        messagebox.showerror(title, f"{title} failed.\nSee log for details.", parent=self.root)

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
        self.append_log(
            f"\n[RUN] started ip={ip} plc_port={plc_port} opc_ua_port={opc_port} password={password_mode}"
        )
        self.status_var.set("RUN monitoring OPC UA every 10 seconds.")

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
            self.append_log("[RUN] stopped")
            self.status_var.set("Ready.")

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
            if opc_result.stdout:
                self.append_log_threadsafe(opc_result.stdout.strip())

            if opc_result.returncode == 0:
                self.append_log_threadsafe("[RUN] OPCUA_CHECK_OK")
                self.set_status_threadsafe("RUN monitoring OPC UA every 10 seconds.")
                self.run_stop_event.wait(RUN_CHECK_INTERVAL_SECONDS)
                continue

            self.append_log_threadsafe("[RUN] OPCUA_CHECK_FAIL")
            self.append_log_threadsafe("[RUN] AUTO_REBOOT_START")

            reboot_result = reboot_plc(ip, plc_port, password)
            if reboot_result.stdout:
                self.append_log_threadsafe(reboot_result.stdout.strip())
            if reboot_result.stderr:
                self.append_log_threadsafe(f"[stderr]\n{reboot_result.stderr.strip()}")

            if reboot_result.returncode == 0:
                self.append_log_threadsafe("[RUN] AUTO_REBOOT_DONE")
            else:
                self.append_log_threadsafe(f"[RUN] AUTO_REBOOT_FAILED returncode={reboot_result.returncode}")

            self.cooldown_until = time.time() + RUN_COOLDOWN_SECONDS
            self.set_status_threadsafe("RUN cooldown: 300s remaining before next OPC UA check.")

        self.set_status_threadsafe("Ready.")

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


def build_parser() -> argparse.ArgumentParser:
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
        cmd.add_argument("--ip", default=DEFAULT_PLC_IP)
        cmd.add_argument("--port", type=int, default=DEFAULT_PLC_PORT)
        cmd.add_argument("--password", default="")

    opc_cmd = sub.add_parser("check-opcua")
    opc_cmd.add_argument("--ip", default=DEFAULT_PLC_IP)
    opc_cmd.add_argument("--opc-port", type=int, default=DEFAULT_OPCUA_PORT)

    return parser


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


def get_resource_path(name: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_dir / name


def main() -> int:
    global ICON_PATH
    ICON_PATH = get_resource_path("lioil.ico")
    parser = build_parser()
    args = parser.parse_args()

    if args.command:
        return run_cli(args)

    app = RebootApp(auto_run=args.run, start_in_tray=args.tray)
    app.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
