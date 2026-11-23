"""Mobile PNG views of existing Decimal aggregates, never new financial totals."""
from __future__ import annotations

import base64
from decimal import Decimal
from io import BytesIO
from typing import Any


def report_chart(report: dict[str, Any], *, categories: bool = False) -> dict[str, Any]:
    """Keep exact values beside the picture; partial categories stay explicitly partial."""
    if categories:
        values = dict(sorted(report['known_by_category'].items(), key=lambda p: Decimal(p[1]), reverse=True)[:12])
        title = 'Подтверждённые расходы'
    else:
        flow = report['bank_movements']
        values = {'Поступления': flow['credits'], 'Списания': flow['debits']} if flow else {}
        title = 'Движение банка, включая переводы'
    return _plot(report, values, title)


def monthly_chart(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Refuse incomparable periods rather than plotting partial months as a trend."""
    if not 2 <= len(reports) <= 12:
        raise ValueError('Нужны минимум два сопоставимых месяца')
    first = reports[0]
    if any(not r['coverage_complete'] or not r['full_calendar_month'] or r['expenses'] is None
           or any(r[k] != first[k] for k in ('currency', 'account_scope', 'version')) for r in reports):
        raise ValueError('Нет сопоставимого покрытия/категорий для динамики')
    values = {r['period'][0][:7]: r['expenses'] for r in reports}
    if len(values) != len(reports):
        raise ValueError('Повтор периода')
    scope = {**first, 'period': [reports[0]['period'][0], reports[-1]['period'][1]]}
    return _plot(scope, values, 'Расходы за сопоставимые месяцы')


def _plot(report: dict[str, Any], values: dict[str, str], title: str) -> dict[str, Any]:
    if not values:
        raise ValueError('Нет подтверждённых значений для диаграммы; это не нули')
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(4.8, max(5, len(values) * .55 + 2.5)), dpi=150)
    fig.set_layout_engine('constrained', rect=(0, .12, 1, .88))
    FigureCanvasAgg(fig)
    axes = fig.subplots()
    from jarvis.finance.telegram.shared_review import NAMES
    labels = [NAMES.get(str(k), str(k))[:32] for k in values]
    bars = axes.barh(labels, [float(Decimal(v)) for v in values.values()], color='#3979b8')
    axes.invert_yaxis()
    axes.bar_label(bars, labels=list(values.values()), padding=3, fontsize=11)
    axes.tick_params(labelsize=11)
    axes.margins(x=.35)
    axes.set_xlabel(report['currency'])
    fig.suptitle(title + '\n' + ' — '.join(report['period']), fontsize=13)
    notice = f"Покрыто счетов: {report['covered_accounts']}/{report['known_accounts']}; без категории: {report['unresolved_categories']}"
    fig.text(.5, .015, notice + '\nТолько принятые выписки; полнота неизвестна', ha='center', fontsize=9)
    output = BytesIO()
    fig.savefig(output, format='png')
    return {'photo_base64': base64.b64encode(output.getvalue()).decode('ascii'),
            'caption': title + '\n' + ' — '.join(report['period']) + ' ' + report['currency'] + '\n' + notice,
            'chart_values': values, 'snapshot_version': report['version']}
