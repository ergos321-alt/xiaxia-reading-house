-- Xiaxia Reading House V2 — Supabase PostgreSQL schema
-- Fresh-install schema. Run in Supabase SQL Editor before the first deploy.
-- PostgreSQL stores structured data/text; all book binaries live in a private
-- Supabase Storage bucket and are accessed only by the Flask server.

begin;

create extension if not exists pgcrypto;

insert into storage.buckets (id, name, public, file_size_limit)
values (
    'xiaxia-reading-house-private',
    'xiaxia-reading-house-private',
    false,
    52428800
)
on conflict (id) do update set
    public = false,
    file_size_limit = excluded.file_size_limit;

-- Even if another project feature later adds broad Storage policies, client
-- roles remain blocked from every object in this private bucket. The backend
-- service-role client bypasses RLS and is the sole Storage caller.
drop policy if exists "reading_house_block_client_storage" on storage.objects;
create policy "reading_house_block_client_storage"
on storage.objects
as restrictive
for all
to anon, authenticated
using (bucket_id <> 'xiaxia-reading-house-private')
with check (bucket_id <> 'xiaxia-reading-house-private');

create table if not exists books (
    id uuid primary key default gen_random_uuid(),
    title text not null check (char_length(title) between 1 and 1000),
    author text not null default '未知作者' check (char_length(author) between 1 and 1000),
    format text not null check (format in ('epub', 'txt')),
    source_filename text not null,
    source_sha256 char(64) not null unique,
    source_object_path text not null unique,
    source_media_type text not null,
    source_byte_size integer not null check (source_byte_size >= 0),
    cover_asset_path text,
    chapter_count integer not null default 0 check (chapter_count >= 0),
    toc jsonb not null default '[]'::jsonb,
    publication_ready boolean not null default false,
    reader_engine text not null default 'legacy'
        check (reader_engine in ('legacy', 'foliate')),
    text_index_status text not null default 'pending'
        check (text_index_status in ('pending', 'processing', 'ready', 'failed')),
    text_index_failure_code text,
    text_index_failure_detail text,
    publication_validated_at timestamptz,
    text_index_updated_at timestamptz not null default now(),
    locator_bridge_status text not null default 'pending'
        check (locator_bridge_status in ('pending', 'building', 'ready', 'failed')),
    locator_bridge_version integer check (locator_bridge_version is null or locator_bridge_version > 0),
    locator_bridge_object_path text,
    locator_bridge_sha256 char(64),
    locator_bridge_failure_code text,
    locator_bridge_updated_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists book_assets (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references books(id) on delete cascade,
    asset_path text not null,
    object_path text not null unique,
    media_type text not null,
    byte_size integer not null check (byte_size >= 0),
    created_at timestamptz not null default now(),
    unique (book_id, asset_path)
);

create table if not exists chapters (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references books(id) on delete cascade,
    chapter_index integer not null check (chapter_index >= 0),
    title text not null,
    href text not null,
    content_html text not null,
    content_text text not null,
    word_count integer not null default 0 check (word_count >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (book_id, chapter_index),
    unique (book_id, href)
);

create table if not exists reading_progress (
    book_id uuid primary key references books(id) on delete cascade,
    chapter_id uuid not null references chapters(id) on delete cascade,
    chapter_index integer not null check (chapter_index >= 0),
    position jsonb not null default '{"block_id":"b000001","char_offset":0,"scroll_fraction":0}'::jsonb,
    percentage numeric(6,3) not null default 0 check (percentage between 0 and 100),
    updated_at timestamptz not null default now()
);

-- Phase 2 keeps foliate CFI/locator progress separate until the dual-anchor
-- phase can safely join it to chapter/block progress. Existing progress rows
-- and their non-null chapter foreign key remain untouched.
create table if not exists publication_reading_progress (
    book_id uuid primary key references books(id) on delete cascade,
    engine text not null default 'foliate-js' check (engine = 'foliate-js'),
    locator jsonb not null default '{}'::jsonb,
    progression numeric(8,7) not null default 0 check (progression between 0 and 1),
    updated_at timestamptz not null default now()
);

create table if not exists annotations (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references books(id) on delete cascade,
    chapter_id uuid not null references chapters(id) on delete cascade,
    selected_text text not null check (char_length(selected_text) > 0),
    start_block_id varchar(64) not null,
    start_offset integer not null check (start_offset >= 0),
    end_block_id varchar(64) not null,
    end_offset integer not null check (end_offset >= 0),
    prefix_text varchar(500) not null default '',
    suffix_text varchar(500) not null default '',
    comment text not null default '',
    status text not null default 'pending' check (status in ('pending', 'seen', 'replied')),
    owner text not null default 'user' check (owner = 'user'),
    content_type text not null default 'user_annotation'
        check (content_type = 'user_annotation'),
    engine_locator jsonb,
    engine_locator_version integer check (engine_locator_version is null or engine_locator_version > 0),
    engine_anchor_verified_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists annotation_replies (
    id uuid primary key default gen_random_uuid(),
    annotation_id uuid not null unique references annotations(id) on delete cascade,
    response text not null check (char_length(response) > 0),
    owner text not null default 'xiaxia' check (owner = 'xiaxia'),
    content_type text not null default 'xiaxia_reply'
        check (content_type = 'xiaxia_reply'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Independent thoughts written by Xiaxia through the Action. They are kept
-- separate from user annotations and replies so authorship/meaning stays clear.
create table if not exists xiaxia_thoughts (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references books(id) on delete cascade,
    chapter_id uuid not null references chapters(id) on delete cascade,
    scope text not null check (scope in ('range', 'block', 'chapter')),
    mark_type varchar(64) not null default 'thought'
        constraint xiaxia_thoughts_mark_type_check
        check (mark_type ~ '^[a-z0-9_-]{1,64}$'),
    content text not null check (char_length(content) between 1 and 50000),
    selected_text text not null default '',
    start_block_id varchar(64),
    start_offset integer check (start_offset is null or start_offset >= 0),
    end_block_id varchar(64),
    end_offset integer check (end_offset is null or end_offset >= 0),
    prefix_text varchar(500) not null default '',
    suffix_text varchar(500) not null default '',
    owner text not null default 'xiaxia' check (owner = 'xiaxia'),
    content_type text not null default 'xiaxia_thought'
        check (content_type = 'xiaxia_thought'),
    engine_locator jsonb,
    engine_locator_version integer check (engine_locator_version is null or engine_locator_version > 0),
    engine_anchor_verified_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (
        (scope = 'chapter' and start_block_id is null and start_offset is null
            and end_block_id is null and end_offset is null)
        or
        (scope in ('range', 'block') and start_block_id is not null
            and start_offset is not null and end_block_id is not null
            and end_offset is not null)
    )
);

create table if not exists thought_user_replies (
    id uuid primary key default gen_random_uuid(),
    thought_id uuid not null unique references xiaxia_thoughts(id) on delete cascade,
    response text not null check (char_length(response) between 1 and 50000),
    owner text not null default 'user' check (owner = 'user'),
    content_type text not null default 'user_reply' check (content_type = 'user_reply'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Preview candidates are validation records, never formal reading traces.
create table if not exists xiaxia_thought_candidates (
    id uuid primary key default gen_random_uuid(),
    batch_id uuid not null,
    book_id uuid references books(id) on delete cascade,
    chapter_id uuid references chapters(id) on delete cascade,
    request_payload jsonb not null default '{}'::jsonb,
    validation_status text not null check (validation_status in ('valid', 'invalid')),
    validation_error text,
    matched_text text not null default '',
    validated_anchor jsonb not null default '{}'::jsonb,
    committed_thought_id uuid references xiaxia_thoughts(id) on delete set null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '24 hours')
);

create table if not exists reading_operation_log (
    id uuid primary key default gen_random_uuid(),
    actor text not null check (actor in ('user', 'xiaxia')),
    operation_type text not null,
    target_type text not null,
    target_id uuid,
    book_id uuid references books(id) on delete cascade,
    chapter_id uuid references chapters(id) on delete set null,
    previous_state jsonb not null default '{"records":[]}'::jsonb,
    new_state jsonb not null default '{"records":[]}'::jsonb,
    undone_at timestamptz,
    created_at timestamptz not null default now()
);

create table if not exists ai_reading_state (
    book_id uuid primary key references books(id) on delete cascade,
    last_chapter_read uuid references chapters(id) on delete set null,
    last_chunk_index integer check (last_chunk_index is null or last_chunk_index >= 0),
    last_chunk_id text,
    last_block_id varchar(64),
    chapter_completed boolean not null default false,
    last_annotation_seen uuid references annotations(id) on delete set null,
    updated_at timestamptz not null default now()
);

-- V2: durable whole-book memory state. Current reading checkpoints stay in
-- reading_progress / ai_reading_state; these rows only represent completion
-- and after-reading memories.
create table if not exists book_memory_state (
    book_id uuid primary key references books(id) on delete cascade,
    user_completed_at timestamptz,
    xiaxia_completed_at timestamptz,
    shared_completed_at timestamptz,
    reflections_revealed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (shared_completed_at is null
        or (user_completed_at is not null and xiaxia_completed_at is not null)),
    check (reflections_revealed_at is null or shared_completed_at is not null)
);

create table if not exists ai_chapter_completions (
    book_id uuid not null references books(id) on delete cascade,
    chapter_id uuid not null references chapters(id) on delete cascade,
    last_chunk_id text,
    last_block_id varchar(64),
    completed_at timestamptz not null default now(),
    primary key (book_id, chapter_id)
);

create table if not exists book_reflections (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references books(id) on delete cascade,
    owner text not null check (owner in ('user', 'xiaxia')),
    rating smallint not null check (rating between 1 and 5),
    review_text text not null check (char_length(review_text) between 1 and 50000),
    submitted_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (book_id, owner)
);

create table if not exists reading_letters (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references books(id) on delete cascade,
    author text not null check (author in ('user', 'xiaxia')),
    recipient text not null check (recipient in ('user', 'xiaxia')),
    content text not null check (char_length(content) between 1 and 50000),
    letter_date date not null default current_date,
    sent_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (book_id, author),
    check (
        (author = 'user' and recipient = 'xiaxia')
        or (author = 'xiaxia' and recipient = 'user')
    )
);

create table if not exists reading_memory_events (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references books(id) on delete cascade,
    event_type text not null check (event_type in (
        'started_reading', 'first_shared_stop',
        'completed_reading', 'back_cover_opened'
    )),
    actor text not null check (actor in ('user', 'xiaxia', 'both')),
    chapter_id uuid references chapters(id) on delete set null,
    source_annotation_id uuid references annotations(id) on delete set null,
    source_thought_id uuid references xiaxia_thoughts(id) on delete set null,
    dedupe_key varchar(160) not null default 'book',
    event_data jsonb not null default '{}'::jsonb,
    happened_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    unique (book_id, event_type, dedupe_key)
);

create index if not exists idx_chapters_book_order
    on chapters (book_id, chapter_index);
create index if not exists idx_annotations_chapter_created
    on annotations (chapter_id, created_at);
create index if not exists idx_annotations_book_status_created
    on annotations (book_id, status, created_at);
create index if not exists idx_annotations_pending
    on annotations (created_at) where status = 'pending';
create index if not exists idx_xiaxia_thoughts_chapter_created
    on xiaxia_thoughts (chapter_id, created_at);
create index if not exists idx_xiaxia_thoughts_book_created
    on xiaxia_thoughts (book_id, created_at);
create index if not exists idx_books_locator_bridge_state
    on books (locator_bridge_status, locator_bridge_updated_at desc);
create index if not exists idx_annotations_engine_locator
    on annotations (book_id, engine_locator_version) where engine_locator is not null;
create index if not exists idx_thoughts_engine_locator
    on xiaxia_thoughts (book_id, engine_locator_version) where engine_locator is not null;
create index if not exists idx_thought_user_replies_thought
    on thought_user_replies (thought_id);
create index if not exists idx_thought_candidates_batch
    on xiaxia_thought_candidates (batch_id, created_at);
create index if not exists idx_operation_log_actor_created
    on reading_operation_log (actor, created_at desc) where undone_at is null;
create index if not exists idx_book_assets_book
    on book_assets (book_id);
create index if not exists idx_reading_progress_updated
    on reading_progress (updated_at desc);
create index if not exists idx_books_publication_readiness
    on books (publication_ready, text_index_status, updated_at desc);
create index if not exists idx_publication_progress_updated
    on publication_reading_progress (updated_at desc);
create index if not exists idx_ai_chapter_completions_book
    on ai_chapter_completions (book_id, completed_at);
create index if not exists idx_book_reflections_book_owner
    on book_reflections (book_id, owner);
create index if not exists idx_reading_letters_book_author
    on reading_letters (book_id, author);
create index if not exists idx_reading_memory_events_book_time
    on reading_memory_events (book_id, happened_at, id);

create or replace function set_reading_house_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_books_updated_at on books;
create trigger trg_books_updated_at before update on books
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_chapters_updated_at on chapters;
create trigger trg_chapters_updated_at before update on chapters
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_publication_progress_updated_at on publication_reading_progress;
create trigger trg_publication_progress_updated_at before update on publication_reading_progress
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_annotations_updated_at on annotations;
create trigger trg_annotations_updated_at before update on annotations
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_annotation_replies_updated_at on annotation_replies;
create trigger trg_annotation_replies_updated_at before update on annotation_replies
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_xiaxia_thoughts_updated_at on xiaxia_thoughts;
create trigger trg_xiaxia_thoughts_updated_at before update on xiaxia_thoughts
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_thought_user_replies_updated_at on thought_user_replies;
create trigger trg_thought_user_replies_updated_at before update on thought_user_replies
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_ai_reading_state_updated_at on ai_reading_state;
create trigger trg_ai_reading_state_updated_at before update on ai_reading_state
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_book_memory_state_updated_at on book_memory_state;
create trigger trg_book_memory_state_updated_at before update on book_memory_state
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_book_reflections_updated_at on book_reflections;
create trigger trg_book_reflections_updated_at before update on book_reflections
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_reading_letters_updated_at on reading_letters;
create trigger trg_reading_letters_updated_at before update on reading_letters
for each row execute function set_reading_house_updated_at();

-- Supabase API roles receive no direct table access. The Flask service connects
-- with the private PostgreSQL connection string and is the only data boundary.
revoke all on table books, book_assets, chapters, reading_progress,
    publication_reading_progress,
    annotations, annotation_replies, xiaxia_thoughts, thought_user_replies,
    xiaxia_thought_candidates, reading_operation_log, ai_reading_state,
    book_memory_state, ai_chapter_completions, book_reflections,
    reading_letters, reading_memory_events
    from anon, authenticated;

alter table books enable row level security;
alter table book_assets enable row level security;
alter table chapters enable row level security;
alter table reading_progress enable row level security;
alter table publication_reading_progress enable row level security;
alter table annotations enable row level security;
alter table annotation_replies enable row level security;
alter table xiaxia_thoughts enable row level security;
alter table thought_user_replies enable row level security;
alter table xiaxia_thought_candidates enable row level security;
alter table reading_operation_log enable row level security;
alter table ai_reading_state enable row level security;
alter table book_memory_state enable row level security;
alter table ai_chapter_completions enable row level security;
alter table book_reflections enable row level security;
alter table reading_letters enable row level security;
alter table reading_memory_events enable row level security;

commit;
