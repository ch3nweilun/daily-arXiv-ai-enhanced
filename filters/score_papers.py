import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

import yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="Raw arXiv jsonl file")
    parser.add_argument("--config", default="personalization/config.yaml")
    parser.add_argument("--trends-dir", default="data/trends")
    return parser.parse_args()


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_jsonl(path: str) -> list[dict[str, Any]]:
    papers = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                papers.append(json.loads(line))
    return papers


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def contains_phrase(text: str, phrase: str) -> bool:
    return normalize(phrase) in normalize(text)


def terms_for_keyword(keyword: dict[str, Any]) -> list[str]:
    return [keyword.get("term", "")] + list(keyword.get("aliases", []))


def score_interest(paper: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str], bool]:
    interests = config.get("interests", {})
    categories = interests.get("categories", [])
    keywords = interests.get("keywords", [])
    negative_keywords = interests.get("negative_keywords", [])
    authors = interests.get("authors", [])
    paper_categories = paper.get("categories", []) or []
    primary_category = paper_categories[0] if paper_categories else ""
    title = paper.get("title", "")
    summary = paper.get("summary", "")
    authors_text = ", ".join(paper.get("authors", []))
    reasons: list[str] = []
    score = 0.0
    force_selected = False
    blocked = False

    if primary_category in categories:
        category_rank = categories.index(primary_category)
        category_score = max(8, 15 - category_rank * 2)
        score += category_score
        reasons.append(f"category:{primary_category}:+{category_score}")
    elif set(paper_categories).intersection(categories):
        score += 8
        reasons.append("category:secondary:+8")

    keyword_score = 0.0
    for keyword in keywords:
        weight = float(keyword.get("weight", 1))
        for term in terms_for_keyword(keyword):
            if not term:
                continue
            if contains_phrase(title, term):
                points = 8 * weight
                keyword_score += points
                reasons.append(f"keyword:title:{term}:+{points:g}")
                break
            if contains_phrase(summary, term):
                points = 4 * weight
                keyword_score += points
                reasons.append(f"keyword:summary:{term}:+{points:g}")
                break
    score += min(keyword_score, 50)

    for author in authors:
        if author and contains_phrase(authors_text, author):
            score += 25
            force_selected = True
            reasons.append(f"author:{author}:+25")

    for term in negative_keywords:
        if contains_phrase(title, term):
            score -= 30
            blocked = True
            reasons.append(f"negative:title:{term}:-30")
        elif contains_phrase(summary, term):
            score -= 15
            blocked = True
            reasons.append(f"negative:summary:{term}:-15")

    paper["_blocked_by_negative"] = blocked
    return max(0.0, min(100.0, score)), reasons, force_selected


def load_trend_history(trends_dir: str, date: str, lookback_days: int) -> list[dict[str, Any]]:
    base_date = datetime.strptime(date, "%Y-%m-%d").date()
    docs = []
    for offset in range(0, lookback_days + 1):
        day = base_date - timedelta(days=offset)
        path = Path(trends_dir) / f"{day.isoformat()}_x_trends.json"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                docs.append(json.load(f))
        except json.JSONDecodeError:
            continue
    return docs


def build_trend_weights(docs: list[dict[str, Any]], date: str) -> dict[str, float]:
    by_query: dict[str, dict[str, int]] = defaultdict(dict)
    trend_weights: dict[str, float] = {}

    for doc in docs:
        doc_date = doc.get("date")
        for item in doc.get("watchlist_counts", []):
            query = item.get("query")
            if query:
                by_query[query][doc_date] = int(item.get("total_count") or 0)
        for item in doc.get("tech_trends", []):
            name = item.get("name")
            if not name:
                continue
            count = item.get("tweet_count") or 0
            trend_weights[name] = max(trend_weights.get(name, 0.0), min(40.0, math.log1p(count) * 3))

    for query, counts_by_date in by_query.items():
        today_count = counts_by_date.get(date, 0)
        history = [count for day, count in counts_by_date.items() if day != date]
        baseline = median(history) if history else 0
        volume_score = min(50.0, math.log1p(today_count) * 5)
        growth_score = 0.0
        if baseline > 0:
            growth_score = min(50.0, max(0.0, (today_count - baseline) / baseline * 25))
        elif today_count > 0:
            growth_score = 25.0
        trend_weights[query] = max(trend_weights.get(query, 0.0), volume_score + growth_score)

    return {term: min(100.0, score) for term, score in trend_weights.items()}


def score_trend(paper: dict[str, Any], trend_weights: dict[str, float]) -> tuple[float, list[str]]:
    title = paper.get("title", "")
    summary = paper.get("summary", "")
    matches = []
    for term, weight in trend_weights.items():
        if contains_phrase(title, term):
            matches.append((weight * 1.0, f"x_trend:title:{term}"))
        elif contains_phrase(summary, term):
            matches.append((weight * 0.6, f"x_trend:summary:{term}"))
    matches.sort(reverse=True, key=lambda item: item[0])
    top_matches = matches[:3]
    score = min(100.0, sum(item[0] for item in top_matches))
    return score, [f"{reason}:+{points:.1f}" for points, reason in top_matches]


def score_utility(paper: dict[str, Any]) -> tuple[float, list[str]]:
    title = paper.get("title", "")
    summary = paper.get("summary", "")
    comment = paper.get("comment") or ""
    text = f"{title} {summary} {comment}"
    score = 0.0
    reasons: list[str] = []

    if re.search(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        score += 30
        reasons.append("utility:github_link:+30")
    if contains_phrase(text, "benchmark") or contains_phrase(text, "evaluation"):
        score += 8
        reasons.append("utility:benchmark_or_evaluation:+8")
    if contains_phrase(text, "dataset"):
        score += 8
        reasons.append("utility:dataset:+8")
    if contains_phrase(text, "code") and (
        contains_phrase(text, "release") or contains_phrase(text, "available")
    ):
        score += 10
        reasons.append("utility:code_release:+10")

    return min(100.0, score), reasons


def select_papers(papers: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    filter_config = config.get("filter", {})
    threshold = float(filter_config.get("final_threshold", 50))
    max_selected = int(filter_config.get("max_selected", 40))
    min_per_category = int(filter_config.get("min_per_category", 3))
    mode = filter_config.get("mode", "hard")

    if mode == "soft":
        for paper in papers:
            paper["filter_decision"] = "selected"
            paper.pop("_force_selected", None)
            paper.pop("_blocked_by_negative", None)
        return sorted(papers, key=lambda item: item["scores"]["final"], reverse=True)

    selected_ids: set[str] = set()
    selected: list[dict[str, Any]] = []

    for paper in sorted(papers, key=lambda item: item["scores"]["final"], reverse=True):
        scores = paper["scores"]
        passes_score = (
            scores["final"] >= threshold
            or scores["interest"] >= 60
            or scores["trend"] >= 75 and scores["interest"] >= 25
            or paper.get("_force_selected", False)
        )
        if passes_score and not paper.get("_blocked_by_negative", False):
            selected_ids.add(paper["id"])
            selected.append(paper)

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for paper in papers:
        categories = paper.get("categories", []) or []
        by_category[categories[0] if categories else "unknown"].append(paper)

    for category_papers in by_category.values():
        chosen = [paper for paper in category_papers if paper["id"] in selected_ids]
        if len(chosen) >= min_per_category:
            continue
        for paper in sorted(category_papers, key=lambda item: item["scores"]["final"], reverse=True):
            if paper["id"] not in selected_ids and not paper.get("_blocked_by_negative", False):
                selected_ids.add(paper["id"])
                selected.append(paper)
                if len([p for p in category_papers if p["id"] in selected_ids]) >= min_per_category:
                    break

    selected = sorted(selected, key=lambda item: item["scores"]["final"], reverse=True)[:max_selected]
    selected_ids = {paper["id"] for paper in selected}
    for paper in papers:
        paper["filter_decision"] = "selected" if paper["id"] in selected_ids else "skipped"
        paper.pop("_force_selected", None)
        paper.pop("_blocked_by_negative", None)
    return selected


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    data_path = Path(args.data)
    date = data_path.name.split(".jsonl")[0]
    papers = load_jsonl(args.data)
    trend_docs = load_trend_history(
        args.trends_dir,
        date,
        int(config.get("x_trends", {}).get("lookback_days", 7)),
    )
    trend_weights = build_trend_weights(trend_docs, date)
    weights = config.get("score_weights", {"interest": 0.7, "trend": 0.2, "utility": 0.1})

    for paper in papers:
        interest, interest_reasons, force_selected = score_interest(paper, config)
        trend, trend_reasons = score_trend(paper, trend_weights)
        utility, utility_reasons = score_utility(paper)
        final = (
            float(weights.get("interest", 0.7)) * interest
            + float(weights.get("trend", 0.2)) * trend
            + float(weights.get("utility", 0.1)) * utility
        )
        paper["scores"] = {
            "interest": round(interest, 2),
            "trend": round(trend, 2),
            "utility": round(utility, 2),
            "final": round(final, 2),
        }
        paper["matched_reasons"] = interest_reasons + trend_reasons + utility_reasons
        paper["_force_selected"] = force_selected

    selected = select_papers(papers, config)
    scored_path = data_path.with_name(data_path.stem + "_scored.jsonl")
    selected_path = data_path.with_name(data_path.stem + "_selected.jsonl")
    write_jsonl(scored_path, sorted(papers, key=lambda item: item["scores"]["final"], reverse=True))
    write_jsonl(selected_path, selected)
    print(f"Wrote scored papers to {scored_path}")
    print(f"Wrote selected papers to {selected_path}")
    print(f"Selected {len(selected)} of {len(papers)} papers")


if __name__ == "__main__":
    main()
