from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import END, LEFT, W, StringVar, Tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from unistream_client import PLCError, check_plc, reboot_plc, validate_plc


DEFAULT_PLC_IP = "192.168.1.6"
DEFAULT_PLC_PORT = 8001
APP_ID = "lioil.UnistreamPLCReboot"
ICON_PATH = Path(__file__).with_name("lioil.ico")


class RebootApp:
    def __init__(self) -> None:
        set_windows_app_id(APP_ID)
        self.root = Tk()
        self.root.title("Unistream PLC Reboot")
        self.root.geometry("980x620")
        self.root.minsize(920, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_window_icon(self.root, ICON_PATH)

        self.ip_var = StringVar(value=DEFAULT_PLC_IP)
        self.port_var = StringVar(value=str(DEFAULT_PLC_PORT))
        self.password_var = StringVar()
        self.status_var = StringVar(value="Ready. Enter the PLC password, or leave it blank only if the PLC has no password.")

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

        ttk.Label(top, text="Port").grid(row=1, column=0, sticky=W, padx=(0, 10), pady=(0, 8))
        ttk.Entry(top, textvariable=self.port_var).grid(row=1, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(top, text="PLC Password").grid(row=2, column=0, sticky=W, padx=(0, 10), pady=(0, 8))
        ttk.Entry(top, textvariable=self.password_var, show="*").grid(row=2, column=1, sticky="ew", pady=(0, 8))

        hint = "Blank password means: send an empty PLC password. This clean-room build does not use UniLogic's saved password store."
        ttk.Label(top, text=hint, foreground="#555", wraplength=900).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(top)
        buttons.grid(row=4, column=0, columnspan=3, sticky=W)

        self.check_button = ttk.Button(buttons, text="Check PLC", command=self.on_check)
        self.check_button.pack(side=LEFT)

        self.validate_button = ttk.Button(buttons, text="Validate", command=self.on_validate)
        self.validate_button.pack(side=LEFT, padx=8)

        self.reboot_button = ttk.Button(buttons, text="Reboot PLC", command=self.on_reboot)
        self.reboot_button.pack(side=LEFT, padx=8)

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
        self.log.insert(END, "Press Reboot PLC when you're ready.\n")
        self.log.configure(state="disabled")

    def _on_window_ready(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def on_close(self) -> None:
        self.root.destroy()

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(END, text.rstrip() + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", END)
        self.log.configure(state="disabled")

    def set_busy(self, busy: bool, status: str) -> None:
        state = "disabled" if busy else "normal"
        self.check_button.configure(state=state)
        self.validate_button.configure(state=state)
        self.reboot_button.configure(state=state)
        self.status_var.set(status)

    def get_inputs(self) -> tuple[str, int, str | None]:
        ip = self.ip_var.get().strip()
        if not ip:
            raise PLCError("PLC IP cannot be empty.")

        try:
            port = int(self.port_var.get().strip())
        except ValueError as exc:
            raise PLCError("Port must be an integer.") from exc

        password = self.password_var.get().strip()
        return ip, port, password or None

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

    def run_async(self, title: str, action, *, check_session: bool = True) -> None:
        try:
            ip, port, password = self.get_inputs()
        except PLCError as exc:
            messagebox.showerror("Input Error", str(exc), parent=self.root)
            return

        if check_session:
            blocking_session = self.check_blocking_session(ip, port)
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
        self.append_log(f"\n[{title}] ip={ip} port={port} password={password_mode}")
        self.set_busy(True, f"{title}...")

        def worker() -> None:
            try:
                result = action(ip, port, password)
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
        self.set_busy(False, "Ready.")

        if stdout:
            self.append_log(stdout)
        if stderr:
            self.append_log(f"[stderr]\n{stderr}")

        if returncode == 0:
            self.append_log(f"[{title}] success")
            if title == "Check PLC":
                messagebox.showinfo("Check PLC", "PLC communication looks normal.", parent=self.root)
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

    def run(self) -> None:
        self.root.mainloop()


def run_cli(args: argparse.Namespace) -> int:
    password = args.password.strip() if args.password else None
    if args.command == "check":
        result = check_plc(args.ip, args.port, password)
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
    sub = parser.add_subparsers(dest="command")

    for name in ("check", "validate", "reboot"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--ip", default=DEFAULT_PLC_IP)
        cmd.add_argument("--port", type=int, default=DEFAULT_PLC_PORT)
        cmd.add_argument("--password", default="")

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

    app = RebootApp()
    app.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
