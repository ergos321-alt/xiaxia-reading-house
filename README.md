# Xiaxia Reading House V1

一间私人双人 AI 共读空间。Supabase PostgreSQL 只保存结构化数据、正文文本和稳定锚点；原始 EPUB/TXT、封面及 EPUB 正文图片保存在私有 Supabase Storage。服务器只提供书籍事实、文本、状态与持久化能力，不生成林知夏人格内容。

本次 V1 集中返修保留了既有 EPUB/TXT 解析、章节顺序、用户阅读进度及 `block ID + character offsets` 批注定位模型，并补齐：EPUB2/EPUB3 封面兼容、异常标题回退与书籍信息编辑、滚动/分页双模式、Android Chrome Selection 入口、批注编辑/删除/总览/精确跳转、林知夏独立想法、长章节分块 Action 阅读、AI chunk checkpoint 和 12 秒轻量自动同步。

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
├── schema.sql
├── openapi.yaml
├── requirements.txt
├── Procfile
├── .env.example
├── migrations/
│   ├── 001_prepare_storage_refactor.sql
│   ├── 002_finalize_storage_refactor.sql
│   └── 003_v1_experience_refactor.sql
├── scripts/
│   └── migrate_legacy_bytea_to_storage.py
├── templates/
│   ├── login.html
│   ├── library.html
│   ├── reader.html
│   ├── annotations_overview.html
│   └── error.html
├── static/
│   ├── css/style.css
│   └── js/
│       ├── library.js
│       ├── reader-utils.js
│       ├── reader.js
│       └── annotations-overview.js
└── tests/
    ├── test_epub_parser.py
    ├── test_api_auth.py
    ├── test_co_reading_api.py
    ├── test_v1_experience_api.py
    ├── test_reader_js.py
    ├── test_storage.py
    └── test_consistency.py
```

## 架构与持久化边界

- Flask 是唯一数据入口。浏览器使用私人密码 Session；Custom GPT Action 使用 `Authorization: Bearer <ACTION_API_TOKEN>`。
- `books`、`chapters`、`reading_progress`、用户 `annotations`、`annotation_replies`、独立 `xiaxia_thoughts` 与 `ai_reading_state` 存在 PostgreSQL。
- `books.source_object_path` 与 `book_assets.object_path` 只保存 Storage object path；`book_assets` 另存 MIME type 和 byte size，不存在长期 `bytea`。
- 私有 bucket `xiaxia-reading-house-private` 保存原始上传文件、封面与 EPUB 图片。所有对象保持 `public=false`，且不向 `anon`/`authenticated` 提供对象 policy。
- 浏览器通过鉴权后的 Flask 资源代理读取封面和正文图片，不接触 service role/Secret key，也不获得永久公开 URL。
- Render 临时磁盘只由 EbookLib 在上传解析期间短暂使用；解析结束即释放，绝不承担持久化。
- 用户批注保存 `chapter_id + start/end block_id + start/end offset`。正常定位只使用这些稳定坐标；`selected_text + prefix/suffix` 仅在出版方 DOM 异常时作为恢复 fallback。
- 林知夏对用户批注的 reply 与林知夏独立 `xiaxia_thoughts` 是两种数据模型。独立想法支持 `range`、`block`、`chapter` 三种 scope，并保留可选 `mark_type`，本轮没有扩展复杂颜色 UI。

## EPUB 元数据规则

- EPUB3 优先识别 manifest item 的 `properties="cover-image"`。
- EPUB2 识别 `<meta name="cover" content="manifest-id">` 指向的图片 item。
- 之后依次使用 EbookLib cover item、常见 cover ID/文件名、单图和最大图片 fallback。
- 命中的封面继续上传到私有 Storage，PostgreSQL 只保存资源元数据。
- metadata title 非空且不超过 160 个字符时使用 metadata；为空或明显异常过长时回退到上传文件名去扩展名。
- 不从正文首段或宣传文案猜书名。书架“编辑信息”可手动修改 title 和 author，并持久化到 PostgreSQL。

## 数据库安装与迁移

### 新部署

在 Supabase SQL Editor 完整运行 `schema.sql`。它会创建全部表、索引、更新时间触发器、RLS，并创建：

- bucket：`xiaxia-reading-house-private`
- `public=false`
- 50 MB 文件上限
- 阻止 `anon`/`authenticated` 访问该 bucket 的 restrictive policies

完成后到 Storage 页面再次确认 bucket 显示为 **Private**，且没有为浏览器角色添加读、写、更新或删除 policy。

### 已部署上一轮 Storage 版 V1

不需要清库，也不需要重新导入书籍。执行顺序：

1. 备份 Supabase PostgreSQL。
2. 在 SQL Editor 运行 `migrations/003_v1_experience_refactor.sql`。
3. 确认事务成功提交。
4. 部署本项目新代码。
5. 运行下文网页、Action 与 Android 手工验收。

`003` 是可重复执行的增量事务：新增 `xiaxia_thoughts`，并给 `ai_reading_state` 增加 `last_chunk_index`、`last_block_id`、`chapter_completed`；不删除或改写现有 books、chapters、progress、annotations、replies 或 Storage metadata。

### 仍是最早的 bytea 版

1. 备份数据库。
2. 运行 `migrations/001_prepare_storage_refactor.sql`。
3. 配置全部 Supabase/Storage 环境变量。
4. 运行 `python scripts/migrate_legacy_bytea_to_storage.py`。
5. 成功后运行 `migrations/002_finalize_storage_refactor.sql`。
6. 再运行 `migrations/003_v1_experience_refactor.sql`。
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
| `DB_POOL_MAX` | 否 | 默认 5 |
| `COOKIE_SECURE` | 否 | Render HTTPS 保持 true |

`SUPABASE_SERVICE_ROLE_KEY` 只能存在于 Render 后端环境变量或本地未提交 `.env`；不得写进 Git、HTML、JavaScript、OpenAPI 或聊天正文。本轮没有新增外部服务，也没有新增必填环境变量。

## Render 部署

1. 把整个目录提交到 Git 仓库，Render 新建 Python Web Service。
2. Build Command：`pip install -r requirements.txt`
3. Start Command：`gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 app:app`
4. Health Check Path：`/health`
5. 配置上表七个必填环境变量后部署。

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

### 林知夏自主连续阅读顺序

1. `listBooks` 取得 `book_id`。
2. `listBookChapters` 取得真实顺序的 `chapter_id`、`chapter_index`、title、word_count。
3. 对任意章调用 `getReadingContext(chapter_id, chunk_index=0)`。
4. 读取响应中的 `blocks[]`；每个 block 都有与网页批注同源的 `block_id`、`block_order`、segment start/end offset 和 text。
5. 若 `chunk_count > 1`，按 `chunk_index=1, 2, ... chunk_count-1` 连续读取。未读完所有 chunk 前不得声称已读完整章。
6. 每读完一个 chunk 调用 `saveAiProgress` 保存 chapter、chunk_index 和可选 last_block_id；只有最后一个 chunk 才传 `chapter_completed=true`，后端会验证。
7. 若要留下独立想法，调用 `createXiaxiaThought`：
   - `scope=range`：使用实际读到的 start/end block ID 与字符 offsets；
   - `scope=block`：传 block_id；
   - `scope=chapter`：不传文本 range。
8. 处理用户批注时可使用 `listPendingAnnotations`，再调用 `markAnnotationSeen` 或 `replyToAnnotation`。

传 `annotation_id` 给 `getReadingContext` 时，响应的 `focus_annotation_chunk_index` 明确标出该批注所属 chunk。默认不会把整本书或完整长章节塞进单次 Action。

## API 契约

| 能力 | Method + Endpoint | 调用方 |
| --- | --- | --- |
| 书架 | `GET /api/books` | 网页 / Action |
| 上传 EPUB/TXT | `POST /api/books` | 网页 |
| 书籍、网页目录与进度 | `GET /api/books/{book_id}` | 网页 |
| 编辑书名/作者 | `PATCH /api/books/{book_id}` | 网页 |
| AI 章节目录 | `GET /api/books/{book_id}/chapters` | Action |
| 网页完整章节 HTML | `GET /api/books/{book_id}/chapters/{chapter_id}` | 网页 |
| 章节批注与 12 秒同步数据 | `GET /api/books/{book_id}/chapters/{chapter_id}/annotations` | 网页 |
| 本书批注总览/筛选 | `GET /api/books/{book_id}/annotations` | 网页 / Action |
| 用户阅读进度 | `PUT /api/books/{book_id}/progress` | 网页 |
| 新建用户划线/批注 | `POST /api/annotations` | 网页 |
| 编辑/补写用户批注 | `PATCH /api/annotations/{annotation_id}` | 网页 / Action |
| 删除批注及关联 reply | `DELETE /api/annotations/{annotation_id}` | 网页 / Action |
| 当前双方阅读状态 | `GET /api/reading/state` | Action |
| 分块章节上下文 | `GET /api/reading/context` | Action |
| 待处理用户批注 | `GET /api/annotations/pending` | Action |
| 标记用户批注已读 | `POST /api/annotations/{annotation_id}/seen` | Action |
| 写入林知夏 reply | `POST /api/annotations/{annotation_id}/reply` | Action |
| 林知夏独立想法 | `POST /api/xiaxia/thoughts` | Action |
| 林知夏 chunk checkpoint | `POST /api/ai/progress` | Action |

既有 endpoint 没有改名。新增接口的 Flask route、数据库字段、前端 fetch、OpenAPI path/operationId、测试和本文档使用同一套字段名。

## 阅读器与定位

- 滚动模式保持原有整章连续阅读。
- 分页模式只用 CSS columns/viewport 布局同一份章节 DOM；没有切碎或重建 publisher block，因此稳定 block ID 和字符 offsets 不变。
- 手机使用左右滑动与两侧点击按钮；桌面使用两侧按钮和方向键。viewport、字号、图片加载或方向变化后动态重算页数。
- 切换模式、重新分页和后台批注同步前先记录可见 block；之后用同一 block 恢复滚动位置或计算其所在页。
- 用户进度继续保存 block_id、char_offset、scroll_fraction，并附带 display_mode、page_index/page_count；恢复优先使用稳定 block。
- 总览跳转传 `chapter_id + annotation_id/thought_id`，阅读器打开对应章节后定位原 anchor。滚动模式滚到 mark，分页模式计算 mark 所在 column；目标短暂闪亮。只有精确 anchor 失效时才使用 selected_text/context fallback。

## Android Chrome Selection 修复策略

没有禁用系统文本选择，也没有替换稳定锚点模型。实现策略：

1. 同时监听 `selectionchange`、`pointerup`、`touchend` 和 `contextmenu`，并在 120/320/620 ms 等延时多次读取 Selection，适应 Android Chrome 长按手柄与原生菜单的异步更新。
2. 一旦得到有效选择，立即 `cloneRange()`，换算并保存同一章节 DOM 的 block ID 与字符 offsets；原生菜单随后清空 Selection 时仍能继续“划线/写想法”。
3. 自定义菜单不拦截正文长按，不设置 `user-select:none`；只在用户点击自定义菜单时短暂阻止 pointerdown 导致的 Selection 丢失。
4. 粗指针设备把菜单放在 `visualViewport.offsetTop + height` 内的底部安全区域；键盘、浏览器工具栏或选择控件改变 visual viewport 时重新定位，避免入口被遮挡。
5. 有活动 Selection、批注编辑窗口或菜单交互时，12 秒轮询不更新 marks，避免后台同步破坏选择。

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
python -m py_compile app.py annotations.py auth.py database.py epub_parser.py reading.py storage.py
node --check static/js/reader-utils.js
node --check static/js/reader.js
node --check static/js/library.js
node --check static/js/annotations-overview.js
```

测试覆盖 EPUB2/EPUB3 cover、异常 title fallback、书籍信息编辑、批注编辑/删除/cascade/补写、总览筛选与跳转字段、独立 AI thought、精确 range、长短章节 chunk、annotation 所属 chunk、书籍目录导航、AI checkpoint 与完成状态、原有用户进度、12 秒同步 API 数据、私有 Storage、Bearer auth，以及 OpenAPI/Flask/SQL/JS/README 跨文件一致性。

## 实机验收

### 桌面

1. 上传真实 EPUB2、EPUB3 和 TXT，检查封面、标题回退、目录与图片。
2. 编辑 title/author，刷新书架确认持久化。
3. 滚动/分页切换，调整字号和窗口，确认附近阅读位置不丢失。
4. 使用左右按钮和键盘方向键翻页，刷新后确认进度恢复。
5. 新建纯划线，点击后“添加想法”，再编辑；分别删除无 reply 和有 reply 的批注，确认后者出现明确警告。
6. 打开批注总览，逐一测试四个筛选以及滚动/分页精确跳回和短暂高亮。

### Android Chrome

1. 使用真实 Android Chrome 登录并打开一章，分别在滚动和分页模式操作。
2. 长按正文、拖动选择手柄；确认系统“复制/分享/全选/网页搜索”等菜单仍可使用。
3. 保持选择，确认 Reading House 的“划线/写想法”入口稳定显示在可见 viewport 底部。
4. 分别保存纯划线和带批注；打开软键盘后重复测试，确认入口与对话框不被遮挡。
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
