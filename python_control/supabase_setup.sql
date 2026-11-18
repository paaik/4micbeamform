-- Drone detector: Supabase setup. Run in SQL Editor.
-- WARNING: re-running deletes all saved detections (drop table).

-- 1. Event data table
drop table if exists detections;

create table detections (
  id bigint generated always as identity primary key,
  recorded_at timestamptz default now(),  -- Time recorded
  drone_type text,                         -- Drone type
  azimuth_deg real,                        -- Azimuth angle, 0-360 degrees
  elevation_deg real,                      -- Elevation angle, 0-90 degrees
  photo_url text,                          -- Photo link
  device_id text                           -- which detector sent it
);

-- 2. Allow the Pi to add rows and the dashboard to read them
alter table detections enable row level security;
create policy "pi can insert" on detections for insert to anon with check (true);
create policy "dashboard can read" on detections for select to anon using (true);

-- 3. Real-time updates for the dashboard (skips if already on)
do $$
begin
  alter publication supabase_realtime add table detections;
exception when duplicate_object then null;
end $$;

-- 4. Photo bucket: create it, or make sure it is public if it exists
insert into storage.buckets (id, name, public) values ('photos', 'photos', true)
on conflict (id) do update set public = true;

-- 5. Allow the Pi to upload photos
drop policy if exists "pi can upload photos" on storage.objects;
create policy "pi can upload photos" on storage.objects
  for insert to anon with check (bucket_id = 'photos');
