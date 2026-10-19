create table lecturas (
  id bigint generated always as identity primary key,
  created_at timestamptz default now(),
  suelo int, suelo_pct int, temp float, hum float,
  bomba int, fan int, luz int
);
create table eventos (
  id bigint generated always as identity primary key,
  created_at timestamptz default now(),
  tipo text, detalle text
);
alter table lecturas enable row level security;
alter table eventos enable row level security;
