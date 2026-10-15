# concert-watch

개인용 클래식 공연 취소표·예매 오픈 알림 (GitHub Actions 백업 감시).

- `watch.json` — 감시 목록. PC 의 `Sync-CloudWatch.ps1` 이 자동으로 올림 (직접 수정하지 않음)
- `state.json` — 마지막 판매 상태. 워크플로가 상태가 바뀔 때만 커밋
- `watch.py` — 5분마다 공개 판매 정보를 확인해 휴대폰(ntfy)으로 알림
- 알림 토픽은 저장소 Secrets(`NTFY_TOPIC`)에만 있음. 결제·계정 정보는 다루지 않음

로그인·좌석 선택·결제는 자동화하지 않는다.

출처: (재)예술경영지원센터 공연예술통합전산망(www.kopis.or.kr)
