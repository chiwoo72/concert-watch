"""
클라우드 감시 (GitHub Actions 에서 5분마다 실행). 휴대폰(ntfy)으로만 알린다.

1) 판매 감시 : watch.json(PC 가 올린 수동 감시) + auto.json(자동 발견) 의 공개 잔여석 정보를 확인
2) 자동 발견 : rules.json 기준으로 KOPIS 에서 공연을 찾는다 (KOPIS_KEY 필요)
   - 관심 연주자(artists) × 지역(regions) : 새 공연 알림 + auto.json 에 판매 감시 자동 등록
   - 해외 유명 오케스트라 내한(visit=Y + famousOrchestras) × 서울·부천아트센터·아트센터 인천 : 새 공연 알림만
- state.json : 판매 상태·발견 기록. 변경 시 워크플로가 커밋.
- 휴대폰 방해금지 01:30~06:40(KST): 보류했다가 끝난 뒤 남아 있으면 알림.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
QUIET = ((1, 30), (6, 40))   # 휴대폰 방해금지 시간 (한국시간 01:30 ~ 06:40)
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "concert-watch/1.0 (personal ticket alert)"
KOPIS = "http://www.kopis.or.kr/openApi/restful"
NOL_REMAIN = "https://nol.yanolja.com/ticket/products/api/remaining-seats?goodsCode={}&playSeq={:03d}"

TITLE_SEARCH_MIN = 60      # 관심 연주자 제목 검색 간격(분)
REGION_SCAN_MIN = 360      # 지역 전체 목록 스캔 간격(분) — 출연진·내한 여부 확인용
DETAILS_PER_RUN = 80       # 한 번 실행에 조회할 공연 상세 최대 수 (KOPIS 호출량 제한)


# ── 공통 ──────────────────────────────────────────────
def load(name, default):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8-sig") as f:
        text = f.read().strip()
    return json.loads(text) if text else default


def save(name, obj):
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)


def fetch(url, retry=True):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.read().decode("utf-8", errors="replace")
    except Exception:
        if not retry:
            raise
        time.sleep(5)   # 일시적인 403·타임아웃이 잦아 한 번만 재시도
        return fetch(url, retry=False)


def is_quiet(now=None):
    t = (now or datetime.now(KST)).time()
    if os.environ.get("CONCERT_TEST_KST"):   # 시험용 시각 지정
        t = datetime.strptime(os.environ["CONCERT_TEST_KST"], "%H:%M").time()
    (sh, sm), (eh, em) = QUIET
    return (sh, sm) <= (t.hour, t.minute) < (eh, em)


def app_link(url):
    """인터파크(NOL) 상품이면 NOL 앱을 바로 여는 주소, 아니면 None."""
    m = re.search(r"(?:GoodsCode=|/goods/|/ticket/products/)(\d{6,})", url or "")
    if not m or not re.search(r"interpark|nol\.yanolja", url):
        return None
    web = f"https://nol.yanolja.com/ticket/products/{m.group(1)}"
    return "nol-app://wv?url=" + urllib.parse.quote(web, safe="")


def notify(title, message, url=None, urgent=False):
    """보냈으면 True, 방해금지 시간이라 생략했으면 False."""
    topic = os.environ.get("NTFY_TOPIC")
    if is_quiet():
        print(f"[방해금지 - 생략] {title} | {message}")
        return False
    print(f"[알림] {title} | {message}")
    if not topic:
        return True
    body = {"topic": topic, "title": title, "message": message[:3500],
            "priority": 5 if urgent else 3, "tags": ["cloud", "musical_note"]}
    if url:
        app = app_link(url)
        body["click"] = app or url
        body["actions"] = ([{"action": "view", "label": "앱으로 예매", "url": app}] if app else []) +                           [{"action": "view", "label": "웹으로 열기" if app else "예매하기", "url": url}]
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # 알림 실패가 감시 전체를 멈추지 않게
        print(f"ntfy 실패: {e}")
    return True


def minutes_since(iso, now):
    return (now - datetime.fromisoformat(iso)).total_seconds() / 60 if iso else 1e9


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST) if s else None


def norm(s):
    return re.sub(r"\s+", "", s or "").lower()


# ── KOPIS ─────────────────────────────────────────────
def kopis(path, **params):
    params["service"] = os.environ["KOPIS_KEY"]
    root = ET.fromstring(fetch(f"{KOPIS}/{path}?{urllib.parse.urlencode(params)}"))
    return [{c.tag: (c.text or "").strip() for c in db} | {"_el": db} for db in root.findall("db")]


def kopis_list(days, **extra):
    """오늘부터 days 일 동안의 클래식 공연 목록 (API 는 1회 31일·100건 제한)."""
    out, start = {}, datetime.now(KST).date()
    for k in range(0, days, 31):
        s, e = start + timedelta(days=k), start + timedelta(days=min(k + 30, days))
        page = 1
        while True:
            rows = kopis("pblprfr", stdate=s.strftime("%Y%m%d"), eddate=e.strftime("%Y%m%d"),
                         shcate="CCCA", cpage=page, rows=100, **extra)
            for r in rows:
                if r.get("prfstate") != "공연완료":
                    out[r["mt20id"]] = r
            if len(rows) < 100:
                break
            page += 1
    return out


def kopis_detail(pid):
    rows = kopis(f"pblprfr/{pid}")
    if not rows:
        return None
    d = rows[0]
    d["relates"] = [{"site": (r.findtext("relatenm") or "").strip(), "url": (r.findtext("relateurl") or "").strip()}
                    for r in d["_el"].iter("relate") if (r.findtext("relateurl") or "").strip()]
    return d


def min_price(text):
    prices = [int(m.replace(",", "")) for m in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{4,})\s*원", text or "")]
    prices = [p for p in prices if p > 0]
    return min(prices) if prices else None


# ── 예매처별 판매 감시 만들기 ──────────────────────────
def interpark_code(url):
    m = re.search(r"(?:GoodsCode=|/goods/|/products/)(\d{6,})", url)
    return m.group(1) if m else None


def interpark_play_seqs(code, multi_day):
    """잔여석 API 는 회차(playSeq) 별. 하루 공연은 001, 여러 날이면 001~ 를 차례로 확인."""
    if not multi_day:
        return [1]
    seqs = []
    for n in range(1, 21):
        try:
            body = fetch(NOL_REMAIN.format(code, n))
        except Exception:
            break
        if '"remainSeat":[]' in body.replace(" ", "") and n > 1:
            break
        seqs.append(n)
        time.sleep(1)
    return seqs or [1]


def build_watches(d, artist, rules):
    """공연 상세 → 판매 감시 항목들 + (감시 가능 예매처, 감시 불가 예매처)."""
    region = d.get("area", "")[:2]
    date_from = datetime.strptime(d["prfpdfrom"], "%Y.%m.%d")
    date_to = datetime.strptime(d["prfpdto"], "%Y.%m.%d")
    base = {
        "keyword": artist, "kopisId": d["mt20id"], "source": "auto",
        "concertDate": date_to.strftime("%Y-%m-%d 23:00"), "openAt": None,
        "limit": rules["limit"], "qty": rules["qty"], "minPrice": min_price(d.get("pcseguidance")),
        "instant": rules["instant"],
    }
    label = f"[{region} {date_from:%m/%d}] {d['prfnm']}"
    watches, ok, ng = [], [], []
    for rel in d["relates"]:
        url, site = rel["url"], rel["site"]
        code = interpark_code(url) if "interpark" in url or "nol.yanolja" in url else None
        if code:
            seqs = interpark_play_seqs(code, date_from != date_to)
            for n in seqs:
                watches.append(base | {
                    "id": f"a{d['mt20id'][2:]}i{n}", "title": label + (" (인터파크" + (f" {n}회차)" if len(seqs) > 1 else ")")),
                    "url": f"https://tickets.interpark.com/goods/{code}", "checkUrl": NOL_REMAIN.format(code, n),
                    "availablePattern": '"remainCnt":[1-9]', "soldOutPattern": '"remainSeat":\\['})
            ok.append("인터파크")
        elif "lotteconcerthall.com/product/performance/" in url:
            watches.append(base | {
                "id": f"a{d['mt20id'][2:]}l", "title": label + " (롯데콘서트홀)", "url": url, "checkUrl": url,
                "availablePattern": '"BookableCount":[1-9]', "soldOutPattern": '"IsSoldOut":true'})
            ok.append("롯데콘서트홀")
        else:
            ng.append(site or url)
    return watches, ok, ng


# ── 자동 발견 ─────────────────────────────────────────
def discover(rules, state, auto, now):
    if not os.environ.get("KOPIS_KEY"):
        print("KOPIS_KEY 없음 - 자동 발견 생략")
        return
    disc = state.setdefault("_discovery", {"seen": {}, "pending": [], "known": {}})
    seen, known = disc["seen"], disc["known"]
    regions = tuple(rules["regions"].values())
    artists = rules["artists"]
    famous = [norm(x) for x in rules.get("famousOrchestras", [])]
    orch_areas = tuple(rules.get("orchestraAreas", []))
    orch_venues = [norm(x) for x in rules.get("orchestraVenues", [])]
    first_a = "seededA" not in disc
    first_b = "seededB" not in disc
    found_a, found_b = [], []

    def in_region(d):
        return (d.get("area") or "").startswith(regions)

    def handle(d):
        pid = d["mt20id"]
        seen[pid] = d.get("prfpdto", "")
        if pid in known or not in_region(d):
            return
        text = norm(d.get("prfnm")) + "|" + norm(d.get("prfcast"))
        artist = next((a for a in artists if norm(a) in text), None)
        when = f"{d.get('prfpdfrom')} {d.get('fcltynm')}"
        first_url = d["relates"][0]["url"] if d["relates"] else \
            f"https://www.kopis.or.kr/por/db/pblprfr/pblprfrView.do?menuId=MNU_00020&mt20Id={pid}"
        if artist:
            watches, ok, ng = build_watches(d, artist, rules)
            existing = {w["id"] for w in auto}
            auto.extend(w for w in watches if w["id"] not in existing)
            known[pid] = {"tier": "A", "artist": artist, "end": d.get("prfpdto", "")}
            sites = ("잔여석 감시: " + "·".join(dict.fromkeys(ok))) if ok else "잔여석 자동 감시 불가"
            if ng:
                sites += " / 직접 확인: " + "·".join(dict.fromkeys(ng))
            found_a.append((f"{d['prfnm']} ({when})", sites, first_url))
        elif d.get("visit") == "Y" and any(f in text for f in famous) and (
                (d.get("area") or "").startswith(orch_areas) or any(v in norm(d.get("fcltynm")) for v in orch_venues)):
            known[pid] = {"tier": "B", "end": d.get("prfpdto", "")}
            found_b.append((f"{d['prfnm']} ({when})", first_url))

    # 1) 관심 연주자 제목 검색 (가볍고 빠름)
    if minutes_since(disc.get("lastTitle"), now) >= TITLE_SEARCH_MIN - 1:
        for a in artists:
            for pid, row in kopis_list(rules["days"], shprfnm=a).items():
                if pid not in known and (row.get("area") or "").startswith(regions):
                    d = kopis_detail(pid)
                    if d:
                        handle(d)
        disc["lastTitle"] = now.isoformat()
        disc["seededA"] = True

    # 2) 지역 전체 스캔 → 처음 보는 공연을 대기열에 (출연진에만 이름이 있는 공연·해외 오케스트라 내한 확인용)
    if minutes_since(disc.get("lastScan"), now) >= REGION_SCAN_MIN - 1:
        queue = set(disc["pending"])
        for code in rules["regions"]:
            queue.update(pid for pid in kopis_list(rules["days"], signgucode=code) if pid not in seen)
        disc["pending"] = sorted(queue)
        disc["lastScan"] = now.isoformat()

    # 3) 대기열 상세 조회 (실행당 제한)
    batch, disc["pending"] = disc["pending"][:DETAILS_PER_RUN], disc["pending"][DETAILS_PER_RUN:]
    for pid in batch:
        if pid in seen:
            continue
        try:
            d = kopis_detail(pid)
        except Exception as e:
            print(f"상세 실패 {pid}: {e}")
            disc["pending"].append(pid)
            continue
        if d:
            handle(d)
        else:
            seen[pid] = ""

    # 알림 — 관심 연주자: 처음에는 요약 1건, 이후에는 공연마다
    if found_a:
        if first_a:
            notify(f"관심 연주자 공연 {len(found_a)}건 자동 감시 시작",
                   "\n".join(f"• {t} — {s}" for t, s, _ in found_a), "https://github.com/chiwoo72/concert-watch")
        else:
            # 첫 지역 스캔 중에 찾은 것은 기존 공연(출연진에만 이름이 있던 공연)이므로 '감시 추가'로 표시
            head = "관심 연주자 공연 감시 추가 " if first_b else "새 공연(티켓 오픈) "
            for t, s, u in found_a:
                notify(head + t.split(" (")[0], f"{t}\n{s}", u, rules["instant"] and not first_b)

    # 알림 — 해외 오케스트라 내한: 첫 지역 스캔이 끝날 때까지 모았다가 요약 1건, 이후에는 공연마다
    if first_b:
        disc.setdefault("bufferB", []).extend(t for t, _ in found_b)
        if disc.get("lastScan") and not disc["pending"]:
            disc["seededB"] = True
            if disc["bufferB"]:
                notify(f"예정된 해외 오케스트라 내한 {len(disc['bufferB'])}건",
                       "\n".join("• " + t for t in disc["bufferB"]))
            disc["bufferB"] = []
    else:
        for t, u in found_b:
            notify("해외 오케스트라 내한(티켓 오픈)", t, u)

    # 지난 공연 기록 정리
    today = now.strftime("%Y.%m.%d")
    disc["seen"] = {k: v for k, v in seen.items() if not v or v >= today}
    disc["known"] = {k: v for k, v in known.items() if v.get("end", "9") >= today}


# ── 판매 감시 ─────────────────────────────────────────
def limit_note(it):
    if not it.get("minPrice"):
        return False, f"가격 미확인 · 한도 {it['limit']:,}원 직접 확인"
    total = it["minPrice"] * it.get("qty", 1)
    return total > it["limit"], f"최저 {it['minPrice']:,}원 x {it.get('qty', 1)}매 = {total:,}원 (한도 {it['limit']:,}원)"


def check_sales(items, state, now):
    quiet = is_quiet(now)
    for it in items:
        st = state.setdefault(it["id"], {"lastState": None, "failCount": 0, "openNoticeSent": False})
        st.setdefault("phonePending", False)
        st.setdefault("everAvailable", st.get("lastState") == "available")
        concert = parse_dt(it.get("concertDate"))
        if concert and now > concert + timedelta(days=1):
            continue
        over, note = limit_note(it)

        # 예매 오픈 알림 (클라우드 실행은 늦어질 수 있어 -15분 ~ +30분 사이 한 번)
        open_at = parse_dt(it.get("openAt"))
        if open_at and not st["openNoticeSent"] and open_at - timedelta(minutes=15) <= now <= open_at + timedelta(minutes=30):
            st["openNoticeSent"] = True
            if over:
                notify("[클라우드] 한도 초과 - 오픈 알림 생략", f"{it['title']} · {note}")
            else:
                notify(f"[클라우드] 곧 예매 오픈 {open_at:%H:%M}", f"{it['title']} · {note}", it["url"], it.get("instant", False))

        if not it.get("availablePattern"):
            continue
        try:
            page = fetch(it["checkUrl"])
        except Exception as e:
            st["failCount"] += 1
            print(f"확인 실패({st['failCount']}): {it['title']} - {e}")
            if st["failCount"] == 3:
                notify(f"[클라우드] 확인 실패: {it['title']}", "3회 연속 실패. 해외 서버 접속이 차단됐을 수 있어요 (PC 감시는 별개).", it["url"])
            continue

        if re.search(it["availablePattern"], page):
            cur = "available"
        elif it.get("soldOutPattern") and re.search(it["soldOutPattern"], page):
            cur = "soldout"
        else:
            cur = "unknown"

        if cur == "unknown":
            st["failCount"] += 1
            if st["failCount"] == 3:
                notify(f"[클라우드] 확인 필요: {it['title']}", "판매 상태 문구를 찾지 못했습니다.", it["url"])
        else:
            st["failCount"] = 0

        if cur == "available" and st["lastState"] != "available":
            if over:
                notify("[클라우드] 예매 가능하지만 한도 초과", f"{it['title']} · {note}", it["url"])
            else:
                head = "취소표 발생!" if st["everAvailable"] else "예매 가능(오픈·잔여석)!"
                if not notify(f"[클라우드] {head} {it['title']}", note, it["url"], it.get("instant", False)):
                    st["phonePending"] = True   # 방해금지 시간 → 끝난 뒤 남아 있으면 알림
        # 방해금지 시간에 생긴 취소표가 끝난 뒤에도 남아 있으면 한 번 알림
        if st["phonePending"] and not quiet:
            if cur == "available":
                notify(f"[클라우드] 새벽에 생긴 취소표, 아직 예매 가능 {it['title']}", note, it["url"], it.get("instant", False))
            if cur != "unknown":
                st["phonePending"] = False
        if cur == "soldout":
            st["phonePending"] = False
        if cur == "available":
            st["everAvailable"] = True
        if cur != "unknown":
            st["lastState"] = cur
        print(f"{it['id']} {it['title']}: {cur}")


def main():
    if os.environ.get("TEST_NOTIFY") == "true":
        notify("[클라우드] 테스트 알림", "GitHub Actions 감시가 휴대폰으로 알림을 보낼 수 있습니다.", "https://github.com/chiwoo72/concert-watch/actions")
        return
    now = datetime.now(KST)
    rules = load("rules.json", {})
    state = load("state.json", {})
    manual = load("watch.json", [])
    auto = load("auto.json", [])

    if rules.get("artists"):
        try:
            discover(rules, state, auto, now)
        except Exception as e:  # 발견 실패가 판매 감시를 막지 않게
            print(f"자동 발견 실패: {e}")

    # 지난 공연은 자동 목록에서 제거, 수동 감시와 같은 주소는 중복 확인하지 않음
    auto = [w for w in auto if not parse_dt(w.get("concertDate")) or now <= parse_dt(w["concertDate"]) + timedelta(days=1)]
    manual_urls = {w.get("checkUrl") for w in manual}
    items = manual + [w for w in auto if w.get("checkUrl") not in manual_urls]
    check_sales(items, state, now)

    ids = {it["id"] for it in manual + auto}
    state = {k: v for k, v in state.items() if k in ids or k.startswith("_")}
    save("state.json", state)
    save("auto.json", sorted(auto, key=lambda w: w["id"]))


if __name__ == "__main__":
    main()
