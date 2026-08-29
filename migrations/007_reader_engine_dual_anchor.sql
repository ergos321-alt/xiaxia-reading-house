-- Xiaxia Reading House Phase 3 — additive dual-anchor and locator bridge state.
-- Existing business anchors and rows are not rewritten.

begin;

alter table books
    add column if not exists locator_bridge_status text not null default 'pending',
    add column if not exists locator_bridge_version integer,
    add column if not exists locator_bridge_object_path text,
    add column if not exists locator_bridge_sha256 char(64),
    add column if not exists locator_bridge_failure_code text,
    add column if not exists locator_bridge_updated_at timestamptz;

alter table annotations
    add column if not exists engine_locator jsonb,
    add column if not exists engine_locator_version integer,
    add column if not exists engine_anchor_verified_at timestamptz;

alter table xiaxia_thoughts
    add column if not exists engine_locator jsonb,
    add column if not exists engine_locator_version integer,
    add column if not exists engine_anchor_verified_at timestamptz;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'books_locator_bridge_status_check'
    ) then
        alter table books add constraint books_locator_bridge_status_check
            check (locator_bridge_status in ('pending', 'building', 'ready', 'failed'));
    end if;
    if not exists (
        select 1 from pg_constraint where conname = 'books_locator_bridge_version_check'
    ) then
        alter table books add constraint books_locator_bridge_version_check
            check (locator_bridge_version is null or locator_bridge_version > 0);
    end if;
    if not exists (
        select 1 from pg_constraint where conname = 'annotations_engine_locator_version_check'
    ) then
        alter table annotations add constraint annotations_engine_locator_version_check
            check (engine_locator_version is null or engine_locator_version > 0);
    end if;
    if not exists (
        select 1 from pg_constraint where conname = 'thoughts_engine_locator_version_check'
    ) then
        alter table xiaxia_thoughts add constraint thoughts_engine_locator_version_check
            check (engine_locator_version is null or engine_locator_version > 0);
    end if;
end;
$$;

create index if not exists idx_books_locator_bridge_state
    on books (locator_bridge_status, locator_bridge_updated_at desc);
create index if not exists idx_annotations_engine_locator
    on annotations (book_id, engine_locator_version)
    where engine_locator is not null;
create index if not exists idx_thoughts_engine_locator
    on xiaxia_thoughts (book_id, engine_locator_version)
    where engine_locator is not null;

commit;
