"""spr_enrich.py — enrich existing valid ICs with Election Commission data.

The PERKESO scraper decides *who exists*. This pass does not search for new
people; it takes every IC already marked valid and looks it up on the SPR voter
roll, then writes back what SPR knows and PERKESO does not: home address,
locality, DUN, Parliament and state. It also records the date checked, so a
second run carries on where this one stopped.

The four-digit captcha is solved offline with ddddocr (no paid solver).

    python3 spr_enrich.py --minutes 20            # enrich a slice of the backlog
    python3 spr_enrich.py --minutes 20 --workers 2
    python3 spr_enrich.py --redo --minutes 10     # refresh everything

Rows gain: spr_t, address, dun, parliament, region (see shards.COLS).
An IC with no voter record still gets spr_t, so it is never re-queried.
"""
import argparse, json, os, random, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

import shards
from shards import COLS, SPR_FROM

BASE = "https://mysprsemak.spr.gov.my/semakan"

_ocr = None
_ocr_lock = threading.Lock()


def ocr():
    """ddddocr is a heavy import and not thread-safe — load and use serially."""
    global _ocr
    if _ocr is None:
        import ddddocr
        _ocr = ddddocr.DdddOcr(show_ad=False, beta=True)
    return _ocr


def semak(sess, icno, tries=3):
    """dict of enrichment fields on a hit, {} when not a voter, None if unknown.

    The captcha slot is per session and single-valued, so one solve = one
    submission; but /refreshCaptcha hands back both the image and the CSRF
    token as JSON, which saves pulling the 16 KB form page every time.
    """
    for i in range(tries):
        try:
            d = sess.get("https://mysprsemak.spr.gov.my/refreshCaptcha",
                         timeout=20).json()
            token = d["token"]
            img = sess.get(d["captcha"].split('src="', 1)[1].split('"', 1)[0],
                           timeout=20).content
            with _ocr_lock:
                answer = ocr().classification(img)
            page = sess.post(f"{BASE}/daftarPemilihPapar", timeout=25, data={
                "_token": token, "NoKp": icno, "captcha": answer}).text
            if "MAKLUMAT PERIBADI" in page:
                return parse(page)
            if "Rekod tidak ditemui" in page:
                return {}
            time.sleep(1 + i)                      # captcha rejected, retry
        except Exception:
            time.sleep(2 + i)
    return None


def cell(page, label):
    m = re.search(r">\s*" + re.escape(label) + r"\s*</td>\s*<td[^>]*>(.*?)</td>", page, re.S)
    if not m:
        return None
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    txt = (txt.replace("&amp;", "&").replace("&#039;", "'").replace("&quot;", '"')
              .replace("&lt;", "<").replace("&gt;", ">"))
    return " ".join(txt.split()) or None


def parse(page):
    """address, dun, parliament, region — all straight from the result table."""
    return {
        "address":    cell(page, "Lokaliti"),
        "dun":        cell(page, "DUN"),
        "parliament": cell(page, "Parlimen"),
        "region":     cell(page, "Negeri"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--redo", action="store_true", help="re-query already enriched ICs")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    out = shards.OUT
    manifest_file = os.path.join(out, "manifest.json")
    try:
        manifest = json.load(open(manifest_file))
        files = [m["file"] for m in manifest if m.get("file")]
    except Exception:
        print("  no manifest — nothing to enrich")
        return

    rows, index = [], {}
    for name in files:
        path = os.path.join(out, name)
        if not os.path.exists(path):
            continue
        for row in json.load(open(path)):
            row = shards.pad(row)
            index[row[0]] = len(rows)
            rows.append(row)

    queue = [r for r in rows
             if a.redo or len(r) <= SPR_FROM or r[SPR_FROM] is None]
    random.shuffle(queue)
    print(f"  {len(rows):,} records, {len(queue):,} to look up")

    stop = time.time() + a.minutes * 60
    done = hits = 0

    def work(row):
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (socso-radar enrich)"
        res = semak(s, row[0])
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if res is None:
            return None
        row[SPR_FROM] = stamp
        row[SPR_FROM + 1] = res.get("address")
        row[SPR_FROM + 2] = res.get("dun")
        row[SPR_FROM + 3] = res.get("parliament")
        row[SPR_FROM + 4] = res.get("region")
        return bool(res)

    try:
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
            futs = {}
            it = iter(queue)
            while True:
                for fut in [f for f in futs if f.done()]:
                    del futs[fut]
                    out_c = fut.result()
                    if out_c is None:
                        continue
                    done += 1
                    hits += 1 if out_c else 0
                    if done % 25 == 0:
                        print(f"  {done} looked up | {hits} with an address "
                              f"| {done / max(time.time() - (stop - a.minutes * 60), 1) * 60:.0f}/min")
                if time.time() > stop or (a.limit and done >= a.limit):
                    if not futs:
                        break
                    if time.time() > stop + 60:
                        break
                    time.sleep(0.5)
                    continue
                while len(futs) < a.workers * 2:
                    row = next(it, None)
                    if row is None:
                        break
                    futs[pool.submit(work, row)] = row
                time.sleep(0.3)
    except KeyboardInterrupt:
        pass

    left = sum(1 for r in rows if len(r) <= SPR_FROM or r[SPR_FROM] is None)
    print(f"\n  {done:,} looked up, {hits:,} matched a voter, {left:,} still to do")
    if not done:
        return

    # rewrite the shards in place, same decade buckets and cap as the merge
    by_decade = {}
    for row in rows:
        e = row[0]
        yy = int(e[:2])
        dec = f"{yy // 10 * 10}s" if yy >= 60 else f"{(yy + 100) // 10 * 10}s"
        by_decade.setdefault(dec, []).append(row)

    for old in os.listdir(out):
        if old.startswith("valid_") and old.endswith(".json"):
            os.remove(os.path.join(out, old))

    manifest = []
    for dec in sorted(by_decade, key=lambda d: int(d[:-1])):
        for entry in shards.write_shards(f"valid_{dec}", by_decade[dec]):
            entry.update({"decade": dec, "fmt": 2})
            manifest.append(entry)
        print(f"    {dec}: {len(by_decade[dec]):,}")
    tmp = manifest_file + ".tmp"
    json.dump(manifest, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, manifest_file)


if __name__ == "__main__":
    main()
