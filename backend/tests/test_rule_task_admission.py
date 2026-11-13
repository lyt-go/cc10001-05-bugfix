"""跨页面回归：解析规则状态与采集任务准入必须一致。

覆盖三类问题：
1. 规则不能从草稿跨级启用，必须先落规则验证中并通过测试；
2. 规则废弃后编号不能复活，只能新建下一代；
3. 任务调度 / 启动必须读取规则落库的当前状态，重复请求只认首个回执。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import unittest

from app.seed import SEED_ROWS
from app.services.parserule import ParseruleService
from app.services.taskcrawl import TaskcrawlService
from app.store import store


class AdmissionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从示例数据重新开始，避免共享内存仓库互相污染。
        store._tables = {
            name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        store.receipts.clear()
        self.rules = ParseruleService()
        self.tasks = TaskcrawlService()

    def test_draft_cannot_jump_to_enabled(self) -> None:
        outcome = self.rules.run_action(1, "启用规则")
        self.assertFalse(outcome.ok)
        self.assertEqual(self.rules.get_entry(1)["规则状态"], "草稿")

    def test_validating_rule_needs_passing_test_before_enable(self) -> None:
        outcome = self.rules.run_action(5, "启用规则")
        self.assertFalse(outcome.ok)
        self.assertIn("未通过", outcome.message)
        self.assertEqual(self.rules.get_entry(5)["规则状态"], "规则验证中")

    def test_enable_after_fixing_fields_and_revalidating(self) -> None:
        self.rules.update_entry(5, {"列表容错": "跳过缺失卡片", "空值占位符": "-"})
        self.assertTrue(self.rules.run_action(5, "重新验证规则").ok)
        self.assertTrue(self.rules.run_action(5, "启用规则").ok)
        self.assertEqual(self.rules.get_entry(5)["规则状态"], "已启用")

    def test_duplicate_enable_only_accepts_first_receipt(self) -> None:
        self.rules.run_action(5, "废弃规则", {"request_id": "R-1"})
        replay = self.rules.run_action(5, "废弃规则", {"request_id": "R-1"})
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.message, "已处理")
        self.assertEqual(self.rules.get_entry(5)["规则状态"], "已废弃")

    def test_deprecated_rule_cannot_be_revived(self) -> None:
        self.assertTrue(self.rules.run_action(5, "废弃规则").ok)
        for action in ("启用规则", "提交规则验证"):
            self.assertFalse(self.rules.run_action(5, action).ok)
        created, message = self.rules.create_entry(
            {"规则编号": "PARS-117", "页面选择器": "x", "内容选择器": "y"}
        )
        self.assertIsNone(created, message)
        next_gen, message = self.rules.create_next_generation(5)
        self.assertIsNotNone(next_gen, message)
        self.assertEqual(next_gen["规则编号"], "PARS-117-V2")
        self.assertEqual(next_gen["规则状态"], "草稿")
        self.assertEqual(self.rules.get_entry(5)["规则状态"], "已废弃")

    def test_task_with_validating_rule_is_view_only(self) -> None:
        task = self.tasks.get_entry(5)  # TASK-208 -> PARS-117
        self.assertTrue(task["调度字段完整"])
        self.assertFalse(task["可启动"])
        self.assertIn("规则验证中", task["受影响原因"])
        self.assertEqual(task["可执行动作"], ["终止任务"])
        outcome = self.tasks.run_action(5, "启动任务")
        self.assertFalse(outcome.ok)
        # 再次提交仍按规则当前状态给出失败结论，刷新后也不会变成可启动。
        self.assertFalse(self.tasks.run_action(5, "启动任务").ok)
        self.assertFalse(self.tasks.get_entry(5)["可启动"])

    def test_repeated_start_accepts_first_success(self) -> None:
        self.rules.update_entry(5, {"列表容错": "跳过缺失卡片", "空值占位符": "-"})
        self.rules.run_action(5, "重新验证规则")
        self.rules.run_action(5, "启用规则")
        first = self.tasks.run_action(5, "启动任务", {"request_id": "T-1"})
        self.assertTrue(first.ok)
        self.assertFalse(first.duplicate)
        again = self.tasks.run_action(5, "启动任务", {"request_id": "T-1"})
        self.assertTrue(again.duplicate)
        self.assertEqual(again.message, "已处理")
        self.assertEqual(self.tasks.get_entry(5)["status"], "执行中")

    def test_completed_task_keeps_history_and_cannot_restart(self) -> None:
        task = self.tasks.get_entry(4)  # TASK-0004 已完成，引用已废弃的 PARS-0004
        self.assertEqual(task["历史规则结论"]["规则状态"], "已启用")
        self.assertEqual(task["规则状态"], "已废弃")
        self.assertFalse(task["可启动"])
        self.assertFalse(self.tasks.run_action(4, "启动任务").ok)

    def test_task_list_reads_live_rule_state(self) -> None:
        rule = store.find("parserule", 2)
        rule["status"] = "已启用"
        rule["validation"] = {"passed": True, "checked_at": "", "checks": [], "failed_fields": []}
        task = self.tasks.get_entry(2)
        self.assertEqual(task["规则状态"], "已启用")
        self.assertTrue(task["可启动"])
        rule["status"] = "规则验证中"
        self.assertFalse(self.tasks.get_entry(2)["可启动"])


if __name__ == "__main__":
    unittest.main()
