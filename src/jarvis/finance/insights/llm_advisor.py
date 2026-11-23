"""LLM generates personalized recommendations."""
import json
import logging

from jarvis.core.llm.orchestrator import complete
from jarvis.core.llm.types import TaskType
from jarvis.finance.insights.models import Insight

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — персональный финансовый советник. 
Проанализируй инсайты о тратах пользователя и дай 2-3 конкретных 
рекомендации. Пиши по-русски, кратко, с цифрами. Без воды.

Формат: JSON {"summary": "...", "recommendations": ["...", "..."]}"""


async def get_advice(insights: list[Insight]) -> str | None:
    if not insights:
        return None

    payload = {
        "insights": [
            {"type": i.type.value, "category": i.category,
             "message": i.message, "suggestion": i.suggestion}
            for i in insights[:10]
        ]
    }

    full_prompt = f"{SYSTEM_PROMPT}\n\nData:\n{json.dumps(payload, ensure_ascii=False)}"

    try:
        result = await complete(
            task=TaskType.OTHER,
            prompt=full_prompt,
        )
        response = result.content
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LLM advice failed: {e}")
        return None

    if response:
        try:
            data = json.loads(response)
            lines = [data.get("summary", "")]
            for r in data.get("recommendations", []):
                lines.append(f"  • {r}")
            return "\n".join(lines)
        except json.JSONDecodeError:
            return response[:500]
    return None
