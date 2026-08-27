# Xiaxia Reading House V2

一间私人双人 AI 共读空间。Supabase PostgreSQL 只保存结构化数据、正文文本和稳定锚点；原始 EPUB/TXT、封面及 EPUB 正文图片保存在私有 Supabase Storage。服务器只提供书籍事实、文本、状态与持久化能力，不生成林知夏人格内容。

V2 以已验收的 V1.1 为唯一基础，保留章节/block、批注、Reply、Undo、进度与现有 Action 契约；在书籍读完以后增加私人记忆层。当前可靠性返修只加强“不同 EPUB → 既有 normalized chapter/block 模型”的入口，不改变稳定锚点或历史阅读数据。

## 项目目录

```text
xiaxia-reading-house/
├── app.py
├── auth.py
├── database.py
├── storage.py
├── epub_parser.py
├── reading.py
├── annotations.py
├── management.py
├── operations.py
├── memories.py
├── schema.sql
├── openapi.yaml
├── requirements.txt
├── Procfile
├── EPUB_IMPORT_PROFILE.md
├── .env.example
├── migrations/
│   ├── 001_prepare_storage_refactor.sql
│   ├── 002_finalize_storage_refactor.sql
│   ├── 003_v1_experience_refactor.sql
│   ├── 004_v1_1_management.sql
│   └── 005_v2_reading_memories.sql
├── scripts/
│   ├── migrate_legacy_bytea_to_storage.py
│   └── profile_epub_memory.py
├── templates/
│   ├── login.html
│   ├── library.html
│   ├── reader.html
│   ├── after_reading.html
│   ├── annotations_overview.html
│   ├── annotations_management.html
│   └── error.html
├── static/
│   ├── css/style.css
│   └── js/
│       ├── library.js
│       ├── reader-utils.js
│       ├── reader.js
│       ├── after-reading.js
│       ├── annotations-overview.js
│       └── annotations-management.js
└── tests/
    ├── test_epub_parser.py
    ├── test_epub_compatibility.py
    ├── test_import_pipeline.py
    ├── test_api_auth.py
    ├── test_co_reading_api.py
    ├── test_v1_experience_api.py
    ├── test_v1_1_management.py
    ├── test_reader_js.py
    ├── test_storage.py
    ├── test_consistency.py
    └── test_v2_memories.py
```

## 架构与持久化边界

- Flask 是唯一数据入口。浏览器使用私人密码 Session；Custom GPT Action 使用 `Authorization: Bearer <ACTION_API_TOKEN>`。
- `books`、`chapters`、`reading_progress`、用户 `annotations`、`annotation_replies`、独立 `xiaxia_thoughts`、`thought_user_replies`、候选记录、操作日志与 `ai_reading_state` 存在 PostgreSQL。V2 另用独立表保存逐章 AI 完成事实、整本完成状态、最终评价、读后信与稀疏记忆事件。
- `books.source_object_path` 与 `book_assets.object_path` 只保存 Storage object path；`book_assets` 另存 MIME type 和 byte size，不存在长期 `bytea`。
- 私有 bucket `xiaxia-reading-house-private` 保存原始上传文件、封面与 EPUB 图片。所有对象保持 `public=false`，且不向 `anon`/`authenticated` 提供对象 policy。
- 浏览器通过鉴权后的 Flask 资源代理读取封面和正文图片，不接触 service role/Secret key，也不获得永久公开 URL。
- importer 直接按 ZIP container → OPF → NAV/NCX → spine/manifest fallback 分阶段读取；不会整本解压到磁盘或内存，也不会读取字体、CSS 与未使用图片。
- 用户批注保存 `chapter_id + start/end block_id + start/end offset`。正常定位只使用这些稳定坐标；`selected_text + prefix/suffix` 仅在出版方 DOM 异常时作为恢复 fallback。
- 四类阅读痕迹在数据层明确分开：`owner=user/content_type=user_annotation`、`owner=xiaxia/content_type=xiaxia_thought`、`owner=xiaxia/content_type=xiaxia_reply`、`owner=user/content_type=user_reply`。独立想法支持 `range`、`block`、`chapter` 与可扩展 `mark_type`，没有扩展复杂颜色 UI。

## EPUB 元数据规则

- EPUB3 优先识别 manifest item 的 `properties="cover-image"`。
- EPUB2 识别 `<meta name="cover" content="manifest-id">` 指向的图片 item。
- 之后尝试 EPUB2 guide 中的封面页与常见 cover ID/文件名；损坏或缺失时采用“无封面”，不会误把任意最大图片当封面。
- 命中的封面继续上传到私有 Storage，PostgreSQL 只保存资源元数据。
- metadata title 非空且不超过 160 个字符时使用 metadata；为空或明显异常过长时回退到上传文件名去扩展名。
- 不从正文首段或宣传文案猜书名。书架“编辑信息”可手动修改 title 和 author，并持久化到 PostgreSQL。

## EPUB 导入可靠性

- EPUB2/EPUB3 同时支持 `toc.ncx`、`nav.xhtml`、spine 与 manifest 正文 fallback。NAV/NCX、guide、metadata、cover、CSS、font 或单张图片损坏时降级，不阻断其余正文。
- href 会统一处理 fragment、URL encoding、相对路径、反斜杠与大小写差异；重复 spine item、空章节和不存在的 TOC 目标会跳过并写入轻量 warning。
- 上传请求先流式落到单次导入临时文件并计算 SHA-256，不再长期保留整本 raw bytes。正文只读取当前 XHTML；图片只分块校验并保存 archive entry/size/hash，生产路径的 `ParsedAsset` 不保存 image bytes。
- 只把正文实际引用的图片与有效封面写入 private Storage；必要图片按 ZIP entry 流式解压、最多 2 路上传，不保存未使用图片、字体或 CSS。内容相同的图片复用同一 private object，但每个原始 asset path 仍保留独立 metadata。
- 全书先解析并验证出有效 normalized chapters，再把 normalized HTML/text 暂存到本次导入目录，随后上传 Storage 和写数据库。chapters/assets 以生成器批量写入；数据库异常会回滚事务，Storage 或数据库任一阶段失败都会按本次 book UUID 清理已确认上传的对象。
- 进程内只允许一本书进入导入重资源区；并发上传返回 `import_resource_exhausted`。Supabase client 首次初始化加锁，避免内部上传线程重复创建 client。
- Gunicorn hard timeout 保持 120 秒；应用在 105 秒设置协作式安全截止，能够在 worker hard kill 前返回 `epub_import_timeout` 并清理本次导入。
- 导入日志逐阶段记录 filename、compressed/uncompressed byte size、archive/XHTML/image/font 数量、EPUB version、manifest/spine/chapter/asset 数量、parse/storage/database/total duration、current/peak RSS、warning count 与 failure stage，不记录正文。

稳定错误码包括：`unsupported_archive`、`invalid_epub`、`no_readable_content`、`epub_parse_failed`、`epub_import_timeout`、`storage_upload_failed`、`database_write_failed`、`import_worker_terminated` 与 `import_resource_exhausted`。浏览器只在 import 收到无 JSON 的 upstream 502/503 时推断 worker/resource 分类；后端明确返回的 Storage 错误不会被覆盖。响应只返回用户可理解的短消息，不暴露 traceback、连接串或数据库细节。

三本真实失败 EPUB 的 archive 结构、修改前后阶段 RSS、2×4 并发压力结果与完整导入结果见 `EPUB_IMPORT_PROFILE.md`。

EPUB 内部链接在导入时规范化，章节 API 再映射到真实 Reading House `chapter_id + block_id + fragment`。同页/跨页脚注会在当前页打开轻量注释纸片；普通章节链接和 backlink 在阅读器内部跳转。缺失脚注显示 `footnote_target_missing`，浏览器不会导航到原 EPUB 相对路径或 404。

## 数据库安装与迁移

### 新部署

在 Supabase SQL Editor 完整运行 `schema.sql`。它会创建全部表、索引、更新时间触发器、RLS，并创建：

- bucket：`xiaxia-reading-house-private`
- `public=false`
- 50 MB 文件上限
- 阻止 `anon`/`authenticated` 访问该 bucket 的 restrictive policies

完成后到 Storage 页面再次确认 bucket 显示为 **Private**，且没有为浏览器角色添加读、写、更新或删除 policy。

### 已部署并已有真实数据的 V1 / V1.1

不需要清库，也不需要重新导入书籍。执行顺序：

1. 备份 Supabase PostgreSQL。
2. 若尚未运行过，先运行 `migrations/003_v1_experience_refactor.sql`。
3. 运行 `migrations/004_v1_1_management.sql`。
4. 运行 `migrations/005_v2_reading_memories.sql`。
5. 确认事务成功提交，再部署本项目新代码。
6. 运行下文网页、Action 与 Android 手工验收。

`004` 是可重复执行的 V1.1 增量事务。`005` 也是可重复执行的增量事务，只新增 V2 表、索引、触发器、RLS 和保守里程碑回填：它不会删除或改写现有 books、chapters、progress、annotations、Thought、Reply 或 Storage metadata。不要重新运行 `schema.sql` 代替 migration。

`005` 只把现有 `percentage=100` 的用户进度认作已完成；林知夏方面只回填旧 checkpoint 能证明的那一个已完成章节，不会用“最后一次停在末章”冒充读完整本。后续每个 `chapter_completed=true` 的最终 chunk 会写入 `ai_chapter_completions`，只有真实章节全部齐全才标记整本完成。若封底 API 返回 `v2_migration_required`，表示部署代码已更新但此 migration 尚未成功提交；既有用户/AI 进度仍会保存，执行 `005` 后重新访问即可，不要清库或重跑 `schema.sql`。

### 仍是最早的 bytea 版

1. 备份数据库。
2. 运行 `migrations/001_prepare_storage_refactor.sql`。
3. 配置全部 Supabase/Storage 环境变量。
4. 运行 `python scripts/migrate_legacy_bytea_to_storage.py`。
5. 成功后运行 `migrations/002_finalize_storage_refactor.sql`。
6. 再按顺序运行 `003_v1_experience_refactor.sql`、`004_v1_1_management.sql` 与 `005_v2_reading_memories.sql`。
7. 部署新代码。

`002` 只有在每条旧资产都已有 `object_path` 后才删除旧 `data` 列；未迁移记录会使脚本中止，不会静默丢失图片。旧架构没有原始文件可迁移；新上传书籍会正常保留原始 EPUB/TXT。

## 环境变量

必须由项目所有者本人提供真实值；工程不含假密钥或默认密码。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 是 | Supabase Session pooler PostgreSQL 连接串，建议 `sslmode=require` |
| `SUPABASE_URL` | 是 | Supabase Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | 是 | 仅后端保存的 Secret/service_role key |
| `SUPABASE_STORAGE_BUCKET` | 是 | `xiaxia-reading-house-private` |
| `PRIVATE_ACCESS_PASSWORD` | 是 | 私人网页访问密码 |
| `ACTION_API_TOKEN` | 是 | Custom GPT Action Bearer token |
| `FLASK_SECRET_KEY` | 是 | 独立随机 Session 签名密钥 |
| `MAX_UPLOAD_MB` | 否 | 默认 50 |
| `DB_POOL_MIN` | 否 | 默认 1，免费实例可设 0 |
| `DB_POOL_MAX` | 否 | 默认 2，与单 worker / 2 threads 对齐 |
| `COOKIE_SECURE` | 否 | Render HTTPS 保持 true |

`SUPABASE_SERVICE_ROLE_KEY` 只能存在于 Render 后端环境变量或本地未提交 `.env`；不得写进 Git、HTML、JavaScript、OpenAPI 或聊天正文。本轮没有新增外部服务，也没有新增必填环境变量。

## Render 部署

1. 把整个目录提交到 Git 仓库，Render 新建 Python Web Service。
2. Build Command：`pip install -r requirements.txt`
3. Start Command：`gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120 app:app`
4. Health Check Path：`/health`
5. 配置上表七个必填环境变量后部署。

本次 EPUB 可靠性返修没有数据库 migration。已运行 V2 的实例只需部署新代码；不重跑 `schema.sql`，不重新导入既有书籍。

健康检查结果：

- `{"status":"ok"}`：数据库、环境变量和私有 bucket 可用。
- `configuration_required`：响应列出缺失变量。
- `database_unavailable`：检查 Session pooler、密码与 SSL。
- `storage_unavailable_or_bucket_public`：检查 URL、server key、bucket 名，并确认 bucket 仍是 Private。

任何必填值缺失时，除 `/health` 和静态文件外服务会返回 503，不会用假值继续运行。

## Custom GPT Action 配置

`openapi.yaml` 已固定真实 HTTPS origin：

```text
https://xiaxia-reading-house.onrender.com
```

在 Custom GPT Actions 导入该文件，认证选择 Bearer/API Key，并填写与 Render `ACTION_API_TOKEN` 完全相同的真实 token。Schema 不暴露网页 Session 登录、上传、浏览器资源代理或用户进度写入接口。

Schema 采用 Actions 兼容的保守写法：路径参数在每个 operation 内直接展开；所有 object 都显式声明 `properties`；没有 `objecta`、server variables、discriminator、`oneOf`/`anyOf`；operationId 唯一；所有 operation description 均少于 300 字符。

本阶段按产品边界冻结 `openapi.yaml`：V2 Web/数据库能力没有加入 Custom GPT Action Schema，既有 operationId、endpoint 和字段均未改变。后端已为 Xiaxia 最终评价和读后信预留独立 Bearer 身份路由，但当前 GPT 不会从冻结 Schema 中发现它们；待单独的 Action 升级阶段再公开，不应手工改动现有 Schema。

### 林知夏自主连续阅读顺序

1. `listBooks` 取得 `book_id`。
2. `listBookChapters` 取得真实顺序的 `chapter_id`、`chapter_index`、title、word_count。
3. 对任意章调用 `getReadingContext(chapter_id, chunk_index=0)`。
4. 读取响应中的 `blocks[]`；每个 block 都有与网页批注同源的 `block_id`、`block_order`、segment start/end offset 和 text。
5. 若 `chunk_count > 1`，按 `chunk_index=1, 2, ... chunk_count-1` 连续读取。未读完所有 chunk 前不得声称已读完整章。
6. 每读完一个 chunk 调用 `saveAiProgress`，回传 context 给出的 `chapter_id + chunk_index + chunk_id` 和可选 `last_block_id`；只有最后一个 chunk 才传 `chapter_completed=true`。完成状态会把缺失或陈旧 block 归一到本章最后一个合法 block，跨章 block 在中途 checkpoint 仍会被拒绝。
7. 一章有多条候选时，优先使用 `previewXiaxiaThoughts` 精确验证，再以 `commitXiaxiaThoughts` 一次提交；Preview 只写 24 小时候选记录，不生成正式 Thought。
8. 单条可用 `createXiaxiaThought`：
   - `scope=range`：使用实际读到的 start/end block ID 与字符 offsets；
   - `scope=block`：传 block_id；
   - `scope=chapter`：不传文本 range。
9. 可通过 `listXiaxiaThoughts`、`updateXiaxiaThought`、`deleteXiaxiaThought` 管理自己的 Thought，通过 Reply CRUD 管理自己对用户批注的回复；`undoLastReadingAction` 只撤销 Xiaxia 最近一次且无后续冲突的操作。
10. 处理用户批注时可使用 `listPendingAnnotations`，再调用 `markAnnotationSeen` 或 `replyToAnnotation`。

传 `annotation_id` 给 `getReadingContext` 时，响应的 `focus_annotation_chunk_index` 明确标出该批注所属 chunk。默认不会把整本书或完整长章节塞进单次 Action。

## API 契约

| 能力 | Method + Endpoint | 调用方 |
| --- | --- | --- |
| 书架 | `GET /api/books` | 网页 / Action |
| 上传 EPUB/TXT | `POST /api/books` | 网页 |
| 书籍、网页目录与进度 | `GET /api/books/{book_id}` | 网页 |
| 编辑书名/作者 | `PATCH /api/books/{book_id}` | 网页 |
| 删除书籍及 Storage 对象 | `DELETE /api/books/{book_id}` | 网页 |
| AI 章节目录 | `GET /api/books/{book_id}/chapters` | Action |
| 网页完整章节 HTML | `GET /api/books/{book_id}/chapters/{chapter_id}` | 网页 |
| 章节批注与 12 秒同步数据 | `GET /api/books/{book_id}/chapters/{chapter_id}/annotations` | 网页 |
| 本书批注总览/筛选 | `GET /api/books/{book_id}/annotations` | 网页 / Action |
| 用户阅读进度 | `PUT /api/books/{book_id}/progress` | 网页 |
| 新建用户划线/批注 | `POST /api/annotations` | 网页 |
| 编辑/补写用户批注 | `PATCH /api/annotations/{annotation_id}` | 网页 |
| 删除批注及关联 reply | `DELETE /api/annotations/{annotation_id}` | 网页 |
| 全局痕迹筛选 | `GET /api/management/traces` | 网页 |
| 批量删除用户痕迹 / Undo | `POST /api/management/traces/batch-delete`、`POST /api/management/undo` | 网页 |
| 当前双方阅读状态 | `GET /api/reading/state` | Action |
| 分块章节上下文 | `GET /api/reading/context` | Action |
| 待处理用户批注 | `GET /api/annotations/pending` | Action |
| 标记用户批注已读 | `POST /api/annotations/{annotation_id}/seen` | Action |
| 写入林知夏 reply | `POST /api/annotations/{annotation_id}/reply` | Action |
| 修改/删除林知夏 reply | `PATCH` / `DELETE /api/annotations/{annotation_id}/reply` | Action |
| 林知夏独立想法 | `POST /api/xiaxia/thoughts` | Action |
| Thought 列表/修改/删除 | `GET /api/xiaxia/thoughts`、`PATCH` / `DELETE /api/xiaxia/thoughts/{thought_id}` | Action |
| Thought 批量删除 | `POST /api/xiaxia/thoughts/batch-delete` | Action |
| Thought Preview/Commit | `POST /api/xiaxia/thoughts/preview`、`POST /api/xiaxia/thoughts/commit` | Action |
| 用户回复 Xiaxia Thought | `POST` / `PATCH` / `DELETE /api/xiaxia/thoughts/{thought_id}/reply` | 网页 |
| 林知夏 chunk checkpoint | `POST /api/ai/progress` | Action |
| 撤销 Xiaxia 最近操作 | `POST /api/actions/undo` | Action |
| 封底聚合数据 | `GET /api/books/{book_id}/back-cover` | 网页 |
| 明确完成整本书 | `POST /api/books/{book_id}/completion` | 网页 |
| 共同停留处 | `GET /api/books/{book_id}/shared-stops` | 网页 |
| 用户最终评价 | `PUT /api/books/{book_id}/reflection` | 网页 |
| 用户读后信 | `PUT /api/books/{book_id}/letter` | 网页 |
| Xiaxia 最终评价/读后信（暂未进 Schema） | `PUT /api/xiaxia/books/{book_id}/reflection`、`PUT /api/xiaxia/books/{book_id}/letter` | 后端 Action 身份预留 |

既有 endpoint 没有改名、没有改变返回结构。V2 Web 接口的 Flask route、数据库字段、前端 fetch、测试和本文档使用同一套字段名；按本阶段要求，V2 路径不写入冻结的 `openapi.yaml`。

## V2 读完以后

- 用户只能在真实最后一章末尾点击“合上正文”完成；前端先用原进度 API 保存最终章节、稳定 block 和 100%，服务器再校验 `chapter_index=最后一章` 且 `percentage>=99.5`，不能从任意页面伪造完成。
- 林知夏沿用原 `/api/ai/progress`。每个通过最终 chunk 校验的章节另记一条 `ai_chapter_completions`；只有数量与该书真实 `chapter_count` 一致才产生 `xiaxia_completed_at`。当前 checkpoint 的响应格式不变。
- `book_reflections` 每书每方一条。读取响应在服务器端按 user/xiaxia perspective 裁剪：揭示前只返回自己的评分/正文以及对方是否提交，不向浏览器传对方隐藏正文；第二方提交后原子写入 `reflections_revealed_at`，两张纸条同时开放并锁定。
- 共同停留处不建立易漂移的结果表。读取时直接使用现有用户 annotation 与 `scope=range/block` 的 Xiaxia Thought，在同一章节的同源 `block_id + offsets` 上按“完全相同 → 范围重叠 → 同一 block”匹配并聚合；chapter Thought 和双方 Reply 不参与。跳回正文仍传原 `chapter_id + annotation_id`。
- `reading_letters` 分开保存 user→xiaxia 与 xiaxia→user，双方整本完成后才可写入或查看。它是每方一封可继续修改的信纸，不是聊天流。
- `reading_memory_events` 只记录开始阅读、第一次共同停留、共同完成、第一次打开封底；Thought/Reply 的增删改和 Undo 不会进入时间线。
- 共读印章只在 `shared_completed_at` 存在时出现在封底，日期直接来自共同完成时间；不进入正文，不作为徽章或成就。
- 删除书籍仍先清理该书私有 Storage 对象，再删除 `books`。V2 五张表都通过 `book_id ON DELETE CASCADE` 接入现有数据库清理，不会留下记忆孤儿数据。

### V2 首轮验收修复

- 最后一页入口仍先调用既有进度 API，再调用完成 API。V2 稀疏时间线是附加能力：表未就绪时不再连带回滚已经验收的用户/Xiaxia checkpoint；封底接口会以 `503 + v2_migration_required + migration` 明确指出缺少 `005`，不再退化为 `internal_server_error`。
- 封底响应增加 `memory_state`，明确区分等待林知夏完成、等待任一方最终评价及双方评价已揭示；原有字段保持不变。双方整本完成后原子产生 `shared_completed_at`，印章立即按该日期显示。
- Android 创建纯划线或带想法的 annotation 成功后，会取消所有延迟 Selection 捕获、清除浏览器 Selection、清空已冻结 snapshot 并移除浮动纸签定位样式；失败时仍保留 snapshot 供重试，稳定 anchor 不变。
- 分页高度改为 `visualViewport` 与书页容器实际 content box 的交集，不再使用固定 `-60px`。iPad/PC 会在 resize、横竖屏切换、WebFont 加载完成及章节图片加载后重排，并预留行尾保护空间；仍使用同一份章节 DOM 与 block ID。

### Preview → Commit 与 Undo

- `previewXiaxiaThoughts` 对每个候选独立验证 book/chapter、稳定 block、offset 与 selected_text，并返回 `candidate_id + matched_text + anchor + validation_status`。候选保存在 `xiaxia_thought_candidates`，24 小时后失效，不会出现在阅读器或正式总览。
- `commitXiaxiaThoughts` 只接受仍有效且未提交的候选；每个 ID 返回 `committed` 或带具体 error 的 `failed`，成功项一次记录为 `batch_create` 操作。
- `reading_operation_log` 保存 actor、operation_type、target、前后快照与时间。Undo 只处理对应 actor 最近一条尚未撤销的 create/update/delete/batch；若目标版本变化、ID 被复用，或新 Reply 已依赖被撤销对象，会返回 `undo_conflict`，不会覆盖后续数据。

### 删除书籍流程

书架删除仅接受已登录 Web Session，不在 Action Schema 中。后端先读取该书的原始文件及全部 `book_assets.object_path`，分批确认删除私有 Storage 对象；Storage 删除失败时保留 PostgreSQL 数据并返回错误。Storage 成功后删除 `books` 行，既有 `ON DELETE CASCADE` 在同一数据库操作中清理章节、双方进度、批注、Thought、Reply、候选与该书操作日志。操作严格按目标 `book_id` 收集路径，不会扫整个 bucket。

## 阅读器与定位

- 滚动模式保持原有整章连续阅读。
- 分页模式只用 CSS columns/viewport 布局同一份章节 DOM；没有切碎或重建 publisher block，因此稳定 block ID 和字符 offsets 不变。可用高度来自可视 viewport 与 reader shell 内容区的实测交集，并为 WebKit 行盒预留少量安全空间。
- 手机使用左右滑动与两侧点击按钮；桌面使用两侧按钮和方向键。viewport、字号、图片加载或方向变化后动态重算页数。
- 切换模式、重新分页和后台批注同步前先记录可见 block；之后用同一 block 恢复滚动位置或计算其所在页。
- 用户进度继续保存 block_id、char_offset、scroll_fraction，并附带 display_mode、page_index/page_count；恢复优先使用稳定 block。
- 总览跳转传 `chapter_id + annotation_id/thought_id`，阅读器打开对应章节后定位原 anchor。滚动模式滚到 mark，分页模式计算 mark 所在 column；目标短暂闪亮。只有精确 anchor 失效时才使用 selected_text/context fallback。

## Android Chrome Selection 修复策略

没有禁用系统文本选择，也没有替换稳定锚点模型。实现策略：

1. 同时监听 `selectionchange`、`pointerup`、`touchend` 和 `contextmenu`，并在 120/320/620 ms 等延时多次读取 Selection，适应 Android Chrome 长按手柄与原生菜单的异步更新。
2. 一旦得到有效选择，立即 `cloneRange()`，换算并保存同一章节 DOM 的 block ID 与字符 offsets；原生菜单随后清空 Selection 时仍能继续“划线/写想法”。
3. 自定义菜单不拦截正文长按，不设置 `user-select:none`；只在用户点击自定义菜单时短暂阻止 pointerdown 导致的 Selection 丢失。
4. 粗指针设备不再固定在底部：优先放到 Selection Range 下方并留出原生手柄间距；空间不足时移到选区上方，最后才在 `visualViewport` 内夹取安全位置。软键盘、工具栏、滚动或横竖屏改变 viewport 时重新定位。
5. 自定义按钮 `pointerdown` 发生时再次冻结 selection snapshot，然后阻止焦点切换清空原生 Selection；真正写入仍使用已冻结的稳定 block/offset，而不是临时 selected text 搜索。
6. 有活动 Selection、批注编辑窗口或菜单交互时，12 秒轮询不更新 marks，避免后台同步破坏选择。
7. 保存成功后统一执行 Selection 收尾：取消仍在排队的 Android 延迟捕获、`removeAllRanges()`、隐藏并复位浮动纸签，再恢复正常阅读；创建请求始终使用按钮点击前冻结的 snapshot。

这部分包含可自动测试的定位、滑动、分页和同步 helper 测试，但仓库交付环境不等同真实 Android 设备；必须按下文执行实机验收。

## 自动同步

阅读器每 12 秒读取当前章节的 annotations、reply 和 xiaxia_thoughts。基于 ID、更新时间、reply 和 anchor 生成签名：没有变化不操作；只有 reply 变化时只更新状态/弹窗；anchor 集合变化才局部重包 marks。同步前后保存可见 block，因此不刷新整页、不改变滚动位置或分页页码，也不会重复插入已有回复。

## 本地运行与测试

需要 Python 3.11+。运行应用还需要已经执行 schema/migration 的 Supabase 项目。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

配置并导出七个必填值后：

```bash
flask --app app run
```

完整自动化与一致性检查：

```bash
pytest
python -m py_compile app.py annotations.py auth.py database.py epub_parser.py reading.py management.py memories.py operations.py storage.py
node --check static/js/reader-utils.js
node --check static/js/reader.js
node --check static/js/library.js
node --check static/js/annotations-overview.js
node --check static/js/annotations-management.js
node --check static/js/after-reading.js
ruff check .
```

测试覆盖 EPUB2/EPUB3 NAV/NCX、TOC/spine/manifest fallback、编码与大小写路径、异常 metadata/cover/image、可恢复 XHTML、ruby/poem/table、100/300/500 小章节压力、批量 DB 写入、Storage/DB/timeout 回滚、同页/跨页脚注、backlink、缺失目标与稳定 block regression；同时保留书籍管理、批注、Thought/Reply、Preview/Commit、Undo、进度、V2 completion/back-cover/shared stops/reflection/letter 和冻结 Action 契约的完整回归。

## V1.1 UI / UX Refresh

- 全站固定为一套暖白旧书页主题，不增加多主题系统或大型 UI 框架。边界和阴影降低对比，阅读正文使用墨灰而非纯黑。
- 书架首页从既有 `/api/books` 排序结果中找到最近有阅读进度的书，以纸条形式显示书名、当前章节和进度；没有新增 API 或数据库字段。上传入口改为轻量“把一本书带回家”，无封面书籍按书架顺序使用五种克制的浅灰棕封面。
- 用户划线 / 批注使用暖棕双层渐变形成轻微不规则的笔迹；林知夏 Thought / Reply 使用更淡的灰蓝笔迹。两者仍对应原有四类 `owner/content_type`，没有合并状态，也没有新增颜色管理功能。
- range / block Thought 仍由原有 `block_id + character offsets` 创建 mark，只在 CSS 层附着极小灰化猫爪；点击整段 Thought mark 打开既有弹窗。chapter Thought 默认只显示“来过”页边入口，不直接展开内容。
- 阅读器顶栏阅读状态只露出返回、书名、章节与 `···`；目录、模式切换、书页痕迹及字号收进轻量菜单，仍复用原有控件 ID 和事件。分页模式使用透明页边点击区，页码只在主动翻页后短暂显示。
- 点击划线或猫爪后，双方内容从右侧纸页展开；正文和两个人的痕迹优先显示，编辑、删除、回复等管理操作统一收进批注纸的 `···`，对应 API 和权限边界没有改变。
- 移动端继续使用 Selection Range 动态定位纸签式操作栏并冻结 selection snapshot；UI Refresh 未恢复固定底栏，也未禁用系统文本选择。分页左右滑动、页边点击、桌面方向键、自动保存和 12 秒同步逻辑均保留。

## 实机验收

### 桌面

1. 上传真实 EPUB2、EPUB3 和 TXT，检查封面、标题回退、目录与图片。
2. 编辑 title/author，刷新书架确认持久化。
3. 滚动/分页切换，调整字号和窗口，确认附近阅读位置不丢失。
4. 使用左右按钮和键盘方向键翻页，刷新后确认进度恢复。
5. 新建纯划线，点击后“添加想法”，再编辑；分别删除无 reply 和有 reply 的批注，确认后者出现明确警告。
6. 打开批注总览，逐一测试四个筛选以及滚动/分页精确跳回和短暂高亮。
7. 在最后一章末页点击“合上正文，翻到读完以后”，确认其他章节不能提前完成，刷新后封底仍可打开。
8. 提交用户评分与读后话，检查页面只显示自己的原文和“林知夏是否已提交”，不能在 HTML/网络响应中找到她的隐藏正文；双方提交后再确认同时揭示并锁定。
9. 检查共同停留处的 exact、范围重叠与同一段落案例，点击“回到这一页”，确认仍由原 annotation anchor 精确跳回。
10. 双方完成后分别保存两封读后信，刷新确认持久化；确认印章仅在封底出现，时间线没有 Thought/Reply CRUD 日志。

### iPad Safari / WebKit

1. 在竖屏与横屏分别打开长章节分页模式，逐页确认最后一行完整显示，正文不会在仍有空白时被裁切。
2. 改变字号、切换分屏宽度并旋转设备，确认重排后仍停留在同一稳定 block 附近。
3. 等待页面字体与章节图片完成加载，再翻到后续页面，确认页数已经自动更新。
4. 在重排后的页面创建划线、打开 Thought 与 Reply，刷新后确认 anchor、页码和阅读进度均可恢复。

### Android Chrome

1. 使用真实 Android Chrome 登录并打开一章，分别在滚动和分页模式操作。
2. 长按正文、拖动选择手柄；确认系统“复制/分享/全选/网页搜索”等菜单仍可使用。
3. 保持选择，确认 Reading House 的“划线/写想法”入口显示在选区附近，并会在空间不足时切换到另一侧，而不是固定在底部。
4. 分别保存纯划线和带批注；每次保存成功后确认浏览器 Selection 与浮动操作纸签都立即消失，可以继续阅读。打开软键盘后重复测试，确认入口与对话框不被遮挡。
5. 点击纯划线补写 comment、编辑、删除；刷新后检查精确位置。
6. 左右滑动和点击翻页，跨页选择可选文本，并检查保存/恢复位置。
7. 保持文本 Selection 至少 15 秒，确认后台轮询不清除当前选择。

除非在真实设备上完成以上步骤，否则不得声称“Android Chrome 已通过实机测试”。

### Custom GPT Action

1. `listBooks → listBookChapters → 任意 chapter_id → chunk_index=0`。
2. 连续读取到最后一个 chunk，逐块保存 checkpoint，最后一块标记 completed。
3. 新开聊天调用 `getReadingState`，检查 chapter/chunk/last_block/completed 恢复。
4. 根据返回 block 和 offsets 创建 range thought，再在网页确认精确位置。
5. 写入用户 annotation reply，等待不超过 12–15 秒，确认网页不刷新整页即出现。

## V1 边界

没有增加 PDF、Kindle、用户注册、公共书库、社交、全文搜索、标签、统计 Dashboard、语音朗读、推荐算法、AI 自动总结、服务器人格生成、AI 后台自主阅读、多 AI 角色、复杂主题或多色管理。DRM EPUB 不处理。
