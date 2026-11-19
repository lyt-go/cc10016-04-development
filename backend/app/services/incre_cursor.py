"""增量游标业务规则：断点快照、位置推进、覆写策略管理。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "incre_cursor"
LIST_FIELDS = ["标记编号", "关联种子", "增量字段", "上次位置", "当前位置", "增量类型", "覆写策略", "更新间隔"]
REQUIRED_FIELDS = ["标记编号", "关联种子", "增量字段"]
STATUS_ORDER = ["草稿", "待校验", "采集中", "已复位"]
ACTION_RULES = {"提交校验": "待校验", "标记采集中": "采集中", "复位游标": "已复位"}
NEGATIVE_ACTIONS = []


def _parse_position(pos: Any) -> tuple[int, str]:
    """解析位置值：支持整数ID或日期字符串。"""
    if pos is None:
        return 0, ""
    s = str(pos).strip()
    if not s:
        return 0, ""
    try:
        return int(s), "numeric"
    except (ValueError, TypeError):
        return 0, "string"


class IncreCursorService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("标记编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        enriched = []
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        for row in rows[start:start + size]:
            d = dict(row)
            d["_snapshot"] = snapshots.get(str(d["id"]), {})
            last = str(d.get("上次位置") or "")
            curr = str(d.get("当前位置") or "")
            try:
                progress = int(curr) - int(last) if last and curr else 0
            except (ValueError, TypeError):
                progress = 0
            d["_progress"] = progress
            enriched.append(d)
        return enriched, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        d = dict(entry)
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        d["_snapshot"] = snapshots.get(str(entry_id), {})
        return d

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["上次位置"] = values.get("上次位置") or "0"
        entry["当前位置"] = values.get("当前位置") or entry["上次位置"]
        rows.append(entry)
        # 初始化快照
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        snapshots[str(entry["id"])] = {"created_at": "2026-11-16T00:00:00Z", "checkpoints": []}
        store.set_pipeline_state("cursor_snapshots", snapshots)
        # 绑定种子
        seed_id = values.get("关联种子ID")
        if seed_id:
            try:
                sid = int(seed_id)
                store.add_relation("seed_to_cursor", "seed_config", sid, MODULE, entry["id"])
            except (ValueError, TypeError):
                pass
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"增量标记 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于增量游标可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "复位游标":
            entry["当前位置"] = entry.get("上次位置") or "0"
            snapshots = store.pipeline_state("cursor_snapshots") or {}
            sp = snapshots.get(str(entry_id), {})
            sp.setdefault("checkpoints", []).append({
                "action": "reset", "to": entry["当前位置"], "at": "2026-11-16T00:00:00Z"
            })
            snapshots[str(entry_id)] = sp
            store.set_pipeline_state("cursor_snapshots", snapshots)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"增量标记已{action}"

    def advance_cursor(self, entry_id: int, new_position: str) -> dict[str, Any]:
        """推进游标到新位置，保存断点快照。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "游标不存在"}
        old = str(entry.get("当前位置") or "0")
        entry["上次位置"] = old
        entry["当前位置"] = str(new_position)
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        sp = snapshots.get(str(entry_id), {})
        sp.setdefault("checkpoints", []).append({
            "from": old, "to": str(new_position), "at": "2026-11-16T00:00:00Z"
        })
        if len(sp["checkpoints"]) > 100:
            sp["checkpoints"] = sp["checkpoints"][-100:]
        snapshots[str(entry_id)] = sp
        store.set_pipeline_state("cursor_snapshots", snapshots)
        return {"ok": True, "old": old, "new": str(new_position)}

    def rollback_to_checkpoint(self, entry_id: int, checkpoint_index: int) -> dict[str, Any]:
        """回滚到指定检查点。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "游标不存在"}
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        sp = snapshots.get(str(entry_id), {})
        checkpoints = sp.get("checkpoints", [])
        if checkpoint_index < 0 or checkpoint_index >= len(checkpoints):
            return {"ok": False, "error": "检查点索引越界"}
        target = checkpoints[checkpoint_index]
        entry["当前位置"] = target.get("from", "0")
        return {"ok": True, "restored_to": entry["当前位置"]}

    # ------------------------------------------------------------------
    # 跨模块联动：增量游标 <-> 种子页段清单
    # ------------------------------------------------------------------

    def _find_linked_seed(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """找到游标关联的采集种子（优先关系索引，回退种子编号匹配）。"""
        rels = store.get_relations("seed_to_cursor", target_module=MODULE, target_id=int(entry["id"]))
        if rels:
            seed = store.find("seed_config", int(rels[-1]["source_id"]))
            if seed is not None:
                return seed
        return store.find_by_field("seed_config", "种子编号", str(entry.get("关联种子") or ""))

    def build_checkpoint_from_manifest(self, entry_id: int) -> dict[str, Any]:
        """读取种子页段清单，只从当前可写页段的起点建立检查点。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "增量标记不存在"}
        seed = self._find_linked_seed(entry)
        if seed is None:
            return {"ok": False, "error": "增量标记未关联采集种子"}
        from app.services.seed_segment import SeedSegmentService
        seg_svc = SeedSegmentService()
        segment = seg_svc.writable_segment(int(seed["id"]))
        if segment is None:
            return {"ok": False, "error": "种子当前没有可写页段，请先在种子管理生成页段清单"}
        start = str(segment.get("游标起点") or "0")
        old = str(entry.get("当前位置") or "")
        entry["上次位置"] = old or str(entry.get("上次位置") or "")
        entry["当前位置"] = start
        # 页段内游标不允许落在本段起点之前
        seg_cur = segment.get("当前游标")
        try:
            below_start = int(str(seg_cur)) < int(start)
        except (TypeError, ValueError):
            below_start = True
        if below_start:
            segment["当前游标"] = start
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        sp = snapshots.get(str(entry_id), {"checkpoints": []})
        sp.setdefault("checkpoints", []).append({
            "action": "checkpoint", "segment": segment.get("段编号"),
            "from": old, "to": start, "at": "2026-11-16T00:00:00Z",
        })
        snapshots[str(entry_id)] = sp
        store.set_pipeline_state("cursor_snapshots", snapshots)
        return {"ok": True, "cursor_id": entry_id, "segment": segment.get("段编号"),
                "checkpoint": start, "message": f"检查点已建立在本段起点 {start}"}

    def consume_page(self, entry_id: int, pages: int = 1) -> dict[str, Any]:
        """在可写页段内推进游标，回写已消费页段（当前游标/已完成页数/阶段）。

        一个种子同一时刻只能有一个可写页段；状态推进后旧页段不能再次产生新数据。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "增量标记不存在"}
        seed = self._find_linked_seed(entry)
        if seed is None:
            return {"ok": False, "error": "增量标记未关联采集种子"}
        from app.services.seed_segment import (
            SeedSegmentService,
            _append_state_record,
            _now,
            _parse_range,
            _to_int,
        )
        seg_svc = SeedSegmentService()
        segment = seg_svc.writable_segment(int(seed["id"]))
        if segment is None:
            return {"ok": False, "error": "种子当前没有可写页段，旧页段不能再次产生新数据"}
        status = str(segment.get("status") or "")
        if status == "待采集":
            segment["status"] = "采集中"
            segment["pending"] = True
            _append_state_record(int(segment["id"]), "开始采集", "待采集", "采集中",
                                 "游标消费自动开启页段")
        elif status != "采集中":
            return {"ok": False, "error": f"页段阶段为「{status}」，不能产生新数据"}
        start, end = _parse_range(segment.get("页段范围"))
        cur = _to_int(segment.get("当前游标"), start)
        done = _to_int(segment.get("已完成页数"), 0)
        want = max(1, int(pages))
        consumed = 0
        while consumed < want and cur <= end:
            cur += 1
            done += 1
            consumed += 1
        segment["当前游标"] = str(cur)
        segment["已完成页数"] = str(done)
        entry["上次位置"] = str(entry.get("当前位置") or "")
        entry["当前位置"] = str(cur - 1)
        segment_done = cur > end
        if segment_done:
            seg_svc._complete_segment(segment, int(seed["id"]), note="页段消费完成自动标记")
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        sp = snapshots.get(str(entry_id), {"checkpoints": []})
        sp.setdefault("checkpoints", []).append({
            "action": "consume", "segment": segment.get("段编号"),
            "consumed": consumed, "position": entry["当前位置"], "at": _now(),
        })
        snapshots[str(entry_id)] = sp
        store.set_pipeline_state("cursor_snapshots", snapshots)
        return {"ok": True, "cursor_id": entry_id, "segment": segment.get("段编号"),
                "consumed": consumed, "segment_done": segment_done,
                "当前游标": segment["当前游标"], "已完成页数": segment["已完成页数"],
                "剩余页": max(end - cur + 1, 0)}
