---
name: relwork
description: 시드 논문 1~3편에서 출발해 인용 그래프(레퍼런스·피인용·레퍼런스의 레퍼런스)를 따라가며, 로컬 LLM 이 연구 맥락에 맞는 관련연구만 거르고 판정해 관련연구 맵(보고서·네트워크 그래프)을 만든다. Trigger — "관련연구 맵 만들어줘", "이 논문 레퍼런스 분석해줘", "레퍼런스 맵", "relwork 돌려줘", "이 논문 인용 그래프 따라가 봐", "경쟁 논문 찾아줘(인용 기준)". 단순 의미 검색(키워드·임베딩)은 이 도구가 아니라 paper search(:8002)를 쓴다.
---

# relwork — 인용 그래프 기반 관련연구 맵

시드 논문의 **인용 그래프만** 따라 후보를 모으고(의미 검색 없음), 로컬 LLM 이 연구 맥락 기준으로
거름(triage) → 판정(judge, 등급 0~3)한다. 최신 논문 우선이라 중간에 끊어도 최근 것은 남는다.

- 저장소: `~/relwork` (코드가 정본). 실행: `R=~/relwork/.venv/bin/relwork` — 어느 cwd 에서든 동작.
- 런 디렉터리: `~/relwork/runs/<run>/` (graph.sqlite·report.md·graph.html …)
- 설정: `~/relwork/.env` (LLM 엔드포인트·키, S2 키 `RELWORK_S2_API_KEY`(있으면), puppeteer 경로). 값 출력 금지.
- LLM = `.env` 의 OpenAI 호환 엔드포인트. 판단은 전부 그 모델이 하고, Claude 는 운영·해석만.

## 입력

| 항목 | 필수 | 누락 시 |
|---|---|---|
| 시드 논문 1~3편 (arXiv id·URL·제목) | ✅ | 물어본다 |
| 연구 맥락 (목적·아이디어·이미 찾은 논문·관심 낮은 영역) | 권장 | 대화/스레드에서 채워 사용자 확인. |
| 홉 수 | 선택 | 기본 3, 사용자 요청 시 최대 5 |
| run 이름 | 선택 | 시드 짧은 이름(kebab-case) |

## STEP

1. **런 생성** — `$R new <run> --seed <id> [--seed <id> ...]` → `run.yaml` + `context.md` 템플릿.
2. **맥락 작성** — `runs/<run>/context.md` 를 자유 형식으로 채운다. 이미 찾은 논문은 **arXiv id 를 함께** 적어야
   보고서의 회수율(recall) 표가 계산된다. 사용자에게 요약 보여주고 확인.
3. **시드 읽기** — `$R seed <run>` → `seeds.yaml` (프로파일·참고문헌). 수 분.
4. **관점(facet) 생성** — `$R facets <run>` → `facets.yaml` (F1.. 이름·우선순위 high/medium/low).
   **사람 게이트**: 관점 목록과 우선순위를 사용자에게 보여주고 고칠 것 받는다(low 는 확장 안 함).
5. **크롤** — 백그라운드로 실행하고 Monitor 로 종료를 기다린다(sleep 폴링 금지):
   `$R crawl <run> --depth N > ~/relwork/runs/<run>/crawl.log 2>&1` (출처 기본 S2, 첫 노드 전에 S2 가 약 4분 계속 실패하면 키가 있을 때 OpenAlex 로 자동 전환, `--source` 를 주면 고정. `--source openalex` 는 S2 가 오래 막힐 때 — 커버리지 약함, 키 `RELWORK_OPENALEX_API_KEY`)가 좌우).
   라운드마다 `report.md` 가 덮어써지므로 중간 결과 공유 가능. 한 번에 6라운드까지만 돌므로, 마지막 라운드에 새 노드가 많이 남았으면 같은 명령을 다시 실행해 이어간다.
   pgrep 로 볼 땐 `pgrep -f "[.]venv/bin/relwork crawl"` (자기 쉘 매칭 방지).
6. **보강** — `$R enrich <run>` (학회·연도: 로컬 논문 DB(선택) → S2 venue → arXiv / 기관: PDF 첫 2쪽 → 로컬 LLM).
7. **내보내기** — `$R export <run>` → `nodes.csv`·`edges.csv`(from 이 to 를 인용)·`graph.html`(네트워크/계보 토글)·
   `graph.png`(네트워크)·`graph_tree.png`(계보).
8. **보고** — `report.md` 핵심(직접 경쟁 등급 3, 허브=맵 안 피인용 상위, 회수율, 놓친 논문과 이유)을 개조식으로 요약하고
   `graph.png`·`graph.html` 첨부.

재실행: 같은 run 에서 `crawl` 을 다시 부르면 캐시(s2.sqlite·graph.sqlite)를 이어 쓴다. 관점을 크게 바꿨으면
기존 `graph.sqlite` 를 다른 이름으로 옮기고 새로 돌린다(삭제 전 사용자 확인).

## 동작 요약 (해석용)

- 확장 대상 = 등급≥2 노드만, low 우선순위 관점은 확장 안 함, 정렬 = 관점 우선순위 → 등급 → 최신.
- 노드 확장 시 레퍼런스 전부 + 피인용 최대 1000편을 후보로. depth N, 등급 3 은 N+1 까지.
- 한계: 거대 허브(GPT-4·CoT 등)로만 이어진 논문, 거름에서 none 으로 빠진 중간 노드 뒤의 논문은 못 찾는다.

## HARD RULES

- 후보는 **인용 그래프에서만**. 키워드/임베딩 검색으로 후보 추가 금지(사용자 결정).
- 로컬 논문 DB(`RELWORK_CORPUS_DB`, 선택)는 학회·연도 조회용 읽기 전용.
- S2 API 키 신청·외부 폼 제출은 사용자가 직접 한다. 키·이메일을 외부로 보내지 않는다.
