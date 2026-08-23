-- Run only after scripts/migrate_legacy_bytea_to_storage.py succeeds.

begin;

do $$
begin
    if exists (
        select 1 from book_assets where object_path is null
    ) then
        raise exception
            'Legacy book_assets remain unmigrated; refusing to drop bytea data';
    end if;
end;
$$;

alter table book_assets drop column if exists data;
alter table book_assets alter column object_path set not null;

commit;

