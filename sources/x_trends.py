import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml


API_BASE = "https://api.x.com/2"


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="personalization/config.yaml")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--output-dir", default="data/trends")
    return parser.parse_args()


def request_json(url: str, token: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def fetch_woeid_trends(token: str, woeids: list[int]) -> list[dict[str, Any]]:
    trends: list[dict[str, Any]] = []
    for woeid in woeids:
        payload = request_json(f"{API_BASE}/trends/by/woeid/{woeid}", token)
        for item in payload.get("data", []):
            item = dict(item)
            item["woeid"] = woeid
            trends.append(item)
    return trends


def try_fetch_woeid_trends(token: str, woeids: list[int]) -> tuple[list[dict[str, Any]], list[str]]:
    trends: list[dict[str, Any]] = []
    errors: list[str] = []
    for woeid in woeids:
        try:
            trends.extend(fetch_woeid_trends(token, [woeid]))
        except requests.HTTPError as exc:
            errors.append(f"woeid:{woeid}: {exc}")
        except Exception as exc:
            errors.append(f"woeid:{woeid}: {exc}")
    return trends, errors


def fetch_recent_count(token: str, query: str) -> dict[str, Any]:
    payload = request_json(
        f"{API_BASE}/tweets/counts/recent",
        token,
        params={"query": query, "granularity": "day"},
    )
    total = sum(bucket.get("tweet_count", 0) for bucket in payload.get("data", []))
    return {
        "query": query,
        "total_count": total,
        "buckets": payload.get("data", []),
    }


def try_fetch_recent_counts(token: str, queries: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    counts: list[dict[str, Any]] = []
    errors: list[str] = []
    for query in queries:
        try:
            counts.append(fetch_recent_count(token, query))
        except requests.HTTPError as exc:
            errors.append(f"query:{query}: {exc}")
        except Exception as exc:
            errors.append(f"query:{query}: {exc}")
    return counts, errors


def contains_any(text: str, terms: list[str]) -> bool:
    haystack = text.lower()
    return any(term.lower() in haystack for term in terms)


def normalize_trend(item: dict[str, Any]) -> dict[str, Any]:
    name = item.get("trend_name") or item.get("name") or item.get("trend") or ""
    return {
        "name": name,
        "tweet_count": item.get("tweet_count"),
        "woeid": item.get("woeid"),
        "raw": item,
    }


def build_empty_result(date: str, reason: str) -> dict[str, Any]:
    return {
        "date": date,
        "source": "x",
        "status": "skipped",
        "reason": reason,
        "raw_trends": [],
        "tech_trends": [],
        "watchlist_counts": [],
    }


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    trend_config = config.get("x_trends", {})
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{args.date}_x_trends.json"

    if not trend_config.get("enabled", False):
        result = build_empty_result(args.date, "x_trends disabled")
    else:
        token = os.environ.get("X_BEARER_TOKEN", "").strip()
        if not token:
            result = build_empty_result(args.date, "X_BEARER_TOKEN is not set")
        else:
            try:
                raw_items, trend_errors = try_fetch_woeid_trends(token, trend_config.get("woeids", [1]))
                raw_trends = [normalize_trend(item) for item in raw_items]
                tech_terms = trend_config.get("tech_terms", [])
                tech_trends = [
                    item for item in raw_trends if contains_any(item.get("name", ""), tech_terms)
                ]
                watchlist_counts, count_errors = try_fetch_recent_counts(
                    token,
                    trend_config.get("watchlist", []),
                )
                errors = trend_errors + count_errors
                result = {
                    "date": args.date,
                    "source": "x",
                    "status": "partial" if errors else "ok",
                    "errors": errors,
                    "raw_trends": raw_trends,
                    "tech_trends": tech_trends,
                    "watchlist_counts": watchlist_counts,
                }
            except requests.HTTPError as exc:
                print(f"X API request failed: {exc}", file=sys.stderr)
                result = build_empty_result(args.date, f"X API request failed: {exc}")
            except Exception as exc:
                print(f"X trend collection failed: {exc}", file=sys.stderr)
                result = build_empty_result(args.date, f"X trend collection failed: {exc}")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Wrote X trends to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
