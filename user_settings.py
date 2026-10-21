"""用户设置：集中配置文献筛选、Zotero 下载、知识库与问答，供命令行或未来客户端使用。"""
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class ResearchSettings:
    """文献筛选条件：关键词、年份范围、关注的问题，以及检索网站和输出位置。"""

    keywords: str = "submesoscale symmetric instability and ocean waves"
    year_start: int = 2018
    year_end: int = date.today().year
    focus_question: str = "Symmetric Instability 在 ocean waves 或 Stokes drift 作用下如何产生、演化或受到抑制？"
    # google_scholar 或 openalex；后者使用官方文献检索 API。
    search_source: str = "openalex"
    max_papers: int = 100
    output_file: Path = Path(__file__).resolve().parent / "abstract_screening.md"

    def validate(self):
        """检查筛选条件：拒绝空关键词、空问题、无效年份及不支持的检索网站。"""
        if not self.keywords.strip() or not self.focus_question.strip():
            raise ValueError("关键词和关注的问题不能为空")
        if not 1 <= self.year_start <= self.year_end <= 9999 or self.max_papers < 1:
            raise ValueError("年份范围无效，且 max_papers 必须为正整数")
        if self.search_source not in {"google_scholar", "openalex"}:
            raise ValueError("检索来源必须为 google_scholar 或 openalex")
        if self.search_source == "openalex" and self.max_papers > 10000:
            raise ValueError("OpenAlex 当前最多检索前 10000 篇，请缩小关键词或年份范围")

    @property
    def report_path(self):
        """按来源隔离筛选报告与进度；Scholar 保持旧路径，OpenAlex 自动添加文件名后缀。"""
        path = Path(self.output_file)
        if self.search_source == "openalex" and not path.stem.endswith(".openalex"):
            return path.with_name(path.stem + ".openalex" + path.suffix)
        return path


@dataclass(frozen=True)
class ModelSettings:
    """模型接口设置：支持 Gemini 原生接口或兼容 Chat Completions 协议的文本接口。"""

    # gemini 或 chat_completions。后者需要设置对应服务商的 base_url、模型和密钥变量。
    provider: str = "gemini"
    model: str = os.environ.get("GEMINI_ABSTRACT_MODEL", "gemini-2.5-flash")
    # 只保存环境变量名称，真实密钥保存在本机环境中，不写入报告或状态文件。
    api_key_env: str = "GEMINI_API_KEY"
    # 未来客户端可在内存中直接传密钥；不建议把真实密钥写在此文件里。
    api_key: str = field(default="", repr=False)
    # Gemini 默认留空；兼容接口填写服务商给出的 API 基础地址（通常以 /v1 结尾）。
    base_url: str = ""
    timeout_seconds: int = 120

    def validate(self):
        """检查接口配置：确认协议、模型、密钥变量名和请求超时时间有效。"""
        if self.provider not in {"gemini", "chat_completions"}:
            raise ValueError("模型接口类型必须为 gemini 或 chat_completions")
        if not self.model.strip() or not self.api_key_env.strip() or self.timeout_seconds <= 0:
            raise ValueError("模型名称、密钥环境变量名称不能为空，超时时间必须大于零")
        if self.provider == "chat_completions" and not self.base_url.strip():
            raise ValueError("兼容接口必须填写 base_url")
        if self.base_url and not self.base_url.startswith(("https://", "http://")):
            raise ValueError("模型接口地址必须以 https:// 或 http:// 开头")


@dataclass(frozen=True)
class DOILookupSettings:
    """第一步 DOI 补查：Scholar 缺失时查询 Crossref，仅匹配完整标题，不核对作者和年份。"""

    enabled: bool = True
    timeout_seconds: int = 12
    max_candidates: int = 5

    def validate(self):
        """限制补查请求时长和候选数，避免无限等待。"""
        if type(self.enabled) is not bool:
            raise ValueError("DOI 补查开关必须为布尔值")
        if not 1 <= self.timeout_seconds <= 60 or not 1 <= self.max_candidates <= 20:
            raise ValueError("DOI 补查超时必须为 1–60 秒，候选数必须为 1–20")


@dataclass(frozen=True)
class DownloadSettings:
    """自动下载设置：连接本机 Zotero 插件，将成功获取的 PDF 保存到项目知识库目录。"""

    zotero_url: str = "http://127.0.0.1:23119"
    bridge_config: Path = Path(__file__).resolve().parent / ".zotero-bridge.json"
    output_dir: Path = Path(__file__).resolve().parent / "papers/pdf/files"
    timeout_seconds: int = 300
    poll_seconds: float = 2
    collection_name: str = ""  # 界面下载时自动使用当前调研名称；命令行可自行填写。

    def validate(self):
        """检查本机连接和等待时间，避免将 Zotero 插件令牌发送到远程服务器。"""
        from urllib.parse import urlsplit
        address = urlsplit(self.zotero_url)
        if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}
                or address.username or address.password or address.path not in {"", "/"}
                or address.query or address.fragment):
            raise ValueError("Zotero 地址必须为本机 HTTP 地址，例如 http://127.0.0.1:23119")
        if self.timeout_seconds <= 0 or self.poll_seconds <= 0:
            raise ValueError("下载超时和查询间隔必须大于零")
        if (not isinstance(self.collection_name, str) or len(self.collection_name) > 255
                or any(ord(char) < 32 for char in self.collection_name)):
            raise ValueError("Zotero 分类名称必须为不含控制字符的文字，最多 255 个字符")


# 修改这些配置对象即可；客户端也可以自行创建对象，不必改全局变量。
RESEARCH_SETTINGS = ResearchSettings()
DOI_LOOKUP_SETTINGS = DOILookupSettings()
MODEL_SETTINGS = ModelSettings()
DOWNLOAD_SETTINGS = DownloadSettings()


@dataclass(frozen=True)
class KnowledgeSettings:
    """第四步知识库配置：按页切分 PDF，在本机保存原文、来源和向量索引。"""

    input_dir: Path | None = None  # 留空时沿用第二步 PDF 下载目录。
    database: Path = Path(__file__).resolve().parent / "papers/knowledge/index.sqlite3"
    answer_dir: Path = Path(__file__).resolve().parent / "papers/answers"
    chunk_chars: int = 1000  # 字符数而非 token 数；每个片段只属于一页。
    overlap_chars: int = 150
    embedding_dimensions: int = 768
    embedding_batch_size: int = 16
    top_k: int = 6
    # 仅为检索过滤阈值，不代表相关概率，需要用实际问题评估后调整。
    min_similarity: float = 0.25

    def validate(self):
        """检查切分、向量批次和检索参数，避免无效索引或过长请求。"""
        if not 100 <= self.chunk_chars <= 1500 or not 0 <= self.overlap_chars < self.chunk_chars:
            raise ValueError("chunk_chars 必须为 100–1500，overlap_chars 必须非负且小于片段长度")
        if not 1 <= self.embedding_dimensions <= 3072 or not 1 <= self.embedding_batch_size <= 32:
            raise ValueError("向量维度必须为 1–3072，批次大小必须为 1–32")
        if not 1 <= self.top_k <= 20 or not -1 <= self.min_similarity <= 1:
            raise ValueError("top_k 必须为 1–20，min_similarity 必须为 -1–1")


@dataclass(frozen=True)
class QASettings:
    """第五步问答设置：独立于摘要筛选，未来界面可直接将问题框文字传给 ask_papers。"""

    question: str = ""


KNOWLEDGE_SETTINGS = KnowledgeSettings()
# 修改 question 即可设置第五步默认提问，不影响第一步筛选。
QA_SETTINGS = QASettings()
# 第一版采用 Gemini 文本向量接口，使用与问答不同的专用模型。
EMBEDDING_MODEL_SETTINGS = ModelSettings(
    provider="gemini", model="gemini-embedding-001", api_key_env="GEMINI_API_KEY",
)
# 问答支持现有 Gemini 或 chat_completions 文本接口，可独立替换模型、密钥和地址。
QA_MODEL_SETTINGS = ModelSettings(
    provider="gemini", model=os.environ.get("GEMINI_QA_MODEL", "gemini-2.5-pro"),
    api_key_env="GEMINI_API_KEY", timeout_seconds=300,
)
