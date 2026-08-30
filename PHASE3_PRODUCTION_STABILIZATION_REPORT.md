# Phase 3 Production Stabilization Report

## Decision

**PENDING USER DEVICE VALIDATION**

代码修复、后端回归和三本真实 EPUB 的 bridge 验证已经完成；当前 Work 环境没有可运行
的 Chromium/WebKit 二进制，也不能代替 Android Chrome 与 iPad Safari 真机操作。因此本
交付不把自动化契约测试冒充为移动端 PASS。

本轮没有进入 Phase 4，没有修改数据库 schema、migration 007、OpenAPI、Custom GPT
Actions、source-first import、text index、shared stops、V2 memories 或固定的 foliate-js
upstream。

## Production root causes

### 1. Android pagination 与原生 Selection 竞争同一滚动状态

固定版 foliate paginator 在 publication document 上监听 `touchmove` 与
`selectionchange`：

- `touchmove` 会阻止默认行为并改变横向滚动；
- selection handle 到达可视范围边缘时，延迟逻辑可能调用 `prev()` / `next()`；
- Android Chrome 同时为了让原生 selection handle 可见而执行自动滚动。

两套控制器同时修改 column/page，导致页面横向错位、选区跨列扩张和 Range 与肉眼高亮
不一致。

修复位于 Reading House adapter，不修改 upstream：选区形成后，由 capture listener
阻止事件继续到 paginator，但不调用 `preventDefault()`，因此 Android 仍控制自己的原生
handles。选区期间记录 page-before/page-after；若仍发生 relocate，立即 fail closed、清除
错误选区并恢复原页。

### 2. Selection 尚未稳定就可能被当作最终 Range

Android 拖动 handles 时会产生多次中间 `selectionchange`。新状态机区分：

`idle → selecting → stable → frozen/submitting → cleared`

只有 pointer/touch 已结束，并且同一 Range signature 连续两次稳定后，才生成 quote、
CFI 与 locator snapshot。跨 document、跨 section、空 Range、异常超长 Range 全部拒绝。

### 3. Modal focus 使 live Selection 失效

打开“写在书页旁”后，焦点会进入 textarea，软键盘和 viewport resize 也会改变浏览器
Selection。旧实现缺少明确的 modal snapshot 生命周期。

现在在 modal 打开前深拷贝不可变 snapshot，随后清除 publication iframe 中真正的
Selection。保存按钮只使用该 snapshot，不再重新读取 `window.getSelection()`；取消、
翻页、切换 flow、调字号或保存成功都会完整清理 snapshot 和 toolbar 状态。

### 4. Durable write 与 decoration 被错误地视为一个原子 UI 操作

旧前端把 annotation POST 和 `addDecoration()` 放在同一失败路径。若数据库已经写入、但
目标 section 此刻没有加载，绘制失败仍会显示“保存失败”，用户重试还可能产生重复痕迹。

现在：

1. POST 成功即确认业务记录已经持久化；
2. 清除 Selection、关闭 toolbar/modal；
3. 在当前 section 尝试即时绘制；
4. 若绘制暂不可用，触发 trace reload/lazy restore，不把 durable write 降级为失败。

POST、mapping、validation 与 decoration 各阶段均有独立诊断状态。

### 5. Xiaxia Thought 的安全纠偏没有处理脚注边界

真实《唐诗选（全二册）》中，“此时相望不相闻，愿逐月华流照君”位于第 13 个 text
chapter 的 `b000004`，原始 offset 为 `239–254`，后面紧接换行、tab 和 `[12]` 脚注
标记。旧 context check 把 canonical remainder 的前导空格与已 normalize 的 suffix 直接
比较，唯一匹配也会被拒绝。

修复继续 fail closed，只允许：

- 在请求明确声明的 block span 内查找；
- normalized selected text 唯一匹配；
- prefix/suffix normalized context 一致；
- start/end correction 各不超过 32 个原始字符；
- 修正后的 Range 再由现有 legacy validation 验证。

歧义、跨出声明 block span、距离过大或 context 不符仍然拒绝。真实目标句已验证可从轻微
偏移安全纠正到 `239–254`，未弱化 anchor correctness。

## Interaction state model

| State | Entry condition | Allowed behavior | Exit |
|---|---|---|---|
| `idle` | 无 publication Range | 正常翻页/滚动 | Range 出现 |
| `selecting` | 同 section 原生 Range 存在 | 保留 native handles；暂停 paginator 的 selection swipe/snap | Range 稳定或取消 |
| `stable` | 两次相同 Range signature | 生成 browser-truth quote/CFI；显示 Reading House toolbar | modal、只划线、取消、翻页 |
| `snapshot_frozen` | 打开 annotation modal | 使用不可变 snapshot；清除 iframe live Selection | 提交或取消 |
| `submitting` | POST 已发出 | 禁止第二次提交；保留 snapshot 供错误重试 | POST 成功/失败 |
| `cleared` | 保存成功、取消、导航、flow/font 改变 | 清 timer、Range、toolbar、snapshot | 回到 `idle` |
| `rejected` | 跨 section、page moved、Range invalid | fail closed；不 POST | 用户重新选择 |

## Locator and record integrity

- decoration identity 始终为 `annotation:<uuid>` / `thought:<uuid>`；CFI 只是位置。
- 同一 CFI 的 user/Thought 可共享视觉范围，但点击会列出全部明确 record，不按数组最后一
  项猜测。
- 每次 restore 先执行 `CFI → live DOM Range → normalized text`，必须等于该 record 的
  `selected_text` 才绘制。
- href、bridge spine index、foliate runtime section index 必须 canonical equality。
- locator 生成找不到目标 loaded section 时返回 `locator_mapping_missing`，没有
  `getContents()[0]`、current section 或 nearest section fallback。
- record navigation 只读取指定 record 的 locator；无法确认时停止跳转，不使用当前
  selection、lastLocator 或章节起点冒充精确位置。

## Existing wrong locator repair

本轮沿用并扩展 per-book application-level revalidation：

```http
POST /api/books/{book_id}/engine-traces/revalidate
Content-Type: application/json

{"rebuild_all": true}
```

它只清空该书 annotation/Thought 的：

- `engine_locator`
- `engine_locator_version`
- `engine_anchor_verified_at`

不会删除或改写 legacy anchor、selected text、comment、Thought、Reply 或 shared stops。
下一次打开相应 section 时再按 legacy seed、browser truth lazy rebuild。用户无需调用 API：
在 `/reader/{book_id}?selection_debug=1` 的诊断纸片中点击“重验本书定位”即可执行同一安全
操作。

默认部署不会自动覆盖全库 locator。

## Production diagnostics

访问：

```text
/reader/{book_id}?selection_debug=1
```

纸片显示 selected text、href、start/end section、CFI 指纹、selection state、page
before/after 与 save stage，并可复制 JSON。普通阅读入口不会显示该面板。

服务端只记录 book/record identity、href、section、selected-text length、截断 CFI hash、
mapping/save stage、duration 和 error code；不记录完整批注、Thought、signed URL 或 secret。

## Files changed

- `annotations.py`
- `static/js/foliate-reader-adapter.js`
- `static/js/foliate-reader.js`
- `static/css/reader-engine.css`
- `tests/test_phase3_dual_anchor.py`
- `tests/test_reader_js.py`
- `tests/phase3_stateful_interaction_browser.mjs` (new)
- `phase3_stabilization_real_epub_results.json` (new evidence)
- `PHASE3_PRODUCTION_STABILIZATION_REPORT.md` (new)
- `PHASE3_STATEFUL_E2E_MATRIX.md` (new)
- `PHASE3_DEVICE_VALIDATION_CHECKLIST.md` (new)
- `README.md`

## Validation evidence

### Automated regression

- pytest: `154 passed, 2 skipped, 0 failed` (156 collected).
- skipped: Chromium browser suites; Playwright package exists but browser binary is unavailable.
- Python compile: pass.
- JavaScript syntax: pass.
- critical Ruff error classes: pass.
- `openapi.yaml`, `source_first.py`, migration 007 and `static/vendor/foliate-js/` are byte-for-byte
  unchanged from the deployed hotfix baseline.

### Real EPUB bridge regression

| EPUB | EPUB | Chapters | Exact sampled round-trip | Result |
|---|---:|---:|---:|---|
| 卡瓦菲斯诗集＋当你起航前往伊萨卡 | 2.0 | 323 | 200/200 | PASS |
| 唐诗选注 | 2.0 | 85 | 200/200 | PASS |
| 唐诗选（全二册） | 3.0 | 71 | 200/200 | PASS |

三本均为用户原始文件，没有修改内容或文件名分支。当前 workspace 没有诗经 EPUB、普通
小说和额外 blind EPUB，因此这些不得伪造为已验收。

### Stateful browser sequence

交付的真实浏览器脚本覆盖：精确选择“君子好逑”、handle touch sequence 不改变 page
CFI、modal focus 后 snapshot 保存、刷新恢复、Thought B / Annotation A 往返、取消后第二
次 selection、同 CFI 多 record identity。脚本使用真实 foliate UI/EPUB 和 HTTP mock
backend，不是单函数 adapter fake。

该脚本在本环境因 Chromium binary 不存在而 skip；它已通过 Node syntax 与 pytest
调度契约，但必须在部署后的真实 Android/iPad 上完成清单后才能把本报告改为 PASS。

## Deployment and rollback

- Migration: **none**（不要重复执行 007）。
- New environment variables: **none**。
- Node/npm runtime: **none**。
- Render build/start command: **unchanged**。
- Supabase schema/bucket/policy: **unchanged**。
- Deploy: 重新部署完整 ZIP，随后对移动端做 hard refresh 或清理该站点缓存。

Rollback:

1. `DUAL_ANCHOR_ENABLED=false`：foliate 保持只读，停止 dual-anchor 写入；
2. `READER_ENGINE_ENABLED=false`：立即回 legacy reader。

两个开关都不会删除 dual anchor 或 legacy anchor。若本构建在 Android 真机仍复现 page
drift/Range expansion，停止继续堆 adapter patch，记录诊断并将结论升级为
`STRUCTURAL ENGINE LIMITATION`，再评估 Readium。

