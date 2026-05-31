import argparse
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from typing import Any

import dotenv
import requests
from langchain.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)
from langchain_openai import ChatOpenAI
from tqdm import tqdm

from structure import FullTextStructure


if os.path.exists(".env"):
    dotenv.load_dotenv()

SYSTEM = """You are a professional paper analyst.
Use the provided full paper text to produce concise, technically precise analysis.
Your output should be in {language}."""

TEMPLATE = """Analyze the following paper text.
Return only a valid JSON object with exactly these string fields:
summary, key_contributions, method_details, limitations, why_it_matters.

Paper text:
{content}
"""


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "header", "footer"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "header", "footer"} and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            cleaned = re.sub(r"\s+", " ", data).strip()
            if cleaned:
                self.parts.append(cleaned)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="AI enhanced jsonl file")
    parser.add_argument("--top_n", type=int, default=int(os.environ.get("FULLTEXT_TOP_N", "10")))
    parser.add_argument("--max_chars", type=int, default=int(os.environ.get("FULLTEXT_MAX_CHARS", "50000")))
    parser.add_argument("--max_retries", type=int, default=2)
    return parser.parse_args()


def load_jsonl(path: str) -> list[dict[str, Any]]:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                items.append(json.loads(line))
    return items


def write_jsonl(path: str, items: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def paper_score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("scores", {}).get("final", 0))
    except (TypeError, ValueError):
        return 0.0


def fetch_arxiv_html(arxiv_id: str, max_chars: int) -> str | None:
    url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        response = requests.get(url, timeout=30)
        if response.status_code != 200:
            print(f"Fulltext HTML unavailable for {arxiv_id}: HTTP {response.status_code}", file=sys.stderr)
            return None
        extractor = TextExtractor()
        extractor.feed(response.text)
        text = extractor.text()
        if len(text) < 2000:
            print(f"Fulltext HTML too short for {arxiv_id}", file=sys.stderr)
            return None
        return text[:max_chars]
    except Exception as exc:
        print(f"Fulltext fetch failed for {arxiv_id}: {exc}", file=sys.stderr)
        return None


def enhance_fulltext(chain, item: dict[str, Any], language: str, max_retries: int) -> dict[str, str] | None:
    for attempt in range(max_retries):
        try:
            result = chain.invoke({
                "language": language,
                "content": item["_fulltext"],
            })
            return result.model_dump()
        except Exception as exc:
            error_text = str(exc)
            if "Error code: 400" in error_text or "invalid_request_error" in error_text:
                print(f"Fulltext LLM invalid request for {item.get('id')}: {exc}", file=sys.stderr)
                return None
            if attempt == max_retries - 1:
                print(f"Fulltext LLM failed for {item.get('id')}: {exc}", file=sys.stderr)
                return None
            wait_seconds = 2 ** attempt
            print(f"Fulltext LLM retry for {item.get('id')} in {wait_seconds}s: {exc}", file=sys.stderr)
            time.sleep(wait_seconds)
    return None


def main() -> None:
    args = parse_args()
    language = os.environ.get("LANGUAGE", "Chinese")
    model_name = os.environ.get("MODEL_NAME", "deepseek-chat")
    items = load_jsonl(args.data)
    top_items = sorted(items, key=paper_score, reverse=True)[:args.top_n]

    llm = ChatOpenAI(model=model_name).with_structured_output(FullTextStructure, method="json_mode")
    prompt = ChatPromptTemplate.from_messages([
        SystemMessagePromptTemplate.from_template(SYSTEM),
        HumanMessagePromptTemplate.from_template(TEMPLATE),
    ])
    chain = prompt | llm

    by_id = {item.get("id"): item for item in items}
    for item in tqdm(top_items, desc="Fulltext enhancement"):
        arxiv_id = item.get("id")
        if not arxiv_id:
            continue
        text = fetch_arxiv_html(arxiv_id, args.max_chars)
        if not text:
            item["fulltext_status"] = "unavailable"
            continue
        item["_fulltext"] = text
        fulltext_ai = enhance_fulltext(chain, item, language, args.max_retries)
        item.pop("_fulltext", None)
        if fulltext_ai:
            item["fulltext_AI"] = fulltext_ai
            item["fulltext_status"] = "ok"
        else:
            item["fulltext_status"] = "failed"
        by_id[arxiv_id] = item

    write_jsonl(args.data, [by_id.get(item.get("id"), item) for item in items])
    print(f"Updated fulltext enhancement in {args.data}", file=sys.stderr)


if __name__ == "__main__":
    main()
