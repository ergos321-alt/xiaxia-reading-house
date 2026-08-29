# Phase 3 Production Hotfix · Locator Integrity / Thought Decoration Misrouting

## Status

**CODE COMPLETE · PENDING PRODUCTION DEVICE VALIDATION**

本报告只覆盖 Phase 3 locator correctness hotfix。没有进入 Phase 4，没有修改
source-first import、text index、数据库 schema、OpenAPI、shared stops 或 V2 memories。

## Production root cause

生产现象由五个相互叠加的缺陷造成：

1. `locatorFromSeed()` 找不到目标 spine content 时回退到 `getContents()[0]`。因此它
   可以在错误章节的当前 DOM 上生成语法合法的 CFI。
2. decoration Map 只使用裸 `record.id`；点击事件又按 CFI value 找记录，阅读页最终
   取 `records[records.length - 1]`。相同 CFI 或错误复用 CFI 时，后一条记录可以遮蔽
   前一条记录身份。
3. backfill 仅验证 CFI 携带的 quote 能否经同一 bridge 算法映射回 legacy anchor，
   没有从 live foliate DOM resolve CFI 并核对实际 Range 文本。
4. engine trace refresh 会为了批量 backfill 调用 `goTo(seed.href)`，改变用户当前阅读
   section，并让后续 backfill 依赖前一条留下的 renderer state。
5. foliate reader 没有消费管理页传入的 `annotation_id` / `thought_id`，所以“回到原文”
   可能只停在当前或最后阅读位置，而非 record-specific locator。

现象 A 的 legacy offset 首次失败是另一条安全防线正常工作：服务端拒绝了不匹配的
block/offset。本 hotfix 没有弱化验证，而是仅对“同一 block、唯一匹配、距原 offset
不超过 32 字符、prefix/suffix（如提供）一致”的范围做安全纠偏；歧义仍拒绝。

## Fix

### Record identity

- decoration key 固定为 `annotation:<uuid>` 或 `thought:<uuid>`。
- CFI 只负责绘制位置，不再承担 record identity。
- 同一 CFI 有多条记录时合并视觉，但点击后显示明确的记录选择，不再隐式选择最后一条。
- 删除或更新一条记录只操作自己的 record key；同 CFI 的其他记录保留。

### Section and browser truth validation

每个新 locator 增加内嵌完整性数据：

```json
{
  "locator_integrity_version": 1,
  "browser_truth": {
    "href": "Text/chapter.xhtml",
    "section_index": 7,
    "text": "浏览器 Range 实际解析出的文字"
  }
}
```

持久化前必须同时满足：

- bridge canonical href 唯一；
- bridge spine index 与 foliate runtime section index 相同；
- foliate section href 与 seed href canonical equality；
- `CFI → live DOM Range → normalized text` 等于 record `selected_text`；
- source hash、engine adapter、bridge version 全部匹配。

任一检查失败返回稳定 locator error，并停止绘制、保存或跳转。不存在
`getContents()[0]` fallback。

### Backfill without reader-state contamination

- 后台 trace refresh 只在目标 section 已加载时生成 locator，不改变可见阅读位置。
- 只有用户明确点击“回到原文”时才允许加载目标 href。
- 显式导航仍会在生成 CFI 前验证 resolved section identity，生成后再次 resolve CFI
  并核对 live Range 文本。
- 当前 section 加载后触发一次受控 trace refresh，因此新 Thought 可即时补画；未加载
  section 等用户实际进入时再绘制。

### Existing wrong locator repair

新增应用层、per-book revalidation，不需要 migration：

- `POST /api/books/{book_id}/engine-traces/revalidate`
- `DELETE /api/books/{book_id}/engine-traces/{annotation|thought}/{record_id}/locator`

首次打开 foliate 书籍时，缺少 integrity v1、source 不匹配或文本证明不一致的旧
locator 会被清空。只清：

- `engine_locator`
- `engine_locator_version`
- `engine_anchor_verified_at`

不会修改或删除 legacy block anchor、selected text、annotation、Thought、Reply 或
shared stops。随后利用 locator seed 按当前 section lazy backfill。浏览器发现 CFI 实文
不一致时只 invalidates 对应 record locator。

## Navigation behavior

`annotation_id` / `thought_id` 查询参数现在按以下顺序处理：

1. 找到指定 record；
2. 验证该 record 自己的 locator；
3. 若 stale，使用该 record 自己的 locator seed 精确重建；
4. `goTo(record.engine_locator.cfi)`；
5. 再次核对 live Range text；
6. 成功才打开该 record 纸片。

无法验证时显示“暂时无法准确定位，已停止跳转”，不会跳到章节开头、当前 selection、
lastLocator 或其他 record。

## Files changed

- `annotations.py`
- `locator_bridge.py`
- `static/js/canonical-text.js`
- `static/js/foliate-reader-adapter.js`
- `static/js/foliate-reader.js`
- `static/css/reader-engine.css`
- `templates/reader.html`
- `tests/test_phase3_dual_anchor.py`
- `tests/phase3_locator_integrity_browser.mjs`
- `README.md`

No schema or migration file changed. foliate-js upstream remains pinned at
`78914aef4466eb960965702401634c2cb348e9b1` with zero upstream patches.

## Validation

- Full pytest collection: 150 tests.
- Automated result in Work environment: 149 passed, 1 skipped, 0 failed.
- Skipped: real Chromium script because the installed Playwright package has no browser binary.
- Ruff: pass.
- Python compile: pass.
- All JavaScript syntax checks: pass.
- OpenAPI SHA-256 unchanged:
  `27b645bea52d6cb92f3bdc25729388b56b116e094c2b106bd6ef0269d7a319bf`.
- foliate `VERSION.json` SHA-256 unchanged:
  `d3ac317e06077816cbc6d57b3d548c08084a2bfe6eb1744cdb6d67ff60c08618`.

Real EPUB bridge regression:

| EPUB | Chapters | Sampled exact round-trip | Result |
|---|---:|---:|---|
| 卡瓦菲斯诗集 | 323 | 200/200 | PASS |
| 唐诗选注 | 85 | 200/200 | PASS |
| 唐诗选（全二册） | 71 | 200/200 | PASS |

The shipped browser test covers distinct A/B records, later-record isolation, same-CFI
identity, missing section fail-closed, and CFI live Range truth verification. It can run as
soon as Playwright Chromium is installed; production Android/iPad validation must not be
claimed until the user completes it.

## Deployment and rollback

- Database migration: none.
- New environment variables: none.
- Render start/build command: unchanged.
- Supabase configuration: unchanged.
- Deploy the complete hotfix project and hard-refresh the browser cache.

Rollback remains:

- `DUAL_ANCHOR_ENABLED=false` stops dual-anchor writes and keeps foliate read-only.
- `READER_ENGINE_ENABLED=false` restores the legacy reader.

All legacy anchors remain available in either rollback mode.

## Production acceptance checklist

Use 《唐诗选（全二册）》 after deployment:

1. Open the foliate reader once; old unverified locators are cleared and lazily rebuilt.
2. Create Xiaxia Thought A on “此时相望不相闻，愿逐月华流照君”.
3. Confirm the grey-blue decoration appears on exactly that sentence.
4. Create user annotation B on a different sentence.
5. Click A, B, then A again; each must navigate to its own sentence.
6. Refresh/reopen and repeat step 5.
7. Create two records on the same exact selection; click the shared decoration and choose
   each record explicitly.
8. Repeat A/B creation and reopen on Android Chrome and iPad Safari.

Final status remains **PENDING PRODUCTION DEVICE VALIDATION** until this checklist passes.
