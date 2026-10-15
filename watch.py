"""
클라우드 백업 감시 (GitHub Actions 에서 5분마다 실행).
PC 의 Watch-Tickets.ps1 과 같은 규칙으로 판매 상태를 확인하고, 휴대폰(ntfy)으로만 알린다.
- watch.json : PC 의 Sync-CloudWatch.ps1 이 올리는 감시 목록 (공연명·URL·판별 문구·한도). 결제 정보 없음.
- state.json : 이 스크립트가 기록하는 상태 (마지막 상태·실패 횟수). 변경 시 워크플로가 커밋.
- NTFY_TOPIC : GitHub Secrets 에서 주입.
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "concert-watch/1.0 (personal ticket alert)"


def load(name, default):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8-sig") as f:
        text = f.read().strip()
    return json.loads(text) if text else default


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", errors="replace")


def notify(title, message, url=None, urgent=False):
    topic = os.environ.get("NTFY_TOPIC")
    print(f"[알림] {title} | {message}")
    if not topic:
        return
    body = {"topic": topic, "title": title, "message": message,
            "priority": 5 if urgent else 3, "tags": ["cloud", "musical_note"]}
    if url:
        body["click"] = url
        body["actions"] = [{"action": "view", "label": "예매하기", "url": url}]
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # 알림 실패가 감시 전체를 멈추지 않게
        print(f"ntfy 실패: {e}")


def limit_note(it):
    if not it.get("minPrice"):
        return False, f"가격 미확인 · 한도 {it['limit']:,}원 직접 확인"
    total = it["minPrice"] * it.get("qty", 1)
    return total > it["limit"], f"최저 {it['minPrice']:,}원 x {it.get('qty', 1)}매 = {total:,}원 (한도 {it['limit']:,}원)"


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST) if s else None


def main():
    if os.environ.get("TEST_NOTIFY") == "true":
        notify("[클라우드] 테스트 알림", "GitHub Actions 감시가 휴대폰으로 알림을 보낼 수 있습니다.", "https://github.com/chiwoo72/concert-watch/actions")
        return
    items = load("watch.json", [])
    state = load("state.json", {})
    now = datetime.now(KST)

    for it in items:
        st = state.setdefault(it["id"], {"lastState": None, "failCount": 0, "openNoticeSent": False})
        concert = parse_dt(it.get("concertDate"))
        if concert and now > concert + timedelta(days=1):
            continue
        over, note = limit_note(it)

        # 예매 오픈 알림 (클라우드 실행은 늦어질 수 있어 -15분 ~ +30분 사이 한 번)
        open_at = parse_dt(it.get("openAt"))
        if open_at and not st["openNoticeSent"] and open_at - timedelta(minutes=15) <= now <= open_at + timedelta(minutes=30):
            st["openNoticeSent"] = True
            if over:
                notify("[클라우드] 한도 초과 — 오픈 알림 생략", f"{it['title']} · {note}")
            else:
                notify(f"[클라우드] 곧 예매 오픈 {open_at:%H:%M}", f"{it['title']} · {note}", it["url"], it.get("instant", False))

        if not it.get("availablePattern"):
            continue
        try:
            try:
                page = fetch(it["checkUrl"])
            except Exception:
                time.sleep(5)
                page = fetch(it["checkUrl"])
        except Exception as e:
            st["failCount"] += 1
            print(f"확인 실패({st['failCount']}): {it['title']} — {e}")
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
                head = "취소표 발생!" if st["lastState"] == "soldout" else "예매 가능!"
                notify(f"[클라우드] {head} {it['title']}", note, it["url"], it.get("instant", False))
        if cur != "unknown":
            st["lastState"] = cur
        print(f"{it['id']} {it['title']}: {cur}")

    # 목록에서 빠진 공연의 상태는 정리
    ids = {it["id"] for it in items}
    state = {k: v for k, v in state.items() if k in ids}
    with open(os.path.join(HERE, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
