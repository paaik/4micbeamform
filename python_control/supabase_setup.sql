-- Drone track log in Supabase. Run once in the SQL Editor.
-- Safe to run again: it adds missing columns and does not delete rows.

create table if not exists detections (
  id bigint generated always as identity primary key,
  recorded_at timestamptz default now(),
  drone_type text,
  azimuth_deg real,
  elevation_deg real,
  confidence real,
  frame bigint,
  photo_url text,
  device_id text
);

alter table detections add column if not exists confidence real;
alter table detections add column if not exists frame bigint;

alter table detections enable row level security;

drop policy if exists "pi can insert" on detections;
create policy "pi can insert" on detections for insert to anon with check (true);

drop policy if exists "dashboard can read" on detections;
create policy "dashboard can read" on detections for select to anon using (true);

do $$
begin
  alter publication supabase_realtime add table detections;
exception when duplicate_object then null;
end $$;

insert into storage.buckets (id, name, public) values ('photos', 'photos', true)
on conflict (id) do update set public = true;

drop policy if exists "pi can upload photos" on storage.objects;
create policy "pi can upload photos" on storage.objects
  for insert to anon with check (bucket_id = 'photos');
