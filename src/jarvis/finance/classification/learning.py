"""Derived human-label provenance and bounded local review; no financial apply."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any, Literal

from jarvis.finance.classification.evaluation import (
    Example,
    confirmed_examples,
    evaluate,
)
from jarvis.finance.classification.layers import l5_merchant_kb
from jarvis.finance.classification.suggestions import rules_revision
from jarvis.finance.event_taxonomy import (
    NON_ECONOMIC_CATEGORIES,
    TAXONOMY_VERSION,
    UNRESOLVED_CATEGORIES,
)
from jarvis.finance.personal_cfo import PersonalCFO, money
from jarvis.finance.personal_product import marketplace

NEIGHBOR_CUTOFF = 0.90

_CYRILLIC = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
    ['a','b','v','g','d','e','e','zh','z','i','i','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','sch','','y','','e','yu','ya'], strict=True))


def normalized(text: str) -> str:
    """Fold local spelling variations; a folded name alone is never proof."""
    text = unicodedata.normalize('NFKC', text).casefold()
    text = ''.join(_CYRILLIC.get(c, c) for c in text)
    text = re.sub(r'\b(?:moscow|moskva|msq|rus|russia|ru|ooo|llc|ltd|inc|jsc|pjsc|pao|ao|ip|corp|term|pos|tst|rrn|auth|txn)\b', ' ', text)
    text = re.sub(r'\d{4,}', ' ', text)
    return ' '.join(re.findall(r'[a-z0-9]+', text))[:300]


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()


def provenance(cfo: PersonalCFO) -> dict[str, Any]:
    """Derive current and withdrawn examples from the original append-only journal."""
    revision=rules_revision()
    with cfo.db.connect() as conn:
        rows=conn.execute('''SELECT c.*,t.merchant,t.description,t.amount,t.currency,t.tx_date,t.category
            FROM category_corrections c JOIN transactions t ON t.id=c.transaction_id
            WHERE c.source='manual' ORDER BY c.transaction_id,c.version''').fetchall()
        annotation={}
        for event in conn.execute("SELECT payload FROM audit_log WHERE event_type='category_updated'"):
            payload=json.loads(event[0])
            if payload.get('source')=='manual' and payload.get('taxonomy_version'):
                annotation[(payload.get('tx_id'),payload.get('version'))]=payload['taxonomy_version']
        latest={r[0]:r[1] for r in conn.execute('SELECT transaction_id,MAX(version) FROM category_corrections GROUP BY transaction_id')}
    examples=[]
    for raw in rows:
        r=dict(raw);current=latest.get(r['transaction_id'])==r['version'] and r['category']==r['new_category']
        eligible=current and not marketplace(r['merchant']) and r['new_category'] not in NON_ECONOMIC_CATEGORIES|{'pending_review','unknown','uncategorized'}
        examples.append({'example_id':fingerprint([r['transaction_id'],r['version']]),
            'transaction_id':r['transaction_id'],'correction_version':r['version'],'label_source':'owner_manual',
            'confirmed_at':r['created_at'],'category':r['new_category'],'status':'confirmed' if current else 'withdrawn',
            'taxonomy_version':TAXONOMY_VERSION,'original_label_taxonomy_version':annotation.get((r['transaction_id'],r['version'])),
            'taxonomy_note':'Legacy correction journal does not record original taxonomy; normalized at snapshot time',
            'rules_revision':revision,'used_in_training':False,'training_usage_scope':'production_only; candidate usage is in evaluation manifest','split':'unassigned','eligible':eligible,
            'merchant':r['merchant'],'text':r['description'],'day':r['tx_date'],'currency':r['currency'],
            'direction':'debit' if money(r['amount'])<0 else 'credit'})
    return {'version':'owner-learning-v1','revision':fingerprint(examples),'examples':examples,
        'confirmed':sum(e['status']=='confirmed' for e in examples),'withdrawn':sum(e['status']=='withdrawn' for e in examples),
        'eligible':sum(e['eligible'] for e in examples),'production_model_changed':False,
        'observed_at':datetime.now(UTC).isoformat()}


def neighbor_proposal(cfo: PersonalCFO, tx_id: int) -> dict[str, Any] | None:
    """Suggest from current owner examples; contradictions abstain, never apply."""
    row=next((r for r in cfo.db.all_transactions() if r['id']==tx_id),None)
    if not row or marketplace(row['merchant']) or row['category'] in NON_ECONOMIC_CATEGORIES:
        return None
    dataset=provenance(cfo)
    if any(e['transaction_id']==tx_id and e['status']=='confirmed' for e in dataset['examples']):
        return None
    direction='debit' if money(row['amount'])<0 else 'credit'
    pool=[e for e in dataset['examples'] if e['eligible'] and e['currency']==row['currency'] and e['direction']==direction][-2000:]
    if not pool:
        return None
    from rapidfuzz import __version__ as rapidfuzz_version
    from rapidfuzz import fuzz, process
    texts=[normalized(e['merchant']+' '+e['text']) for e in pool]
    query=normalized(row['merchant']+' '+row['description'])
    if not query or not any(texts):
        return None
    matches=process.extract(query,texts,scorer=fuzz.ratio,score_cutoff=NEIGHBOR_CUTOFF*100,limit=None)
    if not matches:
        return None
    top=max(score for _,score,_ in matches)/100
    indices=[index for _,score,index in matches if score/100>=max(NEIGHBOR_CUTOFF,top-.05)]
    labels={pool[i]['category'] for i in indices};category=next(iter(labels)) if len(labels)==1 else 'unknown'
    return {'status':'review','paid_cost_rub':'0','result':{'category':category,'reason':'confirmed_example' if len(labels)==1 else 'contradiction',
        'reason_text':'Локальное сходство с актуальными подтверждениями; каждую новую операцию проверьте отдельно.' if len(labels)==1 else 'Похожие подтверждения противоречат друг другу. Назначение нужно уточнить.',
        'review':True,'human_confirmed':False,'confidence_calibrated':False,'similarity':round(top,3),
        'method':f'RapidFuzz ratio {rapidfuzz_version}; normalized text; cutoff{NEIGHBOR_CUTOFF:.2f}; review only',
        'source_revision':dataset['revision'],'references':[{'transaction_id':pool[i]['transaction_id'],'version':pool[i]['correction_version']} for i in indices[:8]],
        'suggestion_origin':'local_confirmed_neighbor','financial_apply':False}}


def clusters(cfo: PersonalCFO, *, limit: int = 20) -> dict[str, Any]:
    """Group lexical hypotheses locally; never merge money or claim duplicate proof."""
    if not 1<=limit<=25:
        raise ValueError('Cluster limit must be within1..25')
    rows=[r for r in cfo.db.all_transactions() if r['category'] not in NON_ECONOMIC_CATEGORIES and not marketplace(r['merchant'])][-500:]
    groups: dict[tuple[str,str,str],list[dict[str,Any]]] = defaultdict(list)
    for row in rows:
        key=(normalized(row['merchant']),row['currency'],'debit' if money(row['amount'])<0 else 'credit')
        if len(key[0])>=3:
            groups[key].append(row)
    dataset=provenance(cfo);labels={e['transaction_id']:e['category'] for e in dataset['examples'] if e['status']=='confirmed'}
    result=[]
    for key,items in sorted(groups.items(),key=lambda pair:len(pair[1]),reverse=True):
        if len(items)<2:continue
        categories={labels[r['id']] for r in items if r['id'] in labels}
        result.append({'id':fingerprint(key)[:16],'size':len(items),'common_features':{'name_fold':key[0],'currency':key[1],'direction':key[2]},
            'examples':[{'id':r['id'],'merchant':r['merchant'],'category':r['category']} for r in items[:8]],
            'suggested_category':next(iter(categories)) if len(categories)==1 else None,'contradictions':len(categories)>1,
            'needs_review':True,'financial_apply':False,'duplicate_proof':False})
        if len(result)>=limit:break
    return {'clusters':result,'scope':'last500 eligible ledger rows; lexical hypotheses, no merge','source_revision':dataset['revision']}


def prepare_history(cfo: PersonalCFO) -> dict[str, Any]:
    """Persist local review proposals for history; never call AI or change money/labels."""
    dataset = provenance(cfo)
    revision = rules_revision()
    confirmed = {e['transaction_id'] for e in dataset['examples'] if e['status'] == 'confirmed'}
    rows = cfo.economic_rows(cfo.db.all_transactions())
    counts = {'prepared': 0, 'needs_review': 0, 'human_confirmed': 0, 'preserved': 0}
    for row in rows:
        if row['id'] in confirmed:
            counts['human_confirmed'] += 1
            continue
        if row['category'] in NON_ECONOMIC_CATEGORIES:
            continue
        key = 'cfo.ai_proposal:'+str(row['id'])
        previous = cfo.db.get_setting(key, {})
        if previous and previous.get('result', {}).get('suggestion_origin') not in {'local_history_rules', 'local_confirmed_neighbor'}:
            counts['preserved'] += 1  # Keep paid/UNKNOWN receipts; never authorize replay.
            continue
        proposal = neighbor_proposal(cfo, row['id']) if dataset['eligible'] else None
        if proposal is None:
            category = cfo.category(row['merchant'], row['description'], money(row['amount']), row['currency'])
            if category in {'pending_review', 'uncategorized'}:
                category = 'unknown'
            proposal = {'status':'review','paid_cost_rub':'0','result':{'category':category,
                'suggestion_origin':'local_history_rules','method':'existing owner/merchant rules',
                'reason_text':'Локальные правила; предложение не является подтверждением владельца.' if category != 'unknown' else 'Надёжного локального правила нет. Нужно уточнение владельца.',
                'source_revision':dataset['revision'],'rules_revision':revision,
                'human_confirmed':False,'confidence_calibrated':False,'review':True,'financial_apply':False}}
        proposal['prepared_at'] = datetime.now(UTC).isoformat()
        proposal['input_sha256'] = fingerprint([row[k] for k in ('merchant','description','amount','currency')])
        cfo.db.set_setting(key, proposal)
        counts['prepared'] += 1
        counts['needs_review'] += proposal['result']['category'] == 'unknown'
    return {'status':'review','total':len(rows),**counts,'paid_cost_rub':'0','financial_apply':False,
        'source_revision':dataset['revision'],'scope':'all local history; bank transfers excluded; no training'}


def review_group_id(row: dict[str, Any]) -> str:
    """Group display names, never financial identities or marketplace contents."""
    merchant=' '.join(str(row['merchant'] or '').casefold().split()) or 'unnamed:'+str(row['id'])
    return fingerprint([merchant,row['currency'],'debit' if money(row['amount'])<0 else 'credit'])


def proposal_groups(cfo: PersonalCFO) -> dict[str, Any]:
    """Read every saved proposal and current correction history without applying it."""
    with cfo.db.connect() as conn:
        proposals={int(r[0].split(':')[-1]):json.loads(r[1]) for r in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'cfo.ai_proposal:%'")}
        history: dict[int,list[dict[str,Any]]]={}
        for r in conn.execute('SELECT * FROM category_corrections ORDER BY transaction_id,version DESC'):
            history.setdefault(r['transaction_id'],[])
            if len(history[r['transaction_id']])<5:
                history[r['transaction_id']].append(dict(r))
    groups: dict[str,dict[str,Any]]={}
    for row in cfo.economic_rows(cfo.db.all_transactions()):
        saved=proposals.get(row['id'],{});suggestion=saved.get('result',{})
        events=history.get(row['id'],[]);latest=events[0] if events else {}
        if not suggestion or row['category'] in NON_ECONOMIC_CATEGORIES or latest.get('source')=='manual':
            continue
        ident=review_group_id(row)
        group=groups.setdefault(ident,{'id':ident,'merchant':row['merchant'] or 'Мерчант не указан',
            'currency':row['currency'],'direction':'расход/движение' if money(row['amount'])<0 else 'поступление/движение',
            'marketplace':marketplace(row['merchant']),'items':[],'unknown_count':0})
        unknown=suggestion.get('category') in {None,'unknown','pending_review','uncategorized'}
        group['unknown_count']+=int(unknown)
        digest=fingerprint([row[k] for k in ('merchant','description','amount','currency')])
        group['items'].append({**{k:row[k] for k in ('id','merchant','tx_date','amount','currency','description','category')},
            'expected_version':latest.get('version',0),'input_sha256':digest,
            'proposed_category':suggestion.get('category','unknown'),'needs_review':unknown,
            'explanation':suggestion.get('reason_text','Назначение требует уточнения владельца.'),
            'input_changed':bool(saved.get('input_sha256') and saved['input_sha256']!=digest),
            'history':events})
    for group in groups.values():
        items=group['items']
        proposed={item['proposed_category'] for item in items}
        amounts=sorted(abs(money(item['amount'])) for item in items)
        typical=amounts[len(amounts)//2] if amounts else money(0)
        reasons=[]
        if len(proposed)>1:
            reasons.append('conflict')
        if all(item['expected_version']==0 for item in items):
            reasons.append('new_merchant')
        if len(items)>=5:
            reasons.append('many_similar')
        if typical>=1000:
            reasons.append('large_amount')
        if group['unknown_count']:
            reasons.append('unknown')
        representative=min(items,key=lambda item:(abs(abs(money(item['amount']))-typical),item['id']))
        group['priority_score']=round(group['unknown_count']*2+len(proposed)*3+min(len(items),20)*.5
            +(2 if 'new_merchant' in reasons else 0)+(2 if typical>=5000 else 0),2)
        group['representative_id']=representative['id']
        group['reasons']=reasons
    ordered=sorted(groups.values(),key=lambda g:(-g.get('priority_score',0),-g['unknown_count'],-len(g['items']),g['id']))
    return {'stored_count':len(proposals),'pending_count':sum(len(g['items']) for g in ordered),
        'unknown_count':sum(g['unknown_count'] for g in ordered),'groups':ordered,
        'financial_apply':False,'notice':'Группа по названию не доказывает одинаковое назначение. Отмечайте только проверенные строки; маркетплейсы проверяйте по составу покупки.'}


MODEL_QUEUE_MIN_TRAIN = 20


def model_queue(cfo: PersonalCFO, *, limit: int = 10) -> dict[str, Any]:
    """Model-based uncertainty queue; cold start falls back to heuristic groups."""
    if not 1<=limit<=25:
        raise ValueError('Queue limit must be within1..25')
    dataset=provenance(cfo);eligible={e['transaction_id'] for e in dataset['examples'] if e['eligible']}
    examples=[Example(e.transaction_id,e.day,normalized(e.merchant),normalized(e.text),e.label)
        for e in confirmed_examples(cfo.db.path) if e.transaction_id in eligible]
    classes=sorted({e.label for e in examples})
    if len(examples)<MODEL_QUEUE_MIN_TRAIN or len(classes)<2:
        return {'status':'cold_start','n_train':len(examples),'classes':classes,
            'strategy':'heuristic_priority','notice':f'Нужно минимум {MODEL_QUEUE_MIN_TRAIN} подтверждённых примеров и 2 класса; используется эвристический приоритет.'}
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    vectorizer=TfidfVectorizer(analyzer='word',ngram_range=(1,2),max_features=4000)
    matrix=vectorizer.fit_transform([example.text for example in examples])
    model=LogisticRegression(max_iter=1000,class_weight='balanced',random_state=42)
    model.fit(matrix,[example.label for example in examples])
    confirmed={example.transaction_id for example in examples}
    candidates=[row for row in cfo.economic_rows(cfo.db.all_transactions())
        if row['category'] in UNRESOLVED_CATEGORIES and row['id'] not in confirmed]
    if not candidates:
        return {'status':'no_candidates','n_train':len(examples),'classes':classes,'items':[],
            'notice':'Все известные операции уже имеют подтверждённую категорию.'}
    probabilities=model.predict_proba(vectorizer.transform([
        normalized(str(row['merchant'] or '')+' '+str(row['description'] or '')) for row in candidates]))
    model_classes=[str(value) for value in model.classes_]
    scored=[]
    for row,probability in zip(candidates,probabilities,strict=True):
        order=sorted(range(len(probability)),key=lambda index:float(probability[index]),reverse=True)
        best=float(probability[order[0]]);second=float(probability[order[1]]) if len(order)>1 else 0.0
        scored.append({'transaction_id':row['id'],'merchant':str(row['merchant'] or ''),
            'margin':round(best-second,4),'uncertainty':round(1-(best-second),4),
            'predicted':model_classes[order[0]],'second':model_classes[order[1]] if len(order)>1 else None})
    scored.sort(key=lambda item:(item['margin'],item['merchant'],item['transaction_id']))
    selected=[];seen:set[str]=set()
    for item in scored:
        key=item['merchant'].casefold().strip() or str(item['transaction_id'])
        if key in seen:
            continue
        seen.add(key);selected.append(item)
        if len(selected)>=limit:
            break
    return {'status':'model_based','n_train':len(examples),'n_candidates':len(candidates),
        'classes':classes,'items':selected,
        'notice':'Неуверенные операции по TF-IDF+LogisticRegression; предложение не является human_confirmed.'}


def onboarding_queue(cfo: PersonalCFO, *, limit: int = 8, cutoff: date | None = None) -> dict[str, Any]:
    """Pick one representative group per proposed category, then fill by priority."""
    if not 1<=limit<=20:
        raise ValueError('Onboarding limit must be within1..20')
    from jarvis.finance.event_taxonomy import CATEGORY_TITLES
    groups=proposal_groups(cfo)['groups'];picks=[];seen:set[str]=set()
    for group in groups:
        categories={item['proposed_category'] for item in group['items']}-{'unknown'}
        category=next(iter(categories)) if len(categories)==1 else 'unknown'
        if category not in CATEGORY_TITLES and category!='unknown':
            continue  # do not onboard obsolete or non-canonical codes
        if category in seen:
            continue
        seen.add(category)
        items=group['items']
        old_items=[item for item in items if cutoff is not None and item['tx_date'] < str(cutoff)]
        amounts=sorted(abs(money(item['amount'])) for item in (old_items or items))
        typical=amounts[len(amounts)//2] if amounts else money(0)
        representative=min(old_items or items,key=lambda item:(abs(abs(money(item['amount']))-typical),item['id']))
        picks.append({'group_id':group['id'],'merchant':group['merchant'],'category':category,
            'representative_id':representative['id'],'representative_date':representative['tx_date'],
            'train_period_available':bool(old_items),'items':len(items),
            'unknown_count':group['unknown_count'],'priority_score':group['priority_score'],
            'reasons':group['reasons']})
        if len(picks)>=limit:
            break
    if not picks:
        return {'status':'empty','groups':[],'notice':'Нет сохранённых предложений; сначала подготовьте их локально.'}
    return {'status':'diversity' if len(picks)>1 else 'cold_start','groups':picks,
        'covered_categories':sorted({pick['category'] for pick in picks if pick['category']!='unknown'}),
        'notice':'По одной группе на категорию; подтверждайте только проверенные строки.'}


def train_and_apply_online_model(db: Any) -> dict[str, Any]:
    """Train on every current human label (no date cutoff) and refresh local proposals."""
    import math

    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion, Pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler

    cfo = db if isinstance(db, PersonalCFO) else PersonalCFO(db)
    examples = confirmed_examples(cfo.db.path)
    if len(examples) < 2 or len({row.label for row in examples}) < 2:
        return {'status': 'not_ready', 'labels': len(examples),
            'reason': 'Нужны минимум два подтверждённых класса; предложения не меняются.'}

    def text_rows(rows: list[dict[str, Any]]) -> list[str]:
        return [row['text'] for row in rows]

    def amount_rows(rows: list[dict[str, Any]]) -> list[list[float]]:
        return [[math.copysign(math.log1p(abs(row['amount'])), row['amount'])] for row in rows]

    features = FeatureUnion([
        ('char', Pipeline([('pick', FunctionTransformer(text_rows, validate=False)),
            ('tfidf', TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), max_features=4000))])),
        ('word', Pipeline([('pick', FunctionTransformer(text_rows, validate=False)),
            ('tfidf', TfidfVectorizer(analyzer='word', ngram_range=(1, 2), max_features=4000))])),
        ('amount', Pipeline([('pick', FunctionTransformer(amount_rows, validate=False)),
            ('scale', StandardScaler())])),
    ])
    rows_by_id = {row['id']: row for row in cfo.db.all_transactions()}
    training = [{'text': row.text, 'amount': float(money(rows_by_id[row.transaction_id]['amount']))}
        for row in examples if row.transaction_id in rows_by_id]
    model: Any = LogisticRegression(class_weight='balanced', max_iter=1000, random_state=42)
    if len(examples) >= 15:
        try:
            from catboost import CatBoostClassifier  # type: ignore[import-not-found]
            model = CatBoostClassifier(iterations=150, verbose=0, random_seed=42,
                loss_function='MultiClass', thread_count=2)
        except (ImportError, OSError, ValueError):
            pass
    try:
        model.fit(features.fit_transform(training), [row.label for row in examples])
    except (ValueError, TypeError, ArithmeticError):
        return {'status': 'not_ready', 'labels': len(examples), 'reason': 'Модель не смогла обучиться.'}

    confirmed = {row.transaction_id for row in examples}
    candidates = [row for row in cfo.economic_rows(cfo.db.all_transactions())
        if row['category'] in UNRESOLVED_CATEGORIES and row['id'] not in confirmed]
    if not candidates:
        return {'status': 'applied', 'labels': len(examples), 'proposals': 0, 'model': type(model).__name__}
    matrix = features.transform([{'text': normalized(str(row['merchant'] or '')+' '+str(row['description'] or '')),
        'amount': float(money(row['amount']))} for row in candidates])
    probabilities = model.predict_proba(matrix)
    classes = [str(value) for value in model.classes_]
    applied = 0
    for row, probability in zip(candidates, probabilities, strict=True):
        index = max(range(len(probability)), key=lambda item: float(probability[item]))
        category = classes[index]
        digest = fingerprint([row[k] for k in ('merchant', 'description', 'amount', 'currency')])
        cfo.db.set_setting('cfo.ai_proposal:'+str(row['id']), {'status': 'review', 'paid_cost_rub': '0',
            'input_sha256': digest, 'result': {'category': category, 'reason': 'online_model',
            'reason_text': 'Локальная модель на ваших подтверждениях; проверьте перед принятием.',
            'review': True, 'human_confirmed': False, 'confidence_calibrated': False,
            'suggestion_origin': 'online_model', 'financial_apply': False}})
        applied += 1
    return {'status': 'applied', 'labels': len(examples), 'proposals': applied,
        'model': type(model).__name__, 'classes': classes}


def label_plan(cfo: PersonalCFO, cutoff: date) -> dict[str, Any]:
    """Count an explicit pilot cohort; counts do not guarantee model accuracy."""
    from collections import Counter

    from jarvis.finance.classification.evaluation import independent_split
    dataset=provenance(cfo);eligible={e['transaction_id'] for e in dataset['examples'] if e['eligible']}
    examples=[Example(e.transaction_id,e.day,normalized(e.merchant),normalized(e.text),e.label)
        for e in confirmed_examples(cfo.db.path) if e.transaction_id in eligible]
    train,test,_=independent_split(examples,cutoff)
    classes=sorted({e.label for e in examples});a=Counter(e.label for e in train);b=Counter(e.label for e in test)
    needs: list[dict[str, Any]]=[{'category':c,'train':a[c],'test':b[c],'additional_train':max(0,50-a[c]),
        'additional_holdout':max(0,30-b[c])} for c in classes]
    distribution={c:sum(example.label==c for example in examples) for c in classes}
    coverage=round(sum(min(value,50) for value in distribution.values())/(50*len(classes))*100,1) if classes else 0.0
    dates=[example.day for example in examples]
    blockers=[]
    if not train:
        blockers.append('no_train_labels_before_cutoff')
    if not test:
        blockers.append('no_holdout_labels_after_cutoff')
    if train and len({example.label for example in train})<2:
        blockers.append('train_has_fewer_than_two_classes')
    next_action=('Подтвердите операции с датой до '+str(cutoff)) if not train else (
        'Подтвердите операции с датой после '+str(cutoff)) if not test else (
        'Добавьте примеры редких категорий' if any(n['additional_train'] or n['additional_holdout'] for n in needs) else 'Когорта готова к review-оценке')
    return {'cutoff':str(cutoff),'classes':needs,'additional_labels':sum(n['additional_train']+n['additional_holdout'] for n in needs),
        'distribution':distribution,'distinct_categories':len(classes),'coverage_percent':coverage,
        'train_examples':len(train),'test_examples':len(test),
        'earliest_label':min(dates).isoformat() if dates else None,'latest_label':max(dates).isoformat() if dates else None,
        'blockers':blockers,'next_action':next_action,
        'target_per_class':{'train':50,'holdout':30,'distinct_merchants_per_split':5},
        'status':'blocked' if not classes or any(n['additional_train'] or n['additional_holdout'] for n in needs) else 'cohort_counts_met',
        'notice':'Операционный минимум для грубого пилота, не гарантия точности. Нужны уникальные тексты, непересекающиеся мерчанты, независимый будущий holdout; похожие строки одной группы не считаются независимыми. Для новой категории нужно ещё80меток.'}


def evaluation(cfo: PersonalCFO, cutoff: date, *, method: Literal['char','word','linear_svm','logreg'] = 'char') -> dict[str, Any]:
    """Reuse independent time/merchant evaluation; unsupported cohorts stay blocked."""
    dataset=provenance(cfo);eligible={e['transaction_id'] for e in dataset['examples'] if e['eligible']}
    examples=[Example(e.transaction_id,e.day,normalized(e.merchant),normalized(e.text),e.label)
        for e in confirmed_examples(cfo.db.path) if e.transaction_id in eligible]
    def predict(train: list[Example], test: list[Example]) -> list[str | None]:
        from sklearn.feature_extraction.text import TfidfVectorizer
        vectorizer=TfidfVectorizer(analyzer='char_wb' if method=='char' else 'word',ngram_range=(3,5) if method=='char' else (1,2),max_features=4000)
        try:
            matrix=vectorizer.fit_transform([e.text for e in train])
            if method in {'linear_svm','logreg'}:
                if len({e.label for e in train})<2:
                    return [None]*len(test)
                if method=='linear_svm':
                    from sklearn.svm import LinearSVC
                    model: Any=LinearSVC(class_weight='balanced',random_state=42)
                else:
                    from sklearn.linear_model import LogisticRegression
                    model=LogisticRegression(max_iter=1000,class_weight='balanced',random_state=42)
                model.fit(matrix,[e.label for e in train])
                return [str(v) for v in model.predict(vectorizer.transform([e.text for e in test]))]
            scores=(vectorizer.transform([e.text for e in test]) @ matrix.T).toarray()
        except ValueError:
            return [None]*len(test)
        predictions: list[str | None]=[]
        for score in scores:
            best=float(max(score));labels={train[i].label for i,value in enumerate(score) if value>=max(.72,best-.05)}
            predictions.append(next(iter(labels)) if len(labels)==1 else None)
        return predictions
    def rules(text: str) -> str | None:
        match=l5_merchant_kb({'merchant':text,'description_clean':text})
        return match.category if match else None
    report=evaluate(examples,cutoff,predict,rules)
    train_ids=set(report['train_ids']);test_ids=set(report['test_ids'])
    manifest=[{k:e[k] for k in ('example_id','transaction_id','correction_version','label_source','confirmed_at','taxonomy_version','status')}|{'split':'train' if e['transaction_id'] in train_ids else 'test' if e['transaction_id'] in test_ids else 'excluded','used_in_training':e['transaction_id'] in train_ids} for e in dataset['examples']]
    method_text={'linear_svm':'word TF-IDF + balanced LinearSVC; seed42',
        'logreg':'word TF-IDF + balanced LogisticRegression; seed42'}.get(method,method+' TF-IDF nearest confirmed example; threshold.72')
    return {**report,'dataset_revision':dataset['revision'],'method':method_text,'manifest':manifest,
        'calibrated_probability':False,'production_model_changed':False,
        'promotion_allowed':False,'promotion_reason':'Review-only candidate; independent real cohort and audited promotion required',
        'label_counts':{k:dataset[k] for k in ('confirmed','withdrawn','eligible')}}


def create_candidate_version(cfo: PersonalCFO, cutoff: date, *, method: Literal['char','word','linear_svm','logreg'] = 'linear_svm') -> dict[str, Any]:
    """Store review-only candidate metadata; never change the active model here."""
    report = evaluation(cfo, cutoff, method=method)
    version = 'cand-' + hashlib.sha256((report['data_sha256'] + ':' + method + ':'
        + datetime.now(UTC).isoformat()).encode()).hexdigest()[:12]
    entry = {'version': version, 'method': method, 'status': 'candidate',
        'created_at': datetime.now(UTC).isoformat(), 'report_status': report['status'],
        'train_size': report['train_size'], 'test_size': report['test_size'],
        'metrics': report.get('metrics'), 'majority_macro_f1': (report.get('majority_baseline') or {}).get('macro_f1'),
        'dataset_revision': report['dataset_revision'], 'promotion_allowed': False,
        'production_model_changed': False}
    history = cfo.db.get_setting('cfo.model_versions', [])
    history.append(entry)
    cfo.db.set_setting('cfo.model_versions', history[-20:])
    return {'status': 'candidate', 'version': version, 'candidate': entry,
        'promotion_allowed': False, 'reason': 'independent cohort and owner confirmation required'}


def promote_candidate(cfo: PersonalCFO, version: str, *, owner_confirmed: bool = False) -> dict[str, Any]:
    """Promote only an evaluated candidate with an explicit owner decision."""
    if not owner_confirmed:
        return {'status': 'blocked', 'reason': 'owner_confirmation_required'}
    history = cfo.db.get_setting('cfo.model_versions', [])
    candidate = next((item for item in history if item.get('version') == version), None)
    if not candidate:
        return {'status': 'blocked', 'reason': 'candidate_not_found'}
    metrics = candidate.get('metrics') or {}
    majority = candidate.get('majority_macro_f1')
    if (candidate.get('report_status') != 'evaluated' or not metrics
            or candidate['train_size'] < 20 or candidate['test_size'] < 10
            or metrics.get('count', 0) < 10
            or majority is None or metrics.get('macro_f1', 0) < majority + 0.05):
        candidate['status'] = 'blocked'
        cfo.db.set_setting('cfo.model_versions', history)
        return {'status': 'blocked', 'reason': 'insufficient_independent_quality',
            'required': {'train': 20, 'test': 10, 'macro_f1_margin': 0.05}}
    previous = cfo.db.get_setting('cfo.active_model', None)
    candidate['status'] = 'active'
    candidate['promoted_at'] = datetime.now(UTC).isoformat()
    cfo.db.set_setting('cfo.active_model', {'version': version, 'method': candidate['method'],
        'promoted_at': candidate['promoted_at'], 'previous': previous})
    cfo.db.set_setting('cfo.model_versions', history)
    return {'status': 'promoted', 'version': version, 'previous': previous}


def rollback_model(cfo: PersonalCFO, *, owner_confirmed: bool = False) -> dict[str, Any]:
    """Return to the previous active version without deleting any history."""
    if not owner_confirmed:
        return {'status': 'blocked', 'reason': 'owner_confirmation_required'}
    active = cfo.db.get_setting('cfo.active_model', None)
    if not active or not active.get('previous'):
        return {'status': 'blocked', 'reason': 'no_previous_model'}
    previous = active['previous']
    cfo.db.set_setting('cfo.active_model', previous)
    return {'status': 'rolled_back', 'version': previous.get('version')}


def compare_models(cfo: PersonalCFO, cutoff: date) -> dict[str, Any]:
    """Run the same disjoint split for each candidate; never promote or fit on test."""
    methods: tuple[Literal['char','word','linear_svm','logreg'], ...]=('char','word','linear_svm','logreg')
    reports={method:evaluation(cfo,cutoff,method=method) for method in methods}
    evaluated: dict[str,dict[str,Any]]={str(method):report for method,report in reports.items() if report['status']=='evaluated'}
    best: str | None=max(evaluated,key=lambda method:float(evaluated[method]['metrics']['macro_f1'])) if evaluated else None
    return {'cutoff':str(cutoff),'status':'evaluated' if evaluated else 'blocked',
        'methods':{method:{key:report[key] for key in ('status','method','train_size','test_size','exclusions')}
            |({'macro_f1':report['metrics']['macro_f1'],'accuracy':report['metrics']['accuracy'],
                'coverage':report['metrics']['coverage']} if report.get('metrics') else {'reason':report.get('reason')})
            for method,report in reports.items()},
        'best_by_macro_f1':best,'promotion_allowed':False,'production_model_changed':False,
        'notice':'Review-only; одинаковый time+merchant split, test не участвует в fit.'}
