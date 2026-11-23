"""16-layer classifier. Each layer returns LayerResult or None."""
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel


@dataclass
class LayerResult:
    category: str
    confidence: float        # 0.0 - 1.0
    layer: str               # e.g., "L1_hard_rules"
    reasoning: str
    weight: float = 1.0      # for final weighted vote


class ClassificationUnavailable(RuntimeError):
    """Optional classifier infrastructure could not produce evidence."""


class LLMCategorizationResponse(BaseModel):
    category: str
    confidence: float
    reason: str


# ============ L0: Preprocessing ============

def l0_preprocess(desc: str, amount: float, category: str = "") -> dict[str, Any]:
    """Normalize description, extract entities."""
    d = (desc or "").strip()
    d_low = d.lower()

    # Extract bank
    bank = "unknown"
    for b in ["t-bank", "tbank", "сбер", "sbol", "sberbank", "альфа", "alfa",
              "vtb", "втб", "yandex", "yandex bank", "вузов"]:
        if b in d_low:
            bank = b
            break

    # Extract merchant (first 2-3 words after cleaning)
    merchant = d
    for prefix in ["операция по карте", "операция по счету", "операция по счёту",
                   "перевод от", "перевод из", "перевод на", "sbp", "sbol"]:
        merchant = re.sub(prefix, "", merchant, flags=re.IGNORECASE).strip()
    merchant = re.sub(r"\*+\d+", "", merchant)
    merchant = " ".join(merchant.split()[:3]).lower()[:50]

    # Amount signature
    amt_abs = abs(amount)
    is_round = abs(amt_abs - round(amt_abs / 100) * 100) < 1 if amt_abs > 0 else False

    now = datetime.now()  # noqa: DTZ005
    return {
        "description_raw": d,
        "description_clean": d_low,
        "bank": bank,
        "merchant": merchant,
        "amount": amount,
        "amount_abs": amt_abs,
        "is_round": is_round,
        "tx_category": category or "",
        "day_of_month": now.day,
        "weekday": now.weekday(),  # 0 = Mon, 6 = Sun
        "is_weekend": now.weekday() >= 5,
    }


# ============ L1: Hard rules ============

def l1_hard_rules(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Parents/stipend/salary by hardcoded bank+amount rules, keywords or name patterns."""
    desc_clean = ctx.get("description_clean", "")
    amount = ctx["amount_abs"]
    bank = ctx["bank"]
    tx_cat = ctx.get("tx_category", "")

    # Check tx category (e.g. stipend, salary)
    if tx_cat in ("salary", "stipend"):
        return LayerResult(
            category=tx_cat,
            confidence=1.0,
            layer="L1_hard_rules",
            reasoning=f"Category is {tx_cat}",
            weight=1.0,
        )

    conn = sqlite3.connect(db_path)
    # Check name patterns first (e.g., николай николаевич, ольга николаевна)
    try:
        name_rows = conn.execute("""
            SELECT name_pattern, transfer_type, typical_category, sender_label
            FROM known_senders
            WHERE name_pattern IS NOT NULL AND name_pattern != ''
        """).fetchall()
    except Exception:  # noqa: BLE001
        name_rows = []

    for name_pat, ttype, cat, label in name_rows:
        if name_pat and name_pat.lower() in desc_clean:
            conn.close()
            return LayerResult(
                category=ttype,
                confidence=0.98,
                layer="L1_hard_rules",
                reasoning=f"name_pattern={name_pat}, label={label}",
                weight=1.0,
            )

    parents = ["николай николаевич", "ольга николаевна", "николай смирнов", "смирнов николай", "ольга смирнова", "смирнова ольга", "сергей николайович"]
    for p in parents:
        if p in desc_clean:
            conn.close()
            return LayerResult(
                category="family_income",
                confidence=1.0,
                layer="L1_hard_rules",
                reasoning=f"Known parent: {p}",
                weight=1.0,
            )

    # Compensation keywords
    for comp_kw in [" за ", "возврат", "долг", "за обед", "за такси", "за кальяны"]:
        if comp_kw in desc_clean:
            conn.close()
            return LayerResult(
                category="compensation",
                confidence=0.80,
                layer="L1_hard_rules",
                reasoning=f"Compensation keyword: {comp_kw.strip()}",
                weight=1.0,
            )

    # Gift keywords
    gift_kws = ["с др", "день рождения", "днём рождения", "подарок", "новый год", "рождество", "поздравляю"]
    for kw in gift_kws:
        if kw in desc_clean:
            conn.close()
            return LayerResult(
                category="gift",
                confidence=0.90,
                layer="L1_hard_rules",
                reasoning=f"Gift keyword: {kw}",
                weight=1.0,
            )

    rows = conn.execute("""
        SELECT sender_pattern, transfer_type, typical_category,
               bank_hint, typical_amounts, sender_label
        FROM known_senders
        WHERE bank_hint IS NOT NULL AND typical_amounts IS NOT NULL
    """).fetchall()
    conn.close()

    for pattern, ttype, cat, bank_hint, amts_raw, label in rows:
        if bank_hint:
            bh_low = bank_hint.lower()
            aliases = {
                "t-bank": ["t-bank", "tbank", "т-банк", "тбанк", "тинькофф", "tinkoff"],
                "sbol": ["sbol", "сбер", "sberbank", "сбербанк"],
                "alfa": ["альфа", "alfa"],
            }
            allowed = aliases.get(bh_low, [bh_low])
            if not any(a in bank for a in allowed) and not any(a in desc_clean for a in allowed):
                continue

        try:
            typical = json.loads(amts_raw) if isinstance(amts_raw, str) else amts_raw
        except (json.JSONDecodeError, TypeError):
            typical = []
        if not isinstance(typical, list):
            typical = [typical]

        for t in typical:
            try:
                t_f = float(t)
            except (ValueError, TypeError):
                continue
            if t_f > 0 and abs(amount - t_f) / max(t_f, 1) <= 0.05:
                return LayerResult(
                    category=ttype,
                    confidence=0.95,
                    layer="L1_hard_rules",
                    reasoning=f"bank={bank_hint}, amount={amount:.0f}, label={label}",
                    weight=1.0,
                )
    return None


# ============ L2: Learned patterns ============

def l2_learned_patterns(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Match by adaptive learned patterns (incoming + outgoing)."""
    conn = sqlite3.connect(db_path)

    # Incoming
    try:
        incoming = conn.execute("""
            SELECT transfer_type, sender_label, times_confirmed, auto_classify_after,
                   amount_min, amount_max, bank_hint
            FROM known_senders
            WHERE amount_min IS NOT NULL
        """).fetchall()
    except Exception:  # noqa: BLE001
        incoming = []

    # Outgoing
    try:
        outgoing = conn.execute("""
            SELECT typical_category, user_label, times_confirmed, auto_classify_after,
                   amount_min, amount_max, merchant_pattern
            FROM outgoing_patterns
        """).fetchall()
    except Exception:  # noqa: BLE001
        outgoing = []
    conn.close()

    bank = ctx["bank"]
    merchant = ctx["merchant"]
    amount = ctx["amount_abs"]

    # Try incoming match
    for (ttype, label, confirmed, auto_after, amin, amax, bhint) in incoming:
        if bhint and bhint.lower() != bank and bhint.lower() not in ctx["description_clean"]:
            continue
        if amin is not None and amax is not None and not (amin <= amount <= amax):
            continue
        confirmed = confirmed or 1
        auto_after = auto_after or 5
        if confirmed >= auto_after:
            conf, note = 0.98, "auto"
        elif confirmed >= 3:
            conf, note = 0.90, "notify"
        else:
            conf, note = 0.70, "ask"
        return LayerResult(
            category=ttype, confidence=conf, layer="L2_learned_in",
            reasoning=f"bank={bhint}, confirmed={confirmed}, note={note}",
            weight=1.0,
        )

    # Try outgoing match
    for (cat, label, confirmed, auto_after, amin, amax, mp) in outgoing:
        if mp and mp != "unknown" and mp not in merchant and mp not in ctx["description_clean"]:
            continue
        if amin is not None and amax is not None and not (amin <= amount <= amax):
            continue
        confirmed = confirmed or 1
        auto_after = auto_after or 5
        if confirmed >= auto_after:
            conf, note = 0.95, "auto"
        elif confirmed >= 3:
            conf, note = 0.88, "notify"
        else:
            conf, note = 0.65, "ask"
        return LayerResult(
            category=cat, confidence=conf, layer="L2_learned_out",
            reasoning=f"merchant={mp}, confirmed={confirmed}",
            weight=1.0,
        )
    return None


# ============ L3: Historical vote (Levenshtein / Jaro-Winkler) ============

def l3_historical_vote(db_path: str, ctx: dict[str, Any], top_k: int = 10) -> LayerResult | None:
    """Find top-K similar past transactions, majority vote their categories."""
    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]
    except ImportError:
        return None

    desc = ctx["description_clean"]
    if not desc or len(desc) < 4:
        return None

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT description, category
        FROM transactions
        WHERE category NOT IN ('unknown', 'uncategorized', 'other', '')
        ORDER BY id DESC
        LIMIT 5000
    """).fetchall()
    conn.close()

    if not rows:
        return None

    scored = []
    for d, cat in rows:
        if not d:
            continue
        score = fuzz.token_set_ratio(desc, d.lower())
        if score >= 80:
            scored.append((score, cat))

    if len(scored) < 3:
        return None

    scored.sort(reverse=True)
    top = scored[:top_k]

    from collections import Counter
    votes = Counter(cat for _, cat in top)
    winner, count = votes.most_common(1)[0]

    if count < 3:
        return None

    conf = min(0.5 + 0.5 * (count / len(top)), 0.85)
    return LayerResult(
        category=winner, confidence=conf, layer="L3_historical",
        reasoning=f"{count}/{len(top)} similar tx voted {winner}",
        weight=0.7,
    )


# ============ L4: Temporal patterns ============

def l4_temporal(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Match by day_of_month / weekday pattern."""
    conn = sqlite3.connect(db_path)
    day = ctx["day_of_month"]
    try:
        rows = conn.execute("""
            SELECT transfer_type, sender_label, day_of_month
            FROM known_senders
            WHERE day_of_month IS NOT NULL
        """).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    conn.close()

    for (ttype, label, dom) in rows:
        if dom == day:
            return LayerResult(
                category=ttype, confidence=0.75, layer="L4_temporal",
                reasoning=f"day_of_month={dom} matches {label}",
                weight=0.5,
            )
    return None


# ============ L5: Merchant Knowledge Base ============

from jarvis.finance.ml_local.merchant_kb import MERCHANT_KB


def l5_merchant_kb(ctx: dict[str, Any]) -> LayerResult | None:
    """Match merchant against built-in KB (list of MerchantEntry)."""
    merchant = str(ctx.get("merchant", "")).lower()
    desc = str(ctx.get("description_clean", "")).lower()
    combined = f"{merchant} {desc}".strip()

    for entry in MERCHANT_KB:
        for keyword in entry.patterns:
            kw = keyword.lower()
            if kw in combined:
                return LayerResult(
                    category=entry.category,
                    confidence=0.95,
                    layer="L5_merchant_kb",
                    reasoning=f"keyword '{keyword}' matched",
                    weight=0.95,
                )
    return None


# ============ L6: Statistical anomaly ============

def l6_statistical(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Outlier evaluation placeholder."""
    return None


# ============ L7: Amount signature ============

def l7_amount_signature(ctx: dict[str, Any]) -> LayerResult | None:
    """Use amount type as signal."""
    amount = ctx["amount_abs"]
    is_round = ctx["is_round"]

    if is_round and amount in (500, 1000, 1500, 2000, 3000, 5000, 10000, 15000, 20000):
        return LayerResult(
            category="peer_transfer", confidence=0.55, layer="L7_signature",
            reasoning=f"round amount {amount:.0f} suggests transfer",
            weight=0.4,
        )
    if 0 < amount < 100:
        return LayerResult(
            category="other", confidence=0.1, layer="L7_signature",
            reasoning="small amount — weak signal",
            weight=0.1,
        )
    return None


# ============ L8: SetFit ML (existing) ============

def l8_setfit(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Use existing SetFit model for classification."""
    try:
        from jarvis.finance.ml_local.local_categorizer import (
            LocalCategorizer,
        )
        cat = LocalCategorizer()
        if not cat.is_available:
            raise ClassificationUnavailable("SetFit model unavailable")
        pred = cat.predict(ctx["description_clean"])
        if pred and pred.get("category"):
            return LayerResult(
                category=pred["category"],
                confidence=float(pred.get("confidence", 0.7)),
                layer="L8_setfit",
                reasoning=f"SetFit prediction: {pred['category']} ({pred.get('confidence', 0.7):.2f})",
                weight=1.0,
            )
    except Exception:  # noqa: BLE001 - model boundary
        raise ClassificationUnavailable("SetFit classification unavailable") from None
    return None


# ============ L8.5: RAG vote ============

_RAG_RETRIEVER: Any = None


def l8_5_rag_vote(ctx: dict[str, Any]) -> LayerResult | None:
    """RAG-based classification: find top-K similar labeled, vote."""
    global _RAG_RETRIEVER
    try:
        if _RAG_RETRIEVER is None:
            from jarvis.rag.embedder import Embedder
            from jarvis.rag.retriever import TransactionRetriever
            from jarvis.rag.store import VectorStore
            store = VectorStore()
            embedder = Embedder()
            _RAG_RETRIEVER = TransactionRetriever(store, embedder)
        retriever = _RAG_RETRIEVER
    except Exception:  # noqa: BLE001 - index initialization boundary
        raise ClassificationUnavailable("Transaction retrieval unavailable") from None

    merchant = ctx.get("merchant", "")
    description = ctx.get("description_clean", "")
    similar = retriever.find_similar(merchant, description, k=15)
    if not similar:
        return None
    category, confidence = retriever.vote_category(similar)
    if confidence < 0.15:
        return None
    return LayerResult(
        category=category,
        confidence=confidence,
        layer="L8_5_rag",
        reasoning=f"RAG vote: {category} ({confidence:.2f})",
        weight=3.0,
    )


# ============ L9: LLM context ============

async def l9_llm_context(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Use screened semantic flags through the common budget; raw context stays local."""
    from decimal import Decimal

    from jarvis.finance.classification.intelligence import propose, screened
    from jarvis.finance.db import FinanceDB
    from jarvis.finance.personal_cfo import PersonalCFO
    db = FinanceDB(db_path)
    merchant = str(ctx.get('merchant') or '')
    amount = Decimal(str(ctx.get('amount', 0)))
    confirmed = PersonalCFO(db).confirmed_categories(merchant, amount)
    features = screened(str(ctx.get('description_clean') or ''), amount, confirmed)
    result = await propose(features)
    if result.get('status') in {'unavailable', 'budget_refused', 'unknown', 'not_repeated'}:
        raise ClassificationUnavailable('Budgeted AI unavailable; review required')
    proposal = result.get('result')
    if proposal and proposal['category'] != 'unknown':
        return LayerResult(category=proposal['category'], confidence=0.49, layer='L9_llm',
            reasoning='AI proposal; uncalibrated; owner review required: ' + proposal['reason'], weight=0.7)
    return None


# ============ L12: Bayesian Classifier ============

def l12_bayesian(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Naive Bayes Gaussian model for classification."""
    try:
        from sklearn.feature_extraction.text import (  # type: ignore[import-not-found]
            TfidfVectorizer,
        )
        from sklearn.naive_bayes import GaussianNB  # type: ignore[import-not-found]
    except ImportError:
        return None

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT description, category FROM transactions
        WHERE category NOT IN ('unknown', 'other', '')
        LIMIT 5000
    """).fetchall()
    conn.close()

    if len(rows) < 50:
        return None

    texts = [r[0] for r in rows if r[0]]
    labels = [r[1] for r in rows if r[0]]

    try:
        vectorizer = TfidfVectorizer(max_features=5000, ngram_range=(1, 2))
        X = vectorizer.fit_transform(texts)
        model = GaussianNB()
        model.fit(X.toarray(), labels)

        test_vec = vectorizer.transform([ctx["description_clean"]]).toarray()
        pred = model.predict(test_vec)[0]
        proba = model.predict_proba(test_vec)[0]
        conf = float(max(proba))

        return LayerResult(
            category=pred, confidence=conf, layer="L12_bayesian",
            reasoning=f"Naive Bayes prediction: {pred} ({conf:.2f})",
            weight=0.8,
        )
    except Exception:  # noqa: BLE001
        return None


# ============ L13: TF-IDF + SGDClassifier ============

def l13_tfidf_sgd(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """TF-IDF + SGDClassifier for text classification."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import SGDClassifier  # type: ignore[import-not-found]
    except ImportError:
        return None

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT description, category FROM transactions
        WHERE category NOT IN ('unknown', 'other', '')
        LIMIT 5000
    """).fetchall()
    conn.close()

    if len(rows) < 50:
        return None

    texts = [r[0] for r in rows if r[0]]
    labels = [r[1] for r in rows if r[0]]

    try:
        vectorizer = TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), max_features=5000)
        X = vectorizer.fit_transform(texts)
        model = SGDClassifier(loss='modified_huber', alpha=0.0001, max_iter=1000)
        model.fit(X, labels)

        X_test = vectorizer.transform([ctx["description_clean"]])
        pred = model.predict(X_test)[0]
        proba = model.predict_proba(X_test)[0] if hasattr(model, 'predict_proba') else [0.5]
        conf = float(max(proba)) if len(proba) > 0 else 0.5

        return LayerResult(
            category=pred, confidence=conf, layer="L13_tfidf_sgd",
            reasoning=f"TF-IDF+SGD prediction: {pred} ({conf:.2f})",
            weight=0.75,
        )
    except Exception:  # noqa: BLE001
        return None


# ============ L14: Ensemble (Random Forest + Logistic Regression) ============

def l14_ensemble(db_path: str, ctx: dict[str, Any]) -> LayerResult | None:
    """Random Forest + LogisticRegression ensemble."""
    try:
        from sklearn.ensemble import (  # type: ignore[import-not-found]
            RandomForestClassifier,
            VotingClassifier,
        )
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT description, category FROM transactions
        WHERE category NOT IN ('unknown', 'other', '')
        LIMIT 5000
    """).fetchall()
    conn.close()

    if len(rows) < 100:
        return None

    texts = [r[0] for r in rows if r[0]]
    labels = [r[1] for r in rows if r[0]]

    try:
        vectorizer = TfidfVectorizer(max_features=5000, ngram_range=(1, 2))
        X = vectorizer.fit_transform(texts)

        rf = RandomForestClassifier(n_estimators=50, random_state=42, n_jobs=-1)
        lr = LogisticRegression(max_iter=500, random_state=42)

        ensemble = VotingClassifier(
            estimators=[('rf', rf), ('lr', lr)],
            voting='soft'
        )
        ensemble.fit(X, labels)

        test_vec = vectorizer.transform([ctx["description_clean"]])
        pred = ensemble.predict(test_vec)[0]
        proba = ensemble.predict_proba(test_vec)[0]
        conf = float(max(proba))

        return LayerResult(
            category=pred, confidence=conf, layer="L14_ensemble",
            reasoning=f"Ensemble prediction: {pred} ({conf:.2f})",
            weight=0.9,
        )
    except Exception:  # noqa: BLE001
        return None
