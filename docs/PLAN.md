# Reference Topology 实施计划

**文档状态：拟实施设计；当前只提交 README.md 与本文件。**

本计划规定 Python 本地 Web 工作台的目标、数据模型和分阶段验收。文中的模块、API、配置及数据库结构均是设计约定，尚未实现；本轮不创建程序、数据库、依赖清单或运行脚本。后续实施以可检查的阶段产物为准，不能把完成文档等同于完成功能。

## 一、目标、范围与关键约定

### 1.1 项目目标

让用户在浏览器选择本地论文 PDF，后端保存可持续定位文件的 URI，解析论文本身及参考文献，保存到本地 SQLite，并建立能够说明来源的有向引用图。用户可在图中查看引用和已知被引用关系，随时打开已有 PDF，修正文献和匹配错误。

### 1.2 需求清单

| 编号 | 需求 | 完成标准 |
| --- | --- | --- |
| R01 | 在 Web 中选择本地 PDF | 支持允许目录内的单选、多选；记录真实位置并明确导入方式 |
| R02 | 保存 URI 并再次打开 | 服务重启后，通过稳定文件 ID 的 HTTP 地址打开正确 PDF |
| R03 | 识别论文自身 | 保存标题、作者顺序、年份、类型、出版物、标识符等；缺失值和冲突可复核 |
| R04 | 解析参考文献 | 保存每条原文、顺序、可获得的页位置、解析字段及解析器版本 |
| R05 | 匹配文献实体 | 采用强标识优先与保守候选匹配；区分文件去重与文献合并 |
| R06 | 展示引用/被引用 | `引用者 → 被引者`；反向查询与正向查询来自相同的有效关系集合 |
| R07 | SQLite 持久化 | 文件、文献、证据、解析版本和人工决定分离；具有约束、索引及迁移方案 |
| R08 | 人工核对与修正 | 可接受、拒绝或纠正候选；自动重解析不覆盖已有人工决定 |
| R09 | 增量导入与恢复 | 重复导入幂等；文件移动、任务中断和解析失败不破坏已有结果 |
| R10 | 本地独立运行 | Windows、Ubuntu 为首批验收平台；核心流程不要求 Zotero、云服务或 API key |
| R11 | 图的证据与范围 | 节点、边及统计注明来源；未确认项、外部统计与本地确认项有明确区别 |
| R12 | 可复现维护 | 能备份、恢复和导出；依赖与解析器版本可记录，原文件保持只读 |

### 1.3 首期范围

首期面向单用户、浏览器与后端同机、本地磁盘 SQLite 的工作方式。核心闭环为：选择文件、登记位置、提取与复核、匹配入库、查看图、打开 PDF、重启后继续使用。

第一版覆盖带文字层的论文 PDF，优先英文及常见编号式、作者年份式书目；中文、双栏、跨页等样本必须进入验收，但在没有验证前不承诺所有格式自动解析成功。扫描件识别为 `needs_ocr`，人工录入可继续使用。

后续扩展包括 Zotero 只读导入、OCR、联网元数据补全、外部施引文献获取及 PDF.js 条目高亮。首期不实现自动下载参考文献全文、云端多人协作、移动端后端、任意远程原文件访问或直接修改 `zotero.sqlite`。

### 1.4 关系语义

- 文献主键是应用生成的稳定 ID，不是 DOI、标题或文件路径。
- PDF 内容身份、文件位置、书目身份独立；相同 SHA-256 只说明文件字节相同。
- `A → B` 只表示 A 引用 B。入边表示当前数据范围内已知的施引文献。
- 引用图允许环；“拓扑结构图”不要求有向无环图，也不代表执行拓扑排序。
- 推荐相似、共同作者、Zotero Related Items、预印本与正式版之间的版本关系，都不能直接成为引用边。
- 文献是否有本地 PDF、元数据是否确认、引用是否确认是三个独立状态。

## 二、上游依据与复用边界

以下为本轮实际阅读的固定提交。引用固定提交用于说明本次设计依据，不表示要求永远使用这一版本；实施时应重新核实需要接入的发行版和接口。

| 上游 | 检视分支与提交 | 可借鉴内容 | 许可证文件 |
| --- | --- | --- | --- |
| Zotero | `main` / `212a7d6a585d1209bc4e698a904490205f139500` | 文献/附件分离、原位文件与托管文件、识别流程 | [COPYING：AGPLv3](https://github.com/zotero/zotero/blob/212a7d6a585d1209bc4e698a904490205f139500/COPYING) |
| Zotero Reference | `bootstrap` / `4a64b65a3ad18e2d98dc2aaad93dffa8f03fde27` | 参考文献区段与跨行解析、多来源、缓存、人工编辑 | [LICENSE：AGPLv3](https://github.com/MuiseDestiny/zotero-reference/blob/4a64b65a3ad18e2d98dc2aaad93dffa8f03fde27/LICENSE) |
| Zotero Reference Linker | `main` / `9423c0bae60c933337983fed8d874ff54343cac6` | DOI/arXiv、书目指纹、标题匹配及歧义处理 | [LICENSE：MIT](https://github.com/ottersem/zotero-reference-linker/blob/9423c0bae60c933337983fed8d874ff54343cac6/LICENSE) |

### 2.1 Zotero

[attachments.js](https://github.com/zotero/zotero/blob/212a7d6a585d1209bc4e698a904490205f139500/chrome/content/zotero/xpcom/attachments.js) 区分导入附件和链接附件，可借鉴其“书目对象不等于文件”的建模方式。

[recognizeDocument.js](https://github.com/zotero/zotero/blob/212a7d6a585d1209bc4e698a904490205f139500/chrome/content/zotero/xpcom/recognizeDocument.js) 先通过 PDFWorker 获取识别数据，随后请求识别服务，再按 DOI、arXiv、ISBN 等取得元数据；无有效文字存在 `noOCR` 路径。该流程包含联网步骤，不能称为可直接移植的纯离线 Python 识别器。

后续 Zotero 适配器优先研究[本地 API](https://github.com/zotero/zotero/blob/212a7d6a585d1209bc4e698a904490205f139500/chrome/content/zotero/xpcom/server/server_localAPI.js) 或导出文件。运行时探测实际客户端版本、可用路由和权限；本轮不把上游 `main` 的能力当成用户某一发行版的保证，也不设计对 Zotero 数据库的直接写入。

### 2.2 Zotero Reference

重点入口为 [pdf.ts](https://github.com/MuiseDestiny/zotero-reference/blob/4a64b65a3ad18e2d98dc2aaad93dffa8f03fde27/src/modules/pdf.ts)、[utils.ts](https://github.com/MuiseDestiny/zotero-reference/blob/4a64b65a3ad18e2d98dc2aaad93dffa8f03fde27/src/modules/utils.ts)、[views.ts](https://github.com/MuiseDestiny/zotero-reference/blob/4a64b65a3ad18e2d98dc2aaad93dffa8f03fde27/src/modules/views.ts) 和 [api.ts](https://github.com/MuiseDestiny/zotero-reference/blob/4a64b65a3ad18e2d98dc2aaad93dffa8f03fde27/src/modules/api.ts)。

可借鉴页面文字与坐标合并、参考区段解析、原文编辑和多来源处理。其阅读器对象及 Zotero 条目 API 需要由 Python 文件解析和数据库接口替代。

`views.ts` 中对两条文献分别调用 `addRelatedItem`，建立的是双向关联。`GraphData.ts` 使用 [ConnectedPapersClient 远程图接口](https://github.com/MuiseDestiny/zotero-reference/blob/4a64b65a3ad18e2d98dc2aaad93dffa8f03fde27/src/modules/GraphData.ts)。这些结果均不能无区别地导入为“从 PDF 原文确认的有向引用”。

### 2.3 Zotero Reference Linker

重点入口为 [PdfSectionExtractor.ts](https://github.com/ottersem/zotero-reference-linker/blob/9423c0bae60c933337983fed8d874ff54343cac6/src/core/PdfSectionExtractor.ts)、[CitationParser.ts](https://github.com/ottersem/zotero-reference-linker/blob/9423c0bae60c933337983fed8d874ff54343cac6/src/core/CitationParser.ts)、[normalize.ts](https://github.com/ottersem/zotero-reference-linker/blob/9423c0bae60c933337983fed8d874ff54343cac6/src/core/normalize.ts)、[LibraryMatcher.ts](https://github.com/ottersem/zotero-reference-linker/blob/9423c0bae60c933337983fed8d874ff54343cac6/src/core/LibraryMatcher.ts)。

其[README](https://github.com/ottersem/zotero-reference-linker/blob/9423c0bae60c933337983fed8d874ff54343cac6/README.md) 和匹配源码采用 DOI、arXiv、无标题条目的书目指纹、规范化标题及保守模糊匹配，并对歧义保持不链接。新项目参考这一职责划分；具体阈值必须用本项目样本验证，不能直接把匹配分数宣称为概率。

[ReaderIntegration.ts](https://github.com/ottersem/zotero-reference-linker/blob/9423c0bae60c933337983fed8d874ff54343cac6/src/reader/ReaderIntegration.ts) 依赖 Zotero Reader 和 PDF.js 内部对象，不能在 Python 中直接导入。该固定版本的 Related Items 集成仍为 TODO，不能据此假设已有可复用的引用数据库。

### 2.4 复用方式

采用“研究职责划分与交互，再实现 Python 适配器”的路线。若后续复制、翻译或改编具体代码，记录上游提交、文件、修改、版权和许可证；确定整体许可后再纳入发布。不能因为改写成 Python 就假设原许可不再适用。本轮不复制上游实现、不新增项目 LICENSE。

## 三、整体架构与技术选择

### 3.1 技术基线

| 层 | 拟选方案 | 选择理由与边界 |
| --- | --- | --- |
| 运行环境 | Python 3.12 及以上的受支持版本 | 首批验证 Windows、Ubuntu；实施时锁定依赖与兼容范围，不在计划中虚构已验证版本组合 |
| HTTP 与页面 | FastAPI、Uvicorn，模板页面与少量 JavaScript | Python 管理业务与文件；首期避免额外独立前端服务 |
| 数据库 | Python 标准库 `sqlite3`，显式 SQL 迁移 | 单机存储、明确约束与事务；不引入外部数据库服务 |
| PDF 预检和元数据 | pypdf | 读取文件结构、自带元数据；不能仅凭 PDF 的 Title 字段确认书目身份 |
| 页面文字与布局 | pdfminer.six | 获取字符、行和文本块位置，为区段与跨行重建提供依据 |
| 结构化解析 | `python-rules` 基线；可选 `grobid-local` | 前者为 Python 规则与人工复核闭环；后者通过 HTTP 连接本机独立服务 |
| 图展示 | Cytoscape.js | 浏览器布局、方向、缩放、筛选和节点交互；图事实由后端生成 |
| 长任务 | SQLite 持久任务表、受控工作进程 | 解析不占用请求线程，不依赖仅存在内存的后台任务 |
| PDF 查看 | 同源 HTTP + 浏览器内置查看器 | 后续增加本地 PDF.js 以稳定支持定位和高亮 |

相关官方依据：[FastAPI](https://fastapi.tiangolo.com/)、[Python sqlite3](https://docs.python.org/3/library/sqlite3.html)、[pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[pdfminer.six](https://pdfminersix.readthedocs.io/en/latest/topic/converting_pdf_to_text.html)、[Cytoscape.js](https://js.cytoscape.org/)。具体库的选用属于本项目设计决定，性能和准确率需要后续验收。

应用安装完成后，核心静态资源从本机提供，不在运行时依赖 CDN。首次安装的依赖及可选模型下载，与处理用户 PDF 时的联网行为分别说明。

### 3.2 业务流程

```mermaid
flowchart TD
    F["选择本地 PDF"] --> I["登记位置与内容身份"]
    I --> O["本机 HTTP 打开 PDF"]
    I --> P["解析文献与参考文献"]
    P --> S["保存原文与解析版本"]
    P --> X["解析失败或需要 OCR"]
    X --> H["人工处理"]
    S --> M["匹配候选文献"]
    M --> C["充分证据确认"]
    M --> H
    H --> C
    C --> E["有效引用关系"]
    E --> G["引用图与反向查询"]
    G --> O
```

文件登记、文字提取、论文识别、条目解析、实体匹配、引用确认分别报告结果。登记成功并不意味着解析成功；提取到文字也不意味着已经确认全部参考文献。

### 3.3 拟划分的模块

| 拟定路径 | 职责 |
| --- | --- |
| `src/reference_topology/api/` | 文件、文献、任务、复核和图查询的 HTTP 接口 |
| `src/reference_topology/files/` | 允许目录、URI 规范化、内容哈希、文件流和位置重绑定 |
| `src/reference_topology/parsing/` | 文字/布局提取、区段分割、单条书目解析、GROBID 适配 |
| `src/reference_topology/matching/` | 标识符规范化、候选索引、身份匹配与冲突解释 |
| `src/reference_topology/curation/` | 人工字段选择、接受/拒绝引用、文献合并和撤销 |
| `src/reference_topology/graph/` | 当前有效关系、局部子图、方向与来源统计 |
| `src/reference_topology/storage/` | sqlite3 连接、查询、事务、迁移和备份 |
| `src/reference_topology/jobs/` | 持久任务、重试、租约和中断恢复 |
| `src/reference_topology/integrations/` | 后续 Zotero 与可选联网服务适配器 |
| `src/reference_topology/templates/`、`static/` | 页面、样式、本机图资源及阅读入口 |
| `tests/fixtures/` | 经许可的合成/公开验证材料及人工标注，不提交私人论文 |

这些目录为后续实施规划，本轮不创建。数据目录与源码目录分开；本地 PDF、数据库、日志和 API key 不进入 Git。

## 四、本地 PDF 的选择、URI 与浏览器访问

### 4.1 两种明确的登记方式

| 模式 | 文件选择 | 后端存储 | URI 语义 |
| --- | --- | --- | --- |
| `linked`，默认 | 工作台枚举用户配置的允许目录，用户选择；也可输入允许目录内的绝对路径 | 原文件只读，保存哈希和位置 | 保存原始 URI 与解析后的规范位置 URI |
| `managed`，兼容 | 浏览器文件选择框或拖放后上传至本机 | 使用内部文件 ID 保存托管副本，禁止按不可信原文件名拼路径 | 托管副本具有真实 URI；不知道原位置时 `original_uri=NULL` |

[HTML 文件选择框](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/file) 不提供真实绝对路径，`C:\fakepath\...`、客户端提交的文件名或 `webkitRelativePath` 不能冒充真实本机绝对 URI。File System Access API 也不作为跨浏览器 MVP 的前提。

默认允许目录由本机配置指定，工作台只显示已授权根目录。后续可加原生系统选择器，但它是本机辅助程序，不作为普通网页天然具有的能力。

### 4.2 URI 与身份处理

1. 后端使用当前平台的路径规则解析用户选择，检查真实路径、符号链接、可读性和 PDF 类型，再用标准 URI 转换记录位置。参见 [pathlib](https://docs.python.org/3/library/pathlib.html#pathlib.Path.as_uri)。
2. 记录 `host_id`、原始 URI、规范 URI、文件大小、修改时间、最后检查时间；中文、空格、`#`、盘符和大小写按平台与 URI 规则处理，不能把整个路径一律转小写。
3. 流式计算 SHA-256；对哈希计算和解析期间文件变化进行前后检查，必要时使用同一打开句柄或临时快照，避免解析内容与记录哈希不一致。
4. 相同哈希复用 `pdf_assets`；位置不同则增加 `file_locations`。同名、同路径但哈希不同，创建新的内容记录并将旧位置映射标为失效，不能覆盖旧解析证据。
5. 文件移动后保留旧位置记录，验证新位置的哈希后重绑定；若内容不同，则提示新版本/新文件，不假装是原文件恢复。
6. 未识别论文也可以登记 PDF；已有元数据但没有 PDF 的文献也可以成为节点。

### 4.3 PDF 打开协议

拟定 `GET /api/files/{file_id}/content` 和 `HEAD /api/files/{file_id}/content`，其中 `file_id` 指 `pdf_assets.id`。后端选择本机可访问的有效位置，再次检查目录边界，返回文件流。

成功响应使用 `Content-Type: application/pdf`、`Content-Disposition: inline`，支持 Range、`206`、`416`、ETag、Last-Modified。可基于 [Starlette FileResponse](https://starlette.dev/responses/) 实现，并验收其在固定依赖版本中的行为。浏览器若禁用了内置 PDF 查看，会受其设置影响；后续 PDF.js 提供统一阅读界面。

不提供任意 `?path=` 文件下载接口。缺失、越界、文件内容已更换分别返回明确错误；不能在旧文件 ID 下悄悄提供另一篇论文。网页保留稳定应用 URL，SQLite 保留文件位置；不持久化临时 `blob:` 地址。[浏览器对 file 来源的处理](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Same-origin_policy) 存在限制，因此 `file://` 只作为文件定位数据。

### 4.4 本机边界

默认仅绑定 `127.0.0.1`；校验 Host、同源请求和本机会话，对修改操作校验 CSRF token，拒绝跨站调用本地文件接口。路径允许范围在登记、目录浏览、重绑定和实际读取时均检查，不只在第一次登记时检查。PDF 解析使用资源限额和可终止工作进程。

归档文献、移除数据库关联与删除磁盘文件是不同动作。首期不删除用户原位 PDF；托管副本清理必须先确认没有有效引用并提供明确操作。远程部署另行设计，不能沿用“后端所在文件系统就是用户文件系统”的前提。

## 五、解析、匹配与人工复核

### 5.1 统一解析输出

每次解析产生不可变快照，至少包含输入文件哈希、文献 ID、引擎名称/版本、配置指纹、开始/结束时间、状态、警告、原始输出及其哈希。

论文自身输出候选标题、有序作者、日期/年份、文献类型、出版物、卷期页码/文章号、DOI/arXiv 等标识、语言和可提取的摘要。PDF 内置元数据、首页、正文及外部数据分别标明来源；不要把 REFERENCES 中他人的 DOI 误认为本论文 DOI。

参考条目输出顺序号、原编号、原文、结构化字段、页码和可用的位置片段。跨页条目使用位置列表；不存在的页码/坐标保存为 `NULL`，不能编造。文中引用位置与文末书目条目是不同对象，正文上下文识别作为后续增强。

### 5.2 默认 Python 本地解析

`python-rules` 分阶段实施：PDF/XMP 元数据读取 → 页面文字和布局 → 参考文献区段定位 → 条目分割 → 字段提取。

区段定位先利用可用结构信息，再识别 References、Bibliography、参考文献等标题及编号/作者年份模式。考虑双栏阅读顺序、跨行、跨页、页眉页脚、附录和无标题书目；不能只搜索 DOI 或只扫描最后一页。

字段提取保留原文，同时生成规范化副本。标题规范化包括空白、大小写、断行连字符及标点处理，但不能丢失用于人工核对的原始文本。优先在已确定的区段内识别标识符；作者、年份、卷页与标题可部分缺失。

这个引擎是有明确支持范围的基线。识别不可靠时返回候选或 `needs_review`，允许用户指定参考区段、粘贴原文或人工拆分/合并条目；不能把空结果统一当成“本论文无参考文献”。PDF 文字恢复本身存在布局困难，文本库也不是 OCR 引擎，见 [pdfminer.six 的布局说明](https://pdfminersix.readthedocs.io/en/latest/topic/converting_pdf_to_text.html) 与 [pypdf 的提取边界](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。

### 5.3 可选本机 GROBID

`grobid-local` 通过 Python HTTP 客户端调用本机独立 GROBID 服务。它可提供论文头部、参考文献和全文 TEI 结构，见 [GROBID 功能](https://grobid.readthedocs.io/en/latest/Introduction/) 与 [REST API](https://grobid.readthedocs.io/en/latest/Grobid-service/)。GROBID 不是 Python 包；其运行资源和安装方式另行说明，不作为基础文件管理功能的前置条件。

适配器根据固定版本和实际端点传入支持的参数。默认离线模式显式设置 `consolidateHeader=0`、`consolidateCitations=0`、`consolidateFunders=0`；支持时设置 `includeRawCitations=1`。这些参数和服务版本写入运行记录。

原因是官方 API 中部分头部补全默认启用，会请求 Crossref 或 biblio-glutton；“服务位于 localhost”并不等于“不访问网络”。保存原始 TEI 与归一化结果；服务不可达、超时、无内容和部分解析分别显示，不自动改用远程公共解析服务。

### 5.4 文献匹配与去重

| 顺序 | 依据 | 接受条件 |
| --- | --- | --- |
| 1 | 规范化 DOI、arXiv 等强标识 | 唯一候选、标识合法，且无明显标题/类型冲突 |
| 2 | 缺少标题时的书目指纹 | 作者、年份、出版物、卷号与首页/文章号形成足够的共同证据 |
| 3 | 规范化标题 + 作者/年份 | 结合字段一致性；标题一致也不能无条件合并 |
| 4 | 保守模糊匹配 | 返回候选、分数、采用字段与冲突原因；首期默认人工决定 |
| 5 | 无可靠候选 | 保留未解决条目；信息足够时可创建无 PDF 的书目节点，否则保留条目占位 |

标识符提取须处理 DOI URL 前缀和参考条目末尾标点，保留原值；arXiv 基础 ID 与版本信息分开，不把预印本与正式出版物仅凭近似标题自动合并。DOI 的候选冲突先进入复核，不能为了满足唯一索引覆盖已有身份。

“条目已经被引用”与“条目对应哪个既有文献”分开判断。缺少本地 PDF 不阻止建立书目节点；但未确认条目边界或实体身份时，不能生成看似已确认的引用事实。新导入文献只增量重新匹配有关的未解决条目，不反复覆盖已确认结果。

### 5.5 人工修正与重解析

- 原文、原始解析输出保持不可变。人工编辑生成修订/候选值及其来源，不直接抹掉旧结果。
- 人工选中的字段优先于之后的自动候选；重新解析发现冲突时展示差异。
- 接受、拒绝和撤回匹配都保留记录。主动拒绝的匹配不能在下一次运行中无提示恢复。
- 新旧条目只能在同一输入内容、原文指纹及位置等证据明确时对齐；不能仅凭 `[12]` 这样的编号继承人工决定。
- 新运行完成并验证后，在短事务中切换当前采用快照。存在人工修订的条目快照时，自动结果先作为候选保存，完成差异核对后才替换。失败、取消、部分异常不得清空上一完整结果。
- 原有人工确认保留记录和来源版本；选用新快照后，相关判断若存在冲突或仍无法确定条目对应，则标记待复核，排除默认已确认统计，不丢失历史、不强行迁移。
- 文献合并采用保留原始 ID 的别名映射及事件记录。撤销存在后续冲突时先列出差异，不能静默覆盖后来编辑。

### 5.6 联网扩展与 Zotero 适配

后续可选 provider 分别承担元数据补全和施引文献扩展。仅补全标题/DOI 的响应不能自动当作引用证据；外部提供明确引用关系时，记录提供者、记录 ID、查询范围、获取时间和原始响应。接入时核实官方接口、凭据、限流和允许用途，不照搬插件历史端点。

默认关闭外部请求，不自动上传 PDF 全文。用户启用某个补全 provider 时说明发送哪些标识或元数据；失败时保留本地结果。Zotero 适配器作为可选只读导入，记录来源实例、library ID、item key 和版本，禁止把普通 Related Items 自动转换成有向引用。

## 六、SQLite3 数据库设计

### 6.1 存储位置与通用约定

数据库拟位于 `<data_dir>/reference_topology.sqlite3`，托管 PDF 位于 `<data_dir>/managed/`，较大的原始 TEI/页面文字结果位于 `<data_dir>/artifacts/`。后两者由数据库记录 URI、内容哈希及所属对象。托管目录作为应用内部允许根目录登记，不能因为未出现在外部论文目录列表中而无法打开。

`data_dir` 默认使用本机用户数据目录，允许指定其他本地磁盘位置。活动数据库不放在网络共享盘或同步中的远程目录；原位 PDF 则仍在用户选择的位置。备份时区分数据库、托管文件、解析证据与外部原位文件。

结构约定：实体 ID 为 `TEXT` UUID；时间为 UTC ISO 8601 `TEXT`；大小、序号和布尔量为 `INTEGER`，布尔量约束为 0/1；分数为可空 `REAL`。未知年份、标识、坐标和路径用 `NULL`，不填虚构值。结构化候选及原始响应可使用 UTF-8 JSON `TEXT`，但文件、文献和关系主外键不藏在 JSON 中。

每个连接启用外键并校验设置生效。默认软归档文献、保留来源历史；外键采用明确的 `RESTRICT`/受控删除策略，避免一次删除级联抹掉引用证据。凡涉及多个对象的一致性变化均由服务层短事务完成。

### 6.2 核心实体关系

```mermaid
flowchart TD
    P["papers 文献"] --> PF["paper_files 附件关联"]
    PF --> A["pdf_assets 内容"]
    A --> L["file_locations 位置"]
    P --> R["extraction_runs 解析版本"]
    A --> R
    R --> E["reference_entries 原始条目"]
    E --> C["citation_assertions 引用判断"]
    P --> C
    C --> V["citation_edges 有效关系视图"]
```

该图省略作者、元数据候选、任务和合并历史。首期采用以下 **13 张实体/管理表**，加可重建的关系视图；不把一整张引用网络作为单个 JSON 字段存入论文记录。

### 6.3 文献与元数据

| 表 | 主要字段及类型 | 约束与用途 |
| --- | --- | --- |
| `papers` | `id TEXT PK`；`title, title_normalized, publication_type, venue, volume, issue, pages, article_number, date_text, abstract, language TEXT`；`year INTEGER`；`record_status TEXT`；`canonical_paper_id TEXT FK papers`；`row_version INTEGER`；`created_at, updated_at, archived_at TEXT` | `record_status` 为 `stub/identified/reviewed`；标题不唯一；根记录的 `canonical_paper_id=NULL`；是否有可读 PDF 由关联查询计算 |
| `paper_identifiers` | `id TEXT PK`；`paper_id TEXT FK papers`；`scheme, value_raw, value_normalized TEXT`；`metadata_claim_id TEXT FK metadata_claims` | 只保存已接受标识；`UNIQUE(scheme, value_normalized)`；候选与冲突标识留在 claims，不抢占唯一身份 |
| `paper_authors` | `paper_id TEXT FK papers`；`position INTEGER`；`display_name, given_name, family_name, orcid TEXT` | `PRIMARY KEY(paper_id, position)`；`position>=1`；保留署名顺序；不按同名作者建立全局身份合并 |
| `metadata_claims` | `id TEXT PK`；`paper_id TEXT FK papers`；`field_name, value_json, source_kind TEXT`；`run_id TEXT FK extraction_runs` 可空；`reference_entry_id TEXT FK reference_entries` 可空；`source_key, source_url, observed_at TEXT`；`score REAL` 可空；`is_selected, is_manual INTEGER`；`supersedes_id TEXT FK metadata_claims`；`created_at TEXT` | 保存字段候选及来源，人工值不被自动覆盖；同一 paper/field 同时最多一个选中值；整组作者和标识集合分别作为有序/结构化字段快照 |

`papers`、`paper_authors` 和 `paper_identifiers` 是以原始 paper ID 为单位的当前选中元数据投影，与 claim 选择在同一事务更新。canonical 展示使用根记录的选中显示字段，并聚合根及其别名的已确认标识；合并不通过重建根记录标识集合来搬移原始所有权。标识集合的 claim 指明每个标识来源；多个 DOI 候选冲突时不静默扩大已确认集合。论文摘要和原始来源允许缺省；文件名只可作为临时显示标签，不当作已确认标题。

首期不做作者身份消歧，也不因作者同名共享一个全局作者记录。预印本、会议版、期刊扩展版默认保留独立书目身份；后续版本关系与 citation 分开建模。

### 6.4 PDF 内容、位置及文献附件

| 表 | 主要字段及类型 | 约束与用途 |
| --- | --- | --- |
| `pdf_assets` | `id TEXT PK`；`sha256 TEXT NOT NULL`；`byte_size INTEGER NOT NULL`；`page_count INTEGER` 可空；`mime_type TEXT`；`derived_from_asset_id TEXT FK pdf_assets` 可空；`created_at TEXT` | `sha256` 唯一；大小非负；内容身份不可修改；OCR/清洗产物另建 asset 并记录父来源；不存 PDF BLOB |
| `file_locations` | `id TEXT PK`；`asset_id TEXT FK pdf_assets`；`host_id, canonical_uri, original_uri, display_name, root_key TEXT`；`location_kind, status TEXT`；`is_current INTEGER`；`observed_size, observed_mtime_ns INTEGER`；`created_at, last_checked_at TEXT` | `location_kind=linked/managed`；`status=available/missing/inaccessible/changed`；上传原路径未知则 `original_uri=NULL`；当前 `(host_id, canonical_uri)` 唯一，历史映射可保留 |
| `paper_files` | `paper_id TEXT FK papers`；`asset_id TEXT FK pdf_assets`；`role TEXT`；`is_preferred INTEGER`；`created_at TEXT` | `PRIMARY KEY(paper_id, asset_id)`；`role=primary/alternate/supplement`；同一文献最多一个首选 PDF；多对多关联不自动推断合订本各文章边界 |

路径恢复时新增/更新位置历史，不删除论文。相同 URI 被新内容占用时，先将旧映射 `is_current=0`，再记录新 asset 的映射；旧解析仍指向旧 hash。相同字节在多个路径可共享 asset；同一论文不同字节的 PDF 是否归于一个 paper，仍由书目识别决定。

`host_id` 表示拥有这些路径的本机环境。把数据库恢复到另一台电脑后，需要重新验证或重绑定位置，不假定旧绝对路径仍然有效。原位文件失联不撤回已经保存的引用证据。

canonical 文献聚合根及别名的附件。自动选择可打开的 PDF 时，依次使用根记录可读首选、别名可读首选、其余可读附件；同级按稳定 ID 排序。用户可以明确选择附件，不能因各原始记录各有首选就把多个文件都宣称为 canonical 唯一首选。

### 6.5 解析版本与原始参考条目

| 表 | 主要字段及类型 | 约束与用途 |
| --- | --- | --- |
| `extraction_runs` | `id TEXT PK`；`asset_id TEXT FK pdf_assets`；`source_paper_id TEXT FK papers`；`parser_name, parser_version, config_json, config_hash, input_sha256, cache_key TEXT`；`status TEXT`；`is_active INTEGER`；`base_run_id TEXT FK extraction_runs` 可空；`output_uri, output_sha256, warning_json, error_code TEXT`；`started_at, finished_at TEXT` | 输入/输出快照不可变；状态和活动指针按生命周期更新；同一 source/asset 同时最多一个当前采用的完整条目快照；失败运行不替换有效版本 |
| `reference_entries` | `id TEXT PK`；`run_id TEXT FK extraction_runs`；`source_paper_id TEXT FK papers`；`ordinal INTEGER`；`label, raw_text, parsed_json, locations_json, text_fingerprint TEXT`；`aligned_from_entry_id TEXT FK reference_entries` 可空；`created_at TEXT` | `UNIQUE(run_id, ordinal)`；序号从 1 开始；原文不可空；位置可以是跨页片段数组；原文保持不可变 |

运行开始前可为导入论文建立 `stub` 身份，之后选择元数据；因此 `source_paper_id` 不必空缺。`reference_entries` 的 source 必须与其 run 的 source 一致，使用复合外键或等价数据库约束落实，不能只依赖前端传参。

运行缓存按 `input_sha256 + parser_name + parser_version + config_hash` 复用解析器原始输出，不对全部运行强制唯一，以允许显式重解析和不同 attempt。同一 asset 关联不同 source 时，各自建立 run 和条目归属，不直接复用另一论文的 run ID。

人工修改参考区段、拆分或合并条目时，建立 `manual-revision` 完整条目快照，并用 `base_run_id` 指向被修订版本；不能用一个不完整条目片段覆盖完整旧结果。`is_active` 同时适用于自动解析与人工修订，两者共用同一活动快照约束；活动人工修订不能被自动重解析静默替换。

`aligned_from_entry_id` 只记录同一输入内容下明确的一对一条目对应，并保存对齐依据；拆分、合并和含糊对应不自动继承人工决定。继承接受/拒绝时创建可追溯的新判断，保留旧判断及 supersedes 关系，防止被拒绝候选在重解析后复活。

大体积输出先写入临时文件，完成后原子移入 artifacts，再在事务中登记 URI/hash 和解析条目；失败时旧版本保持可见，孤立临时文件可回收。第一次只能得到部分结果时明确标记 `partial`；已有完整结果时，不自动用部分结果替换它。

### 6.6 引用判断与有效关系

`citation_assertions` 保存证据和判断，不对 `(citing_paper_id, cited_paper_id)` 设唯一，因为同一引用可以有多个独立来源。

| 字段 | 类型/约束 | 含义 |
| --- | --- | --- |
| `id` | `TEXT PK` | 一条引用判断的稳定 ID |
| `citing_paper_id` | `TEXT NOT NULL FK papers` | 引用者 |
| `cited_paper_id` | `TEXT FK papers`，可空 | 候选或已确认被引者；`accepted` 时必须存在 |
| `reference_entry_id` | `TEXT FK reference_entries`，可空 | PDF 原始条目证据；`source_kind=pdf` 时必填 |
| `source_kind` | `TEXT CHECK(pdf/manual/external)` | 证据类型；人工确认 PDF 匹配仍是 pdf 来源 |
| `provider, provider_record_id, source_url, observed_at` | 可空 `TEXT` | 外部来源身份和获取时间；external 时必须具备可追溯来源 |
| `status` | `TEXT CHECK(candidate/accepted/rejected/retracted)` | 当前版本的判断；未解决项留在原始条目，不必虚构被引目标 |
| `review_state` | `TEXT CHECK(current/needs_review)` | 默认 current；需要重新核对的判断保留历史，但不进入默认已确认图 |
| `is_current, is_pinned` | `INTEGER CHECK(0/1)` | 是否当前判断；人工固定决定可以独立于自动快照保留 |
| `method, matcher_version, evidence_json` | `TEXT` | 匹配规则、候选比较、冲突和人工理由 |
| `score` | 可空 `REAL` | 明确量纲的匹配分数，不宣称为校准概率 |
| `actor` | `TEXT CHECK(system/user)` | 判断由规则还是用户作出 |
| `supersedes_id` | 可空 `TEXT FK citation_assertions` | 被本次判断取代的历史版本 |
| `created_at` | `TEXT NOT NULL` | 判断时间 |

约束：accepted 必须有被引者；PDF 来源必须有条目且条目的 source 与 citing 一致；手工无条目关系必须有说明；同一参考条目最多一个当前 accepted 目标。拒绝与撤回通过新增判断、切换 `is_current` 保留历史，不删除原证据。

首期 `citation_edges` 是有效关系视图/查询，步骤为：

1. 选取 `accepted AND is_current=1 AND review_state='current'`，并验证证据满足以下活动策略。
2. 自动 PDF 判断只来自当前有效解析快照；人工固定判断保留优先级，但也必须通过 review_state 筛选。外部关系按提供者及稳定关系记录身份选取当前 assertion，不能因一次不完整分页响应缺少某边就撤回它；完整批次快照语义留给后续 provider 适配器定义。旧证据可以在历史视图查看。
3. 将原始文献 ID 映射到当前 canonical ID；合并映射保持直接指向根记录。
4. 对 canonical `(citing, cited)` 去重，一对文献只输出一条图边，同时保留证据 ID 列表和来源统计。
5. 合并导致的 canonical 自环进入复核，不计入正常图统计，也不删除原始 assertion。

图边是可重建的投影，不维护第二份无来源的事实表。以后性能需要时可以物化缓存，但更新必须可从 assertions 与活动版本完整重建。`citation_edges` 至少输出 `citing_paper_id, cited_paper_id, evidence_count, has_local_evidence, has_external_evidence`。

人工决定跨重解析的活动策略要与第五节一致：同一文件的明确条目对应关系允许继承；选用新快照后仍存在冲突或不明确对应时，相关旧人工证据设为 `needs_review`，保留展示但不计入已确认统计。`is_pinned` 不绕过这一规则。更换内容 hash 不自动迁移决定；查看当前版本与历史版本必须可区分。

### 6.7 合并历史、任务和迁移

| 表 | 主要字段及类型 | 约束与用途 |
| --- | --- | --- |
| `paper_merge_events` | `id TEXT PK`；`from_paper_id, to_paper_id TEXT FK papers`；`reason, actor, before_json, after_json, touched_versions_json TEXT`；`created_at, reverted_at TEXT` | 保存本次修改的别名映射、字段选择和对象版本；原始 paper、assertion 和来源 ID 不销毁；撤销只回退该事件可安全恢复的变化 |
| `jobs` | `id TEXT PK`；`kind TEXT`；`asset_id TEXT FK pdf_assets`、`paper_id TEXT FK papers` 可空；`request_key, work_key, input_json, status TEXT`；`progress_current, progress_total, attempt INTEGER`；`lease_until, heartbeat_at, error_code, error_message, created_at, updated_at TEXT` | `request_key` 唯一保证重复提交；同一 `work_key` 只能有一个 queued/running；请求身份与可强制重跑的工作内容键分开 |
| `schema_migrations` | `version INTEGER PK`；`name, checksum, applied_at TEXT` | 有序 SQL 迁移和校验；迁移前备份，失败回滚；运行时拒绝不支持的更高 schema |

合并时只更新 canonical 映射和选中元数据，并保存事件；原始 identifier 的所有者可保留，通过 canonical 查询解析到根。映射禁止成环，不能形成未解析长链：若将 B 合并入 C，应把原来指向 B 的别名在同一事务改指 C，并记录全部受影响对象。撤销遇到后续编辑或再次合并时返回冲突和可复核差异。

元数据修改的历史保存在 claims，引用判断历史保存在 assertions，合并历史单列；首期不再额外引入通用事件总线或复杂知识图谱模型。

### 6.8 关键约束与索引草案

以下 SQL 是设计约束的表达，不是已经创建的迁移文件；完整建表需同时实现上面列出的主外键、空值和状态约束。

参与业务唯一性与关系约束的关键字段必须 `NOT NULL`，包括所有实体主键、必需外键、`scheme/value_normalized`、`paper_id/field_name`、`host_id/canonical_uri`、`run_id/ordinal`、`jobs.request_key/work_key`。只有本节明确允许缺省的字段才可为空，避免 NULL 绕过预期唯一约束。

```sql
CREATE UNIQUE INDEX uq_pdf_content ON pdf_assets(sha256);
CREATE UNIQUE INDEX uq_paper_identifier
  ON paper_identifiers(scheme, value_normalized);
CREATE UNIQUE INDEX uq_current_file_location
  ON file_locations(host_id, canonical_uri) WHERE is_current = 1;
CREATE UNIQUE INDEX uq_preferred_pdf
  ON paper_files(paper_id) WHERE is_preferred = 1;
CREATE UNIQUE INDEX uq_selected_claim
  ON metadata_claims(paper_id, field_name) WHERE is_selected = 1;
CREATE UNIQUE INDEX uq_reference_ordinal
  ON reference_entries(run_id, ordinal);
CREATE UNIQUE INDEX uq_active_extraction
  ON extraction_runs(source_paper_id, asset_id) WHERE is_active = 1;
CREATE UNIQUE INDEX uq_accepted_reference_target
  ON citation_assertions(reference_entry_id)
  WHERE status = 'accepted' AND is_current = 1
        AND reference_entry_id IS NOT NULL;
CREATE UNIQUE INDEX uq_job_request ON jobs(request_key);
CREATE UNIQUE INDEX uq_active_work
  ON jobs(work_key) WHERE status IN ('queued', 'running');

CREATE INDEX idx_citation_out
  ON citation_assertions(citing_paper_id, status, is_current);
CREATE INDEX idx_citation_in
  ON citation_assertions(cited_paper_id, status, is_current);
CREATE INDEX idx_reference_source ON reference_entries(source_paper_id);
CREATE INDEX idx_reference_fingerprint
  ON reference_entries(text_fingerprint);
CREATE INDEX idx_run_cache ON extraction_runs(cache_key, status);
CREATE INDEX idx_location_asset ON file_locations(asset_id, status);
CREATE INDEX idx_paper_file_asset ON paper_files(asset_id);
CREATE INDEX idx_paper_title_year ON papers(title_normalized, year);
CREATE INDEX idx_paper_canonical ON papers(canonical_paper_id);
CREATE INDEX idx_job_recovery ON jobs(status, lease_until);
```

约束支持见 [SQLite 部分索引](https://www.sqlite.org/partialindex.html)。需要给其他高频外键查询补索引时，以实际查询计划为依据；不为每个字段无差别创建索引。书目检索初期用明确字段查询，FTS5 作为后续能力检测后启用的扩展。

正向/反向查询使用同一有效视图，例如以下绑定参数查询：

```sql
-- 这篇论文引用谁
SELECT cited_paper_id, evidence_count
FROM citation_edges
WHERE citing_paper_id = :paper_id AND has_local_evidence = 1;

-- 当前本地证据中，谁引用这篇论文
SELECT citing_paper_id, evidence_count
FROM citation_edges
WHERE cited_paper_id = :paper_id AND has_local_evidence = 1;
```

`:paper_id` 在查询前解析为 canonical ID。本地入度计算不同引用者数量，不按原始条目数、重复来源数或 PDF 副本数累加。外部 provider 的 `cited_by_count` 是带时间和来源的元数据统计，不能替代图中的真实入边。

### 6.9 并发、事务、恢复和备份

每个连接启用 `PRAGMA foreign_keys=ON`，设置有限 `busy_timeout`，在本地磁盘启用 WAL，并检查返回模式。WAL 可以改善读写并行，但同一时刻仍只有一个写入者；使用短事务和受控写入队列，解析、文件哈希和网络请求均在事务外进行。官方依据：[外键](https://www.sqlite.org/foreignkeys.html)、[WAL](https://www.sqlite.org/wal.html)、[busy_timeout](https://www.sqlite.org/pragma.html#pragma_busy_timeout)。

任务领取、lease 更新、结果发布采用明确事务；每个工作进程/线程使用自己的连接。长任务完成时再次检查 lease 和输入版本，防止过期 worker 覆盖新结果。请求重复、进程崩溃、重试和用户取消均应幂等。不能只使用进程内 `BackgroundTasks` 就声称支持重启恢复。

使用参数绑定与明确的 UPDATE/UPSERT，避免 `INSERT OR REPLACE` 删除后插入的语义破坏已有外键和来源，见 [SQLite 冲突处理](https://www.sqlite.org/lang_conflict.html)。发布新快照时，旧活动标记、新活动标记、确认结果和状态必须在同一事务切换。

运行中的数据库用 SQLite [backup API](https://www.sqlite.org/backup.html) 创建一致快照，不只复制主文件而忽略 WAL。备份清单记录 schema、托管文件及解析工件 hash；恢复后验证完整性及文件可达性。外部原位 PDF 不默认复制进备份，界面必须说明其恢复依赖。归档与清理分开，清理不得删除仍被来源记录引用的工件。

## 七、Web 工作台与图交互

### 7.1 页面职责

| 页面 | 用户操作与信息 |
| --- | --- |
| 文献库 | 搜索标题/作者/年份/标识；选择 PDF；查看附件可用性、解析状态和任务进度 |
| 文献详情 | 展示采用的元数据与来源；打开首选 PDF；查看参考文献、已知施引文献、历史解析与人工修正 |
| 引用图 | 以选中文献为中心展开入边/出边；缩放、拖动、筛选、点击节点和边查看证据 |
| 待复核 | 对照原始条目和候选，接受/拒绝匹配、创建书目节点、纠正字段、修订条目边界 |
| 任务与维护 | 查看失败原因、重试/取消、重绑定文件、备份/恢复和导出 |
| 设置 | 查看已配置目录；选择解析器和资源限制；分别启用可选 Zotero 与联网能力 |

不把解析器内部日志作为普通用户的主要页面；错误信息给出可以采取的动作，同时保留诊断详情。原始参考文本按文本渲染，避免把 PDF 内容或 provider 返回值当作 HTML 执行。

### 7.2 图的展示规则

| 对象 | 显示与统计规则 |
| --- | --- |
| 已识别文献且有 PDF | 普通文献节点，显示可打开附件标记 |
| 已识别文献但无 PDF | 普通文献节点，显示“无本地 PDF”；可保留 DOI 等外部链接 |
| 不完整或歧义参考条目 | 使用 `reference:{entry_id}` 类型的待确认对象，虚线关系；与 `paper:{id}` 明确分开 |
| 已确认引用 | 实线箭头，支持查看一条或多条来源；箭头始终指向被引文献 |
| 外部 provider 关系 | 独立来源标识和开关；同一边可同时含本地和外部证据 |

默认只统计有效的文献引用边。用户显示待确认项时，虚线不得计入“已确认被引用数”；颜色之外还使用线型、图标或文字，以便区分状态。

默认展示一跳邻居，可逐步扩展。初始建议上限为深度 3、节点 300、边 1,000，服务端强制上限，允许在后续性能验证后调整；响应包含 `truncated`、省略数量和继续展开入口。循环遍历维护 visited 集，不能因图有环无限展开，也不能因年份排序失败删掉真实边。

图布局在浏览器完成，邻居、证据和计数在后端计算。本地入度、当前显示子图入度、外部平台总被引数分别命名，不能混用。多份 PDF 或重复书目条目支持同一引用时，边只计一次。

### 7.3 最小交互闭环

验收演示至少包括 A 引用 B、C，C 引用 B 的小型集合：导入 A 后能看到参考候选；确认 B、C 身份后可显示出边；再导入 C 的 PDF 并确认其参考文献后，B 的本地施引列表包含 A、C。B 没有 PDF 时仍存在于图中；之后导入 B 的 PDF，只新增附件和相应新证据，不复制 B 节点。

## 八、拟定 API、任务与配置

### 8.1 API 契约

以下接口名称为实现基线，尚未提供服务。JSON 接口统一返回稳定对象 ID、状态和可追踪错误码；长任务返回 `202 + job_id`。错误包括 `file_missing`、`path_not_allowed`、`content_changed`、`invalid_pdf`、`needs_ocr`、`parser_unavailable`、`parse_timeout`、`ambiguous_match` 和 `revision_conflict`。

| 方法与路径 | 输入/输出和作用 |
| --- | --- |
| `GET /api/health` | 服务、schema 与本地解析器可用性；不泄露目录、凭据或完整配置 |
| `GET /api/roots` | 列出已配置允许目录的 ID 和显示名称 |
| `GET /api/files/browse` | `root_id` 与相对目录；校验边界后返回子目录及 PDF，支持分页 |
| `POST /api/files/register` | 允许目录内选择结果、`request_key`；登记原位文件，返回任务及文件/文献身份 |
| `POST /api/files/upload` | multipart 文件和 `request_key`；生成托管副本，不接受伪造的客户端绝对路径 |
| `GET/HEAD /api/files/{file_id}/content` | 已登记 PDF 内容，支持 Range 和明确缺失状态 |
| `POST /api/files/{file_id}/rebind` | 新的允许位置；核验 hash 后增加位置映射 |
| `POST /api/files/{file_id}/parse` | 引擎、配置与是否强制重跑；返回 `job_id` |
| `GET /api/jobs/{job_id}` | 当前步骤、进度、尝试次数、错误和结果对象；首期使用短间隔轮询 |
| `POST /api/jobs/{job_id}/cancel` | 请求取消；当前未发布输出不进入活动结果 |
| `POST /api/jobs/{job_id}/retry` | 重试失败/中断任务，保留原请求与 attempt 历史 |
| `GET /api/papers`、`GET /api/papers/{paper_id}` | 分页检索、详情、附件和采用字段的来源 |
| `PATCH /api/papers/{paper_id}` | 人工字段候选和选择，带 `expected_row_version` 防止覆盖较新编辑 |
| `GET /api/papers/{paper_id}/references` | 原文、匹配状态、候选、版本与证据；不能只返回已成功项 |
| `GET /api/papers/{paper_id}/citations` | 按来源范围查询已知施引文献，注明范围及截断状态 |
| `POST /api/references/{entry_id}/decisions` | 接受、拒绝、撤回或新建明确书目身份，保存 actor/理由/版本 |
| `POST /api/papers/{paper_id}/reference-revisions` | 指定基础 run 和修订内容，发布完整人工条目快照 |
| `POST /api/paper-merges`、`POST /api/paper-merges/{id}/revert` | 显示合并影响后执行或撤销，发生后续编辑冲突时返回差异 |
| `GET /api/graph` | `center, direction, depth, source_scope, limit`；返回节点、边、证据摘要和覆盖信息 |
| `GET /api/exports/{format}` | 按选择范围导出 CSL-JSON、BibTeX 或图 JSON；默认不包含绝对本机路径 |
| `POST /api/backups` | 创建本地一致备份任务，返回清单与校验结果 |

目录浏览与显式绝对路径登记最终都解析到允许根内的相对位置；不把 URI 字符串直接拼接到文件读取调用。查询参数使用 schema 校验，数据库使用绑定参数。对同时修改多个版本的操作校验相关 run/assertion 版本，而不仅校验 paper 版本。

### 8.2 任务生命周期

`jobs.status` 拟为 `queued/running/succeeded/partial/failed/cancelled/interrupted`。`needs_review` 是文献或条目的业务状态，不让一个等待人工确认的任务长期占据 running。

工作进程领取任务并写 lease，定期 heartbeat。启动时找出 lease 过期的 running 任务，标记 interrupted；用户重试或配置的有限重试创建新 attempt。重试前检查已有完整结果及幂等键，避免重复节点、位置和关系。任务完成只表示处理结束，界面仍显示解析完整性、待复核数和异常数。

解析运行另记录 `running/succeeded/partial/needs_ocr/failed/cancelled` 等结果；jobs 与 run 的转换必须在实现中列出并验证。没有文字的扫描件可结束为带 `needs_ocr` 结果的 partial 任务，保留文件打开和人工处理能力。

### 8.3 配置草案

以下为未来 TOML 配置的建议结构，**不是当前仓库已经支持的 schema**：

```toml
[server]
host = "127.0.0.1"
port = 8765

[storage]
# 空值使用平台用户数据目录；实际实现须明确解析规则。
data_dir = ""

[files]
default_mode = "linked"
allowed_roots = []  # 首次使用时在本机配置中填入论文目录
max_pdf_size_mb = 100
max_pdf_pages = 1000

[parser]
engine = "python-rules"  # python-rules | grobid-local
timeout_seconds = 120
workers = 1

[parser.grobid]
base_url = "http://127.0.0.1:8070"
consolidate_header = false
consolidate_citations = false
consolidate_funders = false

[network]
enabled = false
providers = []

[graph]
default_depth = 1
maximum_depth = 3
maximum_nodes = 300
maximum_edges = 1000
```

大小、页数、超时和图上限是初始保守配置，需要按验收材料调整，不能当作已验证性能。配置 hash、解析器版本和实际请求参数进入运行记录；未来修改会影响解析结果的默认值时必须改变配置指纹。

联网总开关关闭时，provider 调用和 GROBID 补全参数强制关闭。默认只允许 loopback GROBID 地址；若以后支持远程服务，须作为发送 PDF 的独立模式明确启用。可选 API key 使用环境变量或本机独立凭据配置，不写入普通导出、日志或 Git。

## 九、实施阶段与交付物

| 阶段 | 依赖 | 主要工作 | 阶段验收 |
| --- | --- | --- | --- |
| M0 文档基线 | 无 | README 与本计划、上游依据、数据模型、范围和验收约定 | 本轮仅交付这两个 Markdown 文件 |
| M1 本地文件与持久化 | M0 | 项目骨架、连接与迁移、目录选择、两种登记模式、内容去重、PDF HTTP 打开 | 选择文件 → 记录 URI/hash → 重启 → 再次打开；路径与权限边界可验证 |
| M2 解析与证据 | M1 | 元数据、文字/布局、区段与条目解析、可选本机 GROBID、运行快照、持久任务、人工修订 | 固定样本产生可核对原文与候选；扫描件/失败/部分结果有正确状态；人工决定可保留 |
| M3 匹配与引用图 | M2 | 标识符/书目匹配、无 PDF 节点、接受/拒绝、canonical 合并、有效关系、正反向图查询 | 小型引用集合方向与计数准确；后导入 PDF 补全节点；无误把 Related Items 当引用 |
| M4 可用性与恢复 | M3 | 文件失联/恢复、取消与重试、备份恢复、CSL-JSON/BibTeX/图 JSON 导出、依赖固定、跨平台说明 | Windows、Ubuntu 完成相同闭环；断网、重复导入、崩溃恢复和备份用例通过 |
| M5 可选扩展 | M4 | Zotero 只读导入、OCR、在线补全、外部施引扩展、PDF.js 定位、高级搜索 | 各扩展独立启用与验收，核心离线闭环继续可用 |

每个阶段先完成可以实际使用的纵向流程，再扩展格式覆盖。后续实现提交要同步更新 README 中的真实可用功能和启动方法；未完成模块继续标为计划，不先填“已支持”。

M2 需要定义文献/条目复核的数据接口，M3 完成引用复核和图交互；M1/M2 不以任意模糊规则提前生成已确认引用图。M4 的导出为数据导出，不批量复制或下载第三方 PDF。

## 十、验收方案

本节规定后续实施时的检查，不表示本轮已经运行这些测试。使用人工标注的小型 PDF 集合建立可复核预期，再扩展样本规模；记录样本来源、许可、文件 hash、解析器版本和配置。

### 10.1 功能与异常验收

| 编号 | 场景 | 必须满足的结果 |
| --- | --- | --- |
| A01 | Windows/Ubuntu 允许目录，路径含中文、空格和 `#` | URI 正确；重启后可经 HTTP 打开同一内容 |
| A02 | 浏览器上传，只有文件名 | 记录托管 URI，原始 URI 为空，不出现伪造绝对路径 |
| A03 | 同一 PDF 重复登记、复制到两个目录 | 一个 asset；不同位置可共存；不重复创建 paper 或有效边 |
| A04 | 同一位置被另一份 PDF 覆盖 | 不在旧 file_id 下返回新论文；新建内容身份，旧证据保留 |
| A05 | 原文件移动、丢失、权限变化及恢复 | 文献与关系不丢失，状态明确；相同 hash 重绑定恢复打开 |
| A06 | 越界路径、符号链接、跨站请求 | 不能读取允许范围外文件或执行未经本机会话允许的修改 |
| A07 | PDF HTTP GET、HEAD、Range、非法范围 | 正确类型/inline、完整内容与 206/416；浏览器查看可用 |
| A08 | 本文 DOI 与参考文献 DOI 同时存在 | 不把参考文献标识赋给本论文；候选来源明确 |
| A09 | 单栏、双栏、编号、作者年份、跨页、中文和无 DOI | 保存原始证据；支持范围内正确解析，超出范围进入复核 |
| A10 | 扫描件、加密件、损坏文件、空区段 | 分别显示需 OCR/无法读取/待复核；不谎报“零参考文献解析成功” |
| A11 | 同名论文、近似标题、预印本与正式版 | 不无条件合并；冲突具有可核对理由 |
| A12 | A→B、A→C、C→B；另含一个引用环 | 方向一致、B 的已知施引为 A/C，环不导致无限查询 |
| A13 | 同一引用有多个条目或多个来源 | 图中一条有效边、多个证据；入度按不同引用者计数 |
| A14 | 未识别条目、无 PDF 文献、外部关系 | 展示状态和来源清晰；待确认项不混入已确认计数 |
| A15 | 人工改标题、接受/拒绝匹配后重解析 | 人工决定保留；条目编号变化不造成错配；新冲突进入复核 |
| A16 | 新解析失败/超时/取消/只有部分结果 | 上一完整活动快照可继续查询，不出现半套关系 |
| A17 | 合并与撤销，包含合并后新增编辑 | 原始身份和证据保留；冲突明确；自环不进入正常统计 |
| A18 | 任务重复提交、进程终止、lease 过期及重试 | 可恢复、可解释、幂等；过期 worker 不覆盖新结果 |
| A19 | 全部网络关闭，含本机 GROBID | 核心流程可用；实际请求参数关闭 consolidation，无隐式云端回退 |
| A20 | 活跃数据库备份与异机恢复 | 数据库/工件清单一致；旧路径重新校验；人工决定和关系可重建 |
| A21 | 超过图的深度/节点/边上限 | 响应有界并显示截断，不静默宣称完整网络 |

### 10.2 解析与匹配质量

分别统计区段定位、条目切分、元数据字段提取、实体匹配和最终引用边的质量，不用单一“成功率”遮蔽错误。至少报告：样本数与格式分布、条目级 precision/recall、自动接受匹配的错误率、未解决/歧义比例、人工修订数量和按失败类型的统计。

模糊匹配阈值在开发样本上选择，在独立保留样本上评估；首期模糊结果默认人工接受。置信分数仅表示规则评分，未经校准不得显示为“正确概率”。应公开失败样本类型和支持范围，不预先声称任意 PDF 都能准确解析。

### 10.3 性能与资源

分别测量文件哈希、文字/布局提取、结构化解析、匹配、SQLite 发布、图查询和浏览器渲染时间，记录 PDF 页数、大小、文献/边数量、硬件及软件版本。先验收单篇及小批量，再测 100、1,000、10,000 文献规模的合成图数据；自动解析吞吐与纯图查询性能分别报告。

测试需覆盖解析限额、取消响应和 UI 查询不被长事务阻塞，不在未测量前写入固定速度承诺。图渲染采用有界子图；数据库维护和备份操作不得造成来源信息丢失。

## 十一、需要在实现中持续遵守的决定

1. 本地 URI 与 Web 打开链接分开，原位登记是主路径，上传副本的原始位置允许未知。
2. 原始 PDF 只读；文献、内容、位置、条目、判断分别建模。
3. 核心可离线独立使用；Zotero、OCR、GROBID 增强和联网 provider 均有明确边界。
4. 引用关系有方向、有来源、有当前/历史状态；被引用查询只覆盖已知证据。
5. 不可靠的自动结果进入候选，不用猜测填充书目字段或确认引用。
6. 重新解析可追溯、任务可恢复，人工决定和最后完整结果受到保护。
7. 文献合并保留原始身份，图边按 canonical 端点去重；版本关系和相似关系另行处理。
8. 后续每次发布以实际运行和验收结果更新文档；本轮完成的是设计基线。

返回[项目 README](../README.md)。

