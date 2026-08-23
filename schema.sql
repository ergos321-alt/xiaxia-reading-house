-- Xiaxia Reading House V1 — Supabase PostgreSQL schema
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
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists annotation_replies (
    id uuid primary key default gen_random_uuid(),
    annotation_id uuid not null unique references annotations(id) on delete cascade,
    response text not null check (char_length(response) > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists ai_reading_state (
    book_id uuid primary key references books(id) on delete cascade,
    last_chapter_read uuid references chapters(id) on delete set null,
    last_annotation_seen uuid references annotations(id) on delete set null,
    updated_at timestamptz not null default now()
);

create index if not exists idx_chapters_book_order
    on chapters (book_id, chapter_index);
create index if not exists idx_annotations_chapter_created
    on annotations (chapter_id, created_at);
create index if not exists idx_annotations_book_status_created
    on annotations (book_id, status, created_at);
create index if not exists idx_annotations_pending
    on annotations (created_at) where status = 'pending';
create index if not exists idx_book_assets_book
    on book_assets (book_id);
create index if not exists idx_reading_progress_updated
    on reading_progress (updated_at desc);

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

drop trigger if exists trg_annotations_updated_at on annotations;
create trigger trg_annotations_updated_at before update on annotations
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_annotation_replies_updated_at on annotation_replies;
create trigger trg_annotation_replies_updated_at before update on annotation_replies
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_ai_reading_state_updated_at on ai_reading_state;
create trigger trg_ai_reading_state_updated_at before update on ai_reading_state
for each row execute function set_reading_house_updated_at();

-- Supabase API roles receive no direct table access. The Flask service connects
-- with the private PostgreSQL connection string and is the only data boundary.
revoke all on table books, book_assets, chapters, reading_progress,
    annotations, annotation_replies, ai_reading_state from anon, authenticated;

alter table books enable row level security;
alter table book_assets enable row level security;
alter table chapters enable row level security;
alter table reading_progress enable row level security;
alter table annotations enable row level security;
alter table annotation_replies enable row level security;
alter table ai_reading_state enable row level security;

commit;
