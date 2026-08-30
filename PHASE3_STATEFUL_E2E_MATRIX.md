# Phase 3 Stateful E2E Matrix

## Decision gate

**PENDING USER DEVICE VALIDATION**

状态含义：

- `PASS (automated)`：本地可执行自动化已运行并通过。
- `PASS (real EPUB)`：用户原始 EPUB 已由 server bridge 脚本实际处理。
- `READY / NOT RUN`：浏览器测试已实现，但本环境缺少浏览器 binary。
- `PENDING DEVICE`：必须由真实 Android/iPad 操作；不计入 PASS。
- `NOT AVAILABLE`：本轮 workspace 没有该真实样本；不作推断。

## Stateful interaction sequences

| Sequence | Expected invariant | Automated evidence | Device status |
|---|---|---|---|
| A: open → select A → modal → type → save → refresh → click/jump A | modal focus 不改变 frozen snapshot；保存后 A 仍是 A | READY / NOT RUN (Chromium unavailable) | PENDING DEVICE |
| B: Thought B → refresh → user A → refresh → jump B/A/B | 后创建 A 不污染 B | READY / NOT RUN | PENDING DEVICE |
| C: select first → cancel → select second → save | first Range 不进入 second payload | READY / NOT RUN | PENDING DEVICE |
| D: select → keyboard/textarea focus → save | POST 使用 immutable snapshot，不读 live Selection | PASS (static/API) + READY browser | PENDING DEVICE |
| E: paginated → handle drag | page CFI/transform 不改变，quote exact | READY / NOT RUN | PENDING ANDROID |
| F: same CFI user + Thought | record keys 独立；点击展示明确 choices | PASS (contract) + READY browser | PENDING DEVICE |
| G: save POST succeeds, section not drawable | durable save 成功；decoration deferred/reload | PASS (contract) | PENDING DEVICE |
| H: target section absent/mismatched | fail closed；绝不使用 current/first section | PASS (automated) | PENDING DEVICE |

Browser implementation: `tests/phase3_stateful_interaction_browser.mjs`.

## Selection state coverage

| Case | Assertion | Result |
|---|---|---|
| S1 | paginated exact `君子好逑` | READY / NOT RUN; PENDING ANDROID |
| S2 | drag both handles, no unexpected line/paragraph growth | PENDING ANDROID |
| S3 | cancel first, save second | READY / NOT RUN; PENDING DEVICE |
| S4 | select, no action, page, reselect; no stale state | PASS (contract); PENDING DEVICE |
| S5 | pagination → scroll → pagination, each selection isolated | PASS (contract); PENDING DEVICE |
| S6 | portrait and landscape | PENDING ANDROID/IPAD |
| Cross-section | reject and do not POST | PASS (automated contract) |
| Page relocate during selection | reject, clear, restore page | PASS (automated contract); PENDING ANDROID |
| Cleanup | clear publication iframe Selection, timer, snapshot, toolbar | PASS (automated contract); PENDING ANDROID |

## Write, locator, decoration, navigation

| Area | Case | Result | Evidence |
|---|---|---|---|
| User write | stable engine locator → legacy mapping → DB | PASS (automated) | Phase 3 API suite |
| User write | POST failure keeps modal snapshot for retry | PASS (contract) | `foliate-reader.js` test |
| User write | POST success + draw unavailable is still durable success | PASS (contract) | split save-stage assertions |
| Thought | small offset slip, unique quote and context | PASS (automated) | same/cross-block tests |
| Thought | empty alleged range can safely correct | PASS (automated) | correction-before-empty test |
| Thought | ambiguous/far/cross-boundary correction | PASS (rejected) | fail-closed tests |
| Thought | footnote boundary after newline/tab | PASS (real phrase + automated) | 唐诗 target `239–254` |
| Locator | source/hash/version/href/section validation | PASS (automated) | dual-anchor suite |
| Locator | CFI resolves to expected live text | PASS (contract); browser READY | adapter browser truth path |
| Locator | missing loaded section | PASS (rejected) | no current/first fallback |
| Record identity | `annotation:<uuid>` / `thought:<uuid>` | PASS (automated contract) | adapter test |
| Same CFI | both records retained; explicit choice | PASS (contract); browser READY | stateful sequence F |
| Decoration | current section immediate draw | READY / NOT RUN | browser test |
| Decoration | refresh/reopen restore | READY / NOT RUN | browser test |
| Navigation | record A/B/A remains exact | READY / NOT RUN | browser test |
| Revalidate | per-book clear renderer locator only | PASS (automated) | rebuild-all API test |

## Regression matrix

| Existing capability | Result |
|---|---|
| Legacy reader and pagination | PASS (pytest) |
| Source-first upload | PASS (pytest) |
| Independent text index and retry | PASS (pytest) |
| Publication survives index failure | PASS (pytest) |
| Annotation CRUD | PASS (pytest) |
| Xiaxia Thought CRUD | PASS (pytest) |
| Replies both directions | PASS (pytest) |
| Preview → Commit | PASS (pytest) |
| Undo | PASS (pytest) |
| Reading progress | PASS (pytest) |
| Shared stops legacy semantics | PASS (pytest) |
| Timeline/reflections/letters/stamp/back-cover | PASS (pytest) |
| POC route and pinned engine | PASS (pytest/hash) |
| Custom GPT Actions/OpenAPI | PASS (pytest/hash unchanged) |

Automated total: **154 passed, 2 skipped, 0 failed**.

## Real EPUB matrix

| File | Available | Phase 3 bridge run | Exact round-trip | Production interaction |
|---|---:|---:|---:|---|
| 唐诗选（全二册） | yes | PASS (71/71 chapters) | 200/200 | PENDING DEVICE after deploy |
| 唐诗选注 | yes | PASS (85/85 chapters) | 200/200 | PENDING DEVICE after deploy |
| 卡瓦菲斯诗集＋当你起航前往伊萨卡 | yes | PASS (323/323 chapters) | 200/200 | PENDING DEVICE after deploy |
| 诗经类“君子好逑” EPUB | no | NOT AVAILABLE | — | PENDING USER SAMPLE/DEVICE |
| 普通小说 | no | NOT AVAILABLE | — | PENDING USER SAMPLE/DEVICE |
| Blind EPUB | no | NOT AVAILABLE | — | PENDING USER SAMPLE/DEVICE |

没有用 synthetic fixture 替代后三项，也没有写文件名/出版社专项分支。

## Browser/device matrix

| Device | Exact selection | Modal/save | A/B locator | Decoration reload | Orientation/flow | Status |
|---|---:|---:|---:|---:|---:|---|
| Desktop Chromium | test authored | test authored | test authored | test authored | test authored | READY / NOT RUN |
| Android Chrome | manual checklist ready | pending | pending | pending | pending | PENDING DEVICE |
| iPad Safari | manual checklist ready | pending | pending | pending | pending | PENDING DEVICE |

`PASS` 只能在 Android 与 iPad 清单完成，并补齐至少诗经/普通小说/blind 样本后给出。

