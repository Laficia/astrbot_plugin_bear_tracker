"""Read-only metadata: isolate bots, retain offline names, and refresh renames."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_bear_tracker.group_directory import GroupDirectory
from astrbot_plugin_bear_tracker.webui import BearWebUI


def platform(pid, rows, kind='aiocqhttp'):
    call = AsyncMock(return_value=rows)
    bot = SimpleNamespace(api=SimpleNamespace(call_action=call))
    return SimpleNamespace(meta=lambda: SimpleNamespace(id=pid, name=kind), get_client=lambda: bot), call


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_names_are_scoped_by_selected_bot_and_queries_are_read_only(self):
        a, ac = platform('a', [{'group_id': 100, 'group_name': '甲群'}])
        b, bc = platform('b', [{'group_id': 100, 'group_name': '乙群'}])
        directory = GroupDirectory(SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[a, b])))
        self.assertEqual((await directory.query('a'))['groups'], [{'id': '100', 'name': '甲群'}])
        bc.assert_not_awaited()
        self.assertEqual((await directory.query('b'))['groups'][0]['name'], '乙群')
        ac.assert_awaited_once_with('get_group_list')
        bc.assert_awaited_once_with('get_group_list')
        with self.assertRaises(ValueError):
            await directory.query('unknown')

    async def test_cache_coalesces_requests_and_force_refreshes_renames_and_leavers(self):
        bot, call = platform('a', [{'group_id': 100, 'group_name': '旧名'}, {'group_id': 200, 'group_name': '退出群'}])
        directory = GroupDirectory(SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot])))
        await asyncio.gather(directory.query('a'), directory.query('a'))
        call.assert_awaited_once()
        call.return_value = {'status': 'ok', 'retcode': 0, 'data': [{'group_id': 100, 'group_name': '新名'}]}
        directory.checked['a'] = (0, directory.checked['a'][1])
        result = await directory.query('a', force=True)
        self.assertEqual(result['groups'], [{'id': '100', 'name': '新名'}])
        self.assertFalse(result['stale'])

    async def test_offline_cache_survives_reload_without_exposing_other_bot_names(self):
        a, ac = platform('a', [{'group_id': 100, 'group_name': '<群名 & 测试>'}])
        b, bc = platform('b', [])
        ctx = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[a, b]))
        with tempfile.TemporaryDirectory() as folder:
            await GroupDirectory(ctx, folder).query('a')
            ac.side_effect = RuntimeError('offline')
            bc.side_effect = RuntimeError('offline')
            directory = GroupDirectory(ctx, folder)
            cached = await directory.query('a')
            self.assertTrue(cached['stale'])
            self.assertTrue(cached['cached'])
            self.assertEqual(cached['groups'][0]['name'], '<群名 & 测试>')
            self.assertEqual((await directory.query('b'))['groups'], [])
            self.assertTrue((Path(folder) / 'group_names.json').is_file())

    async def test_timeout_and_bad_response_do_not_replace_a_good_cache(self):
        bot, call = platform('a', [{'group_id': 100, 'group_name': '已知群'}])
        directory = GroupDirectory(SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot])))
        await directory.query('a')
        for bad in [None, {'status': 'failed', 'retcode': 1, 'data': []}]:
            directory.checked.clear()
            call.return_value = bad
            result = await directory.query('a')
            self.assertTrue(result['stale'])
            self.assertEqual(result['groups'][0]['name'], '已知群')
        async def slow(*args):
            await asyncio.sleep(1)
        call.side_effect = slow
        directory.timeout = .001
        directory.checked.clear()
        self.assertTrue((await directory.query('a'))['stale'])

    async def test_unsupported_platform_does_not_call_onebot(self):
        bot, call = platform('official', [], 'qq_official')
        directory = GroupDirectory(SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot])))
        self.assertEqual((await directory.query('official'))['groups'], [])
        call.assert_not_awaited()

    async def test_authenticated_endpoint_keeps_configuration_unchanged(self):
        bot, call = platform('a', [{'group_id': 100, 'group_name': '测试群'}])
        config = {'automation_platform_id': 'a', 'target_sessions': ['a:GroupMessage:100']}
        before = copy.deepcopy(config)
        ui = BearWebUI(SimpleNamespace(config=config, context=SimpleNamespace(register_web_api=Mock(),
            platform_manager=SimpleNamespace(platform_insts=[bot]))))
        with patch('astrbot_plugin_bear_tracker.webui.request', SimpleNamespace(json=AsyncMock(return_value={}))):
            result = await ui.group_names()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['groups'][0]['name'], '测试群')
        self.assertEqual(config, before)
        with patch('astrbot_plugin_bear_tracker.webui.request', SimpleNamespace(json=AsyncMock(return_value={'refresh': 'yes'}))):
            self.assertEqual((await ui.group_names())['status'], 'error')


if __name__ == '__main__':
    unittest.main()
