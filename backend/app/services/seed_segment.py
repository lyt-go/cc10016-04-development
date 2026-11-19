"""种子页段清单业务规则：分页配置解析、页段边界生成、版本冲突处理与断点续采。

主数据链：种子管理解析分页配置 -> 生成种子页段清单（页段边界与增量游标起点同步落库）
-> 增量游标读取清单，只从本段起点建立检查点并回写已消费页段。

约束：
- 页段边界与增量游标起点必须同步落库；边界先发布而游标未写入会让续采重复抓取整页，
  因此生成动作要么两者一起写入，要么整体回滚；
- 阶段只能顺向变更（待采集 -> 采集中 -> 已完成 -> 已归档），归档与恢复均按先后顺序
  留下状态记录（seq 递增）；
- 一个种子在同一时刻只能有一个可写页段，状态推进后旧页段不能再次产生新数据；
- 清单按版本优先级处理（高版本优先、同版本按页段范围升序），中断后只续做未完成项。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from app.store import store

MODULE = "seed_segment"
LIST_FIELDS = ["段编号", "关联种子", "分页参数", "页段范围", "游标起点", "当前游标", "已完成页数", "清单版本"]
REQUIRED_FIELDS = ["段编号", "关联种子", "页段范围"]
STATUS_ORDER = ["待采集", "采集中", "已完成", "已归档"]
ACTION_RULES = {"开始采集": "采集中", "标记完成": "已完成", "归档页段": "已归档"}
SPECIAL_ACTIONS = ["恢复续采"]
NEGATIVE_ACTIONS: list[str] = []
DEFAULT_SEGMENT_SIZE = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_range(text: Any) -> tuple[int, int]:
    """解析页段范围文本 ``1-20`` 为闭区间；无法解析时返回 (0, -1)。"""
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", str(text or "").strip())
    if not m:
        return 0, -1
    return int(m.group(1)), int(m.group(2))


def _version_no(text: Any) -> int:
    m = re.match(r"^[vV](\d+)$", str(text or "").strip())
    return int(m.group(1)) if m else 0


def _priority_key(seg: dict[str, Any]) -> tuple[int, int, int]:
    """版本优先级：高版本在前，同版本按页段范围升序。"""
    start, _ = _parse_range(seg.get("页段范围"))
    return (-_version_no(seg.get("清单版本")), start, _to_int(seg.get("id")))


def parse_pagination_param(text: Any) -> tuple[dict[str, Any] | None, str]:
    """解析分页参数文本为页段边界。

    支持 ``page=1-100`` 闭区间与 ``page=1,2,3`` 显式页码列表；
    开口区间（``offset=0,20,40...``）、游标型（``cursor=auto``）与非参数文本
    （``实时流``）没有可续采的页段边界，返回错误说明。
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "分页参数为空，无法生成页段边界"
    m = re.match(r"^([A-Za-z_][\w]*)\s*=\s*(.+)$", raw)
    if not m:
        return None, f"分页参数「{raw}」不是 key=value 形式的分页表达式"
    param, spec = m.group(1), m.group(2).strip()
    if spec.endswith("...") or spec.endswith("…"):
        return None, f"分页参数「{raw}」是开口区间，缺少终止页，无法生成页段边界"
    rm = re.match(r"^(\d+)\s*-\s*(\d+)$", spec)
    if rm:
        start, end = int(rm.group(1)), int(rm.group(2))
        if start > end:
            return None, f"分页区间「{spec}」起点大于终点"
        return {"param": param, "start": start, "end": end}, ""
    if re.match(r"^\d+(?:\s*,\s*\d+)*$", spec):
        values = [int(v) for v in re.split(r"\s*,\s*", spec)]
        return {"param": param, "start": min(values), "end": max(values)}, ""
    return None, f"分页参数「{raw}」不是数值区间，无法生成页段边界"


def split_page_ranges(start: int, end: int, size: int) -> list[tuple[int, int]]:
    """把闭区间 [start, end] 按 size 切成连续页段边界。"""
    step = max(1, int(size))
    ranges: list[tuple[int, int]] = []
    cur = start
    while cur <= end:
        ranges.append((cur, min(cur + step - 1, end)))
        cur += step
    return ranges


def _state_log() -> dict[str, list[dict[str, Any]]]:
    return store.pipeline_state("seed_segment_state_log") or {}


def _append_state_record(segment_id: int, action: str, from_stage: str, to_stage: str,
                         detail: str = "") -> dict[str, Any]:
    """阶段变更（含归档与恢复）按先后顺序追加状态记录，seq 单调递增。"""
    logs = _state_log()
    records = list(logs.get(str(segment_id), []))
    record = {
        "seq": len(records) + 1,
        "action": action,
        "from_stage": from_stage,
        "to_stage": to_stage,
        "at": _now(),
        "detail": detail,
    }
    records.append(record)
    logs[str(segment_id)] = records
    store.set_pipeline_state("seed_segment_state_log", logs)
    return record


class SeedSegmentService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("段编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=_priority_key)
        total = len(rows)
        start = (page - 1) * size
        enriched = [self._enrich(row) for row in rows[start:start + size]]
        return enriched, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._enrich(entry, with_records=True)

    def _enrich(self, row: dict[str, Any], with_records: bool = False) -> dict[str, Any]:
        d = dict(row)
        start, end = _parse_range(d.get("页段范围"))
        cur = _to_int(d.get("当前游标"), start)
        status = str(d.get("status") or "")
        d["_剩余页"] = max(end - cur + 1, 0) if status not in ("已完成", "已归档") else 0
        d["_可写"] = bool(d.get("writable"))
        logs = _state_log().get(str(d.get("id")), [])
        d["_状态记录数"] = len(logs)
        if with_records:
            d["_状态记录"] = logs
        return d

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        start, _ = _parse_range(entry.get("页段范围"))
        entry["游标起点"] = str(entry.get("游标起点") or "") or (str(start) if start else "0")
        entry["当前游标"] = str(entry.get("当前游标") or "") or entry["游标起点"]
        entry["已完成页数"] = str(entry.get("已完成页数") or "") or "0"
        entry["清单版本"] = str(entry.get("清单版本") or "") or "v1"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 手工登记的页段默认不可写，避免破坏"一个种子同一时刻只有一个可写页段"
        entry["writable"] = False
        rows.append(entry)
        seed = store.find_by_field("seed_config", "种子编号", str(entry.get("关联种子") or ""))
        if seed is not None:
            store.add_relation("seed_to_segment", "seed_config", int(seed["id"]), MODULE, int(entry["id"]))
        _append_state_record(int(entry["id"]), "登记页段", "", STATUS_ORDER[0], "手工登记页段")
        return entry, []

    # ------------------------------------------------------------------
    # 主数据链：种子管理 -> 生成种子页段清单
    # ------------------------------------------------------------------

    def segments_for_seed(self, seed_id: int) -> list[dict[str, Any]]:
        seed = store.find("seed_config", seed_id)
        if seed is None:
            return []
        code = str(seed.get("种子编号") or "")
        return [row for row in store.rows(MODULE) if str(row.get("关联种子") or "") == code]

    def writable_segment(self, seed_id: int) -> dict[str, Any] | None:
        for seg in self.segments_for_seed(seed_id):
            if seg.get("writable"):
                return seg
        return None

    def generate_for_seed(self, seed_id: int, segment_size: int = DEFAULT_SEGMENT_SIZE) -> dict[str, Any]:
        """解析种子分页配置生成页段边界，产出种子页段清单。

        页段边界与增量游标起点同步落库：任一写入失败，两者一起回滚，
        避免边界先发布而游标未写入导致续采重复抓取整页。
        """
        seed = store.find("seed_config", seed_id)
        if seed is None:
            return {"ok": False, "error": f"采集种子 {seed_id} 不存在或已归档", "seed_id": seed_id}
        seed_code = str(seed.get("种子编号") or "")
        page_param = str(seed.get("分页参数") or "")
        boundaries, err = parse_pagination_param(page_param)
        if boundaries is None:
            return {"ok": False, "error": err, "seed_id": seed_id}

        existing = self.segments_for_seed(seed_id)
        version = max((_version_no(seg.get("清单版本")) for seg in existing), default=0) + 1
        ranges = split_page_ranges(boundaries["start"], boundaries["end"], segment_size)

        # 冲突检测：同一分页参数已对应多个页段，且旧游标停在页段中间
        conflict_holder = None
        for seg in existing:
            if not seg.get("writable"):
                continue
            s, e = _parse_range(seg.get("页段范围"))
            cur = _to_int(seg.get("当前游标"), s)
            done = _to_int(seg.get("已完成页数"), 0)
            if str(seg.get("status")) == "采集中" and done > 0 and s <= cur <= e:
                conflict_holder = seg
                break

        rows = store.rows(MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        suffix = seed_code.split("-")[-1] or f"S{seed_id}"
        new_segments: list[dict[str, Any]] = []
        for idx, (s, e) in enumerate(ranges, start=1):
            new_segments.append({
                "id": next_id + idx - 1,
                "段编号": f"SEGM-{suffix}-V{version}-{idx:02d}",
                "关联种子": seed_code,
                "分页参数": page_param,
                "页段范围": f"{s}-{e}",
                "游标起点": str(s),
                "当前游标": str(s),
                "已完成页数": "0",
                "清单版本": f"v{version}",
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "writable": False,
            })

        if conflict_holder is not None:
            # 冲突模式：新页段挂起（不可写），旧页段保持可写，游标不动，登记版本冲突待处理
            rows.extend(new_segments)
            for seg in new_segments:
                store.add_relation("seed_to_segment", "seed_config", seed_id, MODULE, int(seg["id"]))
                _append_state_record(int(seg["id"]), "生成页段", "", STATUS_ORDER[0],
                                     f"清单版本v{version}生成，因版本冲突待处理而挂起")
            conflicts = store.pipeline_state("seed_segment_conflicts") or {}
            conflicts[str(seed_id)] = {
                "seed_id": seed_id,
                "status": "待处理",
                "分页参数": page_param,
                "旧页段": conflict_holder.get("段编号"),
                "旧页段id": int(conflict_holder["id"]),
                "旧游标位置": conflict_holder.get("当前游标"),
                "新版本": f"v{version}",
                "新页段": [seg["段编号"] for seg in new_segments],
                "可选策略": ["沿用旧游标", "新检查点"],
                "at": _now(),
            }
            store.set_pipeline_state("seed_segment_conflicts", conflicts)
            return {
                "ok": True, "conflict": True, "seed_id": seed_id, "version": f"v{version}",
                "segments": [dict(seg) for seg in new_segments],
                "message": "同一分页参数对应多个种子页段且旧游标停在页段中间，已登记版本冲突待处理",
            }

        # 正常模式：页段边界与游标起点同步落库，任一失败整体回滚
        demoted = [seg for seg in existing if seg.get("writable")]
        created_ids = {int(seg["id"]) for seg in new_segments}
        cursor_info: dict[str, Any] = {}
        rollback_cursor: Callable[[], None] | None = None
        try:
            cursor_info, rollback_cursor = self._sync_cursor_start(
                seed_id, seed_code, boundaries["param"], new_segments[0])
            for seg in demoted:
                seg["writable"] = False
            new_segments[0]["writable"] = True
            rows.extend(new_segments)
            for seg in new_segments:
                store.add_relation("seed_to_segment", "seed_config", seed_id, MODULE, int(seg["id"]))
        except Exception as exc:  # 游标起点未写入，页段边界不得单独发布
            if rollback_cursor is not None:
                rollback_cursor()
            rows[:] = [row for row in rows if int(row.get("id", 0)) not in created_ids]
            store.remove_relations("seed_to_segment", target_ids=created_ids)
            for seg in demoted:
                seg["writable"] = True
            new_segments[0]["writable"] = False
            return {"ok": False, "seed_id": seed_id,
                    "error": f"页段边界与游标起点同步落库失败，已整体回滚: {exc}"}

        for seg in demoted:
            _append_state_record(int(seg["id"]), "版本更替", str(seg.get("status") or ""),
                                 str(seg.get("status") or ""), f"被清单版本v{version}取代，转为不可写")
        for idx, seg in enumerate(new_segments):
            _append_state_record(int(seg["id"]), "生成页段", "", STATUS_ORDER[0],
                                 f"清单版本v{version}生成" + ("，首段可写" if idx == 0 else ""))
        return {
            "ok": True, "conflict": False, "seed_id": seed_id, "version": f"v{version}",
            "segments": [dict(seg) for seg in new_segments], "cursor": cursor_info,
            "message": f"种子页段清单v{version}已生成，页段边界与游标起点已同步落库",
        }

    def _sync_cursor_start(self, seed_id: int, seed_code: str, param: str,
                           segment: dict[str, Any]) -> tuple[dict[str, Any], Callable[[], None]]:
        """把增量游标起点同步落库到本段起点；返回 (游标信息, 回滚函数)。

        游标不存在时随边界一起创建——边界与游标起点必须同步落库。
        """
        from app.services.incre_cursor import IncreCursorService

        cursor_svc = IncreCursorService()
        rels = store.get_relations("seed_to_cursor", source_module="seed_config", source_id=seed_id)
        cursor = store.find("incre_cursor", int(rels[-1]["target_id"])) if rels else None
        created_id: int | None = None
        if cursor is None:
            cursor, missing = cursor_svc.create_entry({
                "标记编号": f"INCR-{seed_code.split('-')[-1] or seed_id}",
                "关联种子": seed_code,
                "增量字段": param,
                "增量类型": "页码",
                "覆写策略": "覆写",
                "更新间隔": "1h",
            })
            if missing or cursor is None:
                raise RuntimeError(f"增量游标创建失败: {missing}")
            created_id = int(cursor["id"])
            store.add_relation("seed_to_cursor", "seed_config", seed_id, "incre_cursor", created_id)

        start = str(segment.get("游标起点") or "0")
        old_pos = str(cursor.get("当前位置") or "")
        old_last = str(cursor.get("上次位置") or "")
        cursor["上次位置"] = old_pos or old_last
        cursor["当前位置"] = start
        snapshots = store.pipeline_state("cursor_snapshots") or {}
        sp = snapshots.get(str(cursor["id"]), {"checkpoints": []})
        before = len(sp.get("checkpoints", []))
        sp.setdefault("checkpoints", []).append({
            "action": "segment_start", "segment": segment.get("段编号"),
            "position": start, "previous": old_pos, "at": _now(),
        })
        snapshots[str(cursor["id"])] = sp
        store.set_pipeline_state("cursor_snapshots", snapshots)

        def _rollback() -> None:
            if created_id is not None:
                cursor_rows = store.rows("incre_cursor")
                row = store.find("incre_cursor", created_id)
                if row in cursor_rows:
                    cursor_rows.remove(row)
                store.remove_relations("seed_to_cursor", target_ids={created_id})
                snaps = store.pipeline_state("cursor_snapshots") or {}
                snaps.pop(str(created_id), None)
                store.set_pipeline_state("cursor_snapshots", snaps)
            else:
                cursor["上次位置"] = old_last
                cursor["当前位置"] = old_pos
                snaps = store.pipeline_state("cursor_snapshots") or {}
                sp2 = snaps.get(str(cursor["id"]), {"checkpoints": []})
                sp2["checkpoints"] = sp2.get("checkpoints", [])[:before]
                snaps[str(cursor["id"])] = sp2
                store.set_pipeline_state("cursor_snapshots", snaps)

        info = {"cursor_id": int(cursor["id"]), "标记编号": cursor.get("标记编号"), "起点": start}
        return info, _rollback

    # ------------------------------------------------------------------
    # 阶段流转：只能顺向变更，归档与恢复均按先后顺序留下状态记录
    # ------------------------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"种子页段 {entry_id} 不存在或已归档"
        if action in SPECIAL_ACTIONS:
            return self._restore(entry)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于种子页段可执行范围"
        current = str(entry.get("status") or STATUS_ORDER[0])
        target = ACTION_RULES[action]
        cur_idx = STATUS_ORDER.index(current) if current in STATUS_ORDER else -1
        tgt_idx = STATUS_ORDER.index(target)
        if tgt_idx <= cur_idx:
            return None, f"阶段只能顺向变更：「{current}」不能变更到「{target}」"
        if action == "开始采集":
            if not entry.get("writable"):
                return None, "一个种子同一时刻只能有一个可写页段，非可写页段不能开始采集"
            entry["status"] = target
            entry["pending"] = True
            _append_state_record(entry_id, action, current, target, "可写页段开始采集")
            return entry, f"种子页段已{action}"
        if action == "标记完成":
            if current != "采集中":
                return None, "只有采集中的页段才能标记完成"
            seed = store.find_by_field("seed_config", "种子编号", str(entry.get("关联种子") or ""))
            self._complete_segment(entry, int(seed["id"]) if seed else 0, note="人工标记完成")
            return entry, f"种子页段已{action}"
        # 归档页段
        if current != "已完成":
            return None, "只有已完成的页段才能归档"
        entry["status"] = target
        entry["pending"] = False
        _append_state_record(entry_id, action, current, target, "页段归档为历史引用")
        return entry, f"种子页段已{action}"

    def _restore(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """恢复续采：中断后只续做未完成项，按先后顺序留下状态记录。"""
        current = str(entry.get("status") or "")
        if current in ("已完成", "已归档"):
            return None, f"页段已{current}，中断恢复只续做未完成项"
        if current != "采集中":
            return None, "页段尚未开始采集，无需恢复"
        record = _append_state_record(int(entry["id"]), "恢复续采", current, current,
                                      "中断后按先后顺序恢复，从断点继续续采")
        return entry, f"种子页段已恢复续采（状态记录#{record['seq']}）"

    def _complete_segment(self, seg: dict[str, Any], seed_id: int, note: str = "") -> None:
        old = str(seg.get("status") or "")
        seg["status"] = "已完成"
        seg["pending"] = False
        seg["writable"] = False  # 状态推进后旧页段不能再次产生新数据
        _append_state_record(int(seg["id"]), "标记完成", old, "已完成", note)
        self._promote_next(seed_id)

    def _promote_next(self, seed_id: int) -> dict[str, Any] | None:
        """可写权移交：按版本优先级把可写权交给下一个待采集页段。"""
        segs = self.segments_for_seed(seed_id)
        if any(seg.get("writable") for seg in segs):
            return None
        candidates = [seg for seg in segs if str(seg.get("status")) == "待采集"]
        if not candidates:
            return None
        candidates.sort(key=_priority_key)
        nxt = candidates[0]
        nxt["writable"] = True
        _append_state_record(int(nxt["id"]), "可写移交", str(nxt.get("status") or ""),
                             str(nxt.get("status") or ""), "前序页段完成，按版本优先级取得可写权")
        seed = store.find("seed_config", seed_id)
        seed_code = str(seed.get("种子编号") or "") if seed else ""
        boundaries, _ = parse_pagination_param(str(seed.get("分页参数") or "")) if seed else (None, "")
        param = boundaries["param"] if boundaries else "page"
        self._sync_cursor_start(seed_id, seed_code, param, nxt)
        return nxt

    # ------------------------------------------------------------------
    # 版本冲突处理与中断恢复
    # ------------------------------------------------------------------

    def resolve_conflict(self, seed_id: int, strategy: str) -> dict[str, Any]:
        """处理"同一分页参数对应多个页段且旧游标停在页段中间"的版本冲突。

        - 沿用旧游标：保留当前页段的一次完整续采路径，清单显示当前页段的剩余页和已完成数量；
        - 新检查点：后续请求只覆盖新页段，旧页段转为历史引用，清单显示新页段起点和旧页段历史记录。
        """
        conflicts = store.pipeline_state("seed_segment_conflicts") or {}
        conflict = conflicts.get(str(seed_id))
        if not conflict or conflict.get("status") != "待处理":
            return {"ok": False, "error": "该种子没有待处理的版本冲突"}
        old_seg = store.find(MODULE, int(conflict["旧页段id"]))
        if old_seg is None:
            return {"ok": False, "error": "冲突旧页段不存在"}
        new_segs = [seg for seg in self.segments_for_seed(seed_id)
                    if str(seg.get("清单版本")) == str(conflict["新版本"])]
        new_segs.sort(key=_priority_key)
        if not new_segs:
            return {"ok": False, "error": "冲突新页段不存在"}

        if strategy == "沿用旧游标":
            old_seg["writable"] = True
            s, e = _parse_range(old_seg.get("页段范围"))
            cur = _to_int(old_seg.get("当前游标"), s)
            remaining = max(e - cur + 1, 0)
            done = _to_int(old_seg.get("已完成页数"), 0)
            _append_state_record(int(old_seg["id"]), "冲突处理-沿用旧游标",
                                 str(old_seg.get("status") or ""), str(old_seg.get("status") or ""),
                                 f"保留当前页段续采路径，剩余{remaining}页，已完成{done}页")
            conflict["status"] = "已解决"
            conflict["解决策略"] = strategy
            conflict["resolved_at"] = _now()
            store.set_pipeline_state("seed_segment_conflicts", conflicts)
            return {
                "ok": True, "strategy": strategy, "writable_segment": old_seg.get("段编号"),
                "剩余页": remaining, "已完成数量": done,
                "message": "已沿用旧游标，当前页段保留完整续采路径",
            }

        if strategy == "新检查点":
            old_status = str(old_seg.get("status") or "")
            old_seg["writable"] = False
            old_seg["status"] = "已归档"  # 顺向推进：旧页段转为历史引用
            old_seg["pending"] = False
            _append_state_record(int(old_seg["id"]), "冲突处理-新检查点", old_status, "已归档",
                                 "旧页段转为历史引用，不再产生新数据")
            first = new_segs[0]
            first["writable"] = True
            _append_state_record(int(first["id"]), "冲突处理-新检查点",
                                 str(first.get("status") or ""), str(first.get("status") or ""),
                                 "新页段取得可写权，后续请求只覆盖新页段")
            seed = store.find("seed_config", seed_id)
            seed_code = str(seed.get("种子编号") or "") if seed else ""
            boundaries, _ = parse_pagination_param(str(seed.get("分页参数") or "")) if seed else (None, "")
            param = boundaries["param"] if boundaries else "page"
            cursor_info, _ = self._sync_cursor_start(seed_id, seed_code, param, first)
            conflict["status"] = "已解决"
            conflict["解决策略"] = strategy
            conflict["resolved_at"] = _now()
            store.set_pipeline_state("seed_segment_conflicts", conflicts)
            return {
                "ok": True, "strategy": strategy, "writable_segment": first.get("段编号"),
                "新页段起点": first.get("游标起点"), "旧页段": old_seg.get("段编号"),
                "cursor": cursor_info,
                "message": "已建立新检查点，旧页段转为历史引用",
            }

        return {"ok": False, "error": f"未知策略「{strategy}」，可选：沿用旧游标 / 新检查点"}

    def resume_unfinished(self, seed_id: int) -> dict[str, Any]:
        """中断恢复：按版本优先级扫描清单，只续做未完成项，已完成/已归档跳过。"""
        seed = store.find("seed_config", seed_id)
        if seed is None:
            return {"ok": False, "error": f"采集种子 {seed_id} 不存在"}
        segs = sorted(self.segments_for_seed(seed_id), key=_priority_key)
        resumed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for seg in segs:
            status = str(seg.get("status") or "")
            if status in ("已完成", "已归档"):
                skipped.append({"段编号": seg.get("段编号"), "status": status})
                continue
            if status == "采集中":
                record = _append_state_record(int(seg["id"]), "恢复续采", status, status,
                                              "中断后按先后顺序恢复，从断点继续续采")
                resumed.append({"段编号": seg.get("段编号"), "status": status, "seq": record["seq"]})
            else:
                resumed.append({"段编号": seg.get("段编号"), "status": status, "seq": None})
        promoted = None
        if not any(seg.get("writable") for seg in segs):
            nxt = self._promote_next(seed_id)
            promoted = nxt.get("段编号") if nxt else None
        return {
            "ok": True, "seed_id": seed_id, "resumed": resumed, "skipped_finished": skipped,
            "promoted": promoted,
            "message": f"中断恢复完成：续做{len(resumed)}项，跳过已完成项{len(skipped)}项",
        }

    def manifest_view(self, seed_id: int) -> dict[str, Any]:
        """种子页段清单视图：按版本优先级排序，含剩余页、已完成数量与状态记录。"""
        seed = store.find("seed_config", seed_id)
        if seed is None:
            return {"ok": False, "error": f"采集种子 {seed_id} 不存在"}
        segs = sorted(self.segments_for_seed(seed_id), key=_priority_key)
        logs = _state_log()
        items = []
        for seg in segs:
            s, e = _parse_range(seg.get("页段范围"))
            cur = _to_int(seg.get("当前游标"), s)
            status = str(seg.get("status") or "")
            items.append({
                "id": int(seg["id"]),
                "段编号": seg.get("段编号"),
                "分页参数": seg.get("分页参数"),
                "页段范围": seg.get("页段范围"),
                "游标起点": seg.get("游标起点"),
                "当前游标": seg.get("当前游标"),
                "清单版本": seg.get("清单版本"),
                "status": status,
                "可写": bool(seg.get("writable")),
                "剩余页": max(e - cur + 1, 0) if status not in ("已完成", "已归档") else 0,
                "已完成数量": _to_int(seg.get("已完成页数"), 0),
                "状态记录": logs.get(str(seg["id"]), []),
            })
        conflict = (store.pipeline_state("seed_segment_conflicts") or {}).get(str(seed_id))
        return {
            "ok": True, "seed_id": seed_id, "种子编号": seed.get("种子编号"),
            "items": items, "conflict": conflict,
        }
