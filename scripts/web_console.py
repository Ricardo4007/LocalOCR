"""Serve the local OCR control console and run OCR jobs in the background."""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import secrets
import shutil
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_ocr  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"
UPLOAD_ROOT = PROJECT_ROOT / "input" / "web-console"
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg"}
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
ACCESS_TOKEN = secrets.token_urlsafe(32)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = "idle"
        self.message = "等待开始"
        self.progress_current = 0
        self.progress_total = 0
        self.input_dir = str(PROJECT_ROOT / "input")
        self.output_dir = str(PROJECT_ROOT / "output")
        self.device = "auto"
        self.error = ""
        self.started_at = ""
        self.finished_at = ""
        self.results_ready = False

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "status": self.status,
                "message": self.message,
                "progress_current": self.progress_current,
                "progress_total": self.progress_total,
                "input_dir": self.input_dir,
                "output_dir": self.output_dir,
                "device": self.device,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "results_ready": self.results_ready,
            }


JOB = JobState()
RUNTIME_LOCK = threading.Lock()
RUNTIME_INFO: dict[str, object] | None = None
RUNTIME_LOADING = False


def user_path(value: str, default: str) -> Path:
    """Resolve a user-selected path; relative paths stay beside the application."""
    candidate = Path(value or default).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    return resolved


def safe_error(exc: Exception) -> str:
    """Keep machine-specific paths in local logs rather than API responses."""
    print(f"[web] {type(exc).__name__}: {exc}", file=sys.stderr)
    return f"{type(exc).__name__}: 请检查输入与输出目录，详情见本地日志"


def public_path(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return "<external path>"


def read_results(output_dir: Path) -> list[dict[str, object]]:
    summary_path = output_dir / "summary.csv"
    if not summary_path.exists():
        return []
    rows: list[dict[str, object]] = []
    with summary_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            json_name = row.get("json_file", "")
            detail: dict[str, object] = {}
            if json_name:
                json_path = output_dir / Path(json_name).name
                if json_path.exists():
                    try:
                        detail = json.loads(json_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        detail = {}
            texts = detail.get("texts", [])
            if not texts:
                text_name = row.get("text_file", "")
                if text_name:
                    text_path = output_dir / Path(text_name).name
                    if text_path.exists():
                        texts = [
                            line
                            for line in text_path.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        ]
            stem = Path(row.get("image", "image")).stem
            rows.append(
                {
                    **row,
                    "texts": texts,
                    "scores": detail.get("scores", []),
                    "annotated_file": f"{stem}_ocr_res_img.png",
                }
            )
    return rows


def _load_runtime_info() -> None:
    global RUNTIME_INFO, RUNTIME_LOADING
    try:
        paddle, _ = run_ocr.load_runtime()
        cuda = bool(paddle.device.is_compiled_with_cuda())
        gpu_count_value: object = paddle.device.cuda.device_count() if cuda else 0
        gpu_count = int(str(gpu_count_value))
        result: dict[str, object] = {
            "paddle": str(getattr(paddle, "__version__", "unknown")),
            "cuda": cuda,
            "gpu_count": gpu_count,
            "default_device": run_ocr.choose_device(paddle, "auto"),
        }
    except Exception as exc:  # The UI can show the diagnostic and remain usable.
        result = {"error": f"运行环境检测失败：{safe_error(exc)}"}
    with RUNTIME_LOCK:
        RUNTIME_INFO = result
        RUNTIME_LOADING = False


def runtime_info() -> dict[str, object]:
    global RUNTIME_LOADING
    with RUNTIME_LOCK:
        if RUNTIME_INFO is not None:
            return RUNTIME_INFO.copy()
        if not RUNTIME_LOADING:
            RUNTIME_LOADING = True
            threading.Thread(target=_load_runtime_info, daemon=True).start()
        return {"loading": True}


def normalize_job(payload: dict[str, object]) -> tuple[Path, Path, str, int]:
    input_dir = user_path(str(payload.get("input_dir") or "input"), "input")
    output_dir = user_path(str(payload.get("output_dir") or "output"), "output")
    device = str(payload.get("device") or "auto")
    if device not in {"auto", "cpu", "gpu:0"}:
        raise ValueError("运行设备只能选择自动、CPU 或 GPU 0")
    try:
        cpu_threads = int(str(payload.get("cpu_threads") or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("CPU 线程数必须是整数") from exc
    if cpu_threads < 0 or cpu_threads > 256:
        raise ValueError("CPU 线程数必须为 0 到 256；0 表示自动")
    if not input_dir.exists():
        raise ValueError("输入路径不存在")
    if not input_dir.is_dir() and not input_dir.is_file():
        raise ValueError("输入路径必须是目录或图片文件")
    return input_dir, output_dir, device, cpu_threads


def execute_job(payload: dict[str, object]) -> None:
    try:
        input_dir, output_dir, requested_device, cpu_threads = normalize_job(payload)
        images = run_ocr.input_images(input_dir)
    except (ValueError, OSError) as exc:
        with JOB.lock:
            JOB.status = "failed"
            JOB.message = "配置无效"
            JOB.error = safe_error(exc) if isinstance(exc, OSError) else str(exc)
            JOB.results_ready = False
            JOB.finished_at = datetime.now().isoformat(timespec="seconds")
        return

    if not images:
        with JOB.lock:
            JOB.status = "failed"
            JOB.message = "没有可识别的图片"
            JOB.error = "输入路径中没有 PNG、JPG 或 JPEG 图片"
            JOB.results_ready = False
            JOB.finished_at = datetime.now().isoformat(timespec="seconds")
        return

    args = argparse.Namespace(
        input=input_dir,
        output=output_dir,
        device=requested_device,
        doc_orientation=bool(payload.get("doc_orientation")),
        unwarp=bool(payload.get("unwarp")),
        line_orientation=bool(payload.get("line_orientation")),
        cpu_threads=cpu_threads,
        include_json=bool(payload.get("include_json")),
        info=False,
    )
    with JOB.lock:
        JOB.status = "running"
        JOB.message = f"OCR 模型初始化中 · 0/{len(images)} 张"
        JOB.progress_current = 0
        JOB.progress_total = len(images)
        JOB.input_dir = str(input_dir)
        JOB.output_dir = str(output_dir)
        JOB.device = requested_device
        JOB.error = ""
        JOB.started_at = datetime.now().isoformat(timespec="seconds")
        JOB.finished_at = ""
        JOB.results_ready = False
    try:
        def report_progress(current: int, total: int, message: str) -> None:
            with JOB.lock:
                JOB.progress_current = current
                JOB.progress_total = total
                JOB.message = f"{message} · {current}/{total} 张"

        result_code = run_ocr.run(args, progress=report_progress)
        with JOB.lock:
            JOB.status = "completed" if result_code == 0 else "failed"
            JOB.message = "识别完成" if result_code == 0 else "部分图片识别失败"
            JOB.progress_current = len(images) if result_code == 0 else JOB.progress_current
            JOB.progress_total = len(images)
            JOB.results_ready = True
            JOB.finished_at = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # Detailed diagnostics stay in the local log.
        with JOB.lock:
            JOB.status = "failed"
            JOB.message = "识别失败"
            traceback.print_exc()
            JOB.error = safe_error(exc)
            JOB.results_ready = False
            JOB.finished_at = datetime.now().isoformat(timespec="seconds")


def parse_upload(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    if len(body) > MAX_UPLOAD_BYTES:
        raise ValueError("上传内容超过 250 MB 限制")
    if not content_type.lower().startswith("multipart/form-data;"):
        raise ValueError("上传内容必须是 multipart/form-data")
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + body
    )
    uploads: list[tuple[str, bytes]] = []
    for part in message.iter_attachments():
        filename = part.get_filename()
        data = part.get_payload(decode=True)
        if not filename or not isinstance(data, bytes) or not data:
            continue
        safe_name = Path(filename.replace("\\", "/")).name
        suffix = Path(safe_name).suffix.lower()
        is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = data.startswith(b"\xff\xd8\xff")
        if suffix in ALLOWED_SUFFIXES and (is_png if suffix == ".png" else is_jpeg):
            uploads.append((safe_name, data))
    return uploads


def uploaded_session(value: str) -> Path:
    """Resolve only a directly owned upload batch, never an arbitrary directory."""
    path = user_path(value, "")
    if path.parent != UPLOAD_ROOT.resolve() or not path.name.startswith("session-"):
        raise ValueError("只能清理本地控制台上传的批次")
    if len(path.name) != len("session-") + 8 or any(
        character not in "0123456789abcdef" for character in path.name[8:]
    ):
        raise ValueError("上传批次名称无效")
    return path


def choose_directory(initial: Path) -> str:
    """Open a native Windows folder picker without depending on tkinter."""
    try:
        if sys.platform != "win32":
            raise RuntimeError("当前系统不支持 Windows 原生目录选择器")

        import ctypes
        from ctypes import wintypes

        BFFM_INITIALIZED = 1
        BFFM_SETSELECTIONW = 0x467
        BIF_RETURNONLYFSDIRS = 0x0001
        BIF_NEWDIALOGSTYLE = 0x0040
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.LPARAM,
            wintypes.LPARAM,
        )

        class BrowseInfo(ctypes.Structure):
            _fields_ = [
                ("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", wintypes.LPWSTR),
                ("lpszTitle", wintypes.LPCWSTR),
                ("ulFlags", wintypes.UINT),
                ("lpfn", ctypes.c_void_p),
                ("lParam", wintypes.LPARAM),
                ("iImage", ctypes.c_int),
            ]

        initial_path = str(initial if initial.is_dir() else PROJECT_ROOT)

        @callback_type
        def browse_callback(hwnd, message, _lparam, _data):
            if message == BFFM_INITIALIZED:
                ctypes.windll.user32.SendMessageW(
                    hwnd,
                    BFFM_SETSELECTIONW,
                    1,
                    ctypes.cast(ctypes.c_wchar_p(initial_path), ctypes.c_void_p),
                )
            return 0

        display_name = ctypes.create_unicode_buffer(260)
        info = BrowseInfo(
            0,
            None,
            display_name,
            "选择 OCR 目录",
            BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE,
            ctypes.cast(browse_callback, ctypes.c_void_p),
            0,
            0,
        )
        shell32 = ctypes.windll.shell32
        ctypes.windll.user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            ctypes.c_void_p,
        ]
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return ""
        try:
            selected_path = ctypes.create_unicode_buffer(32768)
            if not shell32.SHGetPathFromIDListW(pidl, selected_path):
                return ""
            return str(Path(selected_path.value).resolve())
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pidl)
    except Exception as exc:
        safe_error(exc)
        raise ValueError("无法打开目录选择器，请查看本地日志") from exc


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalOCRConsole/2.0"

    def log_message(self, format: str, *args: object) -> None:
        # BaseHTTPRequestHandler logs the request target, including query strings.
        # Never log query parameters, which may contain private data.
        safe_args = list(args)
        if safe_args and isinstance(safe_args[0], str):
            parts = safe_args[0].split(" ", 2)
            if len(parts) == 3:
                parts[1] = urlparse(parts[1]).path
                safe_args[0] = " ".join(parts)
        print(f"[web] {format % tuple(safe_args)}")

    def send_private_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'",
        )

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_private_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def authorized(self) -> bool:
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOCAL_HOSTS:
            return False
        origin = self.headers.get("Origin", "")
        if origin:
            origin_host = urlparse(origin).hostname
            if origin_host not in LOCAL_HOSTS:
                return False
        supplied = self.headers.get("X-OCR-Token", "")
        return secrets.compare_digest(supplied, ACCESS_TOKEN)

    def require_authorized(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"error": "本地控制台访问令牌无效"}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") or parsed.path.startswith("/files/"):
            if not self.require_authorized():
                return
        if parsed.path == "/api/status":
            state = JOB.snapshot()
            output_dir = Path(str(state["output_dir"]))
            state["input_dir"] = public_path(str(state["input_dir"]))
            state["output_dir"] = public_path(str(state["output_dir"]))
            results = read_results(output_dir) if state["results_ready"] else []
            self.send_json({"job": state, "results": results})
            return
        if parsed.path == "/api/runtime":
            self.send_json(
                {
                    "runtime": runtime_info(),
                    "allowed_suffixes": sorted(ALLOWED_SUFFIXES),
                }
            )
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            self.serve_file(WEB_ROOT / "index.html", inject_token=True)
            return
        if parsed.path.startswith("/files/"):
            relative = unquote(parsed.path.removeprefix("/files/"))
            output_dir = Path(str(JOB.snapshot()["output_dir"])).resolve()
            file_path = (output_dir / relative).resolve()
            if file_path == output_dir or output_dir not in file_path.parents or not file_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.serve_file(file_path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed_path = urlparse(self.path).path
        if not self.require_authorized():
            return
        if parsed_path == "/api/upload":
            self.handle_upload()
            return
        if parsed_path == "/api/browse":
            self.handle_browse()
            return
        if parsed_path == "/api/cleanup":
            self.handle_cleanup()
            return
        if parsed_path == "/api/shutdown":
            self.send_json({"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if parsed_path != "/api/run":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if JOB.snapshot()["status"] in {"starting", "running"}:
            self.send_json({"error": "已有任务正在运行"}, HTTPStatus.CONFLICT)
            return
        try:
            length = self.content_length(64 * 1024)
            raw = self.rfile.read(length)
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("请求参数必须是 JSON 对象")
            normalize_job(payload)
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            message = safe_error(exc) if isinstance(exc, OSError) else str(exc)
            self.send_json({"error": message}, HTTPStatus.BAD_REQUEST)
            return
        with JOB.lock:
            if JOB.status in {"starting", "running"}:
                self.send_json({"error": "已有任务正在运行"}, HTTPStatus.CONFLICT)
                return
            JOB.status = "starting"
            JOB.message = "任务准备中"
            JOB.results_ready = False
        threading.Thread(target=execute_job, args=(payload,), daemon=True).start()
        self.send_json({"started": True})

    def content_length(self, maximum: int) -> int:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("请求缺少有效的 Content-Length") from exc
        if length < 0 or length > maximum:
            raise ValueError("请求体超过限制或长度无效")
        return length

    def handle_browse(self) -> None:
        try:
            length = self.content_length(8192)
            payload = json.loads(self.rfile.read(length) or b"{}")
            kind = payload.get("kind") if isinstance(payload, dict) else ""
            if kind not in {"input", "output"}:
                raise ValueError("目录类型无效")
            state = JOB.snapshot()
            current = Path(str(state["input_dir"] if kind == "input" else state["output_dir"]))
            selected = choose_directory(current)
            self.send_json({"path": selected, "kind": kind})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        try:
            length = self.content_length(MAX_UPLOAD_BYTES)
            uploads = parse_upload(self.rfile.read(length), content_type)
            if not uploads:
                raise ValueError("没有收到 PNG、JPG 或 JPEG 图片")
            session = UPLOAD_ROOT / f"session-{secrets.token_hex(4)}"
            session.mkdir(parents=True, exist_ok=True)
            saved: list[str] = []
            for filename, data in uploads:
                target = session / filename
                target.write_bytes(data)
                saved.append(filename)
            self.send_json(
                {
                    "input_dir": str(session.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                    "files": saved,
                    "count": len(saved),
                }
            )
        except (ValueError, OSError) as exc:
            message = safe_error(exc) if isinstance(exc, OSError) else str(exc)
            self.send_json({"error": message}, HTTPStatus.BAD_REQUEST)

    def handle_cleanup(self) -> None:
        try:
            length = self.content_length(8192)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("清理请求必须是 JSON 对象")
            session = uploaded_session(str(payload.get("input_dir") or ""))
            if not session.is_dir():
                raise ValueError("上传批次不存在")
            with JOB.lock:
                if JOB.status in {"starting", "running"}:
                    self.send_json({"error": "识别运行中，不能清理上传图片"}, HTTPStatus.CONFLICT)
                    return
                shutil.rmtree(session)
            self.send_json({"deleted": True})
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            message = safe_error(exc) if isinstance(exc, OSError) else str(exc)
            self.send_json({"error": message}, HTTPStatus.BAD_REQUEST)

    def serve_file(self, path: Path, inject_token: bool = False) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = (
            path.read_bytes().replace(b"__OCR_ACCESS_TOKEN__", ACCESS_TOKEN.encode("ascii"))
            if inject_token
            else None
        )
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body) if body is not None else path.stat().st_size))
        self.send_private_headers()
        self.end_headers()
        try:
            if body is not None:
                self.wfile.write(body)
            else:
                with path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=64 * 1024)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start the local HTML OCR console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("The console can only bind to a loopback address.")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    actual_port = server.server_address[1]
    url = f"http://{args.host}:{actual_port}/"
    print(f"Local OCR console: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping local OCR console.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
