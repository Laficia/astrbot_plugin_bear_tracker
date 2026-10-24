"""User-editable S aliases, layered around the preserved forwarding engine."""
import copy
import json
import re
from pathlib import Path
from functools import lru_cache
from .adapter import load_config as original_load_config
from .engine import ForwardBotPlugin as OriginalEngine


@lru_cache(maxsize=1)
def common_alias_catalog():
    return json.loads(Path(__file__).with_name('common_hunt_aliases.json').read_text('utf-8'))


def canonical_hunts(profile):
    catalog=json.loads((Path(__file__).parents[2]/'reference_catalog.json').read_text('utf-8'))
    names=[row['name'] for row in catalog['hunts'].values()]
    names.extend(common_alias_catalog()['ss_hunts'])
    names.extend(profile.get('completions',{}).get('pairs',{}))
    names.extend(profile.get('completions',{}).get('aliases',{}).values())
    return list(dict.fromkeys(names))


def supplement_common_aliases(profile):
    """One-time additive import. Existing S, map and server meanings win."""
    aliases=copy.deepcopy(profile.get('completions',{}).get('aliases',{}))
    names=set(canonical_hunts(profile));reserved={}
    for group in profile.get('decorations',{}).get('groups',[]):
        reserved.update({alias:name for alias,name in group.get('aliases',{}).items() if name not in names})
    reserved.update(profile.get('map_names',{}).get('aliases',{}))
    reserved.update(profile.get('map_images',{}).get('aliases',{}))
    added={};conflicts=[]
    for name,words in common_alias_catalog()['aliases_by_hunt'].items():
        for alias in words:
            old=aliases.get(alias,reserved.get(alias,alias if alias in names else None))
            if old is not None:
                if old!=name:conflicts.append({'alias':alias,'preserved':old,'wiki':name})
                continue
            aliases[alias]=name;added[alias]=name
    return aliases,added,conflicts


def exact_aliases(profile, overrides):
    # An explicitly emptied alias table must also remove bundled defaults.
    aliases=overrides.get('completions',{}).get('aliases')
    if isinstance(aliases,dict):profile['completions']['aliases']=copy.deepcopy(aliases)
    return profile


def load_config(settings,data_dir):
    profile=exact_aliases(original_load_config(settings,data_dir),json.loads(settings.get('overrides_json') or '{}'))
    from ...asset_bundle import map_directory
    profile['map_images']['dir']=map_directory(Path(__file__).parent,data_dir,profile['map_images']['dir'])
    return profile


def validate_aliases(profile):
    aliases=profile['completions'].get('aliases',{})
    names=set(canonical_hunts({**profile,'completions':{**profile['completions'],'aliases':{}}}))
    if not isinstance(aliases,dict) or len(aliases)>1000:raise ValueError('S 怪简称最多 1000 项')
    for alias,name in aliases.items():
        if not isinstance(alias,str) or not alias.strip() or len(alias)>80 or alias!=alias.strip():
            raise ValueError('S 怪简称应为 1～80 字的文字')
        if not isinstance(name,str) or name not in names:raise ValueError('S 怪简称对应的全名不存在：'+str(name))
        if alias in names and alias!=name:raise ValueError('S 怪简称不能使用另一只 S 怪的全名：'+alias)
        if any(c in alias for c in '\n\r=,，、;；|'):raise ValueError('S 怪简称不能包含换行或分隔符')


class AliasDecorator:
    def __init__(self,base,aliases,names):
        self.base=base;self.aliases=aliases
        words=set(names)|set(aliases)
        self.pattern=re.compile('|'.join(map(re.escape,sorted(words,key=len,reverse=True)))) if words else None

    def decorate(self,text):
        hits=[]
        def replace(match):
            word=match.group();name=self.aliases.get(word,word)
            if len(word)==1 and word!=name:
                before=text[match.start()-1:match.start()] if match.start() else ''
                after=text[match.end():match.end()+1]
                # A one-character nickname such as 鸟 must not alter 陆行鸟,
                # another Chinese word, or a data-center label like [鸟-幻影].
                if before=='-' or after=='-' or any(c and re.match(r'\w',c) and c not in 'SsＳｓ' for c in (before,after)):
                    return word
            if word!=name and name not in hits:hits.append(name)
            return name
        normalized=self.pattern.sub(replace,text) if self.pattern else text
        result,decorated=self.base.decorate(normalized) if self.base else (normalized,[])
        return result,list(dict.fromkeys(hits+decorated))


class ForwardBotPlugin(OriginalEngine):
    def _build_decorator(self):
        base=super()._build_decorator()
        aliases=self.config.get('completions',{}).get('aliases',{})
        return AliasDecorator(base,aliases,canonical_hunts(self.config)) if aliases else base
