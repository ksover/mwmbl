import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from logging import getLogger
from pathlib import Path
from time import sleep
from typing import Collection

import requests
from django.conf import settings
from redis import Redis

from mwmbl.crawler.batch import HashedBatch, Link
from mwmbl.crawler.domains import DomainLinkDatabase
from mwmbl.crawler.urls import URLDatabase, URLStatus, FoundURL
from mwmbl.indexer import process_batch
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.blacklist import get_blacklist_domains, is_domain_blacklisted
from mwmbl.indexer.index_batches import get_url_error_status
from mwmbl.indexer.indexdb import BatchStatus
from mwmbl.redis_url_queue import RedisURLQueue, get_domain_max_urls
from mwmbl.utils import get_domain, parse_url

logger = getLogger(__name__)


def update_urls_continuously(data_path: str, new_item_queue: RedisURLQueue):
    batch_cache = BatchCache(Path(data_path) / settings.BATCH_DIR_NAME)
    while True:
        try:
            run(batch_cache, new_item_queue)
        except Exception:
            logger.exception("Error updating URLs")
        sleep(10)


def run(batch_cache: BatchCache, new_item_queue: RedisURLQueue, num_batches: int = 1000):
    process_batch.run(batch_cache, BatchStatus.LOCAL, BatchStatus.URLS_UPDATED, record_urls_in_database, num_batches,
                      new_item_queue)


def record_urls_in_database(batches: Collection[HashedBatch], new_item_queue: RedisURLQueue):
    start = datetime.utcnow()
    blacklist_domains = get_blacklist_domains()
    blacklist_retrieval_time = datetime.utcnow() - start
    logger.info(f"Recording URLs in database for {len(batches)} batches, with {len(blacklist_domains)} blacklist "
                f"domains, retrieved in {blacklist_retrieval_time.total_seconds()} seconds")

    url_users = {}
    url_timestamps = {}
    url_statuses = defaultdict(lambda: URLStatus.NEW)
    domain_links = defaultdict(set)
    for batch in batches:
        for item in batch.items:
            timestamp = get_datetime_from_timestamp(item.timestamp / 1000.0)
            url_timestamps[item.url] = timestamp
            url_users[item.url] = batch.user_id_hash
            if item.content is None:
                url_statuses[item.url] = get_url_error_status(item)
            else:
                url_statuses[item.url] = URLStatus.CRAWLED
                try:
                    crawled_page_domain = get_domain(item.url)
                except ValueError:
                    logger.info(f"Couldn't parse URL {item.url}")
                    continue
                for link in item.content.all_links:
                    process_link(batch.user_id_hash, crawled_page_domain, link, timestamp, url_timestamps, url_users,
                                 blacklist_domains, domain_links)

    found_urls = [FoundURL(url, url_users[url], url_statuses[url], url_timestamps[url])
                  for url in url_statuses.keys() | url_users.keys()]

    logger.info(f"Found URLs, {len(found_urls)}")

    _add_found_urls_to_db_and_queue(found_urls, new_item_queue)

    with DomainLinkDatabase() as domain_link_db:
        for source_domain, target_domains in domain_links.items():
            domain_link_db.update_domain_links(source_domain, target_domains)

    end = datetime.utcnow()
    logger.info(f"Recorded URLs in database in {end - start}")


def _add_found_urls_to_db_and_queue(found_urls, new_item_queue):
    with URLDatabase() as url_db:
        new_urls = url_db.update_found_urls(found_urls)
        new_item_queue.queue_urls(new_urls)
        logger.info(f"Put {len(new_urls)} new items in the URL queue")


def add_hn_links(new_item_queue: RedisURLQueue):
    hn_count = new_item_queue.get_domain_count('news.ycombinator.com')
    max_count = get_domain_max_urls('news.ycombinator.com')

    max_item = requests.get('https://hacker-news.firebaseio.com/v0/maxitem.json').json()
    logger.info(f"Adding HN links, current count: {hn_count}, max count: {max_count}, max item ID: {max_item}")
    num_items_to_add = 100
    while hn_count < max_count:
        urls = [f'https://news.ycombinator.com/item?id={item_id}' for item_id in range(max_item, max_item - num_items_to_add, -1)]
        found_urls = [FoundURL(url, 'hn', URLStatus.NEW, datetime.now()) for url in urls]
        _add_found_urls_to_db_and_queue(found_urls, new_item_queue)
        hn_count = new_item_queue.get_domain_count('news.ycombinator.com')
        max_item -= num_items_to_add
        logger.info(f"Added {len(urls)} HN links, {hn_count} total, max item ID: {max_item}")


def process_link(user_id_hash: str, crawled_page_domain: str, link: Link, timestamp: datetime,
                 url_timestamps: dict[str, datetime], url_users: dict[str, str], blacklist_domains: set[str],
                 domain_links: dict[str, set[str]]):
    try:
        parsed_link = parse_url(link.url)
    except ValueError:
        logger.debug(f"Couldn't parse link: {link.url}")
        return

    if not isinstance(parsed_link.netloc, str):
        logger.debug(f"Couldn't find netloc: {link.url}")
        return

    if is_domain_blacklisted(parsed_link.netloc, blacklist_domains):
        logger.debug(f"Excluding link for blacklisted domain: {parsed_link}")
        return

    url_users[link.url] = user_id_hash
    url_timestamps[link.url] = timestamp
    root_url = f'{parsed_link.scheme}://{parsed_link.netloc}/'
    url_users[root_url] = user_id_hash
    url_timestamps[root_url] = timestamp
    domain_links[crawled_page_domain].add(parsed_link.netloc)


def get_datetime_from_timestamp(timestamp: float) -> datetime:
    batch_datetime = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=timestamp)
    return batch_datetime


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    redis: Redis = Redis.from_url("redis://127.0.0.1:6379", decode_responses=True)
    batch_cache = BatchCache(Path(settings.DATA_PATH) / settings.BATCH_DIR_NAME)
    url_queue = RedisURLQueue(redis, lambda: set())
    start_time = datetime.now()
    run(batch_cache, url_queue, num_batches=500)
    end_time = datetime.now()
    logger.info(f"Finished updating URLs in {end_time - start_time}")
