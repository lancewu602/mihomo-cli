"""`sub test` 手动测速：打对端点、测完按延迟列出来。

守两个容易写错的地方：
  · 订阅节点不在 `/proxies` 里（mihomo 1.19.26 起 `/proxies/<订阅节点>` 是 404），手动测速
    必须走 provider 级 healthcheck（`GET /providers/proxies/{名}/healthcheck`），不能退回去
    逐个节点打 `/proxies/{名}/delay`；
  · 测完按延迟排，序号是 `sub use --delay` 那套——提示里必须带上 `--delay`，否则按这张表
    数出来的序号拿去 `sub use` 会数错。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

from mihomo_cli import subs

NODES = [{"name": "香港 01", "type": "ShadowsocksR", "delay": 10, "alive": True}]


def run_cmd(code: int) -> tuple[int, list, list, mock.Mock]:
    """跑一遍 cmd_sub_test，返回 (退出码, api_raw 的调用参数, _nodes_of 的调用参数, _render_nodes)。"""
    with (
        mock.patch.object(subs, "require_config", return_value=mock.MagicMock()),
        mock.patch.object(subs, "_our_provider", return_value={"name": "airport", "keys": {}}),
        mock.patch.object(subs, "api_raw", return_value=(code, {})) as api_raw,
        mock.patch.object(subs, "_nodes_of", return_value=NODES) as nodes_of,
        mock.patch.object(subs, "_render_nodes") as render,
    ):
        try:
            rc = subs.cmd_sub_test(Namespace())
        except SystemExit as e:
            return int(e.code or 0), api_raw.call_args_list, nodes_of.call_args_list, render
    return rc, api_raw.call_args_list, nodes_of.call_args_list, render


class SubTestCommandTest(unittest.TestCase):
    def test_打的是_provider_健康检查端点(self) -> None:
        rc, calls, _, _ = run_cmd(204)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[0], "/providers/proxies/airport/healthcheck")
        self.assertGreater(kwargs["timeout"], 10)  # 同步请求，得给够（几十个节点要好几秒）

    def test_测完按延迟排(self) -> None:
        _, _, nodes_calls, render = run_cmd(204)
        self.assertEqual(nodes_calls[0], mock.call("airport", by_delay=True))
        _, kwargs = render.call_args
        self.assertTrue(kwargs["by_delay"])

    def test_内核没跑就报错不渲染(self) -> None:
        rc, _, _, render = run_cmd(0)
        self.assertEqual(rc, 1)
        render.assert_not_called()

    def test_内核里没有这个订阅时提示重启(self) -> None:
        rc, _, _, render = run_cmd(404)
        self.assertEqual(rc, 1)
        render.assert_not_called()

    def test_其它非_2xx_也当成失败(self) -> None:
        rc, _, _, render = run_cmd(500)
        self.assertEqual(rc, 1)
        render.assert_not_called()


class RenderNodesTest(unittest.TestCase):
    def render(self, by_delay: bool) -> str:
        out = io.StringIO()
        with (
            mock.patch.object(subs, "current_node", return_value=None),
            contextlib.redirect_stdout(out),
        ):
            subs._render_nodes(NODES, "标题", by_delay=by_delay)
        return out.getvalue()

    def test_按延迟排时提示带_delay(self) -> None:
        self.assertIn("sub use <序号> --delay", self.render(True))

    def test_原顺序时提示不带_delay(self) -> None:
        self.assertNotIn("--delay", self.render(False))


if __name__ == "__main__":
    unittest.main()
