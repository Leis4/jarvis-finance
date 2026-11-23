"""Assemble insights report."""
from datetime import UTC, datetime, timedelta

from jarvis.finance.db import FinanceDB
from jarvis.finance.insights import markov
from jarvis.finance.insights.analyzer import analyze
from jarvis.finance.insights.models import InsightReport

EMOJI = {
    "trend_up": "📈", "trend_down": "📉", "spike": "⚠️",
    "unused_sub": "💤", "over_limit": "🔴",
}


def build_insight_report(db_path: str, period_days: int = 30) -> InsightReport:
    insights = analyze(db_path, period_days)
    # Compute Markov patterns on-the-fly (no caching)
    end_date = datetime.now(UTC).date()
    markov_start = end_date - timedelta(days=180)
    db = FinanceDB(db_path)
    all_recent = db.transactions_between(markov_start, end_date)
    recent_transactions = [
        tx for tx in all_recent
        if float(tx.get("amount", 0)) < 0
        and str(tx.get("category", "")).strip().lower() not in markov.NON_SPEND_CATEGORIES
    ]
    # Build transition matrix and extract insights
    matrix = markov.build_transition_matrix(recent_transactions)
    markov_insights = markov.extract_markov_insights(matrix)
    return InsightReport(
        generated_at=datetime.now(UTC).isoformat(),
        period_days=period_days,
        insights=insights,
        markov_patterns=markov_insights,
    )


def render_insights(report: InsightReport, max_insights: int = 3, format_mode: str = "cli") -> str:
    lines = [f"💡 Insights за {report.period_days} дней"]

    if not report.insights:
        lines.append("")
        lines.append("✅ Никаких аномалий. Траты в норме.")
        return "\n".join(lines)

    lines.append("")
    top = report.top_insights[:max_insights]
    for i in top:
        emoji = EMOJI.get(i.type.value, "ℹ️")
        lines.append(f"{emoji} {i.message}")
        if i.suggestion:
            lines.append(f"   💡 {i.suggestion}")

    if report.markov_patterns:
        lines.append("")
        lines.append("🔗 Паттерны (Markov):")
        for pat in report.markov_patterns:
            lines.append(f"   • {pat}")

    remaining = len(report.insights) - len(top)
    if remaining > 0:
        total_savings = sum(
            i.monthly_cost for i in report.insights
            if i.type.value == "unused_sub"
        )
        lines.append("")
        if format_mode == "telegram":
            lines.append(f"<i>…и ещё {remaining} инсайтов. "
                         f"Вся экономия: ~{total_savings:.0f}₽/мес</i>")
        else:
            lines.append(f"…и ещё {remaining} инсайтов. "
                         f"Вся экономия: ~{total_savings:.0f}₽/мес")

    if report.llm_summary:
        lines.append("")
        lines.append("🧠 Советник:")
        lines.append(report.llm_summary)

    return "\n".join(lines)
