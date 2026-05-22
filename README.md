# ax-trend

GitHub Actions로 open issue를 정규화 제목 기준으로 클러스터링하고, 중복 이슈를 자동으로 닫습니다.

- 수동 실행: workflow dispatch
- 주기 실행: 매일 02:00 UTC
- 기본 정책: 같은 정규화 제목 클러스터에서 본문 품질이 가장 좋은 이슈 1개만 유지
