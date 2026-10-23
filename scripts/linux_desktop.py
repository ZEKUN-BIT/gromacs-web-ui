"""Install and open the Linux desktop launcher without a terminal window."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LauncherError(Exception):
    pass


def project_id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(root.resolve())).hexdigest()


def state_directory(root: Path) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    directory = base / "gromacs-console" / project_id(root)[:12]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def launch_info(root: Path) -> dict:
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "0.0.0.0", "::", "::1"}:
        raise LauncherError("桌面启动请将 .env 中的 HOST 设为 127.0.0.1。")
    try:
        port = int(os.environ.get("PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError
        settings_path = root / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        runtime = Path(settings.get("runtime_root") or os.environ.get("GROMACS_WEB_ROOT") or root / "runtime")
    except (ValueError, TypeError):
        raise LauncherError("启动配置无效，请检查 .env 的 PORT 和 settings.json。") from None
    address = "[::1]" if host in {"::", "::1"} else "127.0.0.1"
    return {"url": f"http://{address}:{port}", "runtime": str((root / runtime).resolve())}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def manual_service_matches(root: Path, runtime: Path) -> bool:
    """Recognize run.sh via its kernel-held lock, without trusting a PID file."""
    try:
        info = (runtime / "web.lock").stat()
        for line in Path("/proc/locks").read_text().splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                continue
            major, minor, inode = fields[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) != (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino):
                continue
            return Path(f"/proc/{int(fields[4])}/cwd").resolve(strict=True) == root.resolve()
    except (OSError, ValueError):
        pass
    return False


def service_matches(root: Path, info: dict) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(info["url"] + "/api/version", timeout=1) as response:
            identity = json.loads(response.read(16 * 1024))
        return (
            isinstance(identity, dict)
            and bool(identity.get("version"))
            and (identity.get("installation_sha256") == project_id(root) or manual_service_matches(root, Path(info["runtime"])))
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def port_in_use(url: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_service(root: Path, directory: Path) -> str:
    if not (root / ".venv/bin/python").is_file():
        raise LauncherError("尚未安装 Python 环境，请先完成项目的 setup.sh 安装步骤。")
    with (directory / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            prepared = subprocess.run(["bash", str(root / "start.sh"), "--desktop-info"], cwd=root, capture_output=True, timeout=15)
            info = json.loads(prepared.stdout) if prepared.returncode == 0 else None
            if not isinstance(info, dict) or set(info) != {"url", "runtime"}:
                raise ValueError
        except (ValueError, subprocess.TimeoutExpired):
            raise LauncherError("无法读取启动配置，请检查项目中的 .env 和 settings.json。") from None
        if service_matches(root, info):
            return info["url"]
        if port_in_use(info["url"]):
            raise LauncherError("启动端口被其他服务占用。请在 .env 中修改 PORT，再重新打开。")
        log = directory / "launch.log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(log, 0o600)
        try:
            env = {**os.environ, "GROMACS_CONSOLE_INSTALLATION_SHA256": project_id(root)}
            child = subprocess.Popen(
                ["bash", str(root / "start.sh")],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=descriptor,
                stderr=descriptor,
                start_new_session=True,
            )
        finally:
            os.close(descriptor)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise LauncherError(f"服务启动失败。请通过快捷方式的“查看启动日志”检查原因。\n日志：{log}")
            if service_matches(root, info):
                return info["url"]
            time.sleep(0.25)
        raise LauncherError(f"服务仍在启动，请稍后再次点击图标。\n日志：{log}")


def desktop_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def desktop_argument(value: str) -> str:
    # Exec is parsed by the desktop implementation, not by a shell.
    escaped = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    return '"' + desktop_string(escaped) + '"'


def install(root: Path) -> list[Path]:
    python = shutil.which("python3")
    if not python:
        raise LauncherError("安装启动器需要 Python 3。")
    script = root / "scripts/linux_desktop.py"
    command = " ".join(desktop_argument(str(part)) for part in (python, script))
    content = (
        "[Desktop Entry]\nType=Application\nVersion=1.0\nName=GROMACS Console\nName[zh_CN]=GROMACS 控制台\n"
        "Comment=Start the local GROMACS workspace and open its browser interface\n"
        "Comment[zh_CN]=启动本机 GROMACS 工作台并打开浏览器\n"
        f"Exec={command} open\nPath={desktop_string(str(root))}\nIcon={desktop_string(str(root / 'app/static/gromacs-console.svg'))}\n"
        "Terminal=false\nCategories=Science;Chemistry;\nStartupNotify=false\nActions=Logs;\n\n"
        f"[Desktop Action Logs]\nName=View startup log\nName[zh_CN]=查看启动日志\nExec={command} logs\n"
    )
    applications = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    paths = [applications / f"gromacs-console-{project_id(root)[:12]}.desktop"]
    if shutil.which("xdg-user-dir"):
        result = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5)
        desktop = Path(result.stdout.strip())
        if result.returncode == 0 and desktop.is_absolute() and desktop.is_dir() and desktop != Path.home():
            paths.append(desktop / "GROMACS Console.desktop")
    for path in paths:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        if shutil.which("gio"):
            subprocess.run(["gio", "set", str(path), "metadata::trusted", "true"], capture_output=True, timeout=5)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications)], capture_output=True, timeout=10)
    return paths


def open_file_or_url(target: str) -> None:
    if not shutil.which("xdg-open"):
        raise LauncherError(f"未找到默认浏览器或文件打开工具，请安装 xdg-utils。\n地址：{target}")
    process = subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if process.wait(timeout=5):
            raise LauncherError(f"默认浏览器或文件管理器未能打开，请检查桌面的默认应用设置。\n地址：{target}")
    except subprocess.TimeoutExpired:
        pass  # Some browsers keep xdg-open attached to the new window.


def show_error(message: str) -> None:
    print(message, file=sys.stderr)
    if shutil.which("zenity"):
        subprocess.run(["zenity", "--error", "--title=GROMACS Console", "--no-markup", "--text=" + message], check=False)
    elif shutil.which("kdialog"):
        subprocess.run(["kdialog", "--error", message, "--title", "GROMACS Console"], check=False)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "GROMACS Console", message], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "open", "logs", "info"])
    parser.add_argument("--no-browser", action="store_true", help="Check/start the service without opening a browser")
    args = parser.parse_args()
    try:
        if args.action == "install":
            for path in install(ROOT):
                print(path)
        elif args.action == "info":
            print(json.dumps(launch_info(ROOT)))
        else:
            directory = state_directory(ROOT)
            if args.action == "logs":
                log = directory / "launch.log"
                if not log.exists():
                    raise LauncherError("尚无桌面启动日志。服务可能由终端启动；点击图标会沿用已运行的实例。")
                open_file_or_url(str(log))
            else:
                url = ensure_service(ROOT, directory)
                print(url)
                if not args.no_browser:
                    open_file_or_url(url)
        return 0
    except (LauncherError, OSError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, LauncherError) else "启动器无法完成操作，请检查项目路径、目录权限和桌面环境。"
        if args.no_browser:
            print(message, file=sys.stderr)
        else:
            show_error(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
