#!/usr/bin/env python3
"""Собирает data/catalog.json из категорийных страниц nevatrip.ru.

Берет поле initialTrips из RSC-данных Next.js, оставляет только активные
позиции, склеивает с data/tags.json (uri -> теги), пишет отчет.
Если сбор упал, прежний catalog.json не трогается, код выхода 1.
"""
import csv, io, json, re, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://nevatrip.ru"
PAGES = ["novogodnie-ekskursii", "avtobusnye-ehkskursii", "muzei"]
MAX_DAYS = 60          # дальше этого срока ближайшая дата считается предпродажей
MIN_EXPECTED = 15      # меньше активных, чем это, считаем сбоем сбора
UA = "nevatrip-vibe-catalog/1.0"
SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSsuGjYx0gJOfw-aWKX0ZQZYiJQ_sOWMMq9aHPjDsY8ME0-IZArrV0A_BPwior0f3O2EJ18KI13M_cX/pub?gid=2053499112&single=true&output=tsv"
VOCAB = {"night","lights","newyear","festive","food","romantic","water","cozy","music","guide","history","palace","museum","indoor","mystic","drive","masterclass","trip","kids","short"}
CLOSED_RE = re.compile(r"Сезон закончился|Продажи с", re.I)


def fetch(path):
    req = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def trips_from_html(html):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', html, re.S)
    text = "".join(json.loads(c) for c in chunks)
    i = text.find('"initialTrips":')
    if i < 0:
        raise ValueError("initialTrips не найден")
    arr, _ = json.JSONDecoder().raw_decode(text[i + len('"initialTrips":'):])
    return arr


def clean_tags(raw):
    return re.sub(r"\(#[0-9A-Fa-f]+\)\{#[0-9A-Fa-f]+\}", "", raw or "").strip("[] ")


def price_num(p):
    m = re.search(r"\d[\d\s ]*", p or "")
    return int(re.sub(r"\D", "", m.group())) if m else None


def load_sheet():
    """Теги и формат из опубликованной Google-таблицы. При сбое: data/tags.json и пустой формат."""
    warn = []
    try:
        req = urllib.request.Request(SHEET_URL, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(raw), delimiter="\t"))
        head = rows[0]
        ci = {h: i for i, h in enumerate(head)}
        out = {}
        for r in rows[1:]:
            if len(r) < len(head) or not r[ci["Ссылка"]].strip():
                continue
            uri = r[ci["Ссылка"]].strip().replace("https://nevatrip.ru/", "").strip("/")
            tags = [t.strip().lower() for t in r[ci["Теги"]].split(",") if t.strip()]
            bad = [t for t in tags if t not in VOCAB]
            if bad:
                warn.append(f"Неизвестные теги у {uri}: {', '.join(bad)} (не участвуют в подборе)")
                tags = [t for t in tags if t in VOCAB]
            out[uri] = {"tags": tags, "format": r[ci["Формат"]].strip()}
        if len(out) < 20:
            raise ValueError(f"в таблице слишком мало строк: {len(out)}")
        return out, warn, "Google-таблица"
    except Exception as e:
        warn.append(f"Таблица недоступна ({e}), взяты теги из data/tags.json")
        old = json.loads((ROOT / "data/tags.json").read_text(encoding="utf-8"))
        return {u: {"tags": t, "format": ""} for u, t in old.items()}, warn, "data/tags.json"


AGE_RE = [
    re.compile(r"возрастное ограничение[^.\n]{0,40}?(\d{1,2})\s*\+", re.I),
    re.compile(r"(?:от|с)\s+(\d{1,2})\s+лет(?:[^.\n]{0,40})(?:допуска|можно|пускают|принима)", re.I),
    re.compile(r"(?:дети|детей|ребен\w+)\s+(?:до\s+)?(\d{1,2})\s+лет[^.\n]{0,30}(?:не допуска|не пуска|не принима|нельзя)", re.I),
    re.compile(r"только если ребенку уже есть (\d{1,2})\s+лет", re.I),
    re.compile(r"(\d{1,2})\s*\+", re.I),
]
def read_age(uri):
    """Минимальный возраст из текста страницы; 0, если ограничения не нашли."""
    import html as _h
    req = urllib.request.Request(f"{BASE}/{uri}", headers={"User-Agent": UA})
    h = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)
    lines = [l.strip() for l in _h.unescape(re.sub(r"<[^>]+>", "\n", body)).split("\n") if l.strip()]
    found = []
    for l in lines:
        if not re.search(r"возраст|ограничен|лет|\d\s*\+|ребен|дет", l, re.I) or len(l) > 300: continue
        for rx in AGE_RE[:4]:
            m = rx.search(l)
            if m: found.append(int(m.group(1)))
        if re.search(r"возраст|ограничен", l, re.I):
            m = AGE_RE[4].search(l)
            if m: found.append(int(m.group(1)))
    found = [a for a in found if 3 <= a <= 21]
    return max(found) if found else 0


DAY_STEMS = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4), ("суббот", 5), ("воскресен", 6)]
def parse_days(sched):
    """Дни недели (0=пн … 6=вс), когда идет прогулка, по строке «Расписание». None, если по тексту не понять."""
    s = (sched or "").lower()
    s = re.split(r"\.\s", s)[0]            # дальше идут пояснения про кнопку «Выбрать дату»
    if re.search(r"\d{1,2}(,| и)?\s*(\d{1,2}\s*)?(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)", s) and "ежедневно" not in s:
        return None                          # конкретные даты, не регулярное расписание
    if "ежедневно" in s:
        days = set(range(7))
        m = re.search(r"кроме\s+([^,.;]+)", s)
        if m:
            for stem, d in DAY_STEMS:
                if m.group(1).strip().startswith(stem[:5]): days.discard(d)
        return sorted(days)
    days = sorted({d for stem, d in DAY_STEMS if stem in s})
    return days or None


def read_schedule(uri):
    """Строка «Расписание» со страницы прогулки (сначала берем из data/pages.json, иначе читаем страницу)."""
    pj = ROOT / "data/pages.json"
    if pj.exists():
        pages = json.loads(pj.read_text(encoding="utf-8"))
        if uri in pages and pages[uri].get("facts", {}).get("Расписание"):
            return pages[uri]["facts"]["Расписание"]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collect_pages import parse
    return parse(fetch(uri)).get("facts", {}).get("Расписание", "")


def load_meta(items):
    """data/meta.json: uri -> {age_min, days}. Новые прогулки дочитываем, остальные берем из файла."""
    path = ROOT / "data/meta.json"
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    changed = False
    for it in items:
        m = meta.setdefault(it["uri"], {})
        if "age_min" not in m:
            try:
                m["age_min"] = read_age(it["uri"]); changed = True; time.sleep(3)
            except Exception as e:
                print(f"Возраст не прочитан: {it['uri']} ({e})")
        if "days" not in m:
            try:
                m["days"] = parse_days(read_schedule(it["uri"])); changed = True; time.sleep(2)
            except Exception as e:
                print(f"Расписание не прочитано: {it['uri']} ({e})")
    if changed:
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    for it in items:
        it["age_min"] = meta.get(it["uri"], {}).get("age_min", 0)
        it["days"] = meta.get(it["uri"], {}).get("days")


THUMB_W = 640
XL_W = 1600
def _fetch_image(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=40).read()


def make_thumbs(items):
    """Копии фото прогулок: маленькая для карточек (site/img) и крупная для картинки-приглашения (site/img/xl).
    Скачиваем только то, чего еще нет."""
    try:
        from PIL import Image
    except Exception:
        print("Pillow не установлен, фото пропущены"); return
    import io as _io
    out_dir = ROOT / "site/img"; out_dir.mkdir(parents=True, exist_ok=True)
    xl_dir = out_dir / "xl"; xl_dir.mkdir(parents=True, exist_ok=True)
    for it in items:
        slug = it["uri"].split("/")[-1]
        dest, dest_xl = out_dir / f"{slug}.jpg", xl_dir / f"{slug}.jpg"
        if (not dest.exists() or not dest_xl.exists()) and (it.get("img") or it.get("img_xl")):
            try:
                raw = None
                for url in (it.get("img_xl"), it.get("img")):
                    if not url: continue
                    try: raw = _fetch_image(url); break
                    except Exception: continue
                if raw is None: raise ValueError("нет доступного фото")
                im = Image.open(_io.BytesIO(raw)).convert("RGB")
                if not dest.exists():
                    h = round(im.height * THUMB_W / im.width)
                    im.resize((THUMB_W, h), Image.LANCZOS).save(dest, "JPEG", quality=72, optimize=True, progressive=True)
                if not dest_xl.exists():
                    w = min(XL_W, im.width); h = round(im.height * w / im.width)
                    im.resize((w, h), Image.LANCZOS).save(dest_xl, "JPEG", quality=74, optimize=True, progressive=True)
                time.sleep(1)
            except Exception as e:
                print(f"Фото не скачалось: {it['uri']} ({e})")
        if dest.exists():
            it["photo"] = f"img/{slug}.jpg"
        if dest_xl.exists():
            it["photo_xl"] = f"img/xl/{slug}.jpg"


INV_W = 1600
def make_invite_photos(items):
    """Атмосферные фото для картинки-приглашения: data/invite_photos.json (uri -> путь в галерее, например 887/11982.jpg).
    Если фото не указано или не скачалось, в приглашении остается обычное фото прогулки."""
    path = ROOT / "data/invite_photos.json"
    if not path.exists(): return
    try:
        from PIL import Image
    except Exception:
        return
    import io as _io
    pins = json.loads(path.read_text(encoding="utf-8"))
    out_dir = ROOT / "site/img/inv"; out_dir.mkdir(parents=True, exist_ok=True)
    for it in items:
        g = pins.get(it["uri"])
        if not g: continue
        dest = out_dir / f'{it["uri"].split("/")[-1]}.jpg'
        if not dest.exists():
            try:
                im = Image.open(_io.BytesIO(_fetch_image(f"{BASE}/assets/gallery/{g}"))).convert("RGB")
                w = min(INV_W, im.width); h = round(im.height * w / im.width)
                im.resize((w, h), Image.LANCZOS).save(dest, "JPEG", quality=76, optimize=True, progressive=True)
                time.sleep(1)
            except Exception as e:
                print(f"Фото для приглашения не скачалось: {it['uri']} ({e})"); continue
        it["photo_inv"] = f'img/inv/{dest.name}'


def main():
    now = time.time()
    found = {}
    for n, page in enumerate(PAGES):
        if n:
            time.sleep(3)
        for t in trips_from_html(fetch(page)):
            found[t["uri"]] = t

    sheet, sheet_warn, source = load_sheet()
    items, skipped = [], []
    for uri, t in found.items():
        label = clean_tags(t.get("tv.e_preview_tags"))
        nt = (t.get("next_trip") or {}).get("next_time")
        if CLOSED_RE.search(label):
            skipped.append((uri, f"метка: {label}")); continue
        if not nt:
            skipped.append((uri, "нет ближайшей даты")); continue
        if nt < now:
            skipped.append((uri, "дата в прошлом")); continue
        if nt - now > MAX_DAYS * 86400:
            skipped.append((uri, f"дата позже {MAX_DAYS} дней")); continue
        items.append({
            "uri": uri, "title": t["pagetitle"], "price_from": price_num(t.get("price")),
            "duration": t.get("duration"), "duration_sec": t.get("duration_from_seconds"),
            "img": BASE + t["tv.e_preview_img_sm"] if t.get("tv.e_preview_img_sm") else None,
            "next_time": nt, "tags": sheet.get(uri, {}).get("tags", []), "format": sheet.get(uri, {}).get("format", ""),
        })

    if len(items) < MIN_EXPECTED:
        print(f"Сбор подозрителен: активных {len(items)} < {MIN_EXPECTED}. catalog.json не обновлен.")
        return 1

    make_thumbs(items)
    make_invite_photos(items)
    load_meta(items)
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items}
    path = ROOT / "data/catalog.json"
    try:
        same = json.loads(path.read_text(encoding="utf-8")).get("items") == items
    except Exception:
        same = False
    if not same:  # не трогаем файл, если ничего не изменилось: не плодим пустые коммиты
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    active = {i["uri"] for i in items}
    no_tags = [i["uri"] for i in items if not i["tags"]]
    dead_tags = [u for u in sheet if u not in active]
    lines = [f"Активных: {len(items)}, пропущено: {len(skipped)}. Теги из: {source}", ""] + ([w for w in sheet_warn] + [""] if sheet_warn else []) + ["Активные без тегов (не участвуют в подборе):"]
    lines += [f"  {u}" for u in no_tags] or ["  нет"]
    lines += ["", "В таблице есть, среди активных нет:"]
    lines += [f"  {u}" + (f"  ({dict(skipped).get(u)})" if u in dict(skipped) else "  (нет на проверенных страницах)") for u in dead_tags] or ["  нет"]
    report = "\n".join(lines)
    (ROOT / "data/report.txt").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # прежний catalog.json остается как есть
        print(f"Сбор упал: {e}")
        sys.exit(1)
