"""Insights module for Spending trends, anomalies and Markov chain patterns."""
from jarvis.finance.insights.analyzer import analyze
from jarvis.finance.insights.llm_advisor import get_advice
from jarvis.finance.insights.models import Insight, InsightReport, InsightType
from jarvis.finance.insights.reporter import build_insight_report, render_insights

__all__ = [
    "Insight",
    "InsightReport",
    "InsightType",
    "analyze",
    "build_insight_report",
    "get_advice",
    "render_insights",
]
