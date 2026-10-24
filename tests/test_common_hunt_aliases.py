"""Wiki additions preserve custom names, other alias categories and SS maps."""
import copy,json,tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
from astrbot_plugin_bear_tracker.components.magic_bucket.s_hunt_names import (
    canonical_hunts,common_alias_catalog,supplement_common_aliases,validate_aliases,load_config,AliasDecorator)
from astrbot_plugin_bear_tracker.components.magic_bucket import webui

PROFILE=json.loads((Path(__file__).parents[1]/'tests/fixture_profile.json').read_text('utf-8'))

class CommonAliasTests(unittest.TestCase):
    def test_three_ss_names_remain_editable_even_when_all_aliases_are_cleared(self):
        profile=copy.deepcopy(PROFILE);profile['completions']['aliases']={}
        names=canonical_hunts(profile)
        self.assertEqual(len(names),50)
        for name in common_alias_catalog()['ss_hunts']:
            self.assertIn(name,names);self.assertNotIn(name,profile['completions']['pairs'])
        profile['completions']['aliases']={'晶哥':'水晶化身之王','叛姐':'得到宽恕的叛乱','克总':'克尔'}
        validate_aliases(profile)
        with tempfile.TemporaryDirectory() as folder:
            loaded=load_config({'overrides_json':json.dumps(profile)},Path(folder))
        self.assertEqual(loaded['completions']['aliases'],profile['completions']['aliases'])

    def test_additions_never_reassign_existing_s_map_or_server_aliases(self):
        profile=copy.deepcopy(PROFILE)
        profile['completions']['aliases']={'鸟蛋':'神穆尔鸟','自定义少爷':'布弗鲁','pzj':'颇胝迦'}
        before=copy.deepcopy(profile)
        merged,added,conflicts=supplement_common_aliases(profile)
        self.assertEqual(profile,before)
        self.assertTrue(all(merged[k]==v for k,v in before['completions']['aliases'].items()))
        self.assertNotIn('一休',merged)
        self.assertIn({'alias':'鸟蛋','preserved':'神穆尔鸟','wiki':'俄菲翁尼厄斯'},conflicts)
        self.assertEqual(added['蛋蛇'],'俄菲翁尼厄斯');self.assertEqual(added['叛姐'],'得到宽恕的叛乱')
        self.assertNotIn('暂无',merged);self.assertNotIn('斯奎克',merged)
        profile['completions']['aliases']=merged
        self.assertEqual(supplement_common_aliases(profile)[1],{})

    def test_short_nickname_cannot_corrupt_data_center_labels_or_existing_full_names(self):
        aliases={'鸟':'极乐鸟','晶哥':'水晶化身之王','叛姐':'得到宽恕的叛乱','克总':'克尔'}
        decorator=AliasDecorator(None,aliases,canonical_hunts(PROFILE))
        result,_=decorator.decorate('陆行鸟 [鸟-幻影群岛] 神穆尔鸟 鸟S 鸟 晶哥 叛姐 克总')
        self.assertEqual(result,'陆行鸟 [鸟-幻影群岛] 神穆尔鸟 极乐鸟S 极乐鸟 水晶化身之王 得到宽恕的叛乱 克尔')

class CommonAliasPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_ss_alias_uses_real_formatter_without_inventing_one_map(self):
        profile=copy.deepcopy(PROFILE);profile['completions']['aliases']=supplement_common_aliases(profile)[0]
        ui=webui.MagicWebUI(SimpleNamespace(context=SimpleNamespace(register_web_api=Mock())))
        body={'profile':profile,'enabled':False,'bot_uin':'','text':'重要 晶哥 叛姐 克总 SS',
              'group':profile['rules'][0]['source_groups'][0]}
        with patch.object(webui,'request',SimpleNamespace(json=AsyncMock(return_value=body))):result=await ui.preview()
        self.assertEqual(result['status'],'ok',result);self.assertTrue(result['data']['messages'])
        for message in result['data']['messages']:
            for name in common_alias_catalog()['ss_hunts']:self.assertIn(name,message['text'])
            self.assertIsNone(message['image'])
