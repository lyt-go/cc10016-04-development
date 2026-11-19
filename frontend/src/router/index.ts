import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
const CrawlSource = () => import('@/views/crawl_source/index.vue')
const SeedConfig = () => import('@/views/seed_config/index.vue')
const SeedSegment = () => import('@/views/seed_segment/index.vue')
const ParseRule = () => import('@/views/parse_rule/index.vue')
const RateLimit = () => import('@/views/rate_limit/index.vue')
const ProxyPool = () => import('@/views/proxy_pool/index.vue')
const SessionMgr = () => import('@/views/session_mgr/index.vue')
const RetryPolicy = () => import('@/views/retry_policy/index.vue')
const FieldExtract = () => import('@/views/field_extract/index.vue')
const AntiSpider = () => import('@/views/anti_spider/index.vue')
const PageRender = () => import('@/views/page_render/index.vue')
const DataValidate = () => import('@/views/data_validate/index.vue')
const IncreCursor = () => import('@/views/incre_cursor/index.vue')
const DedupFilter = () => import('@/views/dedup_filter/index.vue')
const FormatConvert = () => import('@/views/format_convert/index.vue')
const CrawlSchedule = () => import('@/views/crawl_schedule/index.vue')
const ResultStore = () => import('@/views/result_store/index.vue')
const CrawlMonitor = () => import('@/views/crawl_monitor/index.vue')
const ContentExtract = () => import('@/views/content_extract/index.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/crawl_source', name: 'crawl_source', component: CrawlSource },
    { path: '/seed_config', name: 'seed_config', component: SeedConfig },
    { path: '/seed_segment', name: 'seed_segment', component: SeedSegment },
    { path: '/parse_rule', name: 'parse_rule', component: ParseRule },
    { path: '/rate_limit', name: 'rate_limit', component: RateLimit },
    { path: '/proxy_pool', name: 'proxy_pool', component: ProxyPool },
    { path: '/session_mgr', name: 'session_mgr', component: SessionMgr },
    { path: '/retry_policy', name: 'retry_policy', component: RetryPolicy },
    { path: '/field_extract', name: 'field_extract', component: FieldExtract },
    { path: '/anti_spider', name: 'anti_spider', component: AntiSpider },
    { path: '/page_render', name: 'page_render', component: PageRender },
    { path: '/data_validate', name: 'data_validate', component: DataValidate },
    { path: '/incre_cursor', name: 'incre_cursor', component: IncreCursor },
    { path: '/dedup_filter', name: 'dedup_filter', component: DedupFilter },
    { path: '/format_convert', name: 'format_convert', component: FormatConvert },
    { path: '/crawl_schedule', name: 'crawl_schedule', component: CrawlSchedule },
    { path: '/result_store', name: 'result_store', component: ResultStore },
    { path: '/crawl_monitor', name: 'crawl_monitor', component: CrawlMonitor },
    { path: '/content_extract', name: 'content_extract', component: ContentExtract },
  ],
})

export default router
