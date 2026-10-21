"""界面任务服务：管理独立配置、内存密钥、后台进程和运行结果，不依赖浏览器框架。"""
import copy
import json
import os
import secrets
import subprocess
import sys
import threading
from dataclasses import asdict, fields
from importlib import import_module
from pathlib import Path

import user_settings

_catalog = import_module("0model_catalog")
_studies = import_module("0studies")

ROOT = Path(__file__).resolve().parent
_common = import_module("99common")
MODEL_GROUPS = {"screening": "MODEL_SETTINGS",
                "embedding": "EMBEDDING_MODEL_SETTINGS", "qa": "QA_MODEL_SETTINGS"}
GROUPS = {"research": ("RESEARCH_SETTINGS", user_settings.ResearchSettings),
          "download": ("DOWNLOAD_SETTINGS", user_settings.DownloadSettings),
          "knowledge": ("KNOWLEDGE_SETTINGS", user_settings.KnowledgeSettings),
          "qa": ("QA_SETTINGS", user_settings.QASettings)}
PATH_FIELDS = {"output_file", "bridge_config", "output_dir", "input_dir", "database", "answer_dir"}
ACTIONS = {"search", "download", "index", "ask", "zotero-check", "build-bridge"}
RESULT_PREFIX = "__LITERATURE_UI_RESULT__"


def default_settings():
    """将 user_settings 中的默认配置转为界面可编辑的字典，路径转换为字符串。"""
    data = {key: asdict(getattr(user_settings, name)) for key, (name, _) in GROUPS.items()}
    data["knowledge"]["input_dir"] = None
    data["research"]["search_source"] = "openalex"
    data["models"] = {key: asdict(getattr(user_settings, name)) for key, name in MODEL_GROUPS.items()}
    return json.loads(json.dumps(data, default=str, ensure_ascii=False))


def merge_settings(base, incoming):
    """合并已知配置字段，空密钥保留当前值，显式清除标记可移除本次输入的密钥。"""
    if not isinstance(incoming, dict):
        raise ValueError("设置必须为对象")
    result = copy.deepcopy(base)
    for group in GROUPS:
        values = incoming.get(group, {})
        if not isinstance(values, dict) or set(values) - set(result[group]):
            raise ValueError(f"无效的设置分组：{group}")
        result[group].update(values)
    models = incoming.get("models", {})
    if not isinstance(models, dict) or set(models) - set(MODEL_GROUPS):
        raise ValueError("模型设置分组无效")
    for group, values in models.items():
        if not isinstance(values, dict) or set(values) - (set(result["models"][group]) | {"clear_api_key", "has_api_key"}):
            raise ValueError("模型设置字段无效")
        for key, value in values.items():
            if key in {"clear_api_key", "has_api_key"} or (key == "api_key" and not value):
                continue
            result["models"][group][key] = value
        if values.get("clear_api_key"):
            result["models"][group]["api_key"] = ""
    # 界面只维护一个 PDF 目录；清除旧设置中的独立输入目录，建库时沿用下载路径。
    result["knowledge"]["input_dir"] = None
    # 来源由程序管理，旧界面保存的 Scholar 手动选择不再覆盖主备顺序。
    result["research"]["search_source"] = "openalex"
    return result


def configuration_objects(data, *, allow_draft=True):
    """将界面数据转换为独立配置对象并校验类型、路径和模型支持范围，不修改全局配置。"""
    objects = {}
    definitions = [(key, name, cls, data[key]) for key, (name, cls) in GROUPS.items()]
    definitions += [(key, name, user_settings.ModelSettings, data["models"][key])
                    for key, name in MODEL_GROUPS.items()]
    for key, name, cls, values in definitions:
        converted = {}
        for field in fields(cls):
            value = None if key == "knowledge" and field.name == "input_dir" else values[field.name]
            if key == "research" and field.name == "search_source":
                value = "openalex"
            default = getattr(getattr(user_settings, name), field.name)
            if field.name in PATH_FIELDS:
                if value is None or value == "":
                    if field.name != "input_dir":
                        raise ValueError(f"{key}.{field.name} 路径不能为空")
                    value = None
                elif not isinstance(value, str):
                    raise ValueError("路径必须为文字")
                else:
                    path = Path(value).expanduser()
                    value = (path if path.is_absolute() else ROOT / path).resolve()
            elif isinstance(default, bool):
                if type(value) is not bool:
                    raise ValueError(f"{key}.{field.name} 必须为布尔值")
            elif isinstance(default, int):
                if allow_draft and key == "research" and field.name in {"year_start", "year_end", "max_papers"} and value is None:
                    converted[field.name] = None
                    continue
                if type(value) is not int:
                    raise ValueError(f"{key}.{field.name} 必须为整数")
            elif isinstance(default, float):
                if type(value) not in {int, float}:
                    raise ValueError(f"{key}.{field.name} 必须为数字")
            elif not isinstance(value, str):
                raise ValueError(f"{key}.{field.name} 必须为文字")
            converted[field.name] = value
        obj = cls(**converted)
        if key == "research" and allow_draft:
            # 空白草稿可保存、切换和配置模型；已填写的数值仍需合法，正式筛选再检查完整性。
            if (any(value is not None and not 1 <= value <= 9999 for value in (obj.year_start, obj.year_end))
                    or (obj.year_start is not None and obj.year_end is not None and obj.year_start > obj.year_end)
                    or (obj.max_papers is not None and not 1 <= obj.max_papers <= 10000)):
                raise ValueError("年份范围无效，检索上限必须为 1–10000")
        elif hasattr(obj, "validate"):
            obj.validate()
        objects[name] = obj
    question = objects["QA_SETTINGS"].question
    if len(question) > 4000:
        raise ValueError("RAG 提问不能超过 4000 个字符")
    embedding = objects["EMBEDDING_MODEL_SETTINGS"]
    if embedding.provider != "gemini" or embedding.model != "gemini-embedding-001":
        raise ValueError("当前向量接口支持 gemini / gemini-embedding-001")
    return objects


def public_settings(data, *, disk=False):
    """导出设置时移除真实密钥；界面仅返回是否已有可用密钥，磁盘文件也不保存密钥。"""
    result = copy.deepcopy(data)
    for values in result["models"].values():
        key = values.pop("api_key", "")
        if not disk:
            values["api_key"] = ""
            values["has_api_key"] = bool(key or os.environ.get(values["api_key_env"]))
    return result


def run_action(action, data, options):
    """在独立任务进程中应用本次配置，直接调用已有业务模块，并收集可查看的结果路径。"""
    objects = configuration_objects(data, allow_draft=action != "search")
    for name, value in objects.items():
        setattr(user_settings, name, value)
    limit = options.get("limit")
    dry_run = options.get("dry_run", False)
    files = []
    if action == "search":
        stats = import_module("1abstract_read").auto_literature_review()
        report = Path(stats["report"])
        files = [report, report.with_suffix(".papers.json"), report.with_suffix(".papers.csv"), report.with_suffix(".dois.txt")]
    elif action == "download":
        stats = import_module("2download_papers").download_papers(
            limit=limit, dry_run=dry_run)
        if not dry_run:
            files = [objects["DOWNLOAD_SETTINGS"].output_dir / name for name in
                     ("download_report.csv", "manual_downloads.md")]
    elif action == "index":
        stats = import_module("4knowledge_base").build_knowledge_base(limit=limit, dry_run=dry_run)
    elif action == "ask":
        stats = import_module("5ask_papers").ask_papers(
            question=objects["QA_SETTINGS"].question, retrieve_only=options.get("retrieve_only", False))
        files = [Path(stats["report"]), Path(stats["report"]).with_suffix(".json")]
    elif action == "zotero-check":
        bridge = import_module("2download_papers").ZoteroBridge(objects["DOWNLOAD_SETTINGS"])
        try:
            status = bridge.ping()
            print(f"Zotero 插件连接正常：{status}")
            stats = {"connected": True, "failed": 0}
        finally:
            bridge.close()
    elif action == "build-bridge":
        path = import_module("2build_zotero_bridge").build_bridge(config_path=objects["DOWNLOAD_SETTINGS"].bridge_config)
        files = [path]
        stats = {"built": 1, "failed": 0}
    else:
        raise ValueError("未知任务")
    return {"stats": stats, "files": [str(path.resolve()) for path in files if path.is_file()]}


def worker_main():
    """从标准输入读取内存配置，运行单个任务并输出结构化结果，密钥不经过命令参数或临时文件。"""
    try:
        payload = json.load(sys.stdin)
        result = run_action(payload["action"], payload["settings"], payload["options"])
        print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)
        return 1 if result["stats"].get("failed") else 0
    except _common.APICreditsExhaustedError:
        print("API额度耗尽，任务已自动停止；已保存处理进度。", flush=True)
        return 1
    except Exception as exc:
        print(f"任务失败：{exc}", flush=True)
        return 1


class UIService:
    """本地界面状态：单任务队列、脱敏日志、只保存非密钥偏好，并提供结果文件注册表。"""

    def __init__(self, settings_path=None):
        """载入非敏感偏好并初始化任务状态，原始 user_settings.py 保持不变。"""
        self.settings_path = Path(settings_path or ROOT / ".ui-settings.json")
        self.settings = default_settings()
        self.load_error = ""
        saved = {}
        if self.settings_path.exists():
            try:
                # 即使配置文件被手工写入密钥，也不从持久化文件恢复它。
                saved = _common.load_json(self.settings_path)
                # 旧版逐篇阅读配置已退役，迁移时保留其他用户偏好。
                saved.pop("reading", None)
                saved.get("models", {}).pop("reading", None)
                for model in saved.get("models", {}).values():
                    model.pop("api_key", None)
                candidate = merge_settings(self.settings, saved)
                configuration_objects(candidate)
                self.settings = candidate
            except Exception:
                self.load_error = "已保存设置无法读取，当前使用 user_settings.py 默认值；请检查后重新保存。"
        self.lock = threading.RLock()
        self.job = None
        self.process = None
        self.files = {}
        self.secrets = set()
        self.model_catalog = []
        self.catalog_binding = None
        try:
            self.study_workspace = _studies.restore_studies(saved.get("study_workspace"), self.settings,
                                                           merge_settings, configuration_objects)
        except (ValueError, TypeError, KeyError):
            self.load_error = "调研列表无法读取，请检查 .ui-settings.json；为避免覆盖旧项目，当前禁止修改设置。"
            self.study_workspace = _studies.restore_studies(None, self.settings, merge_settings, configuration_objects)
        self.study_config_error = bool(saved.get("study_workspace") is not None and self.load_error)
        self.study_jobs = {}

    def redact(self, text):
        """隐藏日志或错误信息中的已知密钥，包括当前配置、环境变量和任务快照密钥。"""
        text = str(text)
        values = set(self.secrets)
        for model in self.settings["models"].values():
            values.update([model.get("api_key", ""), os.environ.get(model["api_key_env"], "")])
        for value in sorted((value for value in values if value), key=len, reverse=True):
            text = text.replace(value, "[密钥已隐藏]")
        return text

    def snapshot(self):
        """返回前端所需状态，隐藏密钥和后台进程对象。"""
        with self.lock:
            return {"settings": public_settings(self.settings), "connection": self.model_connection(), "job": copy.deepcopy(self.job),
                    "notice": self.load_error, "root": str(ROOT), "studies": self.studies_snapshot()}

    def studies_snapshot(self):
        """返回项目名称与保存位置，不返回各项目的完整配置或密钥。"""
        return {"active_id": self.study_workspace["active_id"],
                "items": [{key: item[key] for key in ("id", "name", "root")}
                          for item in self.study_workspace["items"].values()],
                "default_directory": str(ROOT / "papers" / "studies")}

    def save_study_settings(self, candidate, workspace):
        """将共用设置和项目列表一次性原子写入文件，绝不持久化模型密钥。"""
        if self.study_config_error:
            raise ValueError(self.load_error)
        workspace["items"][workspace["active_id"]]["settings"] = _studies.study_settings(candidate)
        saved = public_settings(candidate, disk=True)
        saved["study_workspace"] = workspace
        _common.save_json(self.settings_path, saved)

    def cleanup_study_files(self, data, *, confirm=False):
        """预览当前调研文件，确认令牌和名称一致且文件未变化后移入回收站，保留配置。"""
        cleanup = import_module("0study_cleanup")
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("请等待任务完成或停止任务后再清理")
            identifier = self.study_workspace["active_id"]
            item = self.study_workspace["items"][identifier]
            if not confirm:
                self.update(data.get("settings", {}), persist=True)
            others = [(other, merge_settings(self.settings, other["settings"]))
                      for key, other in self.study_workspace["items"].items() if key != identifier]
            plan = cleanup.inspect_cleanup(item, self.settings, others, ROOT,
                                            self.settings["download"]["bridge_config"])
            if not confirm:
                token = secrets.token_urlsafe(32)
                self.cleanup_plan = dict(token=token, study_id=identifier, **plan)
                return dict(name=item["name"], token=token, paths=plan["paths"], files=plan["files"], bytes=plan["bytes"])
            previous = getattr(self, "cleanup_plan", None)
            if (not previous or previous["study_id"] != identifier or data.get("name") != item["name"]
                    or not secrets.compare_digest(str(data.get("token", "")), previous["token"])
                    or previous["fingerprint"] != plan["fingerprint"] or previous["paths"] != plan["paths"]):
                raise ValueError("清理范围、文件或当前调研已变化，请重新预览并确认")
            self.cleanup_plan = None
            cleanup.recycle_paths(plan["paths"])
            self.job = None
            self.study_jobs.pop(identifier, None)
            for key, path in list(self.files.items()):
                if any(path.resolve() == Path(target) or path.resolve().is_relative_to(Path(target)) for target in plan["paths"]):
                    self.files.pop(key, None)
            return dict(cleared=plan["files"], state=self.snapshot())

    def change_study(self, data, *, create=False):
        """新建或切换调研，保存当前草稿并恢复目标设置；运行任务期间禁止切换。"""
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("请等待任务完成或停止任务后再切换调研")
            if self.study_config_error:
                raise ValueError(self.load_error)
            current = merge_settings(self.settings, data.get("settings", {}))
            configuration_objects(current)
            workspace = copy.deepcopy(self.study_workspace)
            old_id = workspace["active_id"]
            workspace["items"][old_id]["settings"] = _studies.study_settings(current)
            root = None
            if create:
                name = _studies.study_name(data.get("name"))
                parent = data.get("directory")
                if not isinstance(parent, str) or not parent.strip():
                    raise ValueError("请选择新调研保存位置")
                if any(item["name"].casefold() == name.casefold() for item in workspace["items"].values()):
                    raise ValueError("已有同名调研，请使用其他名称")
                parent = Path(parent).expanduser()
                parent = (parent if parent.is_absolute() else ROOT / parent).resolve()
                root = parent / name
                if root.exists():
                    raise ValueError("该调研文件夹已存在，请使用其他名称或保存位置")
                identifier = secrets.token_hex(8)
                local = _studies.new_study_settings(current, root)
                workspace["items"][identifier] = {"id": identifier, "name": name,
                                                   "root": str(root), "settings": local}
            else:
                identifier = data.get("id")
                if not isinstance(identifier, str) or identifier not in workspace["items"]:
                    raise ValueError("所选调研不存在，请刷新页面")
            candidate = merge_settings(current, workspace["items"][identifier]["settings"])
            configuration_objects(candidate)
            workspace["active_id"] = identifier
            if root is not None:
                root.mkdir(parents=True, exist_ok=False)
                for child in ("pdf", "knowledge", "answers"):
                    (root / child).mkdir()
            self.save_study_settings(candidate, workspace)
            self.study_jobs[old_id] = copy.deepcopy(self.job)
            self.job = copy.deepcopy(self.study_jobs.get(identifier))
            self.settings = candidate
            self.study_workspace = workspace
            return self.snapshot()

    def model_connection(self):
        """返回共用连接和当前会话已验证的模型列表，只暴露密钥是否存在。"""
        screening = self.settings["models"]["screening"]
        provider = _catalog.identify_provider(screening)
        key = ""
        if provider:
            try:
                key = _catalog.resolve_key(provider, "", screening)
            except ValueError:
                pass
        models = self.model_catalog if self.catalog_binding == (provider, key) else []
        return {"provider": provider, "has_api_key": bool(key), "models": copy.deepcopy(models),
                "selection": {role: self.settings["models"][role]["model"] for role in ("screening", "qa")}}

    def connect_models(self, data):
        """验证共用密钥并获取在线模型列表，成功后同时更新筛选与问答连接，失败不改原设置。"""
        provider = data.get("provider")
        with self.lock:
            original = copy.deepcopy(self.settings["models"])
            key = _catalog.resolve_key(provider, data.get("api_key", ""), original["screening"])
            self.secrets.add(key)
        models = _catalog.fetch_models(provider, key)
        ids = {model["id"] for model in models}
        spec = _catalog.PROVIDERS[provider]
        connection = {name: spec[name] for name in ("provider", "base_url", "api_key_env")}
        connection["api_key"] = key
        with self.lock:
            if self.settings["models"] != original:
                raise ValueError("模型设置已变化，请重新应用密钥")
            incoming = copy.deepcopy(data.get("settings", {}))
            if not isinstance(incoming, dict) or "models" in incoming:
                raise ValueError("共用模型连接不接受单独的模型密钥设置")
            incoming["models"] = {}
            for role in ("screening", "qa"):
                selected = original[role]["model"]
                if selected not in ids:
                    selected = models[0]["id"]
                incoming["models"][role] = {**connection, "model": selected}
            # 保留既有向量索引所用模型，仅向 Gemini 向量接口同步 Gemini 密钥。
            if provider == "gemini":
                incoming["models"]["embedding"] = {"api_key": key}
            self.update(incoming, persist=True)
            self.model_catalog = models
            self.catalog_binding = (provider, key)
            return {"settings": public_settings(self.settings), "connection": self.model_connection()}

    def select_models(self, data):
        """仅允许选择本次连接返回的模型，两项任务各选版本但始终共用相同连接和密钥。"""
        with self.lock:
            connection = self.model_connection()
            ids = {model["id"] for model in connection["models"]}
            if not ids:
                raise ValueError("请先应用密钥，获取当前接口的模型列表")
            selection = data.get("selection", {})
            if not isinstance(selection, dict) or set(selection) != {"screening", "qa"}:
                raise ValueError("请分别选择文献筛选和问答模型")
            if any(not isinstance(value, str) or value not in ids for value in selection.values()):
                raise ValueError("所选模型不在当前接口返回的可用列表中")
            shared = self.settings["models"]["screening"]
            config = {name: shared[name] for name in ("provider", "base_url", "api_key_env", "api_key")}
            self.update({"models": {role: {**config, "model": value} for role, value in selection.items()}}, persist=True)
            return {"settings": public_settings(self.settings), "connection": self.model_connection()}

    def update(self, incoming, persist=False):
        """校验并更新本次配置，可选保存非密钥字段；当前任务继续使用启动时的快照。"""
        with self.lock:
            if self.study_config_error:
                raise ValueError(self.load_error)
            candidate = merge_settings(self.settings, incoming)
            configuration_objects(candidate)
            workspace = copy.deepcopy(self.study_workspace)
            workspace["items"][workspace["active_id"]]["settings"] = _studies.study_settings(candidate)
            if persist:
                self.save_study_settings(candidate, workspace)
            self.settings = candidate
            self.study_workspace = workspace
            return public_settings(candidate)

    def register_file(self, path):
        """为业务模块返回的文件分配随机访问编号，浏览器不能通过任意路径直接读取本机文件。"""
        path = Path(path).resolve()
        if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".csv", ".txt", ".pdf", ".xpi"}:
            return None
        identifier = secrets.token_hex(16)
        self.files[identifier] = path
        return {"id": identifier, "name": path.name, "url": f"/api/file/{identifier}"}

    def manual_list(self, incoming):
        """读取当前下载目录的人工补充清单，兼容此前产生的下载记录并导出清单文件。"""
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("请等待当前任务完成后再刷新人工下载清单")
            self.update(incoming, persist=True)
            objects = configuration_objects(self.settings)
            destination = objects["DOWNLOAD_SETTINGS"].output_dir
            module = import_module("2download_papers")
            state = _common.load_json(destination / ".download-state.json")
            module.save_download_state(destination, state)
            report = destination / "manual_downloads.md"
            registered = next(({"id": key, "name": path.name, "url": f"/api/file/{key}"}
                               for key, path in self.files.items() if path == report.resolve()), None)
            return {"items": module.manual_download_items(state), "output_dir": str(destination),
                    "report": registered or self.register_file(report)}

    def import_manual_pdf(self, data, filename, doi, output_dir):
        """锁定本机目录导入文件，拒绝与后台下载或建库同时写入，避免覆盖状态。"""
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("请等待当前任务完成后再导入 PDF")
            destination = configuration_objects(self.settings)["DOWNLOAD_SETTINGS"].output_dir
            if str(destination) != output_dir:
                raise ValueError("下载目录已改变，请刷新清单后重新导入")
            return import_module("2manual_import").import_pdf(data, filename, destination, doi)

    def prepare_result(self, result, action):
        """为报告和引用 PDF 生成本机访问链接；问答内容按结构返回，普通报告返回纯文本。"""
        prepared = {"stats": result["stats"], "files": [], "answer": None, "preview": ""}
        for name in result.get("files", []):
            path = Path(name)
            registered = self.register_file(path)
            if registered:
                prepared["files"].append(registered)
            if path.suffix == ".md" and not prepared["preview"]:
                prepared["preview"] = path.read_text(encoding="utf-8")[:120000]
            if action == "ask" and path.suffix == ".json":
                data = _common.load_json(path)
                for item in data.get("evidence", []):
                    link = self.register_file(item["path"])
                    item["pdf_url"] = f"{link['url']}#page={item['page']}" if link else None
                prepared["answer"] = data
        # 防止模型或第三方错误偶然将密钥带入报告预览。
        return json.loads(self.redact(json.dumps(prepared, ensure_ascii=False)))

    def start(self, action, incoming, options):
        """验证任务并启动独立 Python 进程，只允许一个活动任务，避免同时写同一索引或状态文件。"""
        if action not in ACTIONS or not isinstance(options, dict):
            raise ValueError("任务或参数无效")
        if set(options) - {"limit", "dry_run", "retrieve_only"}:
            raise ValueError("未知任务参数")
        limit = options.get("limit")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("任务篇数必须为正整数，或留空处理全部")
        for name in ("dry_run", "retrieve_only"):
            if name in options and type(options[name]) is not bool:
                raise ValueError("任务开关无效")
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("已有任务运行中，请等待完成或先停止")
            self.update(incoming, persist=True)
            payload = dict(action=action, settings=copy.deepcopy(self.settings), options=options)
            if action == "ask" and not self.settings["qa"]["question"].strip():
                raise ValueError("请先填写知识库问答问题")
            if action == "search":
                research = self.settings["research"]
                if any(research[name] is None for name in ("year_start", "year_end", "max_papers")):
                    raise ValueError("请填写关键词、关注问题、起始年份、结束年份和检索上限后开始筛选")
                configuration_objects(self.settings, allow_draft=False)
            # 将任务启动时的调研名称传给插件，避免后台下载归入其他调研。
            study = self.study_workspace["items"][self.study_workspace["active_id"]]
            payload["settings"]["download"]["collection_name"] = study["name"]
            for model in self.settings["models"].values():
                self.secrets.update([model["api_key"], os.environ.get(model["api_key_env"], "")])
            job_id = secrets.token_hex(8)
            self.job = {"id": job_id, "action": action, "status": "running", "logs": [], "result": None}
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-u", str(Path(__file__).resolve()), "--worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", cwd=ROOT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                process = self.process
                threading.Thread(target=self.collect, args=(process, payload, job_id), daemon=True).start()
            except Exception:
                self.job["status"] = "failed"
                raise
            return copy.deepcopy(self.job)

    def collect(self, process, payload, job_id):
        """向子进程传递内存配置并收集实时日志，完成后注册结果，始终回收进程和管道。"""
        result = None
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False))
            process.stdin.close()
            for line in process.stdout:
                with self.lock:
                    if not self.job or self.job["id"] != job_id:
                        continue
                    if line.startswith(RESULT_PREFIX):
                        result = json.loads(line[len(RESULT_PREFIX):])
                    else:
                        self.job["logs"].append(self.redact(line.rstrip())[:4000])
                        self.job["logs"] = self.job["logs"][-800:]
            code = process.wait()
            with self.lock:
                if self.job["id"] == job_id and self.job["status"] == "running":
                    self.job["status"] = "completed" if code == 0 else "failed"
                    if result:
                        self.job["result"] = self.prepare_result(result, payload["action"])
        except Exception as exc:
            with self.lock:
                if self.job["id"] == job_id and self.job["status"] == "running":
                    self.job["status"] = "failed"
                    self.job["logs"].append(self.redact(f"任务处理失败：{exc}"))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe and not pipe.closed:
                    pipe.close()

    def stop(self):
        """停止当前任务进程，等待回收后再允许启动下一任务；Zotero 已提交的下载可能继续。"""
        with self.lock:
            process = self.process
            if not self.job or self.job["status"] != "running":
                return
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            self.job["status"] = "cancelled"
            self.job["logs"].append("已停止本机任务。已提交的 Zotero 后台下载可能继续。")


if __name__ == "__main__":
    raise SystemExit(worker_main() if sys.argv[1:] == ["--worker"] else 2)
