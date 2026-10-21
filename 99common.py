"""公共工具（99）：统一管理 API 配置、失败重试、文本生成和文件读写，供其他模块调用。"""
import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class APICreditsExhaustedError(RuntimeError):
    """API 余额耗尽异常：要求批量任务保存当前进度并立即停止，而不是继续下一篇。"""


def retry(operation, attempts=3, delay=2):
    """失败重试器：网络异常、限流或服务暂时故障时逐次延长等待时间后重试。

    operation 是需要执行的操作，attempts 是最多尝试次数，delay 是初始等待秒数。
    鉴权失败、无效请求等非临时错误直接抛出，不重复请求。
    """
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            code = code or getattr(getattr(exc, "response", None), "status_code", None)
            message = str(exc).lower()
            if str(code) == "402" or "prepayment credits are depleted" in message:
                raise APICreditsExhaustedError("API额度耗尽") from exc
            transient = str(code) in {"408", "429", "500", "502", "503", "504"}
            transient = transient or isinstance(exc, (TimeoutError, ConnectionError))
            transient = transient or "timeout" in type(exc).__name__.lower()
            transient = transient or type(exc).__name__.lower() in {
                "connectionerror", "connecterror", "networkerror", "readerror", "remoteprotocolerror"}
            if not transient or attempt + 1 >= attempts:
                raise
            time.sleep(delay * 2 ** attempt)


def generate(client, prompt, model=None, *, json_mode=False):
    """AI 文本生成器：发送提示词或文献材料，返回生成的非空文本。

    使用指定模型或环境变量中的默认模型，并通过 retry 处理临时调用失败。
    """
    options = {"model": model or os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"), "contents": prompt}
    if json_mode:
        options["config"] = {"response_mime_type": "application/json"}
    response = retry(lambda: client.models.generate_content(**options))
    candidates = getattr(response, "candidates", None)
    if json_mode and isinstance(candidates, list) and candidates:
        reason = getattr(candidates[0], "finish_reason", None)
        reason = str(getattr(reason, "value", reason))
        if reason == "MAX_TOKENS":
            raise ValueError("问答模型输出达到长度上限，JSON 可能被截断，本次回答未保存；请缩小问题范围或更换模型")
    text = (response.text or "").strip()
    if not text:
        raise ValueError("模型未返回文本，本次结果未保存；可重新运行。")
    return text


def atomic_write(path, text):
    """安全保存文本：先完整写入临时文件，再替换目标文件，降低中途写坏结果的风险。"""
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as handle:
            name = handle.name
            handle.write(text)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def load_json(path):
    """读取 JSON 状态或缓存：文件不存在时返回空字典，内容损坏时明确报错。"""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("状态文件必须是 JSON 对象")
        return data
    except (ValueError, OSError) as exc:
        raise ValueError(f"无法读取状态文件 {path}，请检查后重试：{exc}") from exc


def save_json(path, data):
    """保存 JSON 状态或缓存：将字典转换成保留中文的 JSON 文本，并安全写入文件。"""
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def digest(text):
    """计算文本指纹：把文本转换成 SHA-256 标识，用于缓存、去重和区分同名文件。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_digest(path):
    """计算文件指纹：分块读取文件内容，用于判断文献是否发生变化，避免重复阅读。"""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
