# 自动文献阅读助手

支持 Google Scholar / OpenAlex 检索和摘要筛选、DOI 提取与核验补查、Zotero 自动下载、手动导入和本地文献知识库问答。
关键词和关注问题在 `user_settings.py` 中配置，也可用 `--question` 临时覆盖关注问题。

当前已梳理的流程文件按序号命名：

```text
0assistant.py            统一命令入口
0web_ui.py               本地浏览器交互界面入口
0ui_service.py           界面的配置、后台任务与结果服务，由界面自动调用
0model_catalog.py        共用模型连接：识别官方服务商配置并读取可用文本模型清单
1abstract_read.py        第一步：检索、摘要相关性筛选及 DOI 获取
1openalex_search.py      第一步的 OpenAlex 官方检索接口，分页获取元数据并还原摘要
1doi_lookup.py           第一步的 DOI 补查接口，核验 Crossref 书目元数据，无需单独运行
1model_api.py            第一步的模型接口，由摘要脚本自动调用，无需单独运行
2build_zotero_bridge.py  第二步准备：打包本机 Zotero 插件，首次安装或更新时运行
2download_papers.py      第二步：自动获取全文并保存 PDF
2manual_import.py        第二步人工补充：校验、去重、导入本地 PDF 并关联下载记录
4embedding_api.py        第四步的向量模型接口，由知识库自动调用，无需单独运行
4knowledge_base.py       第四步：按页解析 PDF，增量建立原文与向量知识库
5ask_papers.py           第五步：检索知识库，跨文章回答问题并附原文引用位置
99common.py             公共工具，由其他模块调用，无需单独运行
```

`get_dois.py` 及旧 `doi` 命令已移除。DOI 补查已整合到第一步，不使用原先的标题相似度阈值。
数字开头的 Python 文件通过 `importlib.import_module()` 在内部加载；直接运行脚本不受影响。

## 安装与配置（PowerShell）

需要 Python 3.10 或更高版本。旧虚拟环境保存了其他电脑的解释器路径，建议在本机重新创建：

```powershell
python -m venv .venv_local
.\.venv_local\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:GEMINI_API_KEY = '你的 API Key'
```

命令行可通过 `GEMINI_ABSTRACT_MODEL` 设置摘要模型，通过 `GEMINI_QA_MODEL` 设置问答模型；
也可以在 `user_settings.py` 直接配置。界面可从所选服务商返回的模型列表选择版本。
代理由环境变量 `HTTP_PROXY` / `HTTPS_PROXY` 控制，不再强制使用固定本地端口。
原源码中包含明文 API Key，已移除；建议在服务商控制台轮换原 Key。

## 使用

### 本地交互界面

左侧“当前调研”可切换项目，“新建”填写名称和父目录后，会生成独立的 `pdf/`、
`screening.openalex.md` 及筛选进度、`knowledge/index.sqlite3`、`answers/`。
新调研清空关键词、关注问题、起始年份、结束年份和检索上限，保留共用模型配置；不复制旧 PDF 或向量。
空白筛选条件可以保存和切换，正式检索前需填写完整。
旧版资料原位登记为“原有调研”，切回后恢复原有路径、条件、知识库和问答问题。
切换前自动保存当前表单条件；模型连接、密钥及 Zotero 配对由所有调研共用。
密钥仍仅存在运行中的服务内存，重启软件后需重新应用。项目列表与非密钥设置保存到 `.ui-settings.json`。
运行任务期间不能新建或切换；切换后清除上一调研的回答和导入界面，避免混淆。

“清除此调研文件”会先预览当前调研的真实路径、文件数和大小，输入调研名称确认后，
将专属目录以及另行配置的 PDF、筛选输出、DOI、数据库和问答报告移入 Windows 回收站。
调研名称、研究条件、模型连接和密钥仍保留；Zotero 文献不做清理。
与其他调研共用的路径、程序所在目录、配对文件以及含链接或联接点的目标会阻止清理。
只支持本地固定磁盘；文件被占用、回收站不可用或预览后范围变化时，需处理后重新预览，不自动永久删除。

安装项目依赖后，激活 `.venv_local` 并运行：

```powershell
python 0web_ui.py
```

也可在 Windows 中双击项目目录的 `启动界面.cmd`。程序会启动仅监听 `127.0.0.1` 的本地服务，
并打开浏览器页面；默认地址 `http://127.0.0.1:8765`，端口被占用时会选择空闲端口，实际地址见终端。
保持终端运行，关闭时按 Ctrl+C。界面不需要新增第三方框架或数据库服务。

界面包含五个页面：

- **模型配置**：默认先打开此页。文献筛选和问答共用一个 API Key、一个接口类型（Gemini、OpenAI / GPT 或 DeepSeek），
  分别下拉选择模型版本。点击“应用密钥”从所选服务商官方接口获取文本模型列表，选择版本后自动保存。
- **文献筛选**：左右箭头切换四步卡片：研究条件 → 开始检索 → Zotero 插件 → 文献下载。
  在下载前一步打包插件和检查连接；任务记录固定显示在左侧导航下方，切换页面或步骤不会移动、隐藏或清空日志。
  可先减小“检索上限”测试。
- **文献补充**：分为“补足缺失文献”和“添加其他文献”，支持批量补齐缺失 PDF，
  或直接加入未经过筛选的本地 PDF，再更新知识库。两个入口可用顶部标签或左右箭头切换；
  缺失清单在固定高度区域内滚动，主区域显示导入结果和建库状态，任务日志统一显示在左侧。
- **知识库问答**：独立的问题输入框，直接传入第五步问答；支持只检索原文。回答中的 S1 等引用可展开
  对应原文，PDF 链接提供 `#page=` 页序定位（取决于浏览器 PDF 阅读器支持）。
- **路径设置**：依次配置命中文献下载路径、文献筛选报告路径和问答报告路径。知识库自动读取命中文献下载目录；数据库和 Zotero 配对文件由程序管理，保留原有保存位置，不在界面重复设置。
  每项右侧的“浏览”可打开 Windows 本地选择窗口：下载与问答报告选择文件夹，筛选报告选择 `.md` 文件保存位置；选中后点击“应用路径”保存，取消不改动原路径。

页面首次默认值取自 `user_settings.py`。点击“应用密钥”或“应用路径”时将非密钥偏好保存到 `.ui-settings.json`，
下次启动界面时恢复；不会重写 Python 源文件或改变命令行默认配置。
直接运行任务也会应用当前表单设置；刷新人工下载清单、批量导入时也会保存当前非密钥设置。
API Key 使用密码框，**只保留在当前服务进程内存中**，不保存到偏好文件或浏览器本地存储，
服务退出后需重新输入（也可继续使用环境变量）。接口类型不变时，空密钥框保留本次已配置的密钥；
切换接口类型时不会沿用另一类接口的内存密钥。支持的服务商固定使用官方地址，避免将密钥误发到中转地址。
`AIza`、`sk-proj-` 等前缀只用于本地提示；普通 `sk-` 密钥无法可靠区分 OpenAI 和 DeepSeek，需要手选。
不会逐家尝试密钥。列表获取失败保留原配置，获取成功后仅允许选择返回的适用文本模型，
不会凭显示名称猜测型号；模型列出不代表账户额度或生成权限一定充足。
服务重启后需要再次“应用密钥”刷新清单，普通网页刷新则保留当前服务会话清单。
自定义中转接口仍可通过命令行的 `user_settings.py` 配置，不在这个简化界面自动识别。
知识库内部使用 Gemini 向量模型；应用 Gemini 共用密钥时
也同步供向量接口使用。使用 OpenAI 或 DeepSeek 筛选和问答时，仍需通过 `GEMINI_API_KEY`
环境变量或之前已应用的 Gemini 密钥为现有知识库提供向量接口凭据，不会自动更换索引的向量模型。

模型列表接口参考：[OpenAI](https://developers.openai.com/api/reference/resources/models/methods/list)、
[Gemini](https://ai.google.dev/api/models)、[DeepSeek](https://api-docs.deepseek.com/api/list-models/)。

任务使用独立进程，页面显示实时日志、停止按钮、报告预览和下载链接。
同一界面一次只运行一个任务，避免并发写入同一索引；请勿另开终端并发处理相同输出。
停止本机任务不能撤回已提交的 Zotero 后台下载。
界面启动及预览不会调用模型；正式检索、建库和问答会使用配置的在线服务。
这是本机个人使用的界面，未提供多人账号、远程部署或云端权限管理。

### 手动补充下载未完成的 PDF

“文献补充 → 补足缺失文献”会读取 PDF 下载目录中的历史记录，列出标题、DOI、
文章入口与失败原因；自动下载结束后自动刷新，也可以点击“刷新清单”。
同目录的 `manual_downloads.md` 是可导出的待下载清单，导入完成后同步更新。
清单包含下载失败、未找到全文和中断未完成的记录，不包含尚未运行下载的筛选结果。

1. 点击文章旁的“打开文章入口”，自行通过校园 VPN 或机构登录下载全文。
2. 点击上方“批量导入缺失PDF”，多选文件，为每份文件选择对应文章后统一导入。
   也可点击同一篇文章旁的“导入 PDF”，选择单份文件。无需改名或填写目录；
   程序按用户所选文章关联标题和 DOI，因此应确认选中的是该篇文章的 PDF。
3. 其他本地文献可以在“添加其他文献”中多选或拖入。此方式不猜测 DOI，
   如需关联清单里的文章，可再通过该文章的“导入 PDF”选择同一文件，程序复用现有文件。
4. 完成后切回可访问模型 API 的网络，再点击“更新知识库”。
   界面中的知识库自动使用统一 PDF 下载目录，旧设置中的独立输入目录不再生效。命令行仍可用 `index --input` 指定其他目录。

导入仅复制文件到本机，不会删除下载原件、访问模型或自动建库。每份 PDF 上限 50 MB，
拒绝网页、损坏文件和加密 PDF；扫描件可导入，但当前知识库解析器不提供 OCR。
按文件内容去重；已有文章的不同 PDF 不覆盖，相同 PDF 也不会关联到两个不同 DOI。
导入期间请勿从另一个终端同时修改同一下载目录。

### 摘要检索、问题筛选和 DOI 获取

用户设置统一放在 `user_settings.py`，无需修改 `1abstract_read.py` 中的业务逻辑：

| 配置对象 | 字段 | 用途 |
| --- | --- | --- |
| `RESEARCH_SETTINGS` | `keywords` | 在所选来源检索的关键词 |
| | `year_start`、`year_end` | 文献发表年份范围（含起止年份） |
| | `focus_question` | AI 判断摘要是否合格时需要回答的问题 |
| | `search_source` | 界面固定为 `openalex`，服务异常自动改用 Google Scholar，不再显示来源选项；命令行配置仍可指定来源 |
| | `max_papers`、`output_file` | 检索条目上限与报告输出路径 |
| `MODEL_SETTINGS` | `provider` | `gemini` 原生接口或 `chat_completions` 兼容接口 |
| | `model` | 使用的模型名称 |
| | `api_key_env` | 本机保存密钥的环境变量名称 |
| | `base_url` | 可选的接口基础地址，兼容接口必须填写 |
| | `timeout_seconds` | 模型请求超时秒数 |

可以修改配置类的默认字段，也可以直接替换文件末尾的配置对象，例如：

```python
RESEARCH_SETTINGS = ResearchSettings(
    keywords="symmetric instability stokes drift",
    year_start=2018,
    year_end=2026,
    focus_question="Stokes drift 如何改变对称不稳定的触发条件和湍流混合？",
    output_file=Path(__file__).resolve().parent / "si_wave_screening.md",
)
MODEL_SETTINGS = ModelSettings(
    provider="gemini",
    model="gemini-2.5-flash",
    api_key_env="GEMINI_API_KEY",
)
```

设置本机密钥后，直接启动即可：

```powershell
$env:GEMINI_API_KEY = '你的 API Key'
python 1abstract_read.py
# 等价入口：python 0assistant.py search
```

流程是：所选来源按关键词与年份检索 → 本地复核发表年份 → AI 根据关注问题判断相关性 →
先从合格文章的检索记录提取 DOI，缺失时通过 Crossref 核验补查 → 保存结果。关键词用于检索，不要求每篇标题包含所有关键词；
问题用于语义筛选。缺少可验证年份或足够摘要的文献单独记录，不假定其合格。
Scholar 返回的摘要可能只是片段，因此这是摘要初筛，不能替代全文核查。
AI 只判断“相关／不相关”并简述理由，不要求文章已经回答问题，也不生成问题答案或文章总结。
与问题存在实际联系的研究对象、过程、机制或方法可保留为候选，用于过滤关键词搜索带来的无关结果。
报告和文献清单保留检索来源的摘要，DOI 仍由程序单独提取，不使用模型生成的 DOI。

OpenAlex 通过官方 `/works` API 检索，支持关键词与年份过滤，还原 `abstract_inverted_index`。
OpenAlex 摘要缺失或不足时，通过 Crossref 官方 API 补查：有 DOI 时精确查询该 DOI；
无 DOI 或 DOI 未在 Crossref 登记时，按完整标题匹配唯一候选，不核对作者与年份。
清理摘要的 JATS/XML 标签后进入相关性筛选，保留摘要来源链接；仍无摘要则跳过。
旧版 Google 补查失败或缺少摘要的记录再次筛选时会改用 Crossref，已完成的相关性判断继续复用。
Crossref 限流或拒绝访问时停止本次任务的摘要补查，不反复请求。此流程由 `1crossref_abstract.py` 实现。
普通分页最多检索前 10000 篇，每页最多 100 篇；遇到限流、授权、网络或返回格式错误时自动切换一次 Google Scholar。
切换时保留已有结果，按标题和年份跨来源去重，并共用本次检索上限；正常返回零篇不会触发切换。
如果 Scholar 也失败，报告两种来源的错误并保留已保存的筛选进度。报告与每条记录都保留实际结果来源。
小规模使用可不填密钥；若需要自己的额度，可在启动服务前设置 `OPENALEX_API_KEY` 环境变量（不是模型密钥）。
OpenAlex 的搜索规则与 Scholar 不同，不保证相同关键词得到相同结果。它仅用于检索，不增加 PDF 下载渠道。

切换来源不会覆盖旧筛选记录：例如配置 `abstract_screening.md` 时，Scholar 沿用该文件，OpenAlex 使用
`abstract_screening.openalex.md`，对应的 JSON、CSV、DOI 清单和状态文件也分开保存。下载自动读取所选来源的清单。
默认策略中自动切换得到的 Scholar 结果也存入同一份 `.openalex` 报告，便于下载本次完整结果。

DOI 默认优先采用 OpenAlex 条目字段；OpenAlex 条目无 DOI 时补查 Crossref。
对于 Scholar 检索结果，先按完整标题补查 OpenAlex，再使用 Scholar 自带的明确 DOI，最后补查 Crossref。
若本次已因 OpenAlex 服务故障切换至 Scholar，则跳过 OpenAlex 逐篇补查，避免重复请求失败服务。
补查只接受规范化完整标题一致的唯一 DOI，不依赖作者或年份；同标题对应多个 DOI 时保留歧义，等待人工核查。

默认自动生成以下文件，无需额外执行 DOI 查询脚本：

```text
abstract_screening.md              阅读报告：文章、相关性理由、原始摘要和 DOI 状态
abstract_screening.papers.json     合格且有明确 DOI 的文献清单，包含来源及补查核验依据
abstract_screening.papers.csv      合格且有明确 DOI 的文献表格，附 DOI 来源字段及补查说明
abstract_screening.dois.txt        去重 DOI 列表，可复制到 Zotero 的标识符导入功能
abstract_screening.md.state.json   处理状态，包含不合格、缺摘要、年份无效及失败记录
```

采用 Scholar 条目 DOI 时，只读取 `doi` / `DOI` 字段或明确包含 DOI 的 `pub_url` / `eprint_url`，
不从摘要引文猜测，不让 AI 生成 DOI。OpenAlex 未找到 DOI 且 Scholar 条目存在多个冲突 DOI 时保留歧义。

只有相关文献缺少 DOI 时，程序通过 [Crossref 公共元数据接口](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
检索候选。补查只依据完整标题，不使用标题相似度阈值，也不核对作者、年份或文献类型：

- 完整标题经过大小写、HTML、重音和排版标点规范化后一致，不能仅关键词或部分标题相似；
- 作者或年份缺失、提取错误、与 Crossref 不同，都不会阻止标题匹配；查询不附加年份过滤；
- 符合核验条件的不同 DOI 只有一个，预印本与期刊版等多个候选会保留歧义。

补查成功后 `doi_source` 标记 `Crossref`，结构化结果保留 `doi_match_evidence` 核验依据，
并自动进入原有下载清单。补查不调用 AI，也不向 Crossref 发送模型密钥。
同标题可能存在不同版本，程序会保留多个 DOI 的歧义；重要文献可根据保存的来源进一步核查。

设置位于 `user_settings.py` 的 `DOI_LOOKUP_SETTINGS`，默认启用，超时 12 秒、最多查询 5 个候选；
可设 `DOILookupSettings(enabled=False)` 关闭后续补查请求；已有核验结果仍可复用。
这是补查业务配置，当前界面没有单独开关；后台任务启动时读取该 Python 配置。
不会访问出版商网页或额外解析 PDF 来猜测 DOI。

未找到可靠候选标记 `skipped_no_doi`，多个 DOI 冲突标记 `skipped_ambiguous_doi`，不计为 AI 筛选失败。
网络失败、候选歧义或缺少有效标题的原因会显示在任务日志和报告，并保存在状态文件的 `doi_lookup_note` 中。
没有明确 DOI 的相关文章不进入 `.papers.json`、`.papers.csv` 和下载清单。
重新检索时，已相关的历史记录可以补查 DOI，无需重复调用摘要模型；已核验的 Crossref DOI 会复用。
任务日志末尾分为“本次统计”和“累计结果”，两组均显示 `inspected`、`relevant`、`doi_found`、`doi_skipped`。
本次只统计本轮检索范围内的文献（包含复用判断的文献），累计统计当前输出路径保存的全部去重记录。
本次 `inspected` 为实际遍历的条数，包含重复条目；累计 `inspected` 为去重记录数。
`doi_found` 与 `doi_skipped` 分别表示相关文章中有 DOI 和尚无明确 DOI 的篇数，两者之和等于 `relevant`。
DOI 只能帮助定位论文；仅筛选模式不下载全文，可追加 `--download` 或运行 `download` 命令自动调用 Zotero。

切换兼容接口时，在同一配置脚本中指定服务商给出的基础地址、模型和密钥变量：

```python
MODEL_SETTINGS = ModelSettings(
    provider="chat_completions",
    base_url="https://你的服务商地址/v1",
    model="服务商提供的模型名称",
    api_key_env="LITERATURE_MODEL_API_KEY",
)
```

接口需支持 `/chat/completions`、`messages` 和文本回答，模型需能按要求返回 JSON。
`MODEL_SETTINGS` 用于摘要筛选，`QA_MODEL_SETTINGS` 用于知识库问答。
不要把真实密钥写进共享脚本；客户端可通过 `ModelSettings(api_key=...)` 在内存中传入密钥。
真实密钥不会写入任务状态和结果，轮换密钥不会使已完成记录失效。

未来客户端可以直接构造独立配置，不修改全局设置：

```python
from importlib import import_module
from user_settings import ResearchSettings, ModelSettings

auto_literature_review = import_module("1abstract_read").auto_literature_review
auto_literature_review(
    settings=ResearchSettings(keywords="ocean fronts", focus_question="锋生如何影响垂向输运？",
                              output_file="user_a_screening.md"),
    model_settings=ModelSettings(api_key_env="USER_A_GEMINI_KEY"),
)
```

不同用户或不同问题应使用不同输出路径；相同输出路径不支持同时写入。
关键词、关注问题、年份、筛选模型接口或提示词发生变化时，程序自动使用带条件标识的新报告与进度文件，
例如 `screening.openalex.8c0f12ab34cd5678.md`；对应 JSON、CSV 和 DOI 清单也使用相同标识。
旧报告与进度保留，不混用判断或累计计数；改回原条件可继续原进度。只调整检索上限或更换密钥仍复用进度。
下载模块自动读取当前条件对应的 DOI 清单，无需手动修改路径。
同一调研中之前下载的 PDF 和已建立的知识库不会因调整关键词而自动删除；如需彻底分开资料，请新建调研。
命令行仍可用 `--query`、`--year-start`、`--year-end`、`--question` 覆盖配置，旧 `--topic` 是 `--question` 的别名。

### Zotero 自动下载（一次安装，之后无需逐篇操作）

本项目提供本机 Zotero 插件和 Python 下载模块，不使用界面点击，也不需要逐篇粘贴 DOI。

界面下载时，插件 0.2.0 会在 Zotero 个人库中自动创建或复用与当前调研同名的顶层分类，文献及其 PDF 附件随条目归入该分类。复用已有 DOI 条目时保留其原有分类；多个调研可共用同一条目和 PDF。未获取到 PDF 的文献条目也会归类。更新后需重新安装 `dist/literature-bridge.xpi` 并重启 Zotero；旧插件会提示升级，避免静默漏掉分类。已在本地校验完成并跳过的历史下载不做追溯整理。命令行可通过 `DownloadSettings.collection_name` 指定分类名称。
默认连接 `http://127.0.0.1:23119`；配置位于 `user_settings.py` 的 `DOWNLOAD_SETTINGS`。
插件面向 Zotero 7–10 的桌面扩展接口编写；已完成模拟接口测试，尚需在你的实际版本上验证。
Zotero 自带的全文获取会尝试 DOI 对应页面、文章链接和开放获取来源；可获取范围取决于站点和当前网络权限。
需要额外登录、验证码或没有可访问全文的文章不保证获取成功，记录会保留供后续处理。

首次使用：

1. 安装并启动 Zotero 桌面程序。
2. 在项目终端运行 `python 2build_zotero_bridge.py`，生成 `dist/literature-bridge.xpi`。
3. 在 Zotero 的“工具 → 插件 / 附加组件”窗口，点击齿轮菜单的“从文件安装插件”，选择该 XPI。
4. 如 Zotero 提示重启，完成重启后保持 Zotero 运行。
5. 运行 `python 0assistant.py zotero-check` 检查配对连接。

构建时会生成 `.zotero-bridge.json`，插件与 Python 用其中的随机令牌配对。
源码可以共享，XPI 和配对文件按本机生成，不应上传 GitHub；`.gitignore` 已忽略它们。
重建会复用现有令牌，若删除配对文件后重建，需要重新安装新插件。
Zotero 10 的安装器要求插件声明 `update_url`。本项目尚未部署在线更新服务，该字段使用保留的
`example.invalid` 占位地址；目前通过重新构建并从文件安装更新，不提供在线更新。
如果手动“检查更新”提示无法连接此地址，不影响已安装插件的本机下载功能。
插件只增加本机接口及下载条目，不要求 Zotero 云端账号或 API Key，不直接修改 Zotero 数据库文件。

处理已有清单：

```powershell
# 默认读取当前研究配置对应的 .papers.json，支持预览且不需要 Zotero 在线
python 0assistant.py download --dry-run

# 先运行少量文章验证，再批量执行
python 0assistant.py download --limit 3
python 0assistant.py download

# 也可使用每行一个 DOI 的文本文件
python 0assistant.py download --input abstract_screening.dois.txt
```

串联检索、筛选及下载只需一个命令：

```powershell
python 0assistant.py search --download
```

任务按 DOI 在个人 Zotero 库中查重，已有条目不会重复创建；没有条目时创建并添加
`literature-reading-assistant` 标签。有筛选元数据时直接使用 Scholar 的记录；只有 DOI 时通过
Zotero 精确 DOI 查询元数据，不根据标题推测 DOI。相关插件接口实现在 `zotero_bridge/bootstrap.js`。

通过 Zotero 获取成功的 PDF 同时保留在 Zotero 中，并自动复制到 `DOWNLOAD_SETTINGS.output_dir`
（默认 `papers/pdf/files`），所以无需再手动导出 RDF。
项目文件名使用 DOI 哈希避免 Windows 路径字符问题，报告保留 DOI、标题与文件名之间的映射。
自动下载只通过 Zotero 执行，不调用 AI 模型。未成功的文章记录 DOI 和原因，
在“文献补充”中显示标题和文章入口，方便手动下载并批量导入。

## 第四步：建立文献知识库（RAG）

知识库直接使用 Zotero 下载或手动导入的 PDF 原文，默认按页提取文字、
切分重叠片段，然后通过 Gemini 的专用向量模型生成向量，将原文、页序、标题、DOI、
来源路径及向量保存到本机 SQLite。当前规模使用本地余弦检索，无需另装数据库服务。

先在项目虚拟环境安装更新后的依赖。使用 uv 的环境可以运行：

```powershell
uv pip install --python .venv_local/Scripts/python.exe -r requirements.txt
```

设置集中在 `user_settings.py`：

| 配置 | 用途 |
| --- | --- |
| `KNOWLEDGE_SETTINGS.input_dir` | 留空沿用第二步下载目录 |
| `KNOWLEDGE_SETTINGS.database` | 默认 `papers/knowledge/index.sqlite3` |
| `KNOWLEDGE_SETTINGS.chunk_chars` / `overlap_chars` | 默认每片段最多 1000 字符、重叠 150 字符，不跨页；不是 token 数 |
| `KNOWLEDGE_SETTINGS.embedding_dimensions` | 默认 768 维 |
| `KNOWLEDGE_SETTINGS.top_k` | 默认提供前 6 个检索片段；全文问答必要时补足第二篇文献的命中 |
| `KNOWLEDGE_SETTINGS.min_similarity` | 语义候选过滤阈值；不是相关概率，不用于关键词候选过滤 |
| `EMBEDDING_MODEL_SETTINGS` | 向量接口，当前适配 `gemini` / `gemini-embedding-001` |
| `QA_MODEL_SETTINGS` | 独立的回答模型，支持现有 Gemini 或 Chat Completions 文本接口 |
| `QA_SETTINGS.question` | 第五步默认提问，独立于摘要筛选问题；命令行或界面传入的问题优先 |
| `KNOWLEDGE_SETTINGS.answer_dir` | 默认 `papers/answers`，保存问答报告和检索原文 |

向量模型与回答模型是不同角色，不能用普通聊天模型名称替换向量模型。
两种模型默认共用 `GEMINI_API_KEY`，也可分别配置密钥环境变量和接口地址。
这里的“本地知识库”指数据存储在本机；当前向量计算通过在线 API 发送文本片段，
回答模型接收问题和检索原文。尚未提供离线向量模型适配器。

```powershell
# 只预览 PDF 清单：不读取正文，不需要密钥、不调用 API、不写库
python 4knowledge_base.py --dry-run --limit 1

# 正式建库，先处理一篇；会调用向量 API
$env:GEMINI_API_KEY = '你的 API Key'
python 4knowledge_base.py --limit 1

# 全部建库；未变化的已完成文献自动跳过
python 4knowledge_base.py
```

统一入口为 `python 0assistant.py index`。建库不使用 AI 生成的摘要代替全文。
`--limit` 限制本次尝试新增或更新的文献数，已有完成文献不占名额。
每篇成功后提交事务，失败记录留在数据库中，下次重试；不会把半篇向量作为成功索引。
未变化的 PDF 不重新计算向量，下载记录中新补充的标题与 DOI 可直接更新。
删除 PDF 后再次建库会清理其索引；问答时也会验证候选原文件指纹，拒绝使用已变更或不可读的来源。
单篇向量计算中途失败后重试可能重复计算已发出的批次，目前按整篇提交。

数据库固定对应一个输入目录。更换向量模型、接口地址、维度、切分方式或输入目录时，
请用 `--database` 指定新库，并在问答时使用同一个数据库；程序拒绝混用不兼容向量。
同一数据库不要同时运行多个建库任务。默认知识库和回答目录已加入 `.gitignore`。

## 第五步：基于知识库提问并查看引用

在 `user_settings.py` 中修改 `QASettings.question` 的默认值，或配置文件末尾的对象：

```python
QA_SETTINGS = QASettings(
    question="Stokes drift 如何影响对称不稳定的触发条件与能量转换？",
)
```

此问题不影响第一步摘要筛选，也不影响已有文献向量，无需重新建库。
未传入问题时使用 `QA_SETTINGS.question`，不再回退到 `RESEARCH_SETTINGS.focus_question`。
未来界面读取问题框后，直接调用 `ask_papers(question=问题框文字)`，无需改写配置文件：

```python
from importlib import import_module

ask_papers = import_module("5ask_papers").ask_papers
ask_papers(question="不同研究对 Stokes drift 的作用是否有相反结论？")
```

```powershell
# 使用独立配置 QA_SETTINGS.question
python 5ask_papers.py

# 提出新问题，程序自动检索，不需要重新计算未变化论文的向量
python 5ask_papers.py --question 'Stokes drift 如何影响对称不稳定的触发条件？'

# 先只看检索原文，不调用回答模型；仍需调用向量 API 计算问题向量
python 5ask_papers.py --question 'Stokes drift 与湍流混合' --retrieve-only

# 使用独立测试目录和独立数据库（测试文章主题与海洋问题不同，应另选问题）
python 4knowledge_base.py --input papers/download_smoke_test --database papers/knowledge/smoke.sqlite3 --limit 1
python 5ask_papers.py --database papers/knowledge/smoke.sqlite3 --question '为什么许多研究结果可能是错误的？'
```

统一入口为 `python 0assistant.py ask --question '你的问题'`。
问题先转换成查询向量，再结合语义相似度与 BM25 关键词检索，以倒数排名融合选取候选原文。
中文关键词使用双字切分，跨语言主要依靠向量检索。每次正式提问按最高排名片段首次出现的顺序，
选择前两篇不同文献，将完整 PDF 与检索片段一起输入回答模型；只有一篇可用时发送一篇。
建库仍只提取文字并生成向量；仅检索模式不上传 PDF。新增文章后重新运行建库，再提问即可。

全文输入适配 Gemini 原生接口和 OpenAI 官方 Chat Completions 接口，GPT 模型需支持 PDF/视觉输入。
DeepSeek 和其他兼容接口暂未适配，选择它们会明确报错，不会静默改回片段问答。
目前程序将 PDF 作为请求附件直接发送，两篇文件合计限制为 12 MB、每篇 1–1000 页；超限时提示压缩。
全文输入会增加模型用量和等待时间，不缓存云端文件，也不自动发起额外模型调用。
任务记录与报告会列出实际选中的文献；JSON 的 full_documents 记录文件、页数和指纹，不保存 PDF 字节。

每次问答默认生成带时间标识的 `.md` 和同名 `.json`，可通过 `--output papers/answers/my_answer.md`
指定位置（指定已有路径时会覆盖）。Markdown 包含带 `[S1]` 等编号的回答，以及每个候选来源的
标题、可用 DOI、PDF 页序、页内片段编号、本地 PDF 链接和检索原文；JSON 供后续客户端读取。
标题优先取下载记录，其次 PDF 元数据；DOI 只取下载记录，缺失时明确标记，不从参考文献中猜测。

**PDF 页序从 1 开始，可能不同于论文印刷页码。** S1 等检索引用的位置来自数据库。
全文引用采用 P1:3（第一篇附件的 PDF 第三页），由模型标注，程序校验文献编号和页码范围后生成本地链接；
程序不会把全文结论冒充为检索片段中的证据，也无法自动保证模型标注的页面真正支持该结论。
程序拒绝模型返回的未知引用编号、没有引用的结论和无效 JSON。
这只能验证引用编号存在，不能保证每个结论被原文充分支持，重要结论仍应对照摘录和全文核查。
检索无有效证据时不会调用回答模型补猜；模型也可以在证据不足时返回空结论并说明局限。

遇到 `Reached maximum number of tokens per stream` 时，程序仅对失败页临时将 pypdf 内容流解析预算提高到 500 万并用新读取器重试一次，随后恢复默认限制。
这里的 token 是 PDF 解析操作，与模型 token 或 API 额度无关；仍然超限时报告具体页码并保留失败状态，不写入半篇索引。
再次更新知识库会重试失败文献，未变化的成功文献直接复用。

第一版没有 OCR、图表理解、可靠的章节识别或多轮对话记忆。无文本层的扫描 PDF 会记录失败并提示 OCR；
空白页会报告，不会静默假定覆盖了所有页面。普通 PDF 的双栏、公式和表格也可能提取错序。
先用熟悉的论文和已知答案的问题，检查是否找到了关键证据，再扩大文献数量或增加重排序等功能。

接口实现依据 [Gemini 官方向量文档](https://ai.google.dev/gemini-api/docs/embeddings)，
PDF 文字提取使用 [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。

## 离线验证

```powershell
python -m unittest discover -s tests -v
```

测试使用模拟的模型和网络响应，不使用真实 API Key，不会调用付费接口。
