# Xiaxia Reading House · Phase 3 Dual Anchor Report

## Status

**Implementation and server-side verification: PASS.** Production-device shared-reading validation is **PENDING USER VALIDATION** because this workspace cannot control Android Chrome or iPad Safari and only three real EPUB files were available locally.

Phase 3 does not remove the legacy reader, change the Custom GPT schema, or replace any legacy business anchor. `READER_ENGINE_ENABLED=false` still returns every book to the legacy reader; `DUAL_ANCHOR_ENABLED=false` keeps foliate reading available but disables foliate trace writes.

## Architecture

The same reading trace now has two independent, version-checked identities:

| Layer | Stored identity | Owner |
|---|---|---|
| Reading House business anchor | `chapter_id`, block IDs, offsets, quote, prefix/suffix | Xiaxia APIs, shared stops, management, Undo |
| Web renderer anchor | source hash, href, spine index, CFI, quote context | pinned foliate-js adapter and decorations |

New foliate selection follows this strict path:

1. The browser creates CFI, href, spine index and canonical quote context.
2. The server checks engine, adapter version, bridge version and `source_sha256`.
3. The precomputed book bridge maps the canonical quote to exactly one normalized chapter/block span.
4. Existing persisted block text is read back and must equal the canonical selected quote.
5. One PostgreSQL transaction inserts the legacy anchor and `engine_locator` together.

Ambiguous, missing, stale or source-mismatched selections never create a CFI-only annotation.

## Locator schema

`annotations` and `xiaxia_thoughts` gain nullable `engine_locator jsonb`, `engine_locator_version integer` and `engine_anchor_verified_at timestamptz`. Replies remain record-ID based and receive no locator fields.

The JSON contains `engine`, `engine_adapter_version`, `bridge_version`, `source_sha256`, canonical `href`, `spine_index`, `cfi`, progression, highlight, before and after context.

Book bridge lifecycle is stored on `books`: `locator_bridge_status` (`pending | building | ready | failed`), version, private Storage object path, SHA-256, failure code and update timestamp.

## Canonical text v1

`normalize_text_v1()` and `canonical-text.js` use the same declared semantics:

- Unicode NFC, not compatibility-changing NFKC
- NBSP becomes a normal space
- whitespace/newlines collapse to one space
- soft hyphen, BOM and zero-width join/non-join characters are removed
- ruby base text is kept; `rt` and `rp` readings are excluded
- block boundaries and `br` contribute whitespace; inline text nodes are joined
- punctuation is unchanged; HTML entities are decoded first

Bridge and canonical text versions are both `1`. A future normalization change must create a new version.

## Bridge build and performance

The bridge is built once per source/index version, uploaded as versioned JSON to private Storage, and cached in-process for at most two books. A selection loads the bridge; it does not reparse the EPUB.

Bridge data includes canonical publisher text, real OPF spine mapping, chapter/href mapping and stable block spans. Historical range anchors are measured before an old book can be promoted. `promote_legacy=true` changes a source-backed legacy book to foliate only when **all** historical range anchors are uniquely mappable. Otherwise it remains legacy.

Text-index retry still refuses to rebuild when anchor rows exist. Phase 3 does not change block ID generation.

## Annotation, Thought, Reply and Undo

- User selections save both anchors atomically.
- Foliate overlays render user ink in warm brown and Xiaxia ink in blue-grey; coincident CFI values use a shared outline.
- Editing comment text preserves the locator; deletion removes the row and decoration.
- Overlay clicks open the existing Reading House trace sheet.
- Old annotation/Thought rows are lazily backfilled. The server supplies a validated original-text seed; the browser creates the DOM-dependent CFI; the server revalidates it against the business anchor before caching.
- Invalid or ambiguous backfill leaves the old trace untouched.

Custom GPT still sends chapter/block/offset/text. Thought creation succeeds from its valid business anchor even if renderer backfill is unavailable. Preview/Commit remains a business-anchor operation and does not falsely claim locator completion. Replies are unchanged.

Undo snapshots now contain the engine locator, version and verification time. Renderer-only fields are removed from Action Thought responses and are not added to `openapi.yaml`, listBooks or reading context.

## Shared stops, progress and V2

Shared-stop matching remains the existing chapter/block/offset algorithm. Every new foliate annotation still owns a validated legacy anchor, so `exact`, `overlap` and `same_block` semantics are unchanged.

Publication progress remains separate from `reading_progress`. Foliate completion is accepted only at at least 99.5% overall publication progression and only when the saved locator carries the matching source hash and a valid CFI prefix. Back-cover, reflections, letters, timeline and stamp models are unchanged.

## Existing-book behavior

| Book state | Behavior |
|---|---|
| Source-less legacy | Always legacy; no fabricated bridge |
| Source-backed legacy, bridge pending/failed | Legacy |
| Source-backed legacy, incomplete historical coverage | Legacy |
| Source-backed legacy, 100% coverage and explicit promotion | Foliate with lazy CFI backfill |
| New Phase 2 foliate, bridge pending | Foliate read-only |
| New foliate, bridge ready | Full shared-reading write/decorations |

Old books are not automatically migrated by SQL or deployment.

## Errors and logging

Stable API errors: `locator_bridge_not_ready`, `locator_mapping_missing`, `locator_mapping_ambiguous`, `locator_mapping_validation_failed`, `locator_source_mismatch`, `locator_version_mismatch`, `locator_invalid_cfi`, `locator_backfill_failed`.

Structured logs include book/record/type, source hash, bridge version, href, mapping status, duration and error code. Selected private text, signed URLs and secrets are not logged.

## Real EPUB validation

The three available formerly failing EPUBs were used unchanged. Each built a real bridge and supplied 200 exact Web→legacy samples:

| EPUB | Version | Chapters | Bridge chapters | Exact round-trips | Bridge size | Build time |
|---|---:|---:|---:|---:|---:|---:|
| 卡瓦菲斯诗集＋当你起航前往伊萨卡 | 2.0 | 323 | 323 | 200/200 | 3,865,181 B | 6,686 ms |
| 唐诗选注 | 2.0 | 85 | 85 | 200/200 | 2,436,355 B | 1,660 ms |
| 唐诗选（全二册） | 3.0 | 71 | 71 | 200/200 | 1,258,795 B | 3,915 ms |

Result: **3/3 bridge builds, 600/600 exact sampled legacy round-trips**. Duplicate text was accepted only when context made the match unique. No filename, title, publisher branch or EPUB rewrite exists.

Only these three real files were present. The requested known-good legacy book and blind EPUB remain pending rather than being fabricated.

## Security

- Service-role credentials remain server-only; source and bridge objects remain private.
- Existing signed-URL TTL and no-store behavior are unchanged.
- Client locators are checked against source SHA, engine/adapter/bridge versions, canonical href and validated business text.
- Existing EPUB markup sanitization and script blocking remain active.
- foliate-js remains pinned to `78914aef4466eb960965702401634c2cb348e9b1` with zero upstream patches.

## Known limitations

1. Android Chrome and iPad Safari shared-reading interaction must be confirmed after deployment.
2. A known-good legacy source-backed book and blind EPUB were unavailable.
3. The server validates locator metadata and quote/business mapping but does not execute CFI against publisher DOM; that DOM-dependent operation remains inside the pinned sanitized adapter.
4. Genuinely ambiguous historical anchors remain legacy; the system never guesses.
5. Bridge JSON can be several megabytes; private Storage plus a two-book cache bounds server memory.

## Deployment and rollback

1. Back up Supabase PostgreSQL.
2. Run `migrations/007_reader_engine_dual_anchor.sql` in Supabase SQL Editor.
3. Deploy the complete Phase 3 ZIP.
4. Keep `DUAL_ANCHOR_ENABLED=false` for the first health/legacy check.
5. Set `DUAL_ANCHOR_ENABLED=true` and restart Render for Phase 3 writes.

No Node build, service, bucket change or OpenAPI update is required. New text indexes build their bridge after commit when the flag is enabled. Existing books migrate only through an authenticated `POST /api/books/{book_id}/locator-bridge`; optional `{"promote_legacy":true}` is honored only at 100% historical range coverage.

Rollback needs no down migration: disable `DUAL_ANCHOR_ENABLED` for foliate read-only, or `READER_ENGINE_ENABLED` for full legacy fallback. All business anchors remain intact.

## Phase 3 decision

**PENDING USER VALIDATION.** Server mapping, transactions, real-file bridges and regressions pass. Real-device shared-reading and the two unavailable corpus categories remain the final evidence.
