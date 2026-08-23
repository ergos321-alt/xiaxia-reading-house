-- Safe in-place migration for an existing Storage-based V1 deployment.
-- Adds only the independent Xiaxia-thought table; existing books, chapters,
-- progress, user annotations, replies, and Storage metadata are untouched.

begin;

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

alter table xiaxia_thoughts
    add column if not exists mark_type varchar(64) not null default 'thought';

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'xiaxia_thoughts_mark_type_check'
          and conrelid = 'xiaxia_thoughts'::regclass
    ) then
        alter table xiaxia_thoughts
            add constraint xiaxia_thoughts_mark_type_check
            check (mark_type ~ '^[a-z0-9_-]{1,64}$');
    end if;
end;
$$;

create index if not exists idx_xiaxia_thoughts_chapter_created
    on xiaxia_thoughts (chapter_id, created_at);
create index if not exists idx_xiaxia_thoughts_book_created
    on xiaxia_thoughts (book_id, created_at);

drop trigger if exists trg_xiaxia_thoughts_updated_at on xiaxia_thoughts;
create trigger trg_xiaxia_thoughts_updated_at before update on xiaxia_thoughts
for each row execute function set_reading_house_updated_at();

revoke all on table xiaxia_thoughts from anon, authenticated;
alter table xiaxia_thoughts enable row level security;

alter table ai_reading_state
    add column if not exists last_chunk_index integer,
    add column if not exists last_block_id varchar(64),
    add column if not exists chapter_completed boolean not null default false;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ai_reading_state_last_chunk_index_check'
          and conrelid = 'ai_reading_state'::regclass
    ) then
        alter table ai_reading_state
            add constraint ai_reading_state_last_chunk_index_check
            check (last_chunk_index is null or last_chunk_index >= 0);
    end if;
end;
$$;

commit;
