---
title: KIS Trading Agent
emoji: 📈
colorFrom: blue
colorTo: green
sdk: docker
app_port: 8000
pinned: false
short_description: 한국투자증권 Open API 기반 LLM 주식투자 에이전트 (paper 모드, 실시간 시세)
---

# KIS Trading Agent

한국투자증권 Open API를 사용하는 리스크 제어 기반 주식투자 Agent입니다. **국내(KR/원화)와 미국(US/USD) 주식을 모두 지원**합니다. `ANTHROPIC_API_KEY`를 설정하면 Claude(LLM)가 자연어 요청을 이해하고 도구를 호출하는 **대화형 에이전트**로 동작하고, 키가 없으면 키워드 라우터로 자동 폴백합니다. 기본값은 `mock` 모드라 API 키 없이도 종목 분석, 관심종목 스캔, 주문 드라이런까지 실행됩니다.

> 미국 주식 경로(해외 시세/일봉/잔고/주문)는 KIS 해외 API 스펙을 따라 구현했으나, 실전·모의 계좌로의 라이브 호출은 현행 KIS 해외 스펙(TR ID·파라미터·거래소 코드)으로 한번 더 검증하세요. mock 데모는 키 없이 그대로 동작합니다.

## 기능

- **국내·미국 멀티마켓**: 6자리 코드(KR/원) + 알파벳 티커(US/USD), 종목코드 형식으로 시장 자동 판별. 통화별 한도·손익·표기(₩/$) 분리
- **LLM 에이전트 대화**: Claude tool-use 루프로 자연어 요청을 해석해 아래 도구를 자율적으로 호출
  - 노출 도구: 종목 분석 / 관심종목 스캔 / 잔고 조회 / 주문 초안(preview) — 각 도구에 `market`(KR/US) 인자
  - 안전 설계: 에이전트는 주문 '초안'(승인 대기)만 만들 수 있고, **실제 체결은 사람이 화면에서 직접 승인·실행**합니다.
  - **응답 토큰 스트리밍**(SSE 타이핑 효과), **멀티턴 대화 영속화**(세션 ID로 새로고침해도 대화 복원), 회사명→종목코드 매핑
- 국내주식 현재가/일봉 조회 인터페이스
- SMA, RSI, ATR, 거래량 기반 매수 후보 판단 (지표 결정론적)
- 관심종목 랭킹 스캔
- 지정가 주문 티켓과 리스크 체크
- **포지션·손익 추적**: 체결 시 평균단가/수량 갱신, 매도 시 실현손익, 보유 종목 미실현손익(잔고 탭)
- **실제 작동하는 리스크 한도**: 당일 실현손실 한도(`KIS_DAILY_LOSS_LIMIT_KRW`)·단일 종목 비중 한도(`KIS_MAX_POSITION_PCT`) 초과 시 주문 차단
- **리스크 기반 권장 수량**: 손절 폭과 일일 손실 한도로 보수적 기본 수량 산정(주문 티켓 자동 채움)
- 드라이런, 모의투자, 실전투자 환경 분리
- 실주문 잠금(모의·실전 공통), 1회 주문 한도, 허용 종목, 지정가 주문 강제
- 모든 실주문(비-드라이런·비-mock)은 확인 문구 필요, 실행 시 현재가·리스크 재검증
- 승인/포지션 저장(원자적 쓰기 + 동시성 락), 로그 회전, KIS 레이트리밋 스로틀
- 선택적 대시보드 토큰으로 주문/잔고 엔드포인트 보호

## 실행

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn trading_agent.main:app --app-dir src --reload
```

브라우저에서 `http://127.0.0.1:8000`을 엽니다.

외부 제출용 공개 실행:

```bash
./scripts/start_public.sh
```

기본값은 `0.0.0.0:8000`, `KIS_ENV=mock`, `KIS_ALLOW_LIVE_ORDERS=false`입니다.

## KIS 설정

`.env`에서 환경을 선택합니다.

```bash
KIS_ENV=mock   # mock, paper, prod
```

모의투자:

```bash
KIS_ENV=paper
KIS_PAPER_APP_KEY=...
KIS_PAPER_APP_SECRET=...
KIS_PAPER_ACCOUNT_NO=12345678
KIS_ACCOUNT_PRODUCT_CODE=01
```

실전투자:

```bash
KIS_ENV=prod
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=12345678
KIS_ACCOUNT_PRODUCT_CODE=01
KIS_ALLOW_LIVE_ORDERS=false
```

모의·실전 모두 드라이런을 끈 실주문은 `KIS_ALLOW_LIVE_ORDERS=true`가 아니면 차단되고, `KIS_LIVE_CONFIRM_TEXT`와 같은 확인 문구가 필요합니다. 실행 시 현재가와 리스크를 다시 검증합니다.

## LLM 에이전트 설정

`ANTHROPIC_API_KEY`를 설정하면 자연어 대화 모드가 켜집니다.

```bash
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-4-8   # 기본값
LLM_MAX_STEPS=6
```

키가 없으면 키워드 라우터(`잔고`, 6자리 종목코드, `스캔/관심`)로 자동 폴백하므로 mock 데모는 키 없이도 동작합니다. 에이전트는 분석/스캔/잔고/주문 초안 도구만 호출하며, 실제 체결은 사람이 화면에서 승인해야 합니다.

선택적으로 `DASHBOARD_TOKEN`을 설정하면 주문/잔고 엔드포인트가 `X-Dashboard-Token` 헤더를 요구합니다(미설정 시 공개 mock 데모).

## 공식 API 참고

- 한국투자증권 Open API 포털: https://apiportal.koreainvestment.com/
- 공식 샘플 코드: https://github.com/koreainvestment/open-trading-api

이 프로젝트에서 사용하는 핵심 REST API:

- 접근토큰 발급: `/oauth2/tokenP`
- 주식현재가 시세: `/uapi/domestic-stock/v1/quotations/inquire-price`
- 국내주식기간별시세: `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`
- 주식잔고조회: `/uapi/domestic-stock/v1/trading/inquire-balance`
- 주식주문 현금: `/uapi/domestic-stock/v1/trading/order-cash`

## 과제 제출용 문구

에이전트 이름: `KIS Trading Agent`

사용 Tool: `Python, FastAPI, Anthropic Claude (LLM tool-use), 한국투자증권 Open API, REST API, JavaScript`

소개: 한국투자증권 Open API 기반으로 국내주식 시세를 수집하고 기술지표를 계산하는 도구들을 Claude(LLM)가 tool-use 루프로 호출하는 대화형 투자 보조 Agent입니다. 자연어 요청을 이해해 분석·비교·잔고·주문 초안을 처리하며, 주문 전 리스크 한도와 사람 승인 절차를 강제합니다.

도입 효과:

- AS-IS: 종목별 현재가, 차트, 리스크 기준, 주문 가능 여부를 여러 화면에서 따로 확인해야 했습니다.
- TO-BE: 종목 분석, 관심종목 비교, 주문 전 검증, 실행 로그를 한 화면에서 처리해 반복 리서치 시간을 줄이고 실전 주문 실수를 예방합니다.
