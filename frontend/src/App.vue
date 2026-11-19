<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">多源数据采集编排平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向数据源接入、采集编排、内容解析、反爬对抗与数据校验的全链路数据采集平台。</span>
        <span class="head-user">当前值班：{{ store.operator }} · {{ store.shiftLabel }}</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()

const navItems = [{ label: "运营概览", path: "/" }, { label: "采集源管理", path: "/crawl_source" }, { label: "种子管理", path: "/seed_config" }, { label: "种子页段", path: "/seed_segment" }, { label: "解析规则", path: "/parse_rule" }, { label: "频次控制", path: "/rate_limit" }, { label: "代理池调度", path: "/proxy_pool" }, { label: "会话管理", path: "/session_mgr" }, { label: "重试策略", path: "/retry_policy" }, { label: "字段抽取", path: "/field_extract" }, { label: "反爬对抗", path: "/anti_spider" }, { label: "渲染编排", path: "/page_render" }, { label: "数据校验", path: "/data_validate" }, { label: "增量游标", path: "/incre_cursor" }, { label: "去重过滤", path: "/dedup_filter" }, { label: "格式转换", path: "/format_convert" }, { label: "采集编排", path: "/crawl_schedule" }, { label: "结果落地", path: "/result_store" }, { label: "采集监控", path: "/crawl_monitor" }, { label: "内容抽取", path: "/content_extract" }]
</script>
