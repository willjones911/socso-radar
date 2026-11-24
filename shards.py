"""Shared shard I/O: record columns, compact rows, capped writers."""
import json, os

OUT = "ic_results"
CAP_BYTES = 25 * 1024 * 1024
WARN_BYTES = 60 * 1024 * 1024
HARD_BYTES = 95 * 1024 * 1024
#  Index matters — the dashboard unpacks arrays with the same tuple.
#  9..13 are the SPR enrichment columns (checked-at, address, DUN, parliament,
#  state code); they are empty until spr_enrich.py has looked the IC up.
COLS = ('ic','t','name','genderId','raceId','nationalityId','dateOfDeath',
        'employmentStartDateAct4','employmentStartDateAct800',
        'spr_t','address','dun','parliament','region')
SPR_FROM = 9


def rid(r):
    if isinstance(r, list): return r[0] if r else ''
    return r.get('ic') or r.get('ic_number') or ''

def unpack(rec):
    if isinstance(rec, dict):
        return rec
    if isinstance(rec, (list, tuple)):
        return {c: v for c, v in zip(COLS, rec) if v not in (None, '')}
    return None

def to_compact(rec):
    if isinstance(rec, (list, tuple)):
        rec = unpack(rec)
    pr = (rec.get('perkeso_response') or {}).get('debug', {}).get('data') or [{}]
    entry = pr[0] if pr else {}
    vals = [rid(rec), rec.get('t') or rec.get('checked_at') or '']
    for k in COLS[2:]:
        v = entry.get(k)
        if v in (None, '', False): v = rec.get(k)
        vals.append(v if v not in (None, '', False) else None)
    while len(vals) > 2 and vals[-1] is None: vals.pop()
    return vals

def decade_of(ic):
    """Old buckets: 60s … 100s. Anything unparseable lands in 'other'."""
    try:
        yy = int(str(ic)[:2])
    except (TypeError, ValueError):
        return 'other'
    return f"{yy // 10 * 10}s" if yy >= 60 else f"{(yy + 100) // 10 * 10}s"


def decade_key(d):
    """Sort key so 'other' comes last."""
    return int(d[:-1]) if d[:-1].isdigit() else 999

def git_show(path):
    r = subprocess.run(['git','show',f'origin/main:{path}'],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ''

def git_ls(prefix):
    r = subprocess.run(['git','ls-tree','--name-only','origin/main',OUT+'/'],
                       capture_output=True, text=True)
    if r.returncode != 0: return []
    return [p.split('/',1)[1] for p in r.stdout.split()
            if os.path.basename(p).startswith(prefix)]

def rows(blob, label):
    """Parse a JSON list into compact rows (dicts are compacted, so a
    big shard never stays in memory twice)."""
    if not blob.strip(): return []
    try: data = json.loads(blob)
    except Exception as e:
        print(f"  ⚠ {label}: {e}"); return []
    if isinstance(data, dict): data = list(data.values())
    out = []
    for x in data:
        if isinstance(x, (list, tuple)):
            out.append(list(x))
        elif isinstance(x, dict) and rid(x):
            out.append(to_compact(x))
    return out


def pad(row):
    """Normalise a row to full width so indexing is safe."""
    row = list(row)
    if len(row) < len(COLS):
        row += [None] * (len(COLS) - len(row))
    return row


def write_shards(prefix, rows, always_tag=False, out_dir=None):
    rows = sorted(rows, key=lambda r: r[1] if len(r) > 1 else '')
    groups, cur, size = [], [], 0
    for row in rows:
        row = list(row)
        while len(row) > 2 and row[-1] in (None, ''):   # keep files compact
            row.pop()
        b = json.dumps(row, ensure_ascii=False, default=str,
                       separators=(',',':')).encode()
        if cur and size + len(b) > CAP_BYTES:
            groups.append(cur); cur, size = [], 0
        cur.append(b); size += len(b) + 1
    if cur: groups.append(cur)
    written = []
    for i, group in enumerate(groups):
        # a..z, then 01..99 if a single bucket ever needs >26 shards
        tag = ''
        if always_tag or len(groups) > 1:
            tag = (f"_{i + 1:02d}" if len(groups) > 26
                   else f"_{chr(97 + i)}")
        name = f"{prefix}{tag}.json"
        path = os.path.join(out_dir or OUT, name)
        with open(path, 'wb') as f:
            f.write(b'[' + b','.join(group) + b']')
        mb = os.path.getsize(path)
        print(f"    {name}: {len(group):,} ({mb / 1048576:.1f} MB)")
        if mb >= HARD_BYTES: oversized.append((name, mb))
        elif mb >= WARN_BYTES:
            print(f"    ⚠ {name} getting large ({mb / 1048576:.1f} MB)")
        written.append({'file': name, 'count': len(group)})
    return written
