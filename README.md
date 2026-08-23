# Xiaxia Reading House V1

一间私人双人 AI 共读空间。结构化数据与正文保存在 Supabase PostgreSQL，原始书籍、封面和 EPUB 图片保存在私有 Supabase Storage；全部状态都独立于聊天窗口。

本项目严格实现 V1 冻结范围：EPUB/TXT 导入、书架、移动优先阅读器、自动进度、文字选择与稳定批注定位、林知夏共读 API、Custom GPT Action Schema。服务器只提供书籍事实与共读状态，不生成林知夏的人格回复。

## 项目目录

```text
xiaxia-reading-house/
├── app.py                    # Flask 应用、私人登录与页面入口
├── auth.py                   # 浏览器 Session + Action Bearer 认证
├── database.py               # PostgreSQL 连接池
├── storage.py                # 私有 Supabase Storage 服务端访问
├── epub_parser.py            # EPUB2/3、TXT 解析与稳定 block ID
├── reading.py                # 书籍、章节、进度与共读上下文 API
├── annotations.py            # 划线、批注、状态与林知夏回应 API
├── schema.sql                # Supabase PostgreSQL 完整建表 SQL
├── openapi.yaml              # Custom GPT Action Schema
├── requirements.txt
├── Procfile
├── .env.example
├── .gitignore
├── migrations/               # 仅供旧 bytea 架构安全迁移
├── scripts/                  # 旧二进制资产迁移工具
├── templates/
│   ├── login.html
│   ├── library.html
│   ├── reader.html
│   └── error.html
├── static/
│   ├── css/style.css
│   └── js/
│       ├── library.js
│       └── reader.js
└── tests/
    ├── test_epub_parser.py
    ├── test_api_auth.py
    ├── test_co_reading_api.py
    ├── test_storage.py
    └── test_consistency.py
```

## 架构与数据持久化

- Flask 是唯一数据入口。浏览器通过私人密码 Session 使用；林知夏 Action 通过 `Authorization: Bearer <TOKEN>` 使用。
- Supabase PostgreSQL 只保存结构化数据、正文文本和 Storage object path；不长期保存书籍二进制。
- 私有 bucket `xiaxia-reading-house-private` 保存原始 EPUB/TXT、封面和 EPUB 正文图片。
- Render 临时磁盘只在 EbookLib 解析上传文件时短暂使用，解析完成即释放，不承担持久化。
- `books` 保存元数据和目录；`chapters` 保存净化后的章节 HTML、完整纯文本和稳定 block ID。
- `books.source_object_path` 保存原始上传文件的私有对象路径；`book_assets` 只保存 EPUB 逻辑资源路径、Storage object path、MIME type 和 byte size。
- 浏览器仍通过经过 Session/Bearer 鉴权的 Flask 资源路径读取封面和正文图片；Flask 在服务器端从私有 bucket 下载并代理响应。浏览器不接触 Supabase 密钥，也不获得永久公开 URL。
- 批注同时保存起止 block ID、字符偏移、选中文字、前文和后文。正常恢复走精确坐标；出版社结构异常时用选中文字与上下文 fallback。
- `reading_progress` 每本书一条用户进度；`ai_reading_state` 每本书一条林知夏共读检查点。
- 数据表撤销 `anon`/`authenticated` 权限并启用 RLS。Storage bucket 保持 `public=false`，另有 restrictive policy 阻止客户端角色访问其对象；只有服务器端 elevated key 可以操作。

## 必须由项目所有者完成的外部配置

工程代码不包含也不伪造下列值。第一次部署前必须取得并设置：

1. 一个 Supabase 项目及其 PostgreSQL 连接串。
2. 一个 Render Web Service。
3. Supabase Project URL 与只允许放在后端的 `service_role`/Secret key。
4. 七个真实必填环境变量：`DATABASE_URL`、`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`、`SUPABASE_STORAGE_BUCKET`、`PRIVATE_ACCESS_PASSWORD`、`ACTION_API_TOKEN`、`FLASK_SECRET_KEY`。
5. Render 部署完成后的真实 HTTPS 域名，用于替换 `openapi.yaml` 中的 `https://replace-with-your-render-host.example.com`。
6. 在 Custom GPT 中导入 Action Schema，并把认证方式设为 Bearer，填写与 `ACTION_API_TOKEN` 完全相同的 Token。

## 1. 配置 Supabase

1. 新建或打开 Supabase 项目。
2. 进入 **SQL Editor**，完整粘贴并运行 [`schema.sql`](schema.sql)。它会一次性创建数据表、索引、RLS，并创建：
   - 私有 Storage bucket：`xiaxia-reading-house-private`
   - bucket 上针对 `anon`/`authenticated` 的 restrictive blocking policy
   - 文件大小上限：50 MB
3. 在 **Storage** 页面打开该 bucket，确认：
   - bucket 显示为 **Private**，绝不能切换为 Public。
   - 不为 `anon` 或 `authenticated` 创建读取、上传、更新或删除 policy。
4. SQL 同时创建这些 Reading House 表：
   - `books`
   - `book_assets`
   - `chapters`
   - `reading_progress`
   - `annotations`
   - `annotation_replies`
   - `ai_reading_state`
5. 在 **Project Settings → Database → Connection string** 取得连接串。
6. Render 通常需要 IPv4 可达连接，优先复制 Supabase 的 **Session pooler** 连接串，而不是仅支持 IPv6 的 direct connection。
7. 确认连接串使用 SSL；Supabase 提供的连接串通常已经带有正确参数。如未带，请在末尾加入 `sslmode=require`。
8. 在 Supabase 项目的 **Connect / API Keys** 区域取得：
   - Project URL → `SUPABASE_URL`
   - 后端 Secret key，或旧项目的 `service_role` key → `SUPABASE_SERVICE_ROLE_KEY`

`SUPABASE_SERVICE_ROLE_KEY` 这个变量必须只存在于 Render 后端环境变量或本地私密 `.env` 中。不得写进 GitHub、HTML、JavaScript、Action Schema或聊天正文。本项目不需要浏览器端 anon/publishable key。

### 已经运行过第一版 bytea schema 时

若第一版 SQL 从未执行，直接运行当前 `schema.sql`，不要运行迁移文件。

若数据库已经存在旧 `book_assets.data bytea` 数据：

1. 备份数据库。
2. 运行 `migrations/001_prepare_storage_refactor.sql`。
3. 配置本项目全部 Supabase 环境变量。
4. 在项目根目录运行 `python scripts/migrate_legacy_bytea_to_storage.py`。
5. 脚本成功后运行 `migrations/002_finalize_storage_refactor.sql`。

最终 SQL 会在确认每条旧资产都有 `object_path` 后才删除 `data` 列；发现未迁移记录时会主动中止，不会静默丢失图片。旧架构从未保存原始 EPUB/TXT，因此旧书只能迁移封面与正文图片；此后新上传书籍会同时保留原文件。

## 2. 准备密钥

在本机生成两个独立的高熵随机值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

- 一个作为 `ACTION_API_TOKEN`。
- 一个作为 `FLASK_SECRET_KEY`。
- `PRIVATE_ACCESS_PASSWORD` 是你打开书架时输入的私人密码，应使用独立强密码，不要复用上述 Token。

## 3. 部署到 Render

1. 把整个 `xiaxia-reading-house` 目录作为独立 GitHub 仓库提交。
2. Render 新建 **Web Service** 并连接该仓库。
3. Runtime 选择 Python。若 Render 未自动读取配置，填写：
   - Build Command：`pip install -r requirements.txt`
   - Start Command：`gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 app:app`
   - Health Check Path：`/health`
4. 在 Render **Environment** 添加：

| 变量 | 必填 | 内容 |
| --- | --- | --- |
| `DATABASE_URL` | 是 | Supabase Session pooler PostgreSQL 连接串 |
| `SUPABASE_URL` | 是 | Supabase Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | 是 | 仅服务器使用的 Secret/service_role key |
| `SUPABASE_STORAGE_BUCKET` | 是 | 必须与 SQL 一致：`xiaxia-reading-house-private` |
| `PRIVATE_ACCESS_PASSWORD` | 是 | 只有你知道的网页访问密码 |
| `ACTION_API_TOKEN` | 是 | Custom GPT Action Bearer Token |
| `FLASK_SECRET_KEY` | 是 | 独立随机 Session 签名密钥 |
| `MAX_UPLOAD_MB` | 否 | 默认 `50` |
| `DB_POOL_MIN` | 否 | 默认 `1`；免费实例可设 `0` |
| `DB_POOL_MAX` | 否 | 默认 `5` |
| `COOKIE_SECURE` | 否 | Render HTTPS 保持 `true` |

5. 部署。访问 `https://你的域名/health`：
   - `{"status":"ok"}`：环境变量、数据库和私有 Storage bucket 均可用。
   - `configuration_required`：响应会列出缺失的环境变量。
   - `database_unavailable`：检查连接串、密码、Session pooler 与 SSL。
   - `storage_unavailable_or_bucket_public`：检查 Project URL、server key、bucket 名称，并确认 bucket 仍为 Private。
6. 打开 `/login`，用 `PRIVATE_ACCESS_PASSWORD` 登录，再进入 `/library` 上传第一本书。

任何一个必填环境变量缺失时，除 `/health` 与静态文件外，服务会以 503 拒绝工作，不会用默认密码或临时密钥假装完成。

## 4. 接入 Custom GPT Action

1. 打开 [`openapi.yaml`](openapi.yaml)。
2. 只替换这一处：

```yaml
servers:
  - url: https://replace-with-your-render-host.example.com
```

替换为 Render 页面显示的真实 HTTPS Origin，不带末尾路径，例如：

```yaml
servers:
  - url: https://你的真实服务名.onrender.com
```

3. 在 Custom GPT 的 Actions 中导入修改后的 Schema。
4. Authentication 选择 **API Key / Bearer**（界面名称可能略有差异），Token 填入与 Render `ACTION_API_TOKEN` 完全相同的真实值。
5. 不要把 Token 写入 Schema，也不要发送到聊天正文中。

### 林知夏的推荐调用顺序

1. 新窗口恢复共读状态：`getCurrentReadingState`。
2. 获取未处理内容：`listPendingReadingAnnotations`。
3. 准备阅读或回复：用 `annotation_id` 调用 `getCoReadingContext`，取得书籍、完整章节、批注、用户想法和阅读进度。
4. 决定只读不回复：`markReadingAnnotationSeen`。
5. 写回自己的回应：`replyToReadingAnnotation`。
6. 读到新的章节或处理到新的批注：`updateXiaxiaReadingProgress`。

`include_adjacent=true` 只在当前章不足以理解上下文时使用；默认不把整本书塞进上下文。

## API 契约

| 能力 | Method + Endpoint | 调用方 |
| --- | --- | --- |
| 书架列表 | `GET /api/books` | 网页 / Action |
| 上传 EPUB/TXT | `POST /api/books` | 网页 |
| 书籍、目录与进度 | `GET /api/books/{book_id}` | 网页 |
| 完整章节 | `GET /api/books/{book_id}/chapters/{chapter_id}` | 网页 / Action |
| 章节批注 | `GET /api/books/{book_id}/chapters/{chapter_id}/annotations` | 网页 |
| 自动进度 | `PUT /api/books/{book_id}/progress` | 网页 |
| 新建划线/批注 | `POST /api/annotations` | 网页 |
| 当前共读状态 | `GET /api/reading/state` | Action |
| 整章共读上下文 | `GET /api/reading/context` | Action |
| 待处理批注 | `GET /api/annotations/pending` | Action |
| 标记已读 | `POST /api/annotations/{annotation_id}/seen` | Action |
| 写回林知夏回应 | `POST /api/annotations/{annotation_id}/reply` | Action |
| 更新林知夏进度 | `POST /api/ai/progress` | Action |

## 本地运行与测试

需要 Python 3.11+ 与一个已执行 `schema.sql` 的 PostgreSQL 数据库。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

把 `.env` 中七个必填值配置好并导入当前 Shell 后：

```bash
flask --app app run
```

运行基础测试：

```bash
pytest
```

测试覆盖：真实生成的 EPUB 元数据/目录/章节/封面/正文图片解析、TXT 分章、HTML 净化、稳定 block ID、匿名拒绝、Bearer 认证、私人网页 Session、私有 Storage 上传/下载/清理、公开 bucket 拒绝、服务器资源代理、SQL 无 bytea 检查、Action Schema 与 Flask 路由一致性、前端文件引用。

## V1 验收顺序

部署完成后使用你合法拥有的真实 EPUB 在手机上依次验证：

1. 上传后出现封面、书名、作者；目录和章节顺序正确。
2. 正文、基础图片、上一章/下一章、目录和字号调整正常。
3. 滚动后关闭页面，重新打开恢复到原章节和附近段落。
4. 手机长按选择文字，分别测试“划线”和“写想法”；刷新后仍在原位置。
5. Action 调用当前状态和待处理批注，再按 `annotation_id` 获取完整章节上下文。
6. Action 标记 `seen`；另一条批注写回 `reply`。
7. 网页点击原划线，确认同处显示用户批注与林知夏回应。
8. 新建 Custom GPT 聊天窗口，调用当前状态，确认仍能恢复书、双方进度与待处理批注。

其中第 1–8 项都通过，才算 V1 实机验收完成。

## V1 边界

没有实现 PDF、Kindle 格式、用户注册、公共书库、社交、全文搜索、统计 Dashboard、语音朗读、推荐算法、AI 自动总结、AI 后台自主阅读、AI 生成批注、多 AI 角色、主题系统或成就系统。DRM EPUB 不处理。
