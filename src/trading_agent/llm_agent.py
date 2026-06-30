"""LLM-driven conversational agent.

Wraps the deterministic TradingAgent methods as Claude tools and runs a manual
tool-use loop. The model interprets free-text Korean requests, decides which
tools to call, and writes a grounded natural-language answer.

Safety: the model can analyze, screen, read balances, and *draft* orders
(``preview_order`` creates a pending approval), but it cannot execute orders.
Execution stays a human action in the UI, gated by the risk check and the
live-order confirmation flow.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from . import markets
from .config import Settings
from .names import prompt_table

logger = logging.getLogger("trading_agent.llm")

# Models offered in the UI picker (validated server-side).
AVAILABLE_MODELS = [
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 · 균형 (권장)"},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8 · 고성능"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 · 빠름·저렴"},
]
MODEL_IDS = {m["id"] for m in AVAILABLE_MODELS}
# Models that accept adaptive thinking; thinking is skipped for others (e.g. Haiku).
THINKING_MODELS = {"claude-sonnet-4-6", "claude-opus-4-8", "claude-opus-4-7"}


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot be used (no key / package / credential error)."""


SYSTEM_PROMPT = (
    "당신은 한국투자증권 Open API 기반 국내주식 투자 보조 에이전트입니다. "
    "사용자의 자연어 요청을 이해하고, 제공된 도구를 호출해 실제 데이터를 근거로 한국어로 답합니다.\n\n"
    "원칙:\n"
    "- 현재가, 지표, 점수, 판단은 반드시 도구 결과에서 가져오세요. 숫자를 지어내지 마세요.\n"
    "- 여러 종목 비교나 스캔 요청에는 screen_stocks를 사용하세요.\n"
    "- 주문 요청이 오면 preview_order로 '주문 초안(승인 대기)'만 만들 수 있습니다. "
    "실제 체결은 당신이 할 수 없으며, 사용자가 화면에서 리스크 체크 후 직접 실행해야 합니다. "
    "주문 초안을 만든 뒤에는 리스크 차단/경고 사항과 '실행하려면 주문 탭에서 승인하세요'를 안내하세요.\n"
    "- 사소한 선택(예: 분석 종목이 명확할 때)은 되묻지 말고 진행하세요. "
    "종목코드나 수량이 정말 모호할 때만 질문하세요.\n"
    "- 투자 판단 보조일 뿐 수익을 보장하지 않는다는 점을 과하지 않게 유지하세요.\n"
    "- 국내(KR)와 미국(US) 주식을 모두 지원합니다. 국내는 6자리 코드·원화, 미국은 알파벳 티커·USD로 거래합니다. "
    "도구의 market 인자로 시장을 지정할 수 있고, 생략하면 종목코드 형식으로 자동 판별됩니다.\n"
    "- 사용자가 회사 이름으로 말하면 종목코드/티커로 변환해 호출하세요. "
    "국내 예: " + prompt_table() + ". 미국 예: " + markets.prompt_table() + ". "
    "목록에 없으면 알고 있는 코드/티커를 쓰고, 불확실하면 물으세요.\n"
    "- 최종 답변은 간결하게. 통화 단위(원/$)를 정확히 표기하세요. "
    "도구를 호출하지 않을 때는 탐색적 사고 과정을 출력하지 말고 결론만 쓰세요."
)


_MARKET_PROP = {
    "type": "string",
    "enum": ["KR", "US"],
    "description": "시장: 국내=KR, 미국=US. 생략 시 종목코드 형식으로 자동 판별(6자리=KR, 알파벳 티커=US).",
}


_PLACE_ORDER_TOOL = {
    "name": "place_order",
    "description": (
        "지정가 주문을 바로 체결합니다(사람 승인 없이). 자동매매가 켜져 있을 때만 사용 가능. "
        "리스크 한도를 통과해야 하며, mock/paper는 시뮬레이션·가짜돈, 실전(prod)은 실제 체결입니다. "
        "사용자가 '사줘/팔아줘'처럼 즉시 매매를 요청할 때 호출하세요."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "국내 6자리 코드 또는 미국 티커"},
            "side": {"type": "string", "enum": ["buy", "sell"], "description": "매수=buy, 매도=sell"},
            "quantity": {"type": "integer", "description": "주문 수량(주), 1 이상"},
            "limit_price": {"type": "number", "description": "지정가 (국내 원, 미국 USD·소수점 허용)"},
            "market": _MARKET_PROP,
        },
        "required": ["symbol", "side", "quantity", "limit_price"],
        "additionalProperties": False,
    },
}

AUTO_TRADE_NOTE = (
    "\n- [자동매매 ON] 사용자가 매매를 요청하면 preview_order 대신 place_order로 즉시 체결할 수 있습니다 "
    "(리스크 한도 통과 시). 체결 후 결과와 실현손익을 알려주세요."
)


def _tool_defs(auto_trade: bool = False) -> List[Dict[str, Any]]:
    tools = [
        {
            "name": "analyze_stock",
            "description": (
                "한 종목의 현재가, 기술지표(SMA/RSI/ATR/거래량), 점수, 매수/관망/대기 판단, "
                "리스크 플랜(진입/손절/목표/권장수량)을 계산합니다. 국내·미국 주식 모두 지원. "
                "사용자가 특정 종목 분석·전망·매수 여부를 물을 때 호출하세요."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "국내 6자리 코드(예: 삼성전자 005930) 또는 미국 티커(예: AAPL).",
                    },
                    "market": _MARKET_PROP,
                },
                "required": ["symbol"],
                "additionalProperties": False,
            },
        },
        {
            "name": "screen_stocks",
            "description": (
                "여러 종목을 한 번에 분석해 점수 순으로 정렬합니다. 같은 시장 종목끼리 호출하세요. "
                "'관심종목 스캔', '이 중에 제일 나은 종목', 여러 종목 비교 요청에 호출하세요."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "종목코드/티커 목록 (같은 시장).",
                    },
                    "market": _MARKET_PROP,
                },
                "required": ["symbols"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_balance",
            "description": "계좌 잔고와 보유 종목을 조회합니다(시장별). 잔고/예수금/보유 종목/손익을 물을 때 호출하세요.",
            "input_schema": {
                "type": "object",
                "properties": {"market": _MARKET_PROP},
                "additionalProperties": False,
            },
        },
        {
            "name": "preview_order",
            "description": (
                "지정가 주문 '초안'을 만들고 리스크 체크를 실행합니다(승인 대기 상태로 저장). "
                "실제 체결이 아니며, 사용자가 화면에서 직접 실행해야 합니다. "
                "사용자가 매수/매도 주문을 요청할 때 호출하세요."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "국내 6자리 코드 또는 미국 티커"},
                    "side": {"type": "string", "enum": ["buy", "sell"], "description": "매수=buy, 매도=sell"},
                    "quantity": {"type": "integer", "description": "주문 수량(주), 1 이상"},
                    "limit_price": {"type": "number", "description": "지정가 (국내 원, 미국 USD·소수점 허용)"},
                    "market": _MARKET_PROP,
                },
                "required": ["symbol", "side", "quantity", "limit_price"],
                "additionalProperties": False,
            },
        },
    ]
    if auto_trade:
        tools.append(_PLACE_ORDER_TOOL)
    return tools


class LLMAgent:
    def __init__(self, agent: "TradingAgent", settings: Settings):  # noqa: F821
        self.agent = agent
        self.settings = settings
        self._client = None

    @property
    def available(self) -> bool:
        if not self.settings.has_llm:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def _request_kwargs(self, tools, system):
        """Resolve model + thinking once per chat, return a messages->kwargs builder."""
        cfg = self.agent.llm_config()
        model = cfg["model"]
        use_thinking = bool(cfg["thinking"]) and model in THINKING_MODELS

        def build(messages):
            kwargs = {
                "model": model,
                "max_tokens": 4096,
                "system": system,
                "tools": tools,
                "messages": messages,
            }
            if use_thinking:
                kwargs["thinking"] = {"type": "adaptive"}
            return kwargs

        return build

    def _tools_and_system(self):
        auto_trade = self.agent.auto_config()["auto_trade"]
        tools = _tool_defs(auto_trade)
        system = SYSTEM_PROMPT + (AUTO_TRADE_NOTE if auto_trade else "")
        return tools, system

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - guarded by `available`
                raise LLMUnavailable("anthropic 패키지가 설치되어 있지 않습니다.") from exc
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def chat(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        if not self.available:
            raise LLMUnavailable("LLM이 구성되어 있지 않습니다 (ANTHROPIC_API_KEY 없음).")
        client = self._get_client()
        tools, system = self._tools_and_system()
        messages: List[Dict[str, Any]] = []
        for turn in (history or [])[-8:]:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        make_kwargs = self._request_kwargs(tools, system)
        artifacts: List[Dict[str, Any]] = []
        tool_calls: List[str] = []
        response = None
        for _ in range(max(1, self.settings.llm_max_steps)):
            response = client.messages.create(**make_kwargs(messages))
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls.append(block.name)
                result, artifact = self._dispatch(block.name, block.input or {})
                if artifact is not None:
                    artifacts.append(artifact)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False),
                        "is_error": bool(result.get("error")),
                    }
                )
            messages.append({"role": "user", "content": results})

        text = ""
        if response is not None:
            text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            text = "요청을 처리했지만 답변 텍스트가 비어 있습니다."
        return {
            "kind": "agent",
            "message": text,
            "artifacts": artifacts,
            "tool_calls": tool_calls,
        }

    def chat_stream(self, message: str, history: Optional[List[Dict[str, str]]] = None):
        """Generator variant of chat() yielding SSE-friendly events.

        Yields {'type': 'delta', 'text': ...} as tokens arrive, {'type': 'tool',
        'name': ...} when a tool is called, and finally {'type': 'done', ...}.
        """
        if not self.available:
            raise LLMUnavailable("LLM이 구성되어 있지 않습니다 (ANTHROPIC_API_KEY 없음).")
        client = self._get_client()
        tools, system = self._tools_and_system()
        messages: List[Dict[str, Any]] = []
        for turn in (history or [])[-8:]:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        make_kwargs = self._request_kwargs(tools, system)
        artifacts: List[Dict[str, Any]] = []
        tool_calls: List[str] = []
        text_parts: List[str] = []
        for _ in range(max(1, self.settings.llm_max_steps)):
            with client.messages.stream(**make_kwargs(messages)) as stream:
                for text in stream.text_stream:
                    text_parts.append(text)
                    yield {"type": "delta", "text": text}
                response = stream.get_final_message()
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls.append(block.name)
                yield {"type": "tool", "name": block.name}
                result, artifact = self._dispatch(block.name, block.input or {})
                if artifact is not None:
                    artifacts.append(artifact)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False),
                        "is_error": bool(result.get("error")),
                    }
                )
            messages.append({"role": "user", "content": results})

        text = "".join(text_parts).strip() or "요청을 처리했습니다."
        yield {"type": "done", "message": text, "artifacts": artifacts, "tool_calls": tool_calls}

    def _dispatch(self, name: str, args: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Run a tool. Returns (result_for_model, artifact_for_ui)."""
        try:
            market = args.get("market")
            if name == "analyze_stock":
                report = self.agent.analyze(str(args["symbol"]), market)
                return _compact_analysis(report), {"type": "analysis", "data": report}
            if name == "screen_stocks":
                symbols = [str(s) for s in (args.get("symbols") or [])]
                data = self.agent.screen(symbols, market)
                return _compact_screen(data), {"type": "screen", "data": data}
            if name == "get_balance":
                data = self.agent.balance(market)
                return {"balance": data.get("agent_portfolio", data)}, {"type": "balance", "data": data}
            if name == "preview_order":
                # The model can only draft orders; force dry_run so an LLM-created
                # ticket can never become a real broker order without a human.
                preview = self.agent.preview_order(
                    symbol=str(args["symbol"]),
                    side=str(args["side"]),
                    quantity=int(args["quantity"]),
                    limit_price=float(args["limit_price"]),
                    dry_run=True,
                    market=market,
                )
                return _compact_preview(preview), {"type": "order", "data": preview}
            if name == "place_order":
                # Only callable when auto_trade is on (tool isn't offered otherwise).
                result = self.agent.auto_execute(
                    symbol=str(args["symbol"]),
                    side=str(args["side"]),
                    quantity=int(args["quantity"]),
                    limit_price=float(args["limit_price"]),
                    market=market,
                )
                return result, {"type": "executed", "data": result}
            return {"error": f"알 수 없는 도구: {name}"}, None
        except Exception as exc:  # surface tool failures back to the model
            logger.warning("tool %s failed: %s", name, exc)
            return {"error": str(exc)}, None

    def decide_trades(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """LLM-mode autopilot: pick trades from pre-computed candidates.

        candidates: [{symbol, market, action, score, price, held, unrealized_pct}]
        Returns [{symbol, side, market}] to execute. One cheap JSON call, no tools.
        """
        if not self.available or not candidates:
            return []
        import json as _json

        client = self._get_client()
        model = self.agent.llm_config()["model"]
        instruction = (
            "당신은 보수적인 자동매매 엔진입니다. 아래 후보들을 보고 매수/매도할 종목만 고르세요. "
            "규칙: 점수가 높고(action이 매수 후보) 미보유면 매수 후보, 보유 중 손실이 크거나 충분히 오르면 매도 후보. "
            'JSON만 출력: {"trades":[{"symbol":"...","side":"buy|sell","market":"KR|US"}]}. 확신이 없으면 빈 배열.\n\n'
            + _json.dumps(candidates, ensure_ascii=False)
        )
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": instruction}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < 0:
                return []
            data = _json.loads(text[start : end + 1])
            return [t for t in data.get("trades", []) if t.get("symbol") and t.get("side") in {"buy", "sell"}]
        except Exception as exc:  # pragma: no cover - network/parse
            logger.warning("decide_trades failed: %s", exc)
            return []


def _compact_analysis(report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": report["symbol"],
        "name": report["name"],
        "market": report.get("market"),
        "currency": report.get("currency"),
        "price": report["quote"]["price"],
        "change_pct": report["quote"]["change_pct"],
        "score": report["score"],
        "action": report["action"]["text"],
        "metrics": report["metrics"],
        "signals": [{"name": s["name"], "impact": s["impact"]} for s in report["signals"]],
        "risk_plan": report["risk_plan"],
    }


def _compact_screen(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "results": [
            {
                "symbol": r["symbol"],
                "name": r["name"],
                "currency": r.get("currency"),
                "price": r["quote"]["price"],
                "change_pct": r["quote"]["change_pct"],
                "score": r["score"],
                "action": r["action"]["text"],
            }
            for r in data["results"]
        ],
        "errors": data["errors"],
    }


def _compact_preview(preview: Dict[str, Any]) -> Dict[str, Any]:
    approval = preview["approval"]
    check = approval["risk_check"]
    return {
        "approval_id": approval["id"],
        "status": approval["status"],
        "draft": approval["draft"],
        "blocks": check["blocks"],
        "warnings": check["warnings"],
        "note": "체결하려면 사용자가 주문 탭에서 직접 실행해야 합니다.",
    }
