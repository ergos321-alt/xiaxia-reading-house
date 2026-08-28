-- Xiaxia Reading House Phase 2 — source-first publication readiness.
-- Additive and repeatable: no existing book, chapter, anchor or progress row
-- is deleted or rewritten.

begin;

alter table books
    add column if not exists publication_ready boolean not null default false,
    add column if not exists reader_engine text not null default 'legacy',
    add column if not exists text_index_status text not null default 'pending',
    add column if not exists text_index_failure_code text,
    add column if not exists text_index_failure_detail text,
    add column if not exists publication_validated_at timestamptz,
    add column if not exists text_index_updated_at timestamptz not null default now();

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'books_reader_engine_check'
    ) then
        alter table books add constraint books_reader_engine_check
            check (reader_engine in ('legacy', 'foliate'));
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'books_text_index_status_check'
    ) then
        alter table books add constraint books_text_index_status_check
            check (text_index_status in ('pending', 'processing', 'ready', 'failed'));
    end if;
end;
$$;

-- Existing books stay on the exact reader that owns their historical anchors.
-- A source path is the minimum publication fact; a real chapter row with text
-- is the minimum evidence that the legacy Xiaxia text index already exists.
update books b
set publication_ready = (
        b.source_object_path is not null
        and btrim(b.source_object_path) <> ''
    ),
    reader_engine = 'legacy',
    text_index_status = case
        when exists (
            select 1 from chapters c
            where c.book_id = b.id
              and (btrim(c.content_text) <> '' or btrim(c.content_html) <> '')
        ) then 'ready'
        else 'pending'
    end,
    publication_validated_at = case
        when b.source_object_path is not null and btrim(b.source_object_path) <> ''
        then coalesce(b.publication_validated_at, b.created_at)
        else null
    end,
    text_index_updated_at = coalesce(b.text_index_updated_at, b.updated_at, now());

create table if not exists publication_reading_progress (
    book_id uuid primary key references books(id) on delete cascade,
    engine text not null default 'foliate-js'
        check (engine = 'foliate-js'),
    locator jsonb not null default '{}'::jsonb,
    progression numeric(8,7) not null default 0
        check (progression between 0 and 1),
    updated_at timestamptz not null default now()
);

create index if not exists idx_books_publication_readiness
    on books (publication_ready, text_index_status, updated_at desc);
create index if not exists idx_publication_progress_updated
    on publication_reading_progress (updated_at desc);

drop trigger if exists trg_publication_progress_updated_at
    on publication_reading_progress;
create trigger trg_publication_progress_updated_at
before update on publication_reading_progress
for each row execute function set_reading_house_updated_at();

revoke all on table publication_reading_progress from anon, authenticated;
alter table publication_reading_progress enable row level security;

commit;
