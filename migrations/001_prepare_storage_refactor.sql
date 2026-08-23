-- Only for a database that already ran the earlier bytea-based V1 schema.
-- Fresh installations must run ../schema.sql instead.

begin;

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

drop policy if exists "reading_house_block_client_storage" on storage.objects;
create policy "reading_house_block_client_storage"
on storage.objects
as restrictive
for all
to anon, authenticated
using (bucket_id <> 'xiaxia-reading-house-private')
with check (bucket_id <> 'xiaxia-reading-house-private');

alter table books add column if not exists source_object_path text;
alter table books add column if not exists source_media_type text;
alter table books add column if not exists source_byte_size integer;
alter table book_assets add column if not exists object_path text;

create unique index if not exists idx_books_source_object_path
    on books (source_object_path) where source_object_path is not null;
create unique index if not exists idx_book_assets_object_path
    on book_assets (object_path) where object_path is not null;

commit;

