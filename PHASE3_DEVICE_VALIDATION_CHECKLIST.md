# Phase 3 Android / iPad Manual Validation Checklist

## Before testing

1. 部署本完整构建，不需要 migration 或新环境变量。
2. 保持 `READER_ENGINE_ENABLED=true` 与 `DUAL_ANCHOR_ENABLED=true`。
3. 对站点 hard refresh；记录设备、OS、浏览器完整版本。
4. 用普通阅读入口验证一次，再用以下诊断入口复现：

   `/reader/{book_id}?selection_debug=1`

5. 失败时复制诊断 JSON，并截图页面和原生 selection handles。诊断不会包含用户批注正文。

## Android Chrome · required

### Exact paginated selection

- [ ] 竖屏、分页模式打开含“君子好逑”的书页。
- [ ] 长按并拖动左右 handles，只选择 `君子好逑`。
- [ ] 诊断 `selected_text` 精确为四个字，不含后一行/后段。
- [ ] `start_section == end_section`，href 不变。
- [ ] page-before 与 page-after CFI/section 不发生非预期改变。
- [ ] 页面没有横向错位、半截正文、异常 column snap。
- [ ] 横屏重复以上步骤。

### Selection lifecycle

- [ ] 选择 A → 取消 → 选择 B → “留一句”；保存 payload 只含 B。
- [ ] 选择 → 不操作 → 翻页 → 再选择；旧 toolbar/snapshot 不出现。
- [ ] pagination → select → scroll → select → pagination → select；三次均准确。
- [ ] 保存“只划线”后 publication Selection 与 Reading House toolbar 都消失。
- [ ] 保存“留一句”后同样消失，可以继续翻页。

### Modal and keyboard

- [ ] 选择 `君子好逑` → 打开“写在书页旁”。
- [ ] 点击 textarea、输入“不喜欢”、打开/关闭键盘、滚动 modal。
- [ ] 保存成功，书页痕迹中的 quote 仍为 `君子好逑`。
- [ ] 诊断顺序包含 `modal_snapshot_frozen → post_started → persisted`。
- [ ] refresh 后暖棕 decoration 精确覆盖 `君子好逑`。

### Cross-record integrity

- [ ] 创建 Xiaxia Thought A：`此时相望不相闻，愿逐月华流照君`。
- [ ] 在另一句创建 User Annotation B。
- [ ] jump A → B → A；每次 actual highlighted text 均与记录自身 quote 相同。
- [ ] refresh 后重复 A → B → A。
- [ ] 删除 B 后 jump A；A 不改变。
- [ ] reopen 后 jump A；A 不改变。
- [ ] 同一句创建 user + Xiaxia 两条记录；点击 shared decoration 后能明确选择双方记录。

## iPad Safari · required

- [ ] 分页模式竖屏：selection handle drag 不改变页列，不裁切底部文字。
- [ ] 分页模式横屏：重复 selection、annotation save、Thought jump。
- [ ] 旋转时取消 active selection；旋转后重新选区准确。
- [ ] 地址栏/工具栏伸缩后 viewport 与 page 不错位。
- [ ] modal 打开软键盘后 snapshot 保持；保存成功。
- [ ] font size 增减后 decoration 仍命中同一句。
- [ ] pagination ↔ scroll 切换后 CFI restore 准确。
- [ ] refresh/reopen 后 user 暖棕与 Xiaxia 灰蓝 decoration 都准确。
- [ ] Thought marker、Reply、shared stop 跳转正常。
- [ ] image/footnote-heavy 章节重复 selection、footnote/backlink 和返回。

## Desktop browser · required stateful sequence

- [ ] Sequence A：select A → modal → type → save → refresh → click/jump A。
- [ ] Sequence B：Thought B → refresh → user A → refresh → jump B/A/B。
- [ ] Sequence C：select first → cancel → select second → save。
- [ ] Sequence D：select → textarea focus/keyboard resize → save。
- [ ] Sequence F：same CFI user + Xiaxia → 两条记录独立可达。

## Existing locator repair

仅对已知有错误跳转的单本书执行：

1. 打开 `/reader/{book_id}?selection_debug=1`。
2. 点击“重验本书定位”。
3. 确认页面重载。
4. 依次进入相关章节，让 locator lazy rebuild。
5. 重测 A/B/A 跳转和 decoration。

该操作只清 renderer locator，不删除 legacy anchor、annotation、Thought、Reply 或 shared
stops。不要批量对全库执行。

## Failure capture and stop condition

若 Android 在本构建仍出现横向 page drift 或 Range 异常扩张：

- [ ] 记录设备、Android、Chrome 版本。
- [ ] 截图原生 handles 与错位页面。
- [ ] 复制诊断 JSON。
- [ ] 写下书名、href、flow、portrait/landscape 和操作步骤。
- [ ] 不重复点击保存，不让错误 Range 进入生产数据。

若同一问题可稳定复现，停止继续叠加 adapter workaround；将结论更新为
`STRUCTURAL ENGINE LIMITATION`，整理 upstream reproduction，再决定是否转 Readium。

