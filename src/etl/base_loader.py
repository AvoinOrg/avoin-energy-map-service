import os, logging, time
from typing import Any, Dict, Iterable, Optional, List, Tuple, Callable
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from db.session import SessionLocal
from utils.regions import build_region_municipality_param

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.api_loader")

# Retry connections after 4 failed attempts, the waiting time for each retry is 1,2,4,8s
@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=8))
def http_get_json(url: str, query_parameters: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(url, params=query_parameters, timeout=30)
    response.raise_for_status()  # raise if status >= 400
    return response.json()


class BaseLoader:
    """
    Common class to initialize loader for a certain type of data.
    e.g. Buildings, Addresses
    
    Customize by:
      - overriding map_feature_to_row()
      - setting model_class and index_columns (find model classes in src/db/models)
      - overriding initial_params() if the API needs specific query params
      - optionally overriding feature_list_key / link traversal if API differs
      - name for logging purpose
    """

    # Threading & batching defaults (override per subclass or via __init__)
    MAX_WORKERS: int = 4
    UPSERT_BATCH_SIZE: int = 10000
    API_PAGE_SIZE: int = 10000

    # API paging keys
    features_key: str = "features"
    links_key: str = "links"
    next_rel_value: str = "next"

    model_class = None  # SQLAlchemy ORM model class
    index_columns: Tuple = tuple()  # tuple of model columns used for ON CONFLICT

    def __init__(
        self,
        base_url: str,
        *,
        model_class,
        index_columns: Tuple,
        max_workers: Optional[int] = None,
        upsert_batch_size: Optional[int] = None,
        api_page_size: Optional[int] = None,
        name: str = "etl.loader",
    ):
        self.base_url = base_url
        self.model_class = model_class
        self.index_columns = index_columns
        if max_workers:
            self.MAX_WORKERS = max_workers
        if upsert_batch_size:
            self.UPSERT_BATCH_SIZE = upsert_batch_size
        if api_page_size:
            self.API_PAGE_SIZE = api_page_size
        self.logger = logging.getLogger(name)

    # ---- Hook: per-source base params
    def initial_params(self) -> Dict[str, Any]:
        """Default params for first request; override in subclass if needed."""
        return {"limit": self.API_PAGE_SIZE}

    # ---- Hook: map a raw feature to DB row dict (return None to skip)
    def map_feature_to_row(self, feature: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Override in subclass to map one API feature to a row dict for model_class."""
        raise NotImplementedError

    # ---- Hook: (optional) per-feature validation/filter
    def should_skip_feature(self, row: Dict[str, Any]) -> bool:
        """Return True to skip writing this row (e.g., missing unique key)."""
        return False

    # ---- Paging generator (generic 'links' with rel='next')
    def iterate_features(self, base_params: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
        params = dict(self.initial_params())
        if base_params:
            params.update(base_params)
        url = self.base_url
        page = 1
        
        self.logger.info(params)

        # Ensure 'limit' present
        params.setdefault("limit", self.API_PAGE_SIZE)        

        while True:
            self.logger.info("Extract page %s", page)
            data = http_get_json(url, params)
            features = data.get(self.features_key, [])
            for feature in features:
                yield feature

            next_href = None
            for link in data.get(self.links_key, []):
                if link.get("rel") == self.next_rel_value:
                    next_href = link.get("href")
                    break

            if not next_href:
                break

            # move to next page
            url = next_href
            params = {}  # 'next' already includes all query params
            page += 1

    # ---- Upsert one row
    def upsert_one(self, database_session: Session, row: Dict[str, Any]) -> bool:
        """Insert or update a single row via ON CONFLICT DO UPDATE."""
        try:
            stmt = (
                insert(self.model_class)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=list(self.index_columns),
                    set_={c: row[c] for c in row if c not in self._conflict_field_names()}
                )
            )
            database_session.execute(stmt)
            return True
        except SQLAlchemyError as e:
            self.logger.exception("Upsert failed for %s: %s", row, e)
            return False

    def _conflict_field_names(self) -> set:
        return {getattr(col, "key", getattr(col, "name", str(col))) for col in self.index_columns}

    # ---- Worker for a batch
    def _upsert_batch_worker(self, features_batch: List[Dict[str, Any]]) -> int:
        written = 0
        with SessionLocal() as worker_sess:
            for feature in features_batch:
                row = self.map_feature_to_row(feature)
                if row is None:
                    continue
                if self.should_skip_feature(row):
                    continue
                if self.upsert_one(worker_sess, row):
                    written += 1
            worker_sess.commit()
        return written

    # ---- Run end-to-end
    def run(self, *, base_params: Optional[Dict[str, Any]] = None, max_records: Optional[int] = None) -> int:
        total_seen = 0
        total_written = 0
        start_time = time.perf_counter()
        current_batch: List[Dict[str, Any]] = []
        futures = []

        self.logger.info(
            "Extract started | page_size=%s | workers=%s | upsert_batch=%s | max_records=%s",
            self.API_PAGE_SIZE,
            self.MAX_WORKERS,
            self.UPSERT_BATCH_SIZE,
            max_records,
        )

        try:
            with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as pool:
                for feature in self.iterate_features(base_params=base_params):
                    current_batch.append(feature)
                    total_seen += 1

                    if len(current_batch) >= self.UPSERT_BATCH_SIZE:
                        # hand over to a worker
                        futures.append(pool.submit(self._upsert_batch_worker, current_batch))
                        current_batch = []

                    if max_records and total_seen >= max_records:
                        break

                # flush tail
                if current_batch:
                    futures.append(pool.submit(self._upsert_batch_worker, current_batch))
                    current_batch = []

                # collect results
                completed = 0
                total_batches = len(futures)
                for fut in as_completed(futures):
                    written = fut.result()
                    completed += 1
                    total_written += written
                    elapsed = time.perf_counter() - start_time
                    self.logger.info(
                        "Batch %d/%d finished | batch_written=%d | cumulative_seen=%d | cumulative_written=%d | elapsed=%.2fs",
                        completed,
                        total_batches,
                        written,
                        total_seen,
                        total_written,
                        elapsed,
                    )

        except Exception as exc:
            elapsed = time.perf_counter() - start_time
            self.logger.exception(
                "Extract failed after %.2fs | seen=%d | written=%d | error=%s",
                elapsed,
                total_seen,
                total_written,
                exc,
            )
            raise

        elapsed = time.perf_counter() - start_time
        self.logger.info(
            "Extract completed | seen=%d | written=%d | duration=%.2fs",
            total_seen,
            total_written,
            elapsed,
        )
        return total_written
