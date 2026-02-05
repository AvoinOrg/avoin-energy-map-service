import os, logging, time
from typing import Any, Dict, Iterable, Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from contextlib import contextmanager

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from tenacity import retry, stop_after_attempt, wait_exponential

from db.session import SessionLocal

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.api_loader")


def _build_http_session(
    max_pool: int = 64, retries: int = 2, backoff: float = 0.5
) -> requests.Session:
    """
    One shared keep-alive session with connection pooling & light retry at TCP level.
    Network-level retry here (connect/read), application-level retry via Tenacity below.
    """
    sess = requests.Session()
    retry_cfg = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=max_pool, pool_maxsize=max_pool, max_retries=retry_cfg
    )
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    sess.headers.update({"Accept": "application/json"})
    return sess


# Retry application-level (HTTP 4xx/5xx raised by raise_for_status or JSON decode issues)
@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=8))
def http_get_json(
    session: requests.Session, url: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    resp = session.get(url, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


class BaseLoader:
    """
    Generic features → DB upsert loader.

    Customize by:
      - overriding map_feature_to_row()
      - passing model_class and index_columns (SQLAlchemy model + conflict keys)
      - overriding initial_params() and/or iterate_features() if your API differs
    """

    # Concurrency & batching defaults
    MAX_WORKERS: int = 4  # DB worker threads
    UPSERT_BATCH_SIZE: int = (
        2000  # rows per DB batch (keep memory low, commits frequent)
    )
    API_PAGE_SIZE: int = 5000  # page size to request upstream (server may cap)
    MAX_INFLIGHT_FUTURES: int = 8  # backpressure: limit queued DB batches

    # API paging keys
    features_key: str = "features"
    links_key: str = "links"
    next_rel_value: str = "next"
    number_returned_key: str = "numberReturned"

    # ORM
    model_class = None
    index_columns: Tuple = tuple()

    def __init__(
        self,
        base_url: str,
        *,
        model_class,
        index_columns: Tuple,
        max_workers: Optional[int] = None,
        upsert_batch_size: Optional[int] = None,
        api_page_size: Optional[int] = None,
        max_inflight_futures: Optional[int] = None,
        name: str = "etl.loader",
        select_properties: Optional[
            List[str]
        ] = None,  # e.g. ["id","municipality_number",...]
    ):
        self.base_url = base_url
        self.model_class = model_class
        self.index_columns = index_columns
        if max_workers is not None:
            self.MAX_WORKERS = max_workers
        if upsert_batch_size is not None:
            self.UPSERT_BATCH_SIZE = upsert_batch_size
        if api_page_size is not None:
            self.API_PAGE_SIZE = api_page_size
        if max_inflight_futures is not None:
            self.MAX_INFLIGHT_FUTURES = max_inflight_futures
        self.logger = logging.getLogger(name)
        self.select_properties = select_properties or []

        # one shared HTTP session per loader
        self._http = _build_http_session()

    # ---- Hook: per-source base params
    def initial_params(self) -> Dict[str, Any]:
        params = {"limit": self.API_PAGE_SIZE}
        if self.select_properties:
            # many OGC APIs support property projection; harmless if ignored
            params["properties"] = ",".join(self.select_properties)
        return params

    # ---- Hook: map raw feature → row dict
    def map_feature_to_row(self, feature: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    # ---- Hook: optional filter
    def should_skip_feature(self, row: Dict[str, Any]) -> bool:
        return False

    # ---- Paging generator: next-link or startIndex fallback
    def iterate_features(
        self, base_params: Optional[Dict[str, Any]] = None
    ) -> Iterable[Dict[str, Any]]:
        """
        Handles two patterns:
          1) Server returns links[rel=next] → follow it.
          2) Server omits 'next' but returns exactly 'limit' items → advance via startIndex.
        """
        url = self.base_url
        params = dict(self.initial_params())
        if base_params:
            params.update(base_params)
        params.setdefault("limit", self.API_PAGE_SIZE)

        page = 1
        start_index = 0
        use_start_index = False

        self.logger.info(params)

        while True:
            if use_start_index:
                # on fallback path, ensure we drive with startIndex
                params = {
                    "startIndex": start_index,
                    "limit": params.get("limit", self.API_PAGE_SIZE),
                }
                # preserve filters/properties if present in base_params/initial_params
                if base_params:
                    for k, v in base_params.items():
                        if k not in ("limit", "startIndex"):
                            params[k] = v

            self.logger.info(
                "Extract page %s%s",
                page,
                " (startIndex=%d)" % start_index if use_start_index else "",
            )

            data = http_get_json(self._http, url, params)
            feats = data.get(self.features_key, [])
            returned = data.get(self.number_returned_key)
            # Some services omit numberReturned; fall back to len(features)
            number_returned = int(returned) if isinstance(returned, int) else len(feats)

            for f in feats:
                yield f

            # Prefer rel=next if present
            next_href = None
            for link in data.get(self.links_key, []):
                if link.get("rel") == self.next_rel_value:
                    next_href = link.get("href")
                    break

            if next_href:
                url = next_href
                params = {}
                page += 1
                continue

            # No next link → fallback using startIndex if we exactly filled the page
            limit = params.get("limit", self.API_PAGE_SIZE)
            if number_returned and int(number_returned) == int(limit):
                use_start_index = True
                start_index += int(number_returned)
                page += 1
                # do not change URL on fallback
                continue

            # done
            break

    # ---- Upsert helpers
    def _conflict_field_names(self) -> set:
        # Works for ORM columns or column expressions
        names = set()
        for col in self.index_columns:
            names.add(getattr(col, "key", getattr(col, "name", str(col))))
        return names

    def upsert_one(self, database_session: Session, row: Dict[str, Any]) -> bool:
        try:
            stmt = (
                insert(self.model_class)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=list(self.index_columns),
                    set_={
                        c: row[c] for c in row if c not in self._conflict_field_names()
                    },
                )
            )
            database_session.execute(stmt)
            return True
        except SQLAlchemyError as e:
            self.logger.exception("Upsert failed for %s: %s", row, e)
            return False

    def _upsert_batch_worker(self, features_batch: List[Dict[str, Any]]) -> int:
        """
        Performance Op: Transforms data in memory, then sends ONE massive SQL
        statement to the DB instead of 2000 small ones.
        """
        valid_rows = []

        # 1. CPU Bound: Map all features to rows first
        for feature in features_batch:
            try:
                row = self.map_feature_to_row(feature)
                if row and not self.should_skip_feature(row):
                    valid_rows.append(row)
            except Exception as e:
                self.logger.error(f"Mapping failed for feature: {e}")
                continue

        if not valid_rows:
            return 0

        # 2. IO Bound: Execute single Batch Upsert
        with SessionLocal() as worker_sess:
            try:
                # Prepare the INSERT statement with the list of dictionaries
                stmt = insert(self.model_class).values(valid_rows)

                # Dynamically build the SET clause for ON CONFLICT update.
                # We update every column in the model that isn't part of the unique index/conflict key.
                # 'stmt.excluded' represents the row we were trying to insert.
                update_dict = {
                    col.name: getattr(stmt.excluded, col.name)
                    for col in self.model_class.__table__.columns
                    if col.name not in self._conflict_field_names()
                }

                # Attach the ON CONFLICT clause
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(self.index_columns), set_=update_dict
                )

                worker_sess.execute(stmt)
                worker_sess.commit()
                return len(valid_rows)

            except Exception as e:
                worker_sess.rollback()
                self.logger.exception(
                    "Batch failed, rolled back (%d rows): %s", len(valid_rows), e
                )
                return 0

    def run(
        self,
        *,
        base_params: Optional[Dict[str, Any]] = None,
        max_records: Optional[int] = None,
    ) -> int:
        total_seen = 0
        total_written = 0
        start_time = time.perf_counter()

        self.logger.info(
            "Extract started | page_size=%s | workers=%s | upsert_batch=%s",
            self.API_PAGE_SIZE,
            self.MAX_WORKERS,
            self.UPSERT_BATCH_SIZE,
        )

        futures: List = []

        try:
            with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as pool:
                current_batch: List[Dict[str, Any]] = []

                for feature in self.iterate_features(base_params=base_params):
                    current_batch.append(feature)
                    total_seen += 1

                    # If batch is full, submit it
                    if len(current_batch) >= self.UPSERT_BATCH_SIZE:
                        futures.append(
                            pool.submit(self._upsert_batch_worker, current_batch)
                        )
                        current_batch = []

                        # BACKPRESSURE CONTROL
                        # If we have too many active tasks, PAUSE here until at least one finishes.
                        while len(futures) >= self.MAX_INFLIGHT_FUTURES:
                            # wait() blocks efficiently. No timeout needed.
                            done, _ = wait(futures, return_when=FIRST_COMPLETED)

                            # Clean up finished futures and tally results
                            for fut in done:
                                total_written += fut.result()
                                futures.remove(fut)

                    if max_records and total_seen >= max_records:
                        break

                # Submit final partial batch
                if current_batch:
                    futures.append(
                        pool.submit(self._upsert_batch_worker, current_batch)
                    )

                # Drain remaining futures
                for fut in as_completed(futures):
                    total_written += fut.result()

        except Exception as exc:
            self.logger.exception("Extract failed: %s", exc)
            raise

        elapsed = time.perf_counter() - start_time
        self.logger.info(
            "Extract completed | seen=%d | written=%d | duration=%.2fs",
            total_seen,
            total_written,
            elapsed,
        )
        return total_written
