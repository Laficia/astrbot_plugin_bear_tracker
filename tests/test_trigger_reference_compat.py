"""Minified reference compatibility, validation and non-executable literals."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astrbot_plugin_bear_tracker import trigger_source as source
from astrbot_plugin_bear_tracker.static_literals import read_literal, code_digest
from astrbot_plugin_bear_tracker.webui import BearWebUI


def reference():
    core = 'const ET_SEC_HOUR=175;const ET_SEC_DAY=4200;'
    timers = '(function(t,n){const r=n();t.SRKTimers=r})(this,function(){return {line:1}});'
    groups = {'g': {'hunts': source.DEFAULT_TRIGGER_HUNTS[:5]}, 'c': {'hunts': source.DEFAULT_TRIGGER_HUNTS[5:]}}
    floating = ('if(!window.SRK||!window.TRIGGER_MAP)return;const e=' + json.dumps(groups) + ';'
                'const current=analysis.windows.find(x=>x.start<=now&&now<x.end);'
                'const servers=read(data,key,now);const hunt=data?data.hunts.find(x=>x.k===key):null;'
                'const hot=current&&servers&&servers.ok.length>0;')
    rows = [{'k': key, 'ln': 1, 'rt': [65, 10], 'mt': [39, 6]} for key in source.RULES]
    data = [{'dc': dc, 'hunts': rows} for dc in ['豆豆柴', '莫古力']]
    # Match the current structural group marker while allowing quoted data keys.
    floating = floating.replace('"g":', 'g:').replace('"c":', 'c:')
    page = ''.join('<script>' + s + '</script>' for s in [core+'function certaintyOf(x){return x}', timers, floating,
                'window.SRKMaint.configure('+json.dumps(data)+');'])
    return page, {'calculations_sha256': code_digest(core), 'timers_sha256': code_digest(timers)}


class LiteralTests(unittest.TestCase):
    def test_object_data_supports_minified_js_numbers_without_executing_code(self):
        value, end = read_literal('{dc:"豆豆柴",rt:[30,.6],ln:8,ok:true,none:null,}ignored')
        self.assertEqual(value, {'dc': '豆豆柴', 'rt': [30, .6], 'ln': 8, 'ok': True, 'none': None})
        self.assertEqual('ignored', '{dc:"豆豆柴",rt:[30,.6],ln:8,ok:true,none:null,}ignored'[end:])
        for text in ['{x:fetch("https://example.com")}', '{x:1+2}', '{x:1,x:2}', '{x:()=>1}', '[1,', '{x:Infinity}']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_literal(text)

    def test_layout_and_comments_are_ignored_but_string_contents_are_not(self):
        self.assertEqual(code_digest('const a="Fair Skies";'), code_digest('const a = "Fair Skies"; /* note */'))
        self.assertNotEqual(code_digest('const a="Fair Skies";'), code_digest('const a="FairSkies";'))


class ReferenceTests(unittest.TestCase):
    def test_compact_metadata_is_read_with_all_existing_monsters_and_centers(self):
        page, profile = reference()
        with patch.dict(source.RULE_DATA, {'compact_profile': profile}):
            result = source.decode_page(page)
            self.assertEqual(set(result), {'豆豆柴', '莫古力'})
            self.assertEqual(set(result['豆豆柴']), set(source.RULES))
            self.assertEqual(source.decode_page(page.replace(';', ';\n')), result)

    def test_unknown_math_changed_gate_incomplete_data_and_expressions_fail_closed(self):
        page, profile = reference()
        variants = [page.replace('ET_SEC_HOUR=175', 'ET_SEC_HOUR=176'),
                    page.replace('servers.ok.length>0', 'servers.force.length>0'),
                    page.replace('line:1', 'line:2'),
                    page.replace('"rt": [65, 10]', '"rt": [0, 10]', 1),
                    page.replace('"ln": 1', '"ln": 100', 1),
                    page.replace('"ln": 1', '"ln": run()', 1)]
        with patch.dict(source.RULE_DATA, {'compact_profile': profile}):
            for changed in variants:
                with self.subTest(changed=changed[:80]), self.assertRaises(ValueError):
                    source.decode_page(changed)

    def test_robot_status_is_separate_and_handles_an_offline_client(self):
        connected = SimpleNamespace(meta=lambda: SimpleNamespace(id='selected', name='aiocqhttp'),
                                    get_client=lambda: SimpleNamespace(_wsr_api_clients={'account': object()}))
        ctx = SimpleNamespace(register_web_api=Mock(), platform_manager=SimpleNamespace(platform_insts=[connected]))
        config = {'automation_platform_id': 'selected'}
        before = copy.deepcopy(config)
        ui = BearWebUI(SimpleNamespace(context=ctx, config=config))
        self.assertTrue(ui.bot_connection()['connected'])
        connected.get_client = Mock(side_effect=RuntimeError('offline'))
        self.assertFalse(ui.bot_connection()['connected'])
        self.assertEqual(config, before)


if __name__ == '__main__':
    unittest.main()
