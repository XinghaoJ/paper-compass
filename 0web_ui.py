"""本地交互界面入口：仅监听本机，在浏览器设置文献助手并运行现有任务。"""
import argparse
import json
import mimetypes
import secrets
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_service = import_module("0ui_service")
_path_picker = import_module("0path_picker")
ASSETS = Path(__file__).resolve().parent / "web_ui"


class LocalServer(ThreadingHTTPServer):
    """仅绑定 IPv4 回环地址的界面服务，持有当前会话令牌和任务管理器。"""

    def __init__(self, port=8765, service=None):
        """创建本机会话，端口为零时由操作系统选择空闲端口。"""
        self.service = service or _service.UIService()
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    """界面请求处理：限制来源、校验修改请求令牌，只提供指定静态文件和已登记结果。"""

    def log_message(self, format, *args):
        """不将浏览器请求记录到终端，避免日志混入文件访问标识或个人问题。"""
        pass

    def trusted(self, mutation=False):
        """验证 Host、浏览器来源和修改令牌，拒绝其他网站跨站操作本机助手。"""
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        if mutation and not secrets.compare_digest(self.headers.get("X-UI-Token", ""), self.server.token):
            return False
        return True

    def send_body(self, status, body, mime="application/json; charset=utf-8"):
        """返回带长度和本地内容安全策略的响应，设置与任务信息禁止浏览器缓存。"""
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        """将字典编码为保留中文的 JSON 返回前端。"""
        self.send_body(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        """提供界面、会话状态和已注册文件，不提供目录列表或任意路径访问。"""
        if not self.trusted():
            self.send_json(403, {"error": "只接受本机界面请求"})
            return
        route = urlsplit(self.path).path
        assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        try:
            if route in assets:
                path = ASSETS / assets[route]
                mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                self.send_body(200, path.read_bytes(), mime + "; charset=utf-8")
            elif route == "/api/state":
                self.send_json(200, {**self.server.service.snapshot(), "token": self.server.token})
            elif route.startswith("/api/file/"):
                identifier = route.removeprefix("/api/file/")
                with self.server.service.lock:
                    path = self.server.service.files.get(identifier)
                if not path or not path.is_file():
                    self.send_json(404, {"error": "文件不存在或本次会话未登记此文件"})
                    return
                with path.open("rb") as handle:
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(path.stat().st_size))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    shutil.copyfileobj(handle, self.wfile)
            else:
                self.send_json(404, {"error": "页面不存在"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send_json(500, {"error": self.server.service.redact(str(exc))})

    def do_POST(self):
        """保存界面设置、启动任务或停止任务，所有修改均需会话令牌。"""
        if not self.trusted(mutation=True):
            self.send_json(403, {"error": "会话已失效或请求来源无效，请刷新界面"})
            return
        try:
            route = urlsplit(self.path).path
            if route == "/api/import-pdf":
                if self.headers.get_content_type() != "application/pdf":
                    raise ValueError("导入请求必须为 PDF")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 50 * 1024 * 1024:
                    self.close_connection = True
                    raise ValueError("单份 PDF 不能超过 50 MB")
                params = parse_qs(urlsplit(self.path).query)
                self.connection.settimeout(30)
                content = self.rfile.read(size)
                if len(content) != size:
                    raise ValueError("文件传输不完整，请重新导入")
                result = self.server.service.import_manual_pdf(
                    content, params.get("filename", [""])[0], params.get("doi", [""])[0],
                    params.get("output_dir", [""])[0])
                self.send_json(200, result)
                return
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求必须使用 JSON")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 150000:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("请求必须为对象")
            study_id = data.get("study_id")
            if study_id is not None and study_id != self.server.service.study_workspace["active_id"]:
                raise ValueError("当前调研已变化，请刷新页面后重试")
            route = urlsplit(self.path).path
            if route == "/api/browse-path":
                selected = _path_picker.choose_path(data.get("field"), data.get("current", ""))
                self.send_json(200, {"path": selected})
            elif route == "/api/settings":
                settings = self.server.service.update(data.get("settings", {}), persist=True)
                self.send_json(200, {"settings": settings})
            elif route == "/api/studies/create":
                self.send_json(200, self.server.service.change_study(data, create=True))
            elif route == "/api/studies/switch":
                self.send_json(200, self.server.service.change_study(data))
            elif route == "/api/studies/cleanup-preview":
                self.send_json(200, self.server.service.cleanup_study_files(data))
            elif route == "/api/studies/cleanup":
                self.send_json(200, self.server.service.cleanup_study_files(data, confirm=True))
            elif route == "/api/model-connect":
                self.send_json(200, self.server.service.connect_models(data))
            elif route == "/api/model-selection":
                self.send_json(200, self.server.service.select_models(data))
            elif route == "/api/run":
                job = self.server.service.start(data.get("action"), data.get("settings", {}), data.get("options", {}))
                self.send_json(200, {"job": job})
            elif route == "/api/stop":
                self.server.service.stop()
                self.send_json(200, {"stopped": True})
            elif route == "/api/manual-list":
                self.send_json(200, self.server.service.manual_list(data.get("settings", {})))
            else:
                self.send_json(404, {"error": "接口不存在"})
        except (ValueError, TypeError, KeyError) as exc:
            self.send_json(400, {"error": self.server.service.redact(str(exc))})
        except Exception as exc:
            self.send_json(500, {"error": self.server.service.redact(str(exc))})


def main(argv=None):
    """启动本地界面并可选打开浏览器，退出时回收后台任务和服务端口。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="启动自动文献阅读助手的本地交互界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须为 0–65535")
    try:
        server = LocalServer(args.port)
    except OSError:
        if args.port == 0:
            raise
        server = LocalServer(0)
    print(f"文献阅读助手已启动：{server.origin}\n保持此终端运行；按 Ctrl+C 关闭。", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(server.origin,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭本地界面。")
    finally:
        server.service.stop()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
