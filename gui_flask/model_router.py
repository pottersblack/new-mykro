"""Model router — routes requests to small or large model based on complexity."""
from __future__ import annotations
import re


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return " ".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return str(content)
    return str(content or "")


def _last_user_message(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _content_to_text(msg.get("content", ""))
    return ""


def estimate_complexity(messages: list[dict], mode: str = "ask") -> str:
    """Decide which model to use: 'large' or 'small'.

    Routing heuristic:
    - Plan mode → always large (complex reasoning)
    - Agent mode tool-call decisions → small (fast routing)
    - Agent mode code generation → large
    - Ask mode short questions → small
    - Ask mode complex questions → large
    """
    # Plan mode always uses large model
    if mode == "plan":
        return "large"

    # Get last user message for analysis
    last_user = ""
    total_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and not last_user:
            last_user = _content_to_text(msg.get("content", ""))
        total_content += _content_to_text(msg.get("content", ""))

    # Complexity signals
    signals_large = 0
    signals_small = 0

    # Long input → large
    if len(last_user) > 2000:
        signals_large += 2
    elif len(last_user) < 200:
        signals_small += 1

    # Code blocks in input → large (likely code generation/review)
    if "```" in last_user:
        signals_large += 2

    # Keywords suggesting complex tasks → large
    complex_keywords = [
        r'\b(refactor|architect|design|implement|debug|optimize|migrate)\b',
        r'\b(multi.?file|across.?files|entire.?codebase)\b',
        r'\b(explain.*detail|analyze|review)\b',
        r'\b(create.*from.*scratch|build|scaffold)\b',
    ]
    for pattern in complex_keywords:
        if re.search(pattern, last_user, re.IGNORECASE):
            signals_large += 2

    # Keywords suggesting simple tasks → small
    simple_keywords = [
        r'\b(what|where|how|which|who|when)\b.*\?$',
        r'\b(yes|no|true|false|rename|typo|fix)\b',
        r'\b(list|show|find|search|grep)\b',
    ]
    for pattern in simple_keywords:
        if re.search(pattern, last_user, re.IGNORECASE):
            signals_small += 1

    # Tool responses in conversation (agent loop iteration) → small for routing
    tool_messages = sum(1 for m in messages if m.get("role") == "tool")
    if tool_messages > 0 and mode == "agent":
        signals_small += 1

    # In agent mode with many iterations, use small for efficiency
    if mode == "agent" and tool_messages >= 4:
        signals_small += 2

    # Decision
    if signals_large > signals_small:
        return "large"
    elif signals_small > signals_large:
        return "small"
    else:
        # Default: large for agent (need quality), small for ask (need speed)
        return "large" if mode == "agent" else "small"


def estimate_loop_depth(messages: list[dict], mode: str = "agent", default_depth: int = 25) -> int:
    """Estimate an adaptive iteration budget for the agent loop.

    Returns a bounded recommendation based on request complexity signals.
    """
    if mode == "ask":
        return 1

    last_user = _last_user_message(messages)
    tool_messages = sum(1 for m in messages if m.get("role") == "tool")

    score = 0

    length = len(last_user)
    if length > 3500:
        score += 4
    elif length > 2000:
        score += 3
    elif length > 900:
        score += 2
    elif length > 250:
        score += 1

    if "```" in last_user:
        score += 2

    complex_keywords = [
        r"\b(refactor|architect|design|implement|debug|optimize|migrate)\b",
        r"\b(multi.?file|across.?files|entire.?codebase|end.?to.?end)\b",
        r"\b(orchestrate|workflow|integration|diagnostic|doctor panel)\b",
        r"\b(create.*from.*scratch|build|scaffold|upgrade)\b",
    ]
    for pattern in complex_keywords:
        if re.search(pattern, last_user, re.IGNORECASE):
            score += 2

    if tool_messages:
        score += min(4, tool_messages // 2)

    if mode == "plan":
        score += 2

    # Map score to depth budget.
    if score <= 1:
        estimated = 6
    elif score <= 3:
        estimated = 9
    elif score <= 6:
        estimated = 13
    elif score <= 9:
        estimated = 18
    else:
        estimated = 25

    baseline = max(5, min(25, int(default_depth)))
    # Keep adaptive behavior bounded near configured baseline.
    lower_bound = max(5, baseline - 8)
    upper_bound = min(25, baseline + 8)
    return max(lower_bound, min(estimated, upper_bound))
