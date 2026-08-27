from __future__ import annotations

import html
import re

_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_PROMPT_INJECTION = re.compile(
    r"ignore\s+(all\s+)?previous|system\s+prompt|developer\s+message|忽略.{0,8}(规则|指令)|"
    r"读取.{0,20}(auth\.json|token|密钥)|执行.{0,12}(命令|shell)",
    re.IGNORECASE,
)


def sanitize_external_text(
    value: str,
    *,
    maximum_length: int = 1_200,
) -> tuple[str, bool]:
    """Normalize untrusted display/prompt text and flag instruction-like content."""

    without_active_content = _SCRIPT_OR_STYLE.sub(" ", value)
    without_tags = _TAGS.sub(" ", without_active_content)
    normalized = _WHITESPACE.sub(" ", html.unescape(without_tags)).strip()
    return normalized[:maximum_length], bool(_PROMPT_INJECTION.search(normalized))
