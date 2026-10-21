"""界面共用模型连接：从选定服务商官方接口读取可用文本模型，不逐家试探密钥。"""
import os
import re
from datetime import date

import requests


PROVIDERS = {
    "gemini": {"label": "Gemini", "provider": "gemini", "base_url": "",
               "api_key_env": "GEMINI_API_KEY",
               "models_url": "https://generativelanguage.googleapis.com/v1beta/models"},
    "openai": {"label": "OpenAI / GPT", "provider": "chat_completions",
               "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
               "models_url": "https://api.openai.com/v1/models"},
    "deepseek": {"label": "DeepSeek", "provider": "chat_completions",
                 "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY",
                 "models_url": "https://api.deepseek.com/models"},
}


def identify_provider(model):
    """仅按已配置的协议和官方地址识别服务商，自定义中转地址不自动归为官方接口。"""
    base = model.get("base_url", "").rstrip("/")
    if model.get("provider") == "gemini" and base in {"", "https://generativelanguage.googleapis.com"}:
        return "gemini"
    if model.get("provider") == "chat_completions":
        for name in ("openai", "deepseek"):
            if base in {PROVIDERS[name]["base_url"], "https://api.deepseek.com/v1" if name == "deepseek" else None}:
                return name
    return ""


def resolve_key(provider, supplied, current):
    """优先使用新密钥，留空仅复用同一官方服务商的密钥或该服务商专用环境变量。"""
    if provider not in PROVIDERS:
        raise ValueError("请选择 Gemini、OpenAI / GPT 或 DeepSeek 接口类型")
    if not isinstance(supplied, str) or len(supplied) > 4096:
        raise ValueError("API Key 格式无效")
    key = supplied.strip()
    if not key and identify_provider(current) == provider:
        key = current.get("api_key", "") or os.environ.get(current["api_key_env"], "")
    key = key or os.environ.get(PROVIDERS[provider]["api_key_env"], "")
    if not key.strip():
        raise ValueError("请输入所选服务商的 API Key")
    return key.strip()


def text_model(provider, item):
    """过滤已停用及图像、语音、嵌入等专用模型，保留当前文本适配器能使用的系列。"""
    if not isinstance(item, dict):
        return None
    identifier = item.get("name" if provider == "gemini" else "id", "")
    if not isinstance(identifier, str):
        return None
    identifier = identifier.removeprefix("models/")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", identifier):
        return None
    lower = identifier.lower()
    shutdown = item.get("shutdown_date")
    if isinstance(shutdown, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", shutdown) and shutdown <= date.today().isoformat():
        return None
    if any(word in lower for word in ("embedding", "image", "audio", "tts", "transcribe", "realtime", "live", "robotics")):
        return None
    if provider == "gemini":
        if "generateContent" not in item.get("supportedGenerationMethods", []) or not lower.startswith("gemini-"):
            return None
    elif provider == "openai":
        if not re.match(r"^(gpt-\d|chatgpt-\d|o[134](?:-|$))", lower):
            return None
        # 此适配器使用 Chat Completions，排除仅支持其他协议的专用系列。
        if any(word in lower for word in ("instruct", "codex", "search", "deep-research")) or re.search(r"-pro(?:-|$)", lower):
            return None
    elif provider == "deepseek":
        if not lower.startswith("deepseek-") or "text" not in item.get("output_modalities", ["text"]):
            return None
    display = item.get("displayName" if provider == "gemini" else "name", identifier)
    if not isinstance(display, str) or len(display) > 160:
        display = identifier
    return {"id": identifier, "label": identifier if display == identifier else f"{display} · {identifier}"}


def fetch_models(provider, key):
    """读取官方模型清单，处理 Gemini 分页；错误不回显密钥或服务商原始响应。"""
    spec = PROVIDERS[provider]
    headers = {"x-goog-api-key": key} if provider == "gemini" else {"Authorization": f"Bearer {key}"}
    params = {"pageSize": 1000} if provider == "gemini" else {}
    found = {}
    seen_pages = set()
    try:
        with requests.Session() as session:
            for _ in range(10):
                with session.get(spec["models_url"], headers=headers, params=params,
                                 timeout=(10, 20), allow_redirects=False) as response:
                    status = response.status_code
                    if status != 200:
                        message = {401: "API Key 无效，请检查密钥和接口类型", 403: "该密钥没有访问权限，或当前网络地区受限",
                                   402: "API额度耗尽", 429: "请求过于频繁或配额受限，请稍后重试"}.get(status, f"模型列表请求失败（HTTP {status}）")
                        raise ValueError(message)
                    data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("服务商返回的模型列表格式无效")
                items = data.get("models" if provider == "gemini" else "data")
                if not isinstance(items, list):
                    raise ValueError("服务商返回的模型列表格式无效")
                for item in items:
                    model = text_model(provider, item)
                    if model:
                        found[model["id"]] = model
                page = data.get("nextPageToken") if provider == "gemini" else None
                if not page:
                    break
                if not isinstance(page, str) or page in seen_pages:
                    raise ValueError("模型列表分页异常，请稍后重试")
                seen_pages.add(page)
                params["pageToken"] = page
            else:
                raise ValueError("模型列表分页过多，请稍后重试")
    except requests.RequestException:
        raise ValueError("无法连接所选服务商的模型列表接口，请检查网络后重试") from None
    if not found:
        raise ValueError("该接口未返回可用于文献筛选或问答的文本模型")
    return sorted(found.values(), key=lambda item: item["id"], reverse=True)
