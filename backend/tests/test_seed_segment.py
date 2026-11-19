"""种子页段清单关键链路测试：分页解析、同步落库、阶段顺向、版本冲突与断点续采。"""
from __future__ import annotations

import pytest

from app.engine import chain_seed_segment_cursor
from app.services.incre_cursor import IncreCursorService
from app.services.seed_config import SeedConfigService
from app.services.seed_segment import (
    SeedSegmentService,
    parse_pagination_param,
    split_page_ranges,
)
from app.store import store

STAGE_ORDER = ["待采集", "采集中", "已完成", "已归档"]


def setup_function() -> None:
    """每个测试函数前重置store状态。"""
    store._tables.clear()
    store._pipeline_states.clear()
    store._relations.clear()


def _make_seed(code: str = "SEED-SEG-01", page_param: str = "page=1-10") -> dict:
    svc = SeedConfigService()
    seed, missing = svc.create_entry({
        "种子编号": code,
        "目标URL模式": "https://example.com/list?page={page}",
        "列表页XPath": "//div[@class='item']",
        "分页参数": page_param,
    })
    assert not missing
    assert seed is not None
    return seed


def _generate(seed_id: int, size: int = 20) -> dict:
    return SeedConfigService().generate_page_segments(seed_id, size)


def _segments(seed_id: int) -> list[dict]:
    return SeedSegmentService().segments_for_seed(seed_id)


# ------------------------------------------------------------------
# 分页配置解析
# ------------------------------------------------------------------

def test_parse_pagination_param_range() -> None:
    boundaries, err = parse_pagination_param("page=1-100")
    assert err == ""
    assert boundaries == {"param": "page", "start": 1, "end": 100}
    assert split_page_ranges(1, 100, 20) == [(1, 20), (21, 40), (41, 60), (61, 80), (81, 100)]


def test_parse_pagination_param_explicit_list() -> None:
    boundaries, err = parse_pagination_param("pn=1,2,3,4")
    assert err == ""
    assert boundaries == {"param": "pn", "start": 1, "end": 4}


def test_parse_pagination_param_rejects_unbounded() -> None:
    for text in ["offset=0,20,40...", "cursor=auto", "实时流", "page=100-1", ""]:
        boundaries, err = parse_pagination_param(text)
        assert boundaries is None
        assert err != ""


# ------------------------------------------------------------------
# 生成页段清单：边界与游标起点同步落库
# ------------------------------------------------------------------

def test_generate_segments_syncs_boundaries_and_cursor_start() -> None:
    seed = _make_seed(page_param="page=1-5")
    result = _generate(seed["id"], size=2)
    assert result["ok"] is True
    assert result["conflict"] is False
    assert [s["页段范围"] for s in result["segments"]] == ["1-2", "3-4", "5-5"]
    assert [s["游标起点"] for s in result["segments"]] == ["1", "3", "5"]
    assert all(s["清单版本"] == "v1" for s in result["segments"])

    # 页段边界已落库，且首段可写
    segs = _segments(seed["id"])
    assert len(segs) == 3
    writable = [s for s in segs if s.get("writable")]
    assert len(writable) == 1
    assert writable[0]["页段范围"] == "1-2"

    # 游标起点同步落库：游标与页段边界同时可查
    cursor = store.find("incre_cursor", result["cursor"]["cursor_id"])
    assert cursor is not None
    assert cursor["当前位置"] == "1"
    assert cursor["增量字段"] == "page"
    snapshots = store.pipeline_state("cursor_snapshots")
    checkpoints = snapshots[str(cursor["id"])]["checkpoints"]
    assert checkpoints[-1]["action"] == "segment_start"
    assert checkpoints[-1]["position"] == "1"

    # 跨模块关系索引一并更新
    rels = store.get_relations("seed_to_segment", source_module="seed_config", source_id=seed["id"])
    assert len(rels) == 3


def test_generate_segments_rolls_back_when_cursor_sync_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """边界先发布而游标未写入会让续采重复抓取整页：任一失败必须整体回滚。"""
    seed = _make_seed(page_param="page=1-5")

    def _boom(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError("模拟游标写入失败")

    monkeypatch.setattr(SeedSegmentService, "_sync_cursor_start", _boom)
    result = _generate(seed["id"], size=2)
    assert result["ok"] is False
    assert "回滚" in result["error"]
    # 页段边界没有单独发布
    assert store.rows("seed_segment") == []
    assert store.get_relations("seed_to_segment") == []


def test_generate_segments_rejects_unparsable_param() -> None:
    seed = _make_seed(page_param="实时流")
    result = _generate(seed["id"])
    assert result["ok"] is False
    assert store.rows("seed_segment") == []


# ------------------------------------------------------------------
# 阶段只能顺向变更；归档与恢复按先后顺序留下状态记录
# ------------------------------------------------------------------

def test_stage_transitions_forward_only() -> None:
    seed = _make_seed(page_param="page=1-2")
    result = _generate(seed["id"], size=2)
    seg_id = result["segments"][0]["id"]
    svc = SeedSegmentService()

    entry, msg = svc.run_action(seg_id, "开始采集")
    assert entry is not None and entry["status"] == "采集中"
    entry, msg = svc.run_action(seg_id, "标记完成")
    assert entry is not None and entry["status"] == "已完成"
    entry, msg = svc.run_action(seg_id, "归档页段")
    assert entry is not None and entry["status"] == "已归档"

    # 逆向变更被拒绝
    entry, msg = svc.run_action(seg_id, "开始采集")
    assert entry is None
    assert "顺向" in msg
    # 已归档页段不能恢复续采
    entry, msg = svc.run_action(seg_id, "恢复续采")
    assert entry is None
    assert "未完成" in msg


def test_archive_and_restore_leave_ordered_state_records() -> None:
    seed = _make_seed(page_param="page=1-2")
    result = _generate(seed["id"], size=2)
    seg_id = result["segments"][0]["id"]
    svc = SeedSegmentService()

    svc.run_action(seg_id, "开始采集")
    entry, msg = svc.run_action(seg_id, "恢复续采")
    assert entry is not None and entry["status"] == "采集中"
    svc.run_action(seg_id, "标记完成")
    svc.run_action(seg_id, "归档页段")

    detail = svc.get_entry(seg_id)
    records = detail["_状态记录"]
    seqs = [r["seq"] for r in records]
    assert seqs == sorted(seqs)  # 状态记录按先后顺序排列
    actions = [r["action"] for r in records]
    assert actions == ["生成页段", "开始采集", "恢复续采", "标记完成", "归档页段"]
    # 归档与恢复都留下了状态记录
    assert any(r["action"] == "恢复续采" for r in records)
    assert any(r["action"] == "归档页段" for r in records)
    # 阶段只顺向推进
    stage_idx = [STAGE_ORDER.index(r["to_stage"]) for r in records if r["to_stage"]]
    assert stage_idx == sorted(stage_idx)


# ------------------------------------------------------------------
# 一个种子同一时刻只能有一个可写页段
# ------------------------------------------------------------------

def test_single_writable_segment_invariant() -> None:
    seed = _make_seed(page_param="page=1-4")
    _generate(seed["id"], size=2)          # v1
    result = _generate(seed["id"], size=2)  # v2（无消费，无冲突）
    assert result["ok"] is True and result["conflict"] is False

    segs = _segments(seed["id"])
    writable = [s for s in segs if s.get("writable")]
    assert len(writable) == 1
    assert writable[0]["清单版本"] == "v2"
    # v1 旧页段全部不可写
    assert all(not s.get("writable") for s in segs if s["清单版本"] == "v1")


def test_superseded_segment_cannot_produce_new_data() -> None:
    seed = _make_seed(page_param="page=1-4")
    v1 = _generate(seed["id"], size=2)
    old_seg_id = v1["segments"][0]["id"]
    _generate(seed["id"], size=2)  # v2 取代 v1

    svc = SeedSegmentService()
    entry, msg = svc.run_action(old_seg_id, "开始采集")
    assert entry is None
    assert "可写" in msg

    # 游标消费只落在新的可写页段上
    cursor_svc = IncreCursorService()
    cursor_id = _segments(seed["id"])[0] and store.find_all_by_field("incre_cursor", "关联种子", seed["种子编号"])[0]["id"]
    consume = cursor_svc.consume_page(cursor_id)
    assert consume["ok"] is True
    assert "-V2-" in consume["segment"]


# ------------------------------------------------------------------
# 版本冲突：同一分页参数对应多个页段，旧游标停在页段中间
# ------------------------------------------------------------------

def _make_conflicted_seed() -> tuple[dict, int]:
    """构造冲突场景：v1 页段消费到一半，再次生成同参数页段。"""
    seed = _make_seed(page_param="page=1-10")
    gen = _generate(seed["id"], size=10)
    cursor_id = gen["cursor"]["cursor_id"]
    cursor_svc = IncreCursorService()
    consumed = cursor_svc.consume_page(cursor_id, pages=4)
    assert consumed["ok"] is True and consumed["consumed"] == 4
    gen2 = _generate(seed["id"], size=10)
    assert gen2["ok"] is True and gen2["conflict"] is True
    return seed, cursor_id


def test_conflict_detected_when_cursor_stops_mid_segment() -> None:
    seed, cursor_id = _make_conflicted_seed()
    cursor = store.find("incre_cursor", cursor_id)
    assert cursor["当前位置"] == "4"  # 旧游标停在页段中间，未被新边界覆盖
    conflict = store.pipeline_state("seed_segment_conflicts")[str(seed["id"])]
    assert conflict["status"] == "待处理"
    assert conflict["旧游标位置"] == "5"  # 下一个待采页
    # 冲突期间仍只有一个可写页段（旧页段）
    writable = [s for s in _segments(seed["id"]) if s.get("writable")]
    assert len(writable) == 1
    assert writable[0]["清单版本"] == "v1"


def test_conflict_resolve_keep_old_cursor() -> None:
    """沿用旧游标：保留当前页段的一次完整续采路径，清单显示剩余页和已完成数量。"""
    seed, cursor_id = _make_conflicted_seed()
    svc = SeedSegmentService()
    result = svc.resolve_conflict(seed["id"], "沿用旧游标")
    assert result["ok"] is True
    assert result["剩余页"] == 6      # 10 - 5 + 1
    assert result["已完成数量"] == 4

    view = svc.manifest_view(seed["id"])
    old = next(i for i in view["items"] if i["清单版本"] == "v1")
    new = next(i for i in view["items"] if i["清单版本"] == "v2")
    assert old["可写"] is True
    assert old["剩余页"] == 6
    assert old["已完成数量"] == 4
    assert new["可写"] is False and new["status"] == "待采集"

    # 旧游标不动，续采路径完整
    cursor = store.find("incre_cursor", cursor_id)
    assert cursor["当前位置"] == "4"
    consume = IncreCursorService().consume_page(cursor_id)
    assert consume["ok"] is True and consume["segment"] == old["段编号"]


def test_conflict_resolve_new_checkpoint() -> None:
    """新检查点：后续请求只覆盖新页段，旧页段转为历史引用。"""
    seed, cursor_id = _make_conflicted_seed()
    svc = SeedSegmentService()
    result = svc.resolve_conflict(seed["id"], "新检查点")
    assert result["ok"] is True
    assert result["新页段起点"] == "1"

    view = svc.manifest_view(seed["id"])
    old = next(i for i in view["items"] if i["清单版本"] == "v1")
    new = next(i for i in view["items"] if i["清单版本"] == "v2")
    # 旧页段转为历史引用，不再产生新数据
    assert old["status"] == "已归档"
    assert old["可写"] is False
    assert any(r["action"] == "冲突处理-新检查点" for r in old["状态记录"])
    # 清单显示新页段起点
    assert new["可写"] is True
    assert new["游标起点"] == "1"

    # 游标检查点重置到新页段起点，旧位置留痕
    cursor = store.find("incre_cursor", cursor_id)
    assert cursor["当前位置"] == "1"
    assert cursor["上次位置"] == "4"

    # 后续请求只覆盖新页段
    consume = IncreCursorService().consume_page(cursor_id)
    assert consume["ok"] is True and consume["segment"] == new["段编号"]


def test_conflict_resolve_rejects_unknown_strategy() -> None:
    seed, _ = _make_conflicted_seed()
    result = SeedSegmentService().resolve_conflict(seed["id"], "随便处理")
    assert result["ok"] is False
    assert "可选" in result["error"]


# ------------------------------------------------------------------
# 中断恢复：只续做未完成项；清单按版本优先级处理
# ------------------------------------------------------------------

def test_resume_unfinished_only() -> None:
    seed = _make_seed(page_param="page=1-4")
    gen = _generate(seed["id"], size=2)
    cursor_id = gen["cursor"]["cursor_id"]
    cursor_svc = IncreCursorService()
    cursor_svc.consume_page(cursor_id, pages=2)  # 第一段消费完成，自动移交第二段
    cursor_svc.consume_page(cursor_id)           # 第二段消费一页后中断

    svc = SeedSegmentService()
    result = svc.resume_unfinished(seed["id"])
    assert result["ok"] is True
    # 只续做未完成项：第一段已完成被跳过
    assert len(result["skipped_finished"]) == 1
    assert result["skipped_finished"][0]["status"] == "已完成"
    assert len(result["resumed"]) == 1
    assert result["resumed"][0]["status"] == "采集中"

    # 恢复动作留下了有序状态记录
    view = svc.manifest_view(seed["id"])
    active = next(i for i in view["items"] if i["status"] == "采集中")
    seqs = [r["seq"] for r in active["状态记录"]]
    assert seqs == sorted(seqs)
    assert any(r["action"] == "恢复续采" for r in active["状态记录"])


def test_manifest_sorted_by_version_priority() -> None:
    seed = _make_seed(page_param="page=1-4")
    _generate(seed["id"], size=2)  # v1
    _generate(seed["id"], size=2)  # v2
    view = SeedSegmentService().manifest_view(seed["id"])
    versions = [i["清单版本"] for i in view["items"]]
    assert versions == ["v2", "v2", "v1", "v1"]
    # 同版本内按页段范围升序
    v2_ranges = [i["页段范围"] for i in view["items"] if i["清单版本"] == "v2"]
    assert v2_ranges == ["1-2", "3-4"]


# ------------------------------------------------------------------
# 增量游标：只从本段起点建立检查点，回写已消费页段
# ------------------------------------------------------------------

def test_checkpoint_only_built_from_segment_start() -> None:
    seed = _make_seed(page_param="page=1-5")
    gen = _generate(seed["id"], size=5)
    cursor_id = gen["cursor"]["cursor_id"]
    # 模拟游标被弄脏
    cursor = store.find("incre_cursor", cursor_id)
    cursor["当前位置"] = "99"

    result = IncreCursorService().build_checkpoint_from_manifest(cursor_id)
    assert result["ok"] is True
    assert result["checkpoint"] == "1"  # 只从本段起点建立检查点
    cursor = store.find("incre_cursor", cursor_id)
    assert cursor["当前位置"] == "1"
    snapshots = store.pipeline_state("cursor_snapshots")
    assert snapshots[str(cursor_id)]["checkpoints"][-1]["segment"] == result["segment"]


def test_consume_writes_back_and_promotes_next_segment() -> None:
    seed = _make_seed(page_param="page=1-2")
    gen = _generate(seed["id"], size=1)  # 两段：1-1、2-2
    cursor_id = gen["cursor"]["cursor_id"]
    cursor_svc = IncreCursorService()

    first = cursor_svc.consume_page(cursor_id)
    assert first["ok"] is True and first["segment_done"] is True
    segs = sorted(_segments(seed["id"]), key=lambda s: s["页段范围"])
    assert segs[0]["status"] == "已完成"
    assert segs[0]["writable"] is False  # 状态推进后旧页段不能再次产生新数据
    assert segs[1]["writable"] is True   # 可写权移交
    # 游标检查点同步到新段起点
    cursor = store.find("incre_cursor", cursor_id)
    assert cursor["当前位置"] == "2"

    second = cursor_svc.consume_page(cursor_id)
    assert second["segment_done"] is True
    # 全部完成后没有可写页段，继续消费被拒绝
    third = cursor_svc.consume_page(cursor_id)
    assert third["ok"] is False
    assert "可写页段" in third["error"]


# ------------------------------------------------------------------
# 场景验收：为含多页参数的种子建立页段，推进一次游标后重开页面，
# 核对页段范围、起点和状态顺序
# ------------------------------------------------------------------

def test_multi_page_seed_segment_lifecycle_after_reopen() -> None:
    # 1) 为含多页参数的种子建立页段
    seed = _make_seed(code="SEED-SEG-REOPEN", page_param="page=1-5")
    gen = _generate(seed["id"], size=2)
    assert gen["ok"] is True and gen["conflict"] is False
    assert [s["页段范围"] for s in gen["segments"]] == ["1-2", "3-4", "5-5"]
    cursor_id = gen["cursor"]["cursor_id"]

    # 2) 推进一次游标
    consume = IncreCursorService().consume_page(cursor_id)
    assert consume["ok"] is True and consume["consumed"] == 1

    # 3) 重开页面：重新实例化服务，从仓库重新读取清单
    seg_svc = SeedSegmentService()
    view = seg_svc.manifest_view(seed["id"])
    assert view["ok"] is True

    # 4) 核对页段范围与起点
    ranges = [(i["页段范围"], i["游标起点"]) for i in view["items"]]
    assert ranges == [("1-2", "1"), ("3-4", "3"), ("5-5", "5")]

    first = view["items"][0]
    assert first["status"] == "采集中"
    assert first["当前游标"] == "2"      # 下一待采页
    assert first["已完成数量"] == 1
    assert first["剩余页"] == 1
    assert first["可写"] is True

    # 5) 核对状态顺序：记录按 seq 递增，阶段只顺向推进
    records = first["状态记录"]
    seqs = [r["seq"] for r in records]
    assert seqs == sorted(seqs)
    stage_idx = [STAGE_ORDER.index(r["to_stage"]) for r in records if r["to_stage"]]
    assert stage_idx == sorted(stage_idx)
    assert [r["action"] for r in records] == ["生成页段", "开始采集"]

    # 游标侧同样可核对：检查点建立在本段起点
    cursor = store.find("incre_cursor", cursor_id)
    assert cursor["当前位置"] == "1"


# ------------------------------------------------------------------
# 链路10：种子管理 -> 种子页段清单 -> 增量游标
# ------------------------------------------------------------------

def test_chain_seed_segment_cursor() -> None:
    seed = _make_seed(page_param="page=1-3")
    result = chain_seed_segment_cursor(seed["id"], segment_size=2)
    assert result["ok"] is True
    assert result["generate"]["conflict"] is False
    # 游标读取清单，从本段起点建立检查点
    assert result["checkpoint"]["ok"] is True
    assert result["checkpoint"]["checkpoint"] == "1"
    # 种子页段清单一并更新
    items = result["manifest"]["items"]
    assert [i["页段范围"] for i in items] == ["1-2", "3-3"]
    assert items[0]["可写"] is True


def test_chain_seed_segment_cursor_reports_conflict() -> None:
    seed = _make_seed(page_param="page=1-3")
    first = chain_seed_segment_cursor(seed["id"], segment_size=3)
    cursor_id = first["generate"]["cursor"]["cursor_id"]
    IncreCursorService().consume_page(cursor_id)  # 旧游标停在页段中间
    second = chain_seed_segment_cursor(seed["id"], segment_size=3)
    assert second["ok"] is True
    assert second["generate"]["conflict"] is True
    assert "冲突" in second["message"]
    assert second["manifest"]["conflict"]["status"] == "待处理"


def test_chain_seed_segment_cursor_missing_seed() -> None:
    result = chain_seed_segment_cursor(9999)
    assert result["ok"] is False
    assert "不存在" in result["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
