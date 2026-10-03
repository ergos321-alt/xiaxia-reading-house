# EPUB 真实失败样本：资源与内存报告

测试日期：2026-08-27

## 结论

本次故障不是“文件越大越容易失败”，也不是单一 EPUB 规范分支。

- 完整 Flask/Psycopg/Supabase worker 空载 RSS 已约 76 MB。
- 原配置 `2 workers × 4 threads` 允许最多 8 个导入请求同时在两个进程中驻留；每个导入还会再创建最多 4 个 Storage 上传线程。
- 用《唐诗选注》模拟原配置的 2×4 并发，仅解析阶段两个 worker 的峰值 RSS 合计已达 272.86 MB，尚未包含 Gunicorn master、真实 HTTP multipart、数据库驱动缓冲和 Storage SDK 并发开销。
- 旧导入路径同时保留 multipart/request 数据、完整 EPUB `raw bytes`、全部 normalized HTML/text、全部已用图片 bytes 和数据库参数容器。
- 《唐诗选注》有 181 张正文图片，意味着原实现需要 182 次 Storage HTTP 写入；即使内存未先耗尽，也可能把 120 秒 Gunicorn hard timeout 用完。
- Gunicorn 的 worker timeout 同样会以 SIGKILL 结束 worker，因此仅凭 `SIGKILL` 不能区分内核 OOM kill 与 Gunicorn hard kill。这里的实测根因是“过度并发 + 大对象长驻 + 超多远程请求”的组合，而不是单个压缩文件本身。

## 样本结构

| 样本 | EPUB | 压缩 | 解压总量 | 条目 | XHTML | 图片 | 字体 | 正文章 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 卡瓦菲斯诗集＋当你起航前往伊萨卡 | 2.0 | 0.679 MB | 1.794 MB | 337 | 327 | 4 | 0 | 326 |
| 唐诗选注 | 2.0 | 4.107 MB | 4.748 MB | 274 | 87 | 181 | 0 | 86 |
| 唐诗选（全二册） | 3.0 | 0.885 MB | 1.660 MB | 201 | 73 | 123 | 0 | 72 |

《卡瓦菲斯诗集》压缩体积最小，但 XHTML 数量最多；它的解析成本高于 4.1 MB 的《唐诗选注》。这解释了“小 EPUB 也会失败”。

## 修改前基线 RSS

以下数据为每本书单独启动完整应用进程，并通过 `POST /api/books` 真实解析；Storage/DB 使用无网络 sink，避免远程延迟污染内存结论。

| 样本 | request ready | parse 前 | parse 后 | Storage 前 | Storage 后 | DB 后 | peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 卡瓦菲斯 | 81.57 MB | 82.53 MB | 102.95 MB | 102.95 MB | 103.17 MB | 103.17 MB | 111.14 MB |
| 唐诗选注 | 84.98 MB | 89.37 MB | 100.89 MB | 100.89 MB | 101.12 MB | 101.12 MB | 101.58 MB |
| 唐诗选（全二册） | 81.65 MB | 82.60 MB | 100.61 MB | 100.61 MB | 100.84 MB | 100.84 MB | 104.53 MB |

## 修改后完整导入 RSS

修改后的测试走完整 Flask route、真实 ZIP 解析、真实按条目流式解压、文件型 private-Storage sink 和事务型 DB sink。每本书在独立进程中测试。

| 样本 | parse 前 | asset extraction 前/后 | parse 后 | normalize 落盘后 | Storage 前/后 | DB 前/后 | peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 卡瓦菲斯 | 80.89 MB | 104.32 / 104.32 MB | 104.32 MB | 100.27 MB | 100.27 / 101.43 MB | 101.43 / 103.89 MB | 111.96 MB |
| 唐诗选注 | 80.84 MB | 87.42 / 87.94 MB | 88.29 MB | 85.96 MB | 85.96 / 89.35 MB | 89.35 / 90.40 MB | 90.40 MB |
| 唐诗选（全二册） | 80.84 MB | 97.43 / 97.43 MB | 99.87 MB | 92.59 MB | 92.59 / 93.79 MB | 93.79 / 94.81 MB | 105.80 MB |

重点收益发生在可能持续最久的 Storage 阶段：三本书的 Storage 前驻留分别为 100.27、85.96、92.59 MB。生产路径不再让 `raw EPUB + all image bytes + all chapter strings` 同时常驻。

卡瓦菲斯样本的绝对 peak 没有显著下降，因为峰值来自 327 份 XHTML 的 DOM 构建/清洗阶段；但它在 5 秒内完成，并在进入远程 Storage 前释放 normalized chapter 大字符串。这个结果也说明不能用单一 peak 数字掩盖“峰值持续时长”和“进程并发倍数”。

## 真实样本完整导入结果

| 样本 | HTTP | chapters | book_assets | 实际 Storage object 数 | 总耗时（本地 sink） |
| --- | ---: | ---: | ---: | ---: | ---: |
| 卡瓦菲斯诗集＋当你起航前往伊萨卡 | 201 | 326 | 4 | 5 | 5.041 s |
| 唐诗选注 | 201 | 86 | 181 | 182 | 1.433 s |
| 唐诗选（全二册） | 201 | 72 | 53 | 47 | 3.286 s |

成功率：3/3，100%。《唐诗选（全二册）》有重复图片内容，53 条 `book_assets` 只写入 46 个不同图片 object，加原书共 47 次 Storage upload。

当前环境没有生产 Supabase/Render 凭据，因此这里的 3/3 是“真实用户 EPUB + 完整应用控制流 + 文件型 Storage/事务型 DB adapter”，不是对生产 Supabase 写入的冒充。部署后，真实 provider 的 storage/database duration 与各阶段 RSS 会由新增 structured log 直接给出。

## 实施项

- request stream 先分块落到本次导入临时文件，并在同一遍计算 SHA-256；不再 `upload.read()` 保留整本 bytes。
- 生产 path parser 只保存图片元数据、archive entry、byte size 与 SHA-256；图片 bytes 不进入 `ParsedBook`。
- 图片按 ZIP entry 流式解压到临时文件，最多 2 路上传；不会一次性解压整个 EPUB。
- 相同图片内容以 SHA-256 复用 Storage object；`book_assets.asset_path` 仍逐项保留，不改 schema。
- normalized chapter HTML/text 在 Parse First 验证完成后暂存到本次导入目录，释放字符串堆，再以生成器交给现有 DB transaction。
- BeautifulSoup DOM 在章节循环中主动 `decompose()`，并每 50 章回收循环对象。
- Supabase client 首次初始化增加进程内锁。
- 全局单导入互斥；第二个并发上传返回 `import_resource_exhausted`，不会与当前导入争抢内存。
- Gunicorn 改为 `1 worker × 2 threads`；hard timeout 仍为 120 秒，应用 deadline 仍为 105 秒。
- Storage deadline 可在 hard timeout 前停止后续 entry，清理已确认上传对象并返回 `epub_import_timeout`。
- 非 JSON 的 upstream 502 在书籍上传 UI 映射为 `import_worker_terminated`；非 JSON 503 映射为 `import_resource_exhausted`。后端明确返回的 `storage_upload_failed` 不会被错误覆盖。
- structured log 逐阶段实时记录 archive 结构、duration、current RSS、peak RSS 和 failure stage；不记录正文。

## 复测命令

```bash
python scripts/profile_epub_memory.py sample_epub_a.epub sample_epub_b.epub
python -m pytest -q
python -m ruff check .
python -m py_compile app.py annotations.py auth.py database.py epub_parser.py reading.py management.py memories.py operations.py storage.py
for file in static/js/*.js; do node --check "$file"; done
```

本轮不需要数据库 migration，也不修改 normalized block、annotation anchor、OpenAPI 或既有历史数据。
