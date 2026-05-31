# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
import arxiv
import os
import sys
import time


class DailyArxivPipeline:
    def __init__(self):
        self.page_size = 100
        self.enable_api_fallback = os.environ.get("ENABLE_ARXIV_API_FALLBACK", "false").lower() == "true"
        self.client = arxiv.Client(
            page_size=self.page_size,
            delay_seconds=6,
            num_retries=1,
        )

    def fetch_paper(self, paper_id):
        search = arxiv.Search(id_list=[paper_id])
        for attempt in range(5):
            try:
                return next(self.client.results(search))
            except Exception as exc:
                wait_seconds = min(60, 2 ** attempt * 5)
                print(
                    f"arXiv API fallback failed for {paper_id}: {exc}; "
                    f"retrying in {wait_seconds}s",
                    file=sys.stderr,
                )
                time.sleep(wait_seconds)
        print(f"arXiv API fallback unavailable for {paper_id}; keeping parsed metadata", file=sys.stderr)
        return None

    def process_item(self, item: dict, spider):
        item["pdf"] = f"https://arxiv.org/pdf/{item['id']}"
        item["abs"] = f"https://arxiv.org/abs/{item['id']}"

        has_required_metadata = all([
            item.get("authors"),
            item.get("title"),
            item.get("categories"),
            item.get("summary"),
        ])
        if has_required_metadata:
            return item

        if not self.enable_api_fallback:
            missing_fields = [
                field for field in ("authors", "title", "categories", "summary")
                if not item.get(field)
            ]
            print(
                f"arXiv API fallback disabled for {item['id']}; missing fields: {missing_fields}",
                file=sys.stderr,
            )
            item["authors"] = item.get("authors") or []
            item["title"] = item.get("title") or item["id"]
            item["categories"] = item.get("categories") or []
            item["comment"] = item.get("comment")
            item["summary"] = item.get("summary") or ""
            return item

        paper = self.fetch_paper(item["id"])
        if paper is None:
            item["authors"] = item.get("authors") or []
            item["title"] = item.get("title") or item["id"]
            item["categories"] = item.get("categories") or []
            item["comment"] = item.get("comment")
            item["summary"] = item.get("summary") or ""
            return item

        item["authors"] = item.get("authors") or [a.name for a in paper.authors]
        item["title"] = item.get("title") or paper.title
        item["categories"] = item.get("categories") or paper.categories
        item["comment"] = item.get("comment") or paper.comment
        item["summary"] = item.get("summary") or paper.summary
        return item
