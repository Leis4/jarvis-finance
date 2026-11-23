"""LLM-powered analysis of supervisor report."""

from __future__ import annotations

import json
import logging

from jarvis.core.llm.orchestrator import complete
from jarvis.core.llm.types import TaskType
from jarvis.finance.supervisor.models import Finding, Severity

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — финансовый ассистент. Проанализируй отчёт о здоровье 
системы персональных финансов. Верни JSON:
{
  "summary": "краткая сводка (2-3 предложения)",
  "recommendations": ["конкретное действие 1", "действие 2"],
  "priority": "OK" | "INFO" | "WARNING" | "CRITICAL"
}
Пиши по-русски, кратко, конкретно."""


async def analyze_with_llm(findings: list[Finding]) -> str | None:
    """Send findings to LLM orchestrator, get summary + recommendations.
    Returns None if LLM is unavailable or fails.
    """
    unhealthy = [f for f in findings if f.severity != Severity.OK]
    if not unhealthy:
        return "✅ Все проверки пройдены. Система здорова."

    payload = {
        "findings": [
            {
                "check": f.check_name,
                "severity": f.severity.value,
                "title": f.title,
                "detail": f.detail,
                "suggestion": f.suggestion,
            }
            for f in unhealthy
        ]
    }

    user_msg = json.dumps(payload, ensure_ascii=False, indent=2)
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_msg}"

    try:
        response = await complete(
            task=TaskType.DETECT_ANOMALY,
            prompt=full_prompt,
        )
        if not response or not response.content or not response.content.strip():
            return None

        # Parse response if JSON
        try:
            clean_resp = response.content.strip()
            clean_resp = clean_resp.removeprefix("```json")
            clean_resp = clean_resp.removeprefix("```")
            clean_resp = clean_resp.removesuffix("```")
            clean_resp = clean_resp.strip()

            data = json.loads(clean_resp)
            lines = [str(data.get("summary", "")).strip()]
            recs = data.get("recommendations", [])
            if recs:
                lines.append("")
                lines.append("Рекомендации:")
                for r in recs:
                    lines.append(f"  • {r}")
            return "\n".join(lines).strip()
        except json.JSONDecodeError:
            return response.content[:500].strip()
    except Exception as e:  # noqa: BLE001
        logger.info("LLM analysis unavailable or failed: %s", e)
        return None
