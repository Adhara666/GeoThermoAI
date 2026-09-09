# -*- coding: utf-8 -*-
"""理解层（第二阶段）验收测试。

对应升级方案 9.2 的验收条款与总体技术方案 §4：

  组 1  候选操作契约：枚举外内容一律判无效，共享修饰不做笛卡尔积
  组 2  时间解析：以消息接收时间为锚、单日不扩月、模糊时间先追问
  组 3  槽位账本：默认值来源如实、否定后历史不回填
  组 4  目标绑定：先收全部候选再判唯一，多个合理目标就追问
  组 5  验收①「武汉 7 月、南京 8 月，都是 2025 年，月度产品」两个任务不串
  组 6  验收②「不是武汉」清除并留否定记录，后续不再出现武汉
  组 7  验收③「武汉去年 7 月做 LST」用锚点日期，不拿当前年冒充
  组 8  验收④「只下载 Sentinel-2」只下载，不自动训练
  组 9  验收⑤ Chat 模式只回答，不建任何任务
  组 10 重启加分项：追问后重启，问题还在，回答后任务继续
  组 11 答案一次消费：过期卡片不重复执行
  组 12 失败兜底：模型不可用时不猜城市、不默认下载

模型调用被替换成**脚本化桩**：既能确定性复跑，又真实走过
「模型输出 → JSON 解析 → 枚举校验 → 绑定 → 落台账」的全链路。
真实模型输出见 tests/e2e_stage2_understanding.py 的联机记录。

运行（无需 pytest）：python tests/test_understanding.py
"""

import datetime
import json
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.state_kernel import (  # noqa: E402
    StateStore,
    questions as q_store,
    receive_command,
    tasks as t_store,
)
from core.agent import understanding  # noqa: E402
from core.agent.understanding import (  # noqa: E402
    binding,
    capabilities,
    operations as ops,
    resolution as res,
    timeparse,
)
from core.agent.understanding.slotbook import (  # noqa: E402
    SRC_DEFAULT,
    SRC_USER,
    SlotBook,
)
from core.agent.understanding.understander import (  # noqa: E402
    CandidateUnderstander,
    enforce_chat_mode,
)

PASS: list = []
FAIL: list = []

ANCHOR = datetime.datetime(2026, 9, 10, 4, 0, tzinfo=datetime.timezone.utc)
ANCHOR_DATE = datetime.date(2026, 9, 10)   # UTC+8 下的本地日期


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


# ── 测试替身 ─────────────────────────────────────────────────────


class StubAssistant:
    """脚本化模型：按顺序吐出预置回复，记录收到的 system prompt。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def _call_api(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else "API调用失败: 脚本已用尽"


def make_understander(replies):
    return CandidateUnderstander(StubAssistant(replies))


def write_geojson(path: Path, name: str):
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature",
                      "properties": {"name": name},
                      "geometry": {"type": "Polygon", "coordinates": [[
                          [114.0, 30.4], [114.6, 30.4],
                          [114.6, 30.8], [114.0, 30.8], [114.0, 30.4]]]}}],
    }, ensure_ascii=False), encoding="utf-8")


class Harness:
    """一套「台账 + 研究区目录 + 桩模型」的最小环境。"""

    def __init__(self, tmp: Path, name: str, areas=("武汉市_市", "南京市_市")):
        self.db = tmp / f"{name}.sqlite3"
        self.store = StateStore(self.db)
        self.areas_dir = tmp / f"{name}_areas"
        self.areas_dir.mkdir(parents=True, exist_ok=True)
        for area in areas:
            write_geojson(self.areas_dir / f"{area}.geojson", area)
        self.user = "u1"
        self.project = "p1"
        receipt = receive_command(
            self.store, user_id=self.user, project_id=self.project,
            conversation_id="c1", message="（建立对话）",
            dedup_key=f"seed-{name}", operation_type="chat.command")
        self.conv_pk = receipt.conversation_id

    def area_paths(self):
        return sorted(self.areas_dir.glob("*.geojson"))

    def ctx(self, message: str, chat_mode: str = "work"):
        ledger = understanding.load_ledger_context(
            self.store, user_id=self.user, conversation_id=self.conv_pk)
        return understanding.build_resolve_context(
            message=message, received_at=ANCHOR, tz_offset=8.0,
            chat_mode=chat_mode, study_area_paths=self.area_paths(),
            ledger=ledger)

    def send(self, message: str, reply: str, chat_mode: str = "work"):
        agent = make_understander([reply])
        ctx = self.ctx(message, chat_mode)
        return understanding.handle_message(
            self.store, agent, user_id=self.user, project_id=self.project,
            conversation_id=self.conv_pk, message=message, ctx=ctx)

    def tasks(self):
        return self.store.read(lambda c: t_store.list_tasks(
            c, user_id=self.user, conversation_id=self.conv_pk))

    def open_questions(self):
        return self.store.read(lambda c: q_store.list_open_questions(
            c, user_id=self.user, conversation_id=self.conv_pk))

    def reopen(self):
        """模拟服务重启：关库重开，内存态全丢，只剩磁盘上的台账。"""
        self.store.close()
        self.store = StateStore(self.db)

    def close(self):
        self.store.close()


def reply(operations, shared=None, note=""):
    return json.dumps({"operations": operations,
                       "shared_modifiers": shared or [],
                       "note": note}, ensure_ascii=False)


# ── 组 1：候选操作契约 ───────────────────────────────────────────


def test_contract():
    print("测试组 1：候选操作契约（枚举外内容一律判无效）")
    good = ops.parse_batch(json.loads(reply([{
        "op": "create", "label": "A", "capability": "full_lst",
        "patches": {"region": {"action": "set", "value": "武汉"}},
    }])))
    check("合法候选解析通过", good.valid, str(good.errors))

    bad_op = ops.parse_batch({"operations": [{"op": "run_pipeline"}]})
    check("未注册的操作类型被拒绝",
          not bad_op.valid and any("run_pipeline" in e for e in bad_op.errors),
          str(bad_op.errors))

    bad_cap = ops.parse_batch({"operations": [
        {"op": "create", "capability": "train_xgboost"}]})
    check("未注册的能力被拒绝",
          not bad_cap.valid and any("train_xgboost" in e for e in bad_cap.errors),
          str(bad_cap.errors))

    bad_field = ops.parse_batch({"operations": [{
        "op": "create", "capability": "full_lst",
        "patches": {"output_path": {"action": "set", "value": "/tmp/x.tif"}}}]})
    check("模型编造的文件路径字段被拒绝",
          any("output_path" in e for e in bad_field.errors),
          str(bad_field.errors))

    flood = ops.parse_batch({"operations": [
        {"op": "create", "capability": "full_lst"} for _ in range(20)]})
    check("候选数量超上限整批判无效", not flood.valid, str(flood.errors))

    # 共享修饰：缺 applies_to 整条丢弃，绝不广播到全部目标
    no_scope = ops.parse_batch({
        "operations": [{"op": "create", "label": "A", "capability": "full_lst"},
                       {"op": "create", "label": "B", "capability": "full_lst"}],
        "shared_modifiers": [{"patches": {"time": {"action": "set",
                                                   "value": "2025 年"}}}],
    })
    check("共享修饰没写作用范围时被丢弃（不做笛卡尔积）",
          not no_scope.shared
          and any("作用于哪些目标" in w for w in no_scope.warnings),
          str(no_scope.warnings))
    check("丢弃一条修饰不会连累其余候选",
          no_scope.valid and len(no_scope.operations) == 2, str(no_scope.errors))

    scoped = ops.apply_shared_modifiers(ops.parse_batch({
        "operations": [
            {"op": "create", "label": "A", "capability": "full_lst",
             "patches": {"time": {"action": "set", "value": "7 月"}}},
            {"op": "create", "label": "B", "capability": "full_lst"}],
        "shared_modifiers": [{"patches": {"model": {"action": "set",
                                                    "value": "rf"}},
                              "applies_to": ["B"]}],
    }))
    a = next(o for o in scoped.operations if o.label == "A")
    b = next(o for o in scoped.operations if o.label == "B")
    check("共享修饰只落到列出的标签上",
          a.patch_for(ops.F_MODEL) is None
          and b.patch_for(ops.F_MODEL) is not None)


# ── 组 2：时间解析 ───────────────────────────────────────────────


def test_time():
    print("测试组 2：时间解析（锚点 / 单日不扩月 / 模糊先问）")
    single = timeparse.resolve("2025-07-15", anchor_date=ANCHOR_DATE,
                               tz_offset=8.0)
    check("单日保持单日，不扩成整月",
          single.ok and single.start == "2025-07-15" and single.end == "2025-07-15",
          f"实际 {single.start}~{single.end}")

    month = timeparse.resolve("2025 年 7 月", anchor_date=ANCHOR_DATE,
                              tz_offset=8.0)
    check("整月解析为完整自然月",
          month.ok and month.start == "2025-07-01" and month.end == "2025-07-31",
          f"实际 {month.start}~{month.end}")
    check("整月判定正确", timeparse.is_whole_month(month.start, month.end))

    last_year = timeparse.resolve("去年 7 月", anchor_date=ANCHOR_DATE,
                                  tz_offset=8.0)
    check("相对年按锚点解析，不拿当前年冒充",
          last_year.ok and last_year.year == ANCHOR_DATE.year - 1,
          f"实际 {last_year.year}")
    check("锚点日期随解析结果存档",
          last_year.anchor_date == ANCHOR_DATE.isoformat()
          and last_year.relative is True)

    for expr in ("去年夏天", "最近", "2025 年"):
        vague = timeparse.resolve(expr, anchor_date=ANCHOR_DATE, tz_offset=8.0)
        check(f"模糊时间「{expr}」不放行、给出追问理由",
              (not vague.ok) and bool(vague.reason), f"实际 {vague}")

    future = timeparse.resolve("2030-07-15", anchor_date=ANCHOR_DATE,
                               tz_offset=8.0)
    check("未来日期被拒绝", not future.ok, str(future))

    # 跨时区锚点：UTC 15:00 在 UTC+8 已是次日
    late = datetime.datetime(2026, 9, 10, 17, 0, tzinfo=datetime.timezone.utc)
    check("锚点按用户时区换算",
          timeparse.anchor_from(late, 8.0) == datetime.date(2026, 9, 11))


# ── 组 3：槽位账本 ───────────────────────────────────────────────


def test_slotbook():
    print("测试组 3：槽位账本（来源如实 / 否定不回填）")
    book = SlotBook().fill_default(ops.F_MODEL, "rf", SRC_DEFAULT)
    check("默认值来源记为默认且未确认",
          book.source(ops.F_MODEL) == SRC_DEFAULT
          and book.confirmed(ops.F_MODEL) is False)

    book = book.set(ops.F_REGION, "武汉市_市", SRC_USER, evidence="做武汉")
    check("用户明确值标为已确认", book.confirmed(ops.F_REGION))

    cleared = book.clear(ops.F_REGION, evidence="不是武汉")
    check("清除后字段为空", not cleared.has_value(ops.F_REGION))
    check("清除留下否定记录",
          any(n["value"] == "武汉市_市" for n in cleared.negations(ops.F_REGION)))

    refilled = cleared.set(ops.F_REGION, "武汉市_市", "memory")
    check("历史来源不能把被否定的值填回来",
          not refilled.has_value(ops.F_REGION))

    echoed = cleared.set(ops.F_REGION, "武汉市_市", SRC_USER, override=True)
    check("模型标了「用户明确」也挡得住（没有原句依据）",
          not echoed.has_value(ops.F_REGION))

    reaffirmed = cleared.set(ops.F_REGION, "武汉市_市", SRC_USER,
                             override=True, allow_negated=True)
    check("用户本轮重申可以撤销否定",
          reaffirmed.value(ops.F_REGION) == "武汉市_市"
          and not reaffirmed.negations(ops.F_REGION))

    from core.agent.understanding.slotbook import grounded_in_message
    patch = ops.FieldPatch(field=ops.F_REGION, action=ops.PATCH_SET,
                           value="武汉市_市")
    check("原句提到才算本轮明确重申",
          grounded_in_message(patch, "还是用武汉吧")
          and not grounded_in_message(patch, "那就按 2025 年 8 月来吧"))

    low = SlotBook().set(ops.F_MODEL, "rf", SRC_USER).set(ops.F_MODEL, "x",
                                                          SRC_DEFAULT)
    check("低优先级来源不覆盖用户明确值", low.value(ops.F_MODEL) == "rf")


# ── 组 4：目标绑定 ───────────────────────────────────────────────


def test_binding(tmp: Path):
    print("测试组 4：目标绑定（先收全部候选再判唯一）")
    areas = tmp / "bind_areas"
    areas.mkdir(exist_ok=True)
    for name in ("武汉市_市", "武汉市_江夏区", "南京市_市"):
        write_geojson(areas / f"{name}.geojson", name)
    paths = sorted(areas.glob("*.geojson"))

    ambiguous = binding.bind_region("武汉", paths)
    check("同名前缀多个候选判为歧义（不取第一个）",
          ambiguous.kind == binding.BIND_AMBIGUOUS and len(ambiguous.options) == 2,
          f"实际 {ambiguous.kind}/{len(ambiguous.options)}")

    unique = binding.bind_region("南京市_市", paths)
    check("精确命中唯一绑定并带内容指纹",
          unique.kind == binding.BIND_UNIQUE and len(unique.content_hash) == 64)

    missing = binding.bind_region("上海", paths)
    check("找不到时不猜，列出已有候选",
          missing.kind == binding.BIND_MISSING and len(missing.options) == 3)

    check("序号指称解析", binding.parse_ordinal("第二个") == 2
          and binding.parse_ordinal("第 3 组") == 3)

    tasks = [{"id": "t1", "label": "武汉 A", "capability": "full_lst"},
             {"id": "t2", "label": "南京 B", "capability": "full_lst"}]
    check("按序号绑定任务",
          binding.bind_task("第二个", tasks).task["id"] == "t2")
    check("按地区绑定任务",
          binding.bind_task("南京", tasks).task["id"] == "t2")
    check("指代不唯一时追问",
          binding.bind_task("这张图", tasks).kind == binding.BIND_AMBIGUOUS)


# ── 组 5：验收① 多任务不串 ──────────────────────────────────────


def test_two_cities(tmp: Path):
    print("测试组 5：验收①「武汉 7 月、南京 8 月，都是 2025 年，月度产品」")
    h = Harness(tmp, "two_cities")
    message = "武汉 7 月、南京 8 月，都是 2025 年，月度产品"
    model_reply = reply(
        [
            {"op": "create", "label": "A", "capability": "full_lst",
             "target_ref": "武汉",
             "patches": {"region": {"action": "set", "value": "武汉市_市",
                                    "evidence": "武汉 7 月"},
                         "time": {"action": "set", "value": "7 月",
                                  "evidence": "武汉 7 月"},
                         "product_mode": {"action": "set", "value": "monthly",
                                          "evidence": "月度产品"}}},
            {"op": "create", "label": "B", "capability": "full_lst",
             "target_ref": "南京",
             "patches": {"region": {"action": "set", "value": "南京市_市",
                                    "evidence": "南京 8 月"},
                         "time": {"action": "set", "value": "8 月",
                                  "evidence": "南京 8 月"},
                         "product_mode": {"action": "set", "value": "monthly",
                                          "evidence": "月度产品"}}},
        ],
        shared=[{"patches": {"time": {"action": "set", "value": "2025 年"}},
                 "applies_to": []}],
    )
    # 共享修饰这里故意不写作用范围 → 被丢弃；模型已把月份写进各自补丁，
    # 但缺年份，所以先看会不会追问年份（这正是「不拿当前年冒充」）
    result = h.send(message, model_reply)
    check("缺年份时不放行，先追问", result.kind == understanding.KIND_ASK,
          f"实际 {result.kind}：{result.message}")
    check("不拿当前年补齐缺失的年份",
          all(not (SlotBook(t["slots"]).value(ops.F_TIME) or {}).get("start")
              for t in h.tasks()),
          str([SlotBook(t["slots"]).value(ops.F_TIME) for t in h.tasks()]))

    # 正确写法：共享修饰带 applies_to
    h2 = Harness(tmp, "two_cities_ok")
    ok_reply = reply(
        [
            {"op": "create", "label": "A", "capability": "full_lst",
             "patches": {"region": {"action": "set", "value": "武汉市_市"},
                         "time": {"action": "set", "value": "2025 年 7 月"},
                         "product_mode": {"action": "set", "value": "monthly"}}},
            {"op": "create", "label": "B", "capability": "full_lst",
             "patches": {"region": {"action": "set", "value": "南京市_市"},
                         "time": {"action": "set", "value": "2025 年 8 月"},
                         "product_mode": {"action": "set", "value": "monthly"}}},
        ])
    result2 = h2.send(message, ok_reply)
    tasks = h2.tasks()
    check("一条消息登记两个独立任务", len(tasks) == 2, f"实际 {len(tasks)}")

    by_region = {}
    for task in tasks:
        book = SlotBook(task["slots"])
        by_region[str(book.value(ops.F_REGION))] = book

    wuhan = by_region.get("武汉市_市")
    nanjing = by_region.get("南京市_市")
    check("两个任务分别绑定到武汉与南京",
          wuhan is not None and nanjing is not None, str(list(by_region)))
    if wuhan and nanjing:
        wt, nt = wuhan.value(ops.F_TIME), nanjing.value(ops.F_TIME)
        check("武汉绑定 2025 年 7 月",
              wt["start"] == "2025-07-01" and wt["end"] == "2025-07-31",
              str(wt))
        check("南京绑定 2025 年 8 月（月份不串）",
              nt["start"] == "2025-08-01" and nt["end"] == "2025-08-31",
              str(nt))
        check("两个任务的边界文件各自独立",
              (wuhan.get(ops.F_REGION)["detail"]["content_hash"]
               != nanjing.get(ops.F_REGION)["detail"]["content_hash"]))
        check("月度产品方式被记住",
              wuhan.value(ops.F_PRODUCT_MODE) == "monthly"
              and nanjing.value(ops.F_PRODUCT_MODE) == "monthly")
    check("两个任务都判为信息齐全",
          all(t["summary_status"] == t_store.TASK_READY for t in tasks),
          str([t["summary_status"] for t in tasks]))
    check("信息齐全时不追问", not h2.open_questions())

    # 交给执行链的参数与槽位逐项一致（§5.3）
    if tasks:
        spec = understanding.ResolvedTask(
            {**tasks[0], "task_id": tasks[0]["id"]})
        plan = spec.to_plan()
        acq = next(s for s in plan["steps"] if s["skill"] == "data_acquisition")
        check("执行参数与已确认槽位一致",
              acq["params"]["start_date"] == plan["time_range"]["start"]
              and acq["params"]["region"] == plan["region"]["study_area_file"]
              and acq["params"]["composite"] == "monthly", str(acq))
    h.close()
    h2.close()


# ── 组 6：验收② 否定 ────────────────────────────────────────────


def test_negation(tmp: Path):
    print("测试组 6：验收②「不是武汉」后续不再出现武汉")
    h = Harness(tmp, "negation")
    h.send("武汉 2025 年 7 月做 LST，配对模式", reply([{
        "op": "create", "label": "A", "capability": "full_lst",
        "patches": {"region": {"action": "set", "value": "武汉市_市"},
                    "time": {"action": "set", "value": "2025 年 7 月"},
                    "product_mode": {"action": "set", "value": "pair"}}}]))
    check("第一轮建立了武汉任务",
          any(SlotBook(t["slots"]).value(ops.F_REGION) == "武汉市_市"
              for t in h.tasks()))

    result = h.send("不是武汉", reply([{
        "op": "clear", "target_ref": "武汉",
        "patches": {"region": {"action": "clear", "value": "武汉市_市",
                               "evidence": "不是武汉"}}}]))
    tasks = h.tasks()
    book = SlotBook(tasks[0]["slots"])
    check("武汉被真正清除", not book.has_value(ops.F_REGION), str(book.to_bundle()))
    check("否定记录已持久化",
          any("武汉" in str(n.get("value")) for n in book.negations(ops.F_REGION)))
    check("清除后任务转为等待信息并追问",
          tasks[0]["summary_status"] == t_store.TASK_AWAITING_INFO
          and result.kind == understanding.KIND_ASK,
          f"{tasks[0]['summary_status']}/{result.kind}")

    # 后续一轮：模型（或历史合槽）想把武汉塞回来，必须被挡住
    h.send("那就按 2025 年 8 月来吧", reply([{
        "op": "set", "target_ref": "",
        "patches": {"region": {"action": "set", "value": "武汉市_市"},
                    "time": {"action": "set", "value": "2025 年 8 月"}}}]))
    after = SlotBook(h.tasks()[0]["slots"])
    check("下一轮历史合槽不把武汉填回来",
          after.value(ops.F_REGION) != "武汉市_市",
          f"实际 {after.value(ops.F_REGION)}")

    # 后续不会出现武汉下载：任务没有就绪，也就没有可执行计划
    ready = [t for t in h.tasks() if t["summary_status"] == t_store.TASK_READY]
    check("被否定的地区不会进入执行链", not ready, str(ready))
    h.close()


# ── 组 7：验收③ 相对年份 ────────────────────────────────────────


def test_relative_year(tmp: Path):
    print("测试组 7：验收③「武汉去年 7 月做 LST」用锚点日期")
    h = Harness(tmp, "relyear")
    h.send("武汉去年 7 月做 LST，配对模式", reply([{
        "op": "create", "label": "A", "capability": "full_lst",
        "patches": {"region": {"action": "set", "value": "武汉市_市"},
                    "time": {"action": "set", "value": "去年 7 月",
                             "evidence": "去年 7 月"},
                    "product_mode": {"action": "set", "value": "pair"}}}]))
    tasks = h.tasks()
    book = SlotBook(tasks[0]["slots"])
    time_value = book.value(ops.F_TIME)
    check("解析成锚点前一年的 7 月，不是当前年",
          time_value["start"] == f"{ANCHOR_DATE.year - 1}-07-01"
          and time_value["start"][:4] != str(ANCHOR_DATE.year), str(time_value))
    check("锚点日期与原表达一并存档",
          time_value["anchor_date"] == ANCHOR_DATE.isoformat()
          and time_value["raw"] == "去年 7 月"
          and time_value["relative"] is True, str(time_value))
    check("相对时间已落到绝对区间，重跑不再重新解释",
          time_value["end"] == f"{ANCHOR_DATE.year - 1}-07-31")
    h.close()


# ── 组 8：验收④ 只下载 ──────────────────────────────────────────


def test_download_only(tmp: Path):
    print("测试组 8：验收④「只下载 Sentinel-2」不自动训练")
    h = Harness(tmp, "download")
    h.send("武汉 2025 年 7 月，只下载 Sentinel-2", reply([{
        "op": "create", "label": "A", "capability": "download_subset",
        "patches": {"region": {"action": "set", "value": "武汉市_市"},
                    "time": {"action": "set", "value": "2025 年 7 月"},
                    "datasets": {"action": "set", "value": ["sentinel2"]}}}]))
    tasks = h.tasks()
    check("任务能力为下载子集",
          len(tasks) == 1 and tasks[0]["capability"] == ops.CAP_DOWNLOAD_SUBSET,
          str([t["capability"] for t in tasks]))
    check("信息齐全，不追问",
          tasks[0]["summary_status"] == t_store.TASK_READY and not h.open_questions(),
          tasks[0]["summary_status"])

    spec = understanding.ResolvedTask({**tasks[0], "task_id": tasks[0]["id"]})
    skills = [s["skill"] for s in spec.steps()]
    check("只编到下载一步，不含训练与导出",
          skills == ["data_acquisition"], str(skills))
    check("数据集合限定为 Sentinel-2", spec.datasets == ["sentinel2"],
          str(spec.datasets))
    check("旧执行链意图为部分流程",
          spec.legacy_intent == "partial", spec.legacy_intent)

    unsupported = SlotBook(tasks[0]["slots"]).value(ops.F_DATASETS)
    check("数据集合只保留注册过的取值",
          all(d in ops.DATASETS for d in unsupported), str(unsupported))
    h.close()


# ── 组 9：验收⑤ Chat 模式 ──────────────────────────────────────


def test_chat_mode(tmp: Path):
    print("测试组 9：验收⑤ Chat 模式只回答，不建任何任务")
    h = Harness(tmp, "chatmode")
    # 模型即便返回了生产候选，Chat 模式也必须在代码层面拦下（不是提示词约束）
    agent = make_understander([reply([{
        "op": "create", "label": "A", "capability": "full_lst",
        "patches": {"region": {"action": "set", "value": "武汉市_市"},
                    "time": {"action": "set", "value": "2025 年 7 月"}}}])])
    ctx = h.ctx("什么是地表温度", chat_mode="chat")
    batch = agent.understand(
        "什么是地表温度", anchor_date=ctx.anchor_date, tz_label="UTC+8",
        chat_mode="chat", study_areas=[], open_tasks=[], open_questions=[])
    gated = enforce_chat_mode(batch)
    check("Chat 模式把生产候选全部拦下",
          all(o.op == ops.OP_REPLY_ONLY for o in gated.operations),
          str([o.op for o in gated.operations]))

    outcome = res.resolve(gated, ctx)
    check("拦下后只剩「只回答」", outcome.reply_only and not outcome.changes)

    result = h.send("什么是地表温度", reply([{"op": "reply_only"}]),
                    chat_mode="chat")
    check("Chat 模式结果为只回答",
          result.kind == understanding.KIND_REPLY_ONLY, result.kind)
    check("台账里一个任务都没有", not h.tasks(), str(h.tasks()))
    check("台账里一个问题都没有", not h.open_questions())
    h.close()


# ── 组 10：重启加分项 ───────────────────────────────────────────


def test_restart(tmp: Path):
    print("测试组 10：重启加分项（追问后重启，问题还在，回答后继续）")
    h = Harness(tmp, "restart")
    result = h.send("给武汉做个地表温度", reply([{
        "op": "create", "label": "A", "capability": "full_lst",
        "patches": {"region": {"action": "set", "value": "武汉市_市"}}}]))
    check("缺时间时产生追问", result.kind == understanding.KIND_ASK,
          f"{result.kind}：{result.message}")
    before = h.open_questions()
    check("追问已落台账", len(before) == 1, str(len(before)))
    question_id = before[0]["id"]
    check("问题绑定了任务与版本",
          bool(before[0]["targets"]) and before[0]["targets"][0]["task_version"] >= 1,
          str(before[0]["targets"]))

    h.reopen()   # 服务重启

    after = h.open_questions()
    check("重启后问题还在", len(after) == 1 and after[0]["id"] == question_id,
          str([q["id"] for q in after]))
    check("重启后问题正文与候选保留",
          after[0]["prompt"] == before[0]["prompt"])

    ctx = h.ctx("2025 年 7 月")
    applied = understanding.answer_question(
        h.store, user_id=h.user, project_id=h.project,
        conversation_id=h.conv_pk, question_id=question_id,
        text="2025 年 7 月", ctx=ctx)
    check("重启后回答仍可被消费", applied is not None, str(applied))

    tasks = h.tasks()
    book = SlotBook(tasks[0]["slots"])
    check("答案已写入草稿",
          (book.value(ops.F_TIME) or {}).get("start") == "2025-07-01",
          str(book.value(ops.F_TIME)))
    # 整月 + 完整生产 → 还要问配对/月度，这是「只问真正缺的信息」
    follow = h.open_questions()
    check("原问题已关闭，后续只问真正还缺的产品方式",
          len(follow) == 1 and follow[0]["id"] != question_id
          and "整月" in follow[0]["prompt"], str([q["prompt"] for q in follow]))

    ctx2 = h.ctx("配对模式")
    understanding.answer_question(
        h.store, user_id=h.user, project_id=h.project,
        conversation_id=h.conv_pk, question_id=follow[0]["id"],
        text="配对模式", ctx=ctx2)
    final = h.tasks()[0]
    check("信息补齐后任务转为就绪",
          final["summary_status"] == t_store.TASK_READY,
          final["summary_status"])
    check("补齐后不再有待答问题", not h.open_questions())
    h.close()


# ── 组 11：答案一次消费 ─────────────────────────────────────────


def test_answer_once(tmp: Path):
    print("测试组 11：答案一次消费（过期卡片不重复执行）")
    h = Harness(tmp, "answer_once")
    h.send("给武汉做个地表温度", reply([{
        "op": "create", "label": "A", "capability": "full_lst",
        "patches": {"region": {"action": "set", "value": "武汉市_市"}}}]))
    question_id = h.open_questions()[0]["id"]

    ctx = h.ctx("2025 年 7 月")
    first = understanding.answer_question(
        h.store, user_id=h.user, project_id=h.project,
        conversation_id=h.conv_pk, question_id=question_id,
        text="2025 年 7 月", ctx=ctx)
    check("首次回答生效", first is not None)

    again = understanding.answer_question(
        h.store, user_id=h.user, project_id=h.project,
        conversation_id=h.conv_pk, question_id=question_id,
        text="2025 年 9 月", ctx=ctx)
    check("同一问题第二次提交不再生效", again is None)
    book = SlotBook(h.tasks()[0]["slots"])
    check("任务时间未被过期卡片覆盖",
          book.value(ops.F_TIME)["start"] == "2025-07-01",
          str(book.value(ops.F_TIME)))

    stored = h.store.read(lambda c: q_store.load_question(c, question_id))
    check("原问题终态为已回答且答复留档",
          stored["status"] == q_store.Q_ANSWERED
          and stored["answer"]["text"] == "2025 年 7 月", str(stored["status"]))

    # 任务版本推进后，指向旧版本的卡片必须判失效
    follow = h.open_questions()[0]
    task_id = h.tasks()[0]["id"]

    def _bump(conn):
        row = conn.execute("SELECT version FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        return t_store.patch_task(conn, task_id, int(row[0]),
                                  ambiguity=["版本推进"])

    h.store.submit_write(_bump)
    stale = understanding.answer_question(
        h.store, user_id=h.user, project_id=h.project,
        conversation_id=h.conv_pk, question_id=follow["id"],
        text="配对模式", ctx=h.ctx("配对模式"))
    check("目标版本变化后旧卡片失效，不错误执行", stale is None)
    h.close()


# ── 组 12：失败兜底 ─────────────────────────────────────────────


def test_fallback(tmp: Path):
    print("测试组 12：失败兜底（不猜城市、不默认下载）")
    h = Harness(tmp, "fallback")
    result = h.send("帮我处理一下", "API调用失败: 连接超时")
    check("模型不可用时如实说明",
          result.kind == understanding.KIND_FAILED
          and "联系不上语言模型" in result.message, result.message)
    check("模型不可用时不建任何任务", not h.tasks())
    check("模型不可用时不产生追问", not h.open_questions())

    broken = h.send("帮我处理一下", "这是一段没有 JSON 的胡言乱语")
    check("输出无效时如实说明", broken.kind == understanding.KIND_FAILED,
          broken.message)
    check("输出无效时仍不建任务", not h.tasks())

    # 一次格式修复：第一次坏、第二次好
    agent = make_understander([
        "不是 JSON",
        reply([{"op": "create", "label": "A", "capability": "full_lst",
                "patches": {"region": {"action": "set", "value": "武汉市_市"},
                            "time": {"action": "set", "value": "2025 年 7 月"},
                            "product_mode": {"action": "set", "value": "pair"}}}]),
    ])
    ctx = h.ctx("武汉 2025 年 7 月配对模式")
    fixed = understanding.handle_message(
        h.store, agent, user_id=h.user, project_id=h.project,
        conversation_id=h.conv_pk, message="武汉 2025 年 7 月配对模式", ctx=ctx)
    check("一次格式修复后可用", fixed.kind == understanding.KIND_PROCEED,
          f"{fixed.kind}：{fixed.message}")
    check("格式修复只重试一次", len(agent.assistant.replies) == 0)
    h.close()


# ── 组 13：能力目录与旧链路接缝 ─────────────────────────────────


def test_capabilities():
    print("测试组 13：能力目录（必需字段与旧链路映射）")
    check("完整生产必需字段齐全",
          set(capabilities.required_fields(ops.CAP_FULL_LST))
          == {ops.F_REGION, ops.F_TIME, ops.F_PRODUCT_MODE})
    check("完整生产映射到 7 步主链",
          len(capabilities.legacy_steps(ops.CAP_FULL_LST)) == 7)
    check("查询能力不产生任何步骤",
          capabilities.legacy_steps(ops.CAP_QUERY) == []
          and not capabilities.is_production(ops.CAP_QUERY))
    check("填洞映射到独立后处理",
          capabilities.legacy_steps(ops.CAP_GAPFILL) == ["lst_gapfill"])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gtai-understanding-test-"))
    try:
        test_contract()
        test_time()
        test_slotbook()
        test_binding(tmp)
        test_two_cities(tmp)
        test_negation(tmp)
        test_relative_year(tmp)
        test_download_only(tmp)
        test_chat_mode(tmp)
        test_restart(tmp)
        test_answer_once(tmp)
        test_fallback(tmp)
        test_capabilities()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
