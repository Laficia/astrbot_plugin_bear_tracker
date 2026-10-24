"""Read JSON-like JS data literals only; expressions and code are rejected."""
import json
import re


def read_literal(source, start=0):
    index, count = start, 0

    def skip():
        nonlocal index
        while index < len(source) and source[index].isspace():
            index += 1

    def value(depth=0):
        nonlocal index, count
        skip()
        count += 1
        if depth > 40 or count > 20000 or index >= len(source):
            raise ValueError('参考站静态资料超出解析范围')
        token = source[index]
        if token == '"':
            result, length = json.JSONDecoder().raw_decode(source[index:])
            index += length
            return result
        if token in '[{':
            array = token == '['
            end = ']' if array else '}'
            index += 1
            result = [] if array else {}
            skip()
            while index < len(source) and source[index] != end:
                if array:
                    result.append(value(depth + 1))
                else:
                    skip()
                    if source[index] == '"':
                        key = value(depth + 1)
                    else:
                        match = re.match(r'[A-Za-z_$][\w$]*', source[index:])
                        if not match:
                            raise ValueError('参考站资料包含不支持的键名')
                        key = match[0]
                        index += len(key)
                    skip()
                    if source[index:index+1] != ':' or key in result:
                        raise ValueError('参考站资料键名重复或格式无效')
                    index += 1
                    result[key] = value(depth + 1)
                skip()
                if source[index:index+1] == end:
                    break
                if source[index:index+1] != ',':
                    raise ValueError('参考站资料包含表达式，拒绝执行')
                index += 1
                skip()
            if source[index:index+1] != end:
                raise ValueError('参考站资料未完整闭合')
            index += 1
            return result
        match = re.match(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true\b|false\b|null\b', source[index:])
        if not match:
            raise ValueError('参考站资料包含表达式，拒绝执行')
        raw = match[0]
        index += len(raw)
        if raw in ('true', 'false', 'null'):
            return {'true': True, 'false': False, 'null': None}[raw]
        return float(raw) if any(c in raw for c in '.eE') else int(raw)

    result = value()
    return result, index


def code_digest(source):
    """Ignore layout/comments while preserving string and template contents."""
    import hashlib
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/|[^\s]', source)
    return hashlib.sha256(''.join(t for t in tokens if not t.startswith(('//', '/*'))).encode()).hexdigest()

def minified_timer_digest(source):
    """Normalize one-letter minifier names, preserving properties and literals.

    This is only used for the small audited, closed timer module. Property names,
    operators, numbers, strings, long external names and token order remain fixed.
    It cannot approve newly structured calculations or execute JavaScript.
    """
    import hashlib
    tokens=re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_$][\w$]*|[^\s]',source)
    tokens=[t for t in tokens if not t.startswith(('//','/*'))]
    names={};result=[]
    for i,t in enumerate(tokens):
        previous=tokens[i-1] if i else ''
        following=tokens[i+1] if i+1<len(tokens) else ''
        property_name=previous=='.' or (previous in ('{',',') and following==':')
        if re.fullmatch('[A-Za-z]',t) and not property_name:
            if t not in names:names[t]='@'+str(len(names))
            t=names[t]
        result.append(t)
    return hashlib.sha256('|'.join(result).encode()).hexdigest()
