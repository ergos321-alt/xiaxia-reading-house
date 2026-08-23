-- Xiaxia Reading House V1.1 management migration.
-- Safe for an existing V1 database: no book, chapter, annotation, Thought,
-- reply, progress, or Storage data is removed.

begin;

alter table annotations
    add column if not exists owner text not null default 'user',
    add column if not exists content_type text not null default 'user_annotation';
alter table annotation_replies
    add column if not exists owner text not null default 'xiaxia',
    add column if not exists content_type text not null default 'xiaxia_reply';
alter table xiaxia_thoughts
    add column if not exists owner text not null default 'xiaxia',
    add column if not exists content_type text not null default 'xiaxia_thought';
alter table ai_reading_state add column if not exists last_chunk_id text;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'annotations_owner_v11_check') then
        alter table annotations add constraint annotations_owner_v11_check check (owner = 'user');
    end if;
    if not exists (select 1 from pg_constraint where conname = 'annotations_content_type_v11_check') then
        alter table annotations add constraint annotations_content_type_v11_check check (content_type = 'user_annotation');
    end if;
    if not exists (select 1 from pg_constraint where conname = 'annotation_replies_owner_v11_check') then
        alter table annotation_replies add constraint annotation_replies_owner_v11_check check (owner = 'xiaxia');
    end if;
    if not exists (select 1 from pg_constraint where conname = 'annotation_replies_content_type_v11_check') then
        alter table annotation_replies add constraint annotation_replies_content_type_v11_check check (content_type = 'xiaxia_reply');
    end if;
    if not exists (select 1 from pg_constraint where conname = 'xiaxia_thoughts_owner_v11_check') then
        alter table xiaxia_thoughts add constraint xiaxia_thoughts_owner_v11_check check (owner = 'xiaxia');
    end if;
    if not exists (select 1 from pg_constraint where conname = 'xiaxia_thoughts_content_type_v11_check') then
        alter table xiaxia_thoughts add constraint xiaxia_thoughts_content_type_v11_check check (content_type = 'xiaxia_thought');
    end if;
end $$;

create table if not exists thought_user_replies (
    id uuid primary key default gen_random_uuid(),
    thought_id uuid not null unique references xiaxia_thoughts(id) on delete cascade,
    response text not null check (char_length(response) between 1 and 50000),
    owner text not null default 'user' check (owner = 'user'),
    content_type text not null default 'user_reply' check (content_type = 'user_reply'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

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

create index if not exists idx_thought_user_replies_thought on thought_user_replies (thought_id);
create index if not exists idx_thought_candidates_batch on xiaxia_thought_candidates (batch_id, created_at);
create index if not exists idx_operation_log_actor_created
    on reading_operation_log (actor, created_at desc) where undone_at is null;

drop trigger if exists trg_thought_user_replies_updated_at on thought_user_replies;
create trigger trg_thought_user_replies_updated_at before update on thought_user_replies
for each row execute function set_reading_house_updated_at();

revoke all on table thought_user_replies, xiaxia_thought_candidates,
    reading_operation_log from anon, authenticated;
alter table thought_user_replies enable row level security;
alter table xiaxia_thought_candidates enable row level security;
alter table reading_operation_log enable row level security;

commit;
