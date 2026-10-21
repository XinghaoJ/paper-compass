"""第一步的模型接口：由摘要筛选脚本自动调用，将提示词发送给用户配置的模型。"""
import os
import base64
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from importlib import import_module

_common = import_module("99common")
generate = _common.generate
retry = _common.retry


def create_client(settings):
    """创建模型连接：根据独立配置读取密钥并创建 Gemini 客户端或 HTTP 会话。"""
    settings.validate()
    key = (settings.api_key or os.environ.get(settings.api_key_env, "")).strip()
    if not key:
        raise ValueError(f"请先设置模型密钥环境变量 {settings.api_key_env}")
    if settings.provider == "gemini":
        from google import genai
        options = {"timeout": settings.timeout_seconds * 1000}
        if settings.base_url:
            options["base_url"] = settings.base_url
        return genai.Client(api_key=key, http_options=options)
    import requests
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {key}"})
    return session


def generate_text(prompt, settings, client, *, json_mode=False, pdfs=None):
    """统一调用文本接口；问答可启用 JSON 输出模式，普通文本调用保持原有行为。"""
    content = prompt
    if pdfs:
        if settings.provider != "gemini" and urlsplit(settings.base_url).hostname != "api.openai.com":
            raise ValueError("当前接口尚未适配完整 PDF 输入，请切换 Gemini 或支持 PDF 的 OpenAI 问答模型")
        # 这是本程序的保守请求上限，给 Base64 和提示词预留空间，不截断或偷偷退回片段。
        if sum(Path(item["path"]).stat().st_size for item in pdfs) > 12 * 1024 * 1024:
            raise ValueError("前两篇 PDF 合计超过本程序 12 MB 的直接上传上限，请压缩 PDF 后重建知识库")
        content = [prompt] if settings.provider == "gemini" else [{"type": "text", "text": prompt}]
        for item in pdfs:
            data = Path(item["path"]).read_bytes()
            if not data.startswith(b"%PDF-") or hashlib.sha256(data).hexdigest() != item["fingerprint"]:
                raise ValueError("待上传 PDF 已变化或无效，请更新知识库后重新提问")
            label = f"完整文献 {item['document_id']}：{item['title']}，共 {item['pages']} 个 PDF 页"
            if settings.provider == "gemini":
                content.extend([label, {"inline_data": {"mime_type": "application/pdf", "data": data}}])
            else:
                content.extend([{"type": "text", "text": label}, {"type": "file", "file": {
                    "filename": item["document_id"] + ".pdf",
                    "file_data": "data:application/pdf;base64," + base64.b64encode(data).decode("ascii")}}])
    if settings.provider == "gemini":
        if json_mode:
            return generate(client, content, settings.model, json_mode=True)
        return generate(client, content, settings.model)

    def request():
        """发送一次兼容接口请求：检查 HTTP 状态，提取模型回答，临时错误由外层重试。"""
        payload = {"model": settings.model, "messages": [{"role": "user", "content": content}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = client.post(
            settings.base_url.rstrip("/") + "/chat/completions",
            json=payload,
            timeout=settings.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if json_mode and isinstance(choices, list) and choices and isinstance(choices[0], dict):
            if choices[0].get("finish_reason") == "length":
                raise ValueError("问答模型输出达到长度上限，JSON 可能被截断，本次回答未保存；请缩小问题范围或更换模型")
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("模型接口响应缺少 choices[0].message.content") from exc
        if not isinstance(text, str) or not text.strip():
            raise ValueError("模型未返回有效文本")
        return text.strip()

    return retry(request)
