-- Xiaxia Reading House V2 reading-memory migration.
-- Safe for the deployed V1.1 database: this migration only adds V2 tables,
-- indexes, triggers, policies, and conservative milestone backfills.

begin;

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

-- ai_reading_state remains the current checkpoint. This table records the
-- chapters whose final chunk was explicitly completed, so V2 never infers a
-- whole-book completion from a checkpoint in the final chapter alone.
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

-- This is a sparse product timeline, not an operation log. Edits, deletes,
-- replies, and management actions are intentionally excluded.
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

create index if not exists idx_ai_chapter_completions_book
    on ai_chapter_completions (book_id, completed_at);
create index if not exists idx_book_reflections_book_owner
    on book_reflections (book_id, owner);
create index if not exists idx_reading_letters_book_author
    on reading_letters (book_id, author);
create index if not exists idx_reading_memory_events_book_time
    on reading_memory_events (book_id, happened_at, id);

drop trigger if exists trg_book_memory_state_updated_at on book_memory_state;
create trigger trg_book_memory_state_updated_at before update on book_memory_state
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_book_reflections_updated_at on book_reflections;
create trigger trg_book_reflections_updated_at before update on book_reflections
for each row execute function set_reading_house_updated_at();

drop trigger if exists trg_reading_letters_updated_at on reading_letters;
create trigger trg_reading_letters_updated_at before update on reading_letters
for each row execute function set_reading_house_updated_at();

insert into book_memory_state (book_id)
select id from books
on conflict (book_id) do nothing;

-- Only the latest chapter completion is provable in a legacy checkpoint.
insert into ai_chapter_completions (
    book_id, chapter_id, last_chunk_id, last_block_id, completed_at
)
select ais.book_id, ais.last_chapter_read, ais.last_chunk_id,
       ais.last_block_id, ais.updated_at
from ai_reading_state ais
join chapters c on c.id = ais.last_chapter_read and c.book_id = ais.book_id
where ais.chapter_completed = true and ais.last_chapter_read is not null
on conflict (book_id, chapter_id) do nothing;

-- A legacy 100% user progress is the only safe automatic user-completion
-- backfill. Lower percentages are never rounded up.
update book_memory_state bms
set user_completed_at = rp.updated_at
from reading_progress rp
where rp.book_id = bms.book_id
  and rp.percentage >= 100
  and bms.user_completed_at is null;

update book_memory_state bms
set xiaxia_completed_at = completed.latest_completed_at
from (
    select b.id as book_id, max(acc.completed_at) as latest_completed_at
    from books b
    join chapters c on c.book_id = b.id
    join ai_chapter_completions acc
      on acc.book_id = b.id and acc.chapter_id = c.id
    group by b.id, b.chapter_count
    having count(distinct acc.chapter_id) = b.chapter_count
       and b.chapter_count > 0
) completed
where completed.book_id = bms.book_id
  and bms.xiaxia_completed_at is null;

update book_memory_state
set shared_completed_at = greatest(user_completed_at, xiaxia_completed_at)
where user_completed_at is not null
  and xiaxia_completed_at is not null
  and shared_completed_at is null;

insert into reading_memory_events (
    book_id, event_type, actor, dedupe_key, happened_at
)
select rp.book_id, 'started_reading', 'user', 'book', rp.updated_at
from reading_progress rp
where rp.percentage > 0
on conflict (book_id, event_type, dedupe_key) do nothing;

insert into reading_memory_events (
    book_id, event_type, actor, dedupe_key, happened_at
)
select book_id, 'completed_reading', 'both', 'book', shared_completed_at
from book_memory_state
where shared_completed_at is not null
on conflict (book_id, event_type, dedupe_key) do nothing;

revoke all on table book_memory_state, ai_chapter_completions,
    book_reflections, reading_letters, reading_memory_events
    from anon, authenticated;

alter table book_memory_state enable row level security;
alter table ai_chapter_completions enable row level security;
alter table book_reflections enable row level security;
alter table reading_letters enable row level security;
alter table reading_memory_events enable row level security;

commit;
