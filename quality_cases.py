"""Versioned capability regression questions with local, deterministic grading."""
import json
import re

VERSION = "capability-v2"
INSTRUCTIONS = ("\n\n可以充分推理，不限制推理正文长度。不要调用搜索、代码执行或计算器工具。"
                "请在最后单独一行用“最终答案：”给出答案。")
CCF = "https://github.com/HoneyMeta/ccfingerprint/blob/main/src/dataset.json"
TRACE = "https://github.com/wangchao0708/TraceOne/blob/main/src/traceone/canary.py"
BBEH = "https://github.com/google-deepmind/bbeh/blob/main/bbeh/benchmark_tasks/"

CASES = [
    {"id": "crt", "name": "约束推理 · 同余方程", "group": "基础回归", "type": "integer", "answer": 654,
     "source_url": CCF, "origin": "公开原题改述",
     "prompt": "求满足下列条件的最小正整数n：n除以7余3，除以11余5，除以13余4。最终答案只填整数。"},
    {"id": "inclusion-exclusion", "name": "组合计数 · 包含与排除", "group": "基础回归", "type": "integer", "answer": 400,
     "source_url": CCF, "origin": "公开原题改述",
     "prompt": "统计所有满足0<n<1000的整数n：n能被3或5整除，同时不能被7整除。共有多少个？最终答案只填整数。"},
    {"id": "string-transform", "name": "符号处理 · 循环移位与倒序", "group": "基础回归", "type": "exact", "answer": "A9K2M7QF5D3C8B6T4",
     "source_url": TRACE, "origin": "按公开生成规则实例化",
     "prompt": "给定字符串Q7M2K9A4T6B8C3D5F。先将前7个字符整体移到末尾（向左循环移动7个字符），再将整个字符串倒序。最终答案只填处理后的字符串，保持大小写。"},
    {"id": "second-difference", "name": "规则推导 · 二阶差分数列", "group": "基础回归", "type": "integer", "answer": 510,
     "source_url": TRACE, "origin": "按公开生成规则实例化",
     "prompt": "一个数列满足a_1=17；对于所有k>=1，a_(k+1)-a_k=5+3*(k-1)。求a_18。最终答案只填整数。"},
    {"id": "meeting-schedule", "name": "综合推理 · 跨时区会议安排", "group": "综合推理", "type": "meeting",
     "answer": {"minutes": 100, "start": "14:00", "end": "15:40"},
     "source_url": BBEH + "bbeh_temporal_sequence/task.json", "origin": "按公开题型改编，非官方原题",
     "prompt": """四个人要在同一个日期安排会议。下面列出各自时区和当地可用时间，区间端点表示可以开始或结束的时刻：
甲：UTC+8，可用09:00—12:00、13:00—17:00。
乙：UTC+9，可用10:20—12:40、14:00—17:00。
丙：UTC+8，可用10:10—11:00、11:20—13:50、14:10—15:40。
丁：UTC+7，可用08:40—10:50、12:20—15:20。
规则：
1. 会议按北京时间（UTC+8）的整点或半点开始，连续进行，至少45分钟。
2. 乙在会议结束后，必须在同一段可用时间内再留出至少15分钟。
3. 丁在会议前、后，必须在同一段可用时间内分别留出至少10分钟。
4. 丙可以缺席会议开头最多10分钟，但之后必须连续参加至会议结束；甲、乙、丁必须全程参加。
求最长会议的分钟数及对应的北京时间起止时刻。
最终答案填JSON对象，键为minutes（整数）、start和end（HH:MM格式）。"""},
    {"id": "boolean-expression", "name": "组合逻辑 · 嵌套条件判断", "group": "综合推理", "type": "choice", "answer": "E",
     "source_url": BBEH + "bbeh_boolean_expressions/task.json", "origin": "按公开题型改编，非官方原题",
     "prompt": """按Python运算规则，以下五个表达式中只有一个为True，请找出它。**表示乘方，%表示取余。不得运行代码。
A = not ((17*19 > 320) and (144/12 == 12))
B = (not False and 7*13 == 90) or not (25-3*7 == 4)
C = not ((not (8*9 < 70) and 3**4 == 81) or (105%8 == 1))
D = not (not (11**2-9**2 == 40)) and not (max(4,-7,9)-min(4,-7,9) > 15)
E = (not (6*7 == 41)) and not (False or 2**5 < 30)
最终答案只填一个选项字母。"""},
]
for case in CASES:
    case["version"] = VERSION
    case["prompt"] += INSTRUCTIONS


def grade(text, case):
    """Read an explicit final answer; keep unparseable output out of ability scores."""
    text = text.strip()
    matches = list(re.finditer(r"(?:最终答案|Final\s+Answer)\s*[:：]\s*", text, re.I))
    candidate = text[matches[-1].end():].strip() if matches else text
    candidate = re.sub(r"^\*{1,2}\s*", "", candidate)
    if candidate.startswith('```'):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I).split('```', 1)[0].strip()
    kind = case["type"]
    if kind in ("meeting", "json"):
        try:
            value, _ = json.JSONDecoder().raw_decode(candidate)
        except (ValueError, TypeError):
            return None, "未提取到有效的最终答案JSON，待人工复核"
        if kind == "meeting":
            if not isinstance(value, dict) or not all(k in value for k in ("minutes", "start", "end")):
                return None, "最终答案缺少会议时长或起止时间，待人工复核"
            value = {k: value[k] for k in ("minutes", "start", "end")}
            if isinstance(value["minutes"], bool):
                return None, "会议时长格式无效，待人工复核"
            try:
                value["minutes"] = float(value["minutes"])
            except (TypeError, ValueError):
                return None, "会议时长不是数字，待人工复核"
            for key in ("start", "end"):
                clock = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value[key]).strip())
                if not clock or int(clock[1]) > 23 or int(clock[2]) > 59:
                    return None, "会议时间格式无效，待人工复核"
                value[key] = f"{int(clock[1]):02d}:{int(clock[2]):02d}"
        passed = value == case["answer"]
    else:
        lines = [line.strip() for line in candidate.splitlines() if line.strip()]
        candidate = (lines[0] if matches and lines else lines[-1] if lines else '').strip('`* \t\"\'。.')
        if kind == "integer":
            if not re.fullmatch(r"[+-]?\d+(?:\.0+)?", candidate):
                return None, "未提取到明确的最终整数答案，待人工复核"
            passed = int(candidate.split('.')[0]) == case["answer"]
        elif kind == "choice":
            choice = re.fullmatch(r"(?:选项\s*)?[（(]?([A-Ea-e])[)）]?", candidate)
            if not choice:
                return None, "未提取到明确的最终选项，待人工复核"
            passed = choice[1].upper() == case["answer"]
        else:
            if not candidate or len(candidate.split()) != 1:
                return None, "未提取到明确的最终字符串，待人工复核"
            passed = candidate == case["answer"]
    return int(passed), "最终答案正确" if passed else "最终答案与标准答案不符"
