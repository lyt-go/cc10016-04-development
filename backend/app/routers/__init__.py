"""业务模块路由汇总。

这里统一按别名导入再暴露 ROUTERS：模块名有可能和内置名撞车（某个业务模块就叫 dict、list
这种名字时），按名字直接 import 会把内置类型覆盖掉，函数注解在运行时求值就会报
'module' object is not subscriptable。
"""
from __future__ import annotations

from app.routers import crawl_source as router_crawl_source
from app.routers import seed_config as router_seed_config
from app.routers import seed_segment as router_seed_segment
from app.routers import parse_rule as router_parse_rule
from app.routers import rate_limit as router_rate_limit
from app.routers import proxy_pool as router_proxy_pool
from app.routers import session_mgr as router_session_mgr
from app.routers import retry_policy as router_retry_policy
from app.routers import field_extract as router_field_extract
from app.routers import anti_spider as router_anti_spider
from app.routers import page_render as router_page_render
from app.routers import data_validate as router_data_validate
from app.routers import incre_cursor as router_incre_cursor
from app.routers import dedup_filter as router_dedup_filter
from app.routers import format_convert as router_format_convert
from app.routers import crawl_schedule as router_crawl_schedule
from app.routers import result_store as router_result_store
from app.routers import crawl_monitor as router_crawl_monitor
from app.routers import content_extract as router_content_extract
from app.routers import chain as router_chain

ROUTERS = [router_crawl_source, router_seed_config, router_seed_segment, router_parse_rule, router_rate_limit, router_proxy_pool, router_session_mgr, router_retry_policy, router_field_extract, router_anti_spider, router_page_render, router_data_validate, router_incre_cursor, router_dedup_filter, router_format_convert, router_crawl_schedule, router_result_store, router_crawl_monitor, router_content_extract, router_chain]
