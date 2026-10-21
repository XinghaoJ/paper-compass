"""第四步向量接口：调用专用 Embedding 模型，为文献和问题生成可比较的向量。"""
import math
from importlib import import_module

_api = import_module("1model_api")
retry = import_module("99common").retry


def normalize_vector(values, dimensions):
    """校验并归一化向量：拒绝数量错误、非有限数或零向量，便于使用点积计算余弦相似度。"""
    if len(values) != dimensions:
        raise ValueError(f"向量维度不匹配：需要 {dimensions}，得到 {len(values)}")
    vector = [float(value) for value in values]
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("向量包含无效数字")
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm or not math.isfinite(norm):
        raise ValueError("向量长度无效")
    return [value / norm for value in vector]


class GeminiEmbedder:
    """Gemini 向量客户端：区分文献与查询任务，复用现有密钥读取和请求重试逻辑。"""

    def __init__(self, settings, dimensions=768, batch_size=16, client=None):
        """初始化向量接口配置；实际需要向量时才创建网络连接，支持注入测试客户端。"""
        settings.validate()
        if settings.provider != "gemini" or settings.model != "gemini-embedding-001":
            raise ValueError("当前向量适配器支持 gemini / gemini-embedding-001；其他模型需增加对应适配器")
        self.settings = settings
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.client = client
        self.owned = client is None

    def prepare(self):
        """提前检查密钥并创建客户端，不发送向量请求，配置缺失时立即终止整个任务。"""
        if self.client is None:
            self.client = _api.create_client(self.settings)

    def embed(self, texts, *, query=False):
        """分批获取并校验向量，文献使用 RETRIEVAL_DOCUMENT，提问使用 RETRIEVAL_QUERY。"""
        if not texts:
            return []
        self.prepare()
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            response = retry(lambda: self.client.models.embed_content(
                model=self.settings.model, contents=batch,
                config={"task_type": "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
                        "output_dimensionality": self.dimensions}))
            embeddings = response.embeddings or []
            if len(embeddings) != len(batch):
                raise ValueError("向量接口返回的条目数量不匹配")
            vectors.extend(normalize_vector(item.values, self.dimensions) for item in embeddings)
        return vectors

    def close(self):
        """释放本模块创建的连接；调用者注入的客户端由调用者管理。"""
        if self.owned and self.client is not None:
            self.client.close()
            self.client = None
