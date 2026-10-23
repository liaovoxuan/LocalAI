#!/usr/bin/env python3
"""LocalAI Tiny 综合基准：内存、吞吐、上下文、并发和留出题准确率。"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from daily_life_guides import daily_life_eval_cases


ROOT = Path(__file__).resolve().parent


def load_localai():
    spec = importlib.util.spec_from_file_location("localai_benchmark_target", ROOT / "LocalAI-modle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def current_rss_bytes() -> int:
    """读取当前 RSS，而不是进程生命周期峰值。"""
    try:
        value = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip()
        return int(value) * 1024
    except (OSError, subprocess.SubprocessError, ValueError):
        statm = Path("/proc/self/statm")
        if statm.exists():
            pages = int(statm.read_text().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        return 0


def peak_rss_bytes() -> int:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index]


GENERAL_HOLDOUT = [
    # 基础聊天：这些完整句子不在训练列表中。
    ("嗨呀，今天方便聊聊吗", "greeting", ""),
    ("麻烦简单说说该怎么称呼你", "identity", "本地"),
    ("你具体能帮我处理哪些事情", "ability", "计算"),
    ("多谢你刚才的解答", "thanks", ""),
    ("我先下线了，改天再聊", "farewell", ""),
    ("我今天心里有点不好受", "negative", ""),
    ("今天事情特别顺利", "positive", ""),
    ("請問你支援繁體中文嗎", "language", "繁體"),
    ("Could you briefly introduce yourself?", "identity", "local"),
    ("I appreciate the help you gave me.", "thanks", ""),
    # 数学和实用工具。
    ("请帮忙算一下 19*23", "math", "437"),
    ("(7+5)/3", "math", "4"),
    ("sqrt(81)", "math", "9"),
    ("cos(60°)", "math", "0.5"),
    ("1/7 + 2/7", "math", "3/7"),
    ("解方程 4*x+8=28", "math", "x = 5"),
    ("因式分解 x^2-7x+12", "math", "(x - 4)(x - 3)"),
    ("半径为5的圆面积", "math", "78.539"),
    ("2.5 km to m", "utility", "2500"),
    ("2 GiB to MiB", "utility", "2048"),
    ("2025-02-01和2025-03-01相差几天", "utility", "28"),
    ("100摄氏度换算华氏度", "utility", "212"),
    # 新旧百科、历史、字符和英语词典。
    ("请解释一下细菌", "encyclopedia", "细菌"),
    ("什麼是滲透壓", "encyclopedia", "半透膜"),
    ("WHAT IS CELL BIOLOGY?", "encyclopedia", "basic unit of life"),
    ("介绍一下地震", "encyclopedia", "地壳"),
    ("什么是光锥", "encyclopedia", "相对论"),
    ("What is a comet?", "encyclopedia", "Solar System"),
    ("什麼是播客", "encyclopedia", "RSS"),
    ("氧气是什么", "encyclopedia", "O₂"),
    ("秦始皇是什么人", "history", "第一位"),
    ("五四运动为什么发生", "history", "1919"),
    ("When did Apollo 11 land on the Moon?", "history", "1969"),
    ("誰是拿破崙", "history", "法國"),
    ("第二次世界大战何时结束", "history", "1945"),
    ("龘这个字表示什么", "character", "群龙腾飞"),
    ("仉能不能作为姓氏", "character", "姓氏"),
    ("爨的普通话读音是什么", "character", "cuàn"),
    ("请用friend给我造个句子", "english_word", "close friend"),
    ("define beautiful", "english_word", "beautiful"),
    ("How do I use world in a sentence?", "english_word", "Example"),
    # 地域化法律知识与安全拒答。
    ("中国大陆民事诉讼一般时效是多久", "legal", "三年"),
    ("臺灣民法現在幾歲成年", "legal", "18歲"),
    ("What section of U.S. copyright law discusses fair use?", "legal", "§107"),
    ("美国合同纠纷适用哪一州的规则", "legal", "具体州"),
    ("香港劳动争议能直接套用大陆劳动法吗", "legal", "独立的法律制度"),
]


LEGAL_LIFE_HOLDOUT = [
    # 中国大陆生活场景。
    ("我在广州工作，公司拖了两个月工资还没发，怎么合法处理", "欠薪", "劳动人事争议仲裁委员会"),
    ("深圳老板突然把我开除了，也不给书面理由", "被辞退或裁员", "人力资源社会保障"),
    ("我在北京连续加班但工资条里没有加班费", "加班和加班费争议", "考勤"),
    ("在杭州上班时摔伤，公司不肯处理", "工伤或工作场所受伤", "就医"),
    ("上海网购买到坏电器，店家拒绝退钱", "退款、瑕疵商品或消费争议", "全国12315"),
    ("在成都被人冒充客服骗走了钱", "诈骗或可疑转账", "110"),
    ("南京退租后房东一直扣着押金", "租房押金、维修或租赁争议", "住房城乡建设"),
    ("武汉的朋友借钱到期不还，我该怎么办", "借款、欠款或催收争议", "12348"),
    ("在苏州发生车祸，对方不愿交换资料", "交通事故", "交警"),
    ("我在重庆遭到家人暴力威胁", "家庭暴力或人身安全", "110"),
    ("我的个人信息在广东被平台泄露了", "个人信息或隐私泄露", "个人信息保护"),
    ("北京单位因为性别拒绝录用我", "就业歧视或职场性骚扰", "证据"),
    ("在上海看病后怀疑发生医疗过错", "医疗纠纷", "病历"),
    ("我的原创图片在深圳被商家盗用", "著作权或内容被抄袭", "原始创作"),
    ("广州的合同对方到期不履行", "合同违约", "合同"),
    # 美国州/城市场景。
    ("My California employer has withheld my last two paychecks.", "unpaid-wage", "Wage and Hour Division"),
    ("I was fired without an explanation while working in Texas.", "Dismissal or layoff", "Preserve"),
    ("My New York landlord will not return my security deposit.", "Rental deposit, repair, or tenancy dispute", "housing complaints"),
    ("A seller in California refused a refund for a defective appliance.", "Refund, defective product, or consumer dispute", "USAGov"),
    ("I transferred money after a phishing message in Texas.", "Fraud or suspicious transfer", "FBI"),
    ("I was injured at work in California and need to know the lawful next steps.", "Workplace injury", "medical"),
    ("My personal information was leaked by a company in New York.", "Privacy or personal-data leak", "identity theft"),
    ("My work was copied by a business in California.", "Copyright infringement", "Copyright Office"),
    ("A contract was breached in Texas; what records should I preserve?", "Breach of contract", "Preserve"),
    ("I am facing domestic violence in California and need immediate help.", "Domestic violence or immediate safety", "emergency services"),
    # 中国台湾地区生活场景。
    ("我在臺灣工作，公司積欠兩個月薪資", "欠薪", "勞資爭議調解"),
    ("臺灣公司突然叫我離職，也沒有書面通知", "被辭退或裁員", "勞動部"),
    ("我在臺灣上班受傷，雇主不願處理", "工傷或工作場所受傷", "就醫"),
    ("臺灣網購收到故障商品，賣家不退款", "退款、瑕疵商品或消費爭議", "1950"),
    ("在臺灣收到假客服訊息後被騙轉帳", "詐騙或可疑轉帳", "165"),
    ("臺灣租屋退租後房東不還押金", "租房押金、維修或租賃爭議", "地方政府"),
    ("臺灣朋友借錢後一直不還", "借款、欠款或催收爭議", "法律扶助"),
    ("我在臺灣遇到車禍，對方想直接離開", "交通事故", "警察"),
    ("我在臺灣遭遇家庭暴力威脅", "家庭暴力或人身安全", "113"),
    ("臺灣網站把我的個人資料外洩了", "個人資料或隱私外洩", "1950"),
    ("我在臺灣職場遭到性騷擾", "就業歧視或職場性騷擾", "證據"),
    ("臺灣醫療處置出問題，要先保存什麼", "醫療糾紛", "病歷"),
    ("我在臺灣發表的文章被整篇抄走", "著作權或內容被抄襲", "原始創作"),
    ("臺灣簽的契約對方拒絕履行", "契約違約", "法律扶助"),
    ("臺灣離婚時孩子監護怎麼處理", "離婚、子女或家庭程序", "法律扶助"),
]


DAILY_LIFE_HOLDOUT = daily_life_eval_cases()


DOMESTIC_HOLDOUT = [
    ("在不在呀，聊两句", "greeting", ""),
    ("我先撤了，回聊", "farewell", ""),
    ("辛苦你了哈", "thanks", ""),
    ("你到底是干啥的", "identity", "本地"),
    ("今儿状态贼好", "positive", ""),
    ("最近有点闹心", "negative", ""),
    ("现在啥时候了", "time", "现在时间"),
    ("你能说普通话不", "language", "简体中文"),
    ("来个段子听听", "joke", "程序员"),
    ("刚才是我不对哈", "apology", ""),
    ("这回答挺靠谱", "compliment", ""),
    ("你是谁整出来的", "creator", "本地训练"),
    ("帮我瞅瞅23*17", "math", "391"),
    ("帮我计祘 18+24", "math", "42"),
    ("给我唠唠五四运动", "history", "1919"),
    ("给我科普下光合作用", "encyclopedia", "二氧化碳"),
    ("仉这字念啥", "character", "zhǎng"),
    ("friend咋用", "english_word", "close friend"),
    ("我在郑州上班，老板压了俩月工资", "legal", "劳动人事争议仲裁委员会"),
    ("北京租房押金一直不给退", "legal", "住房城乡建设"),
    ("我在广州网购买到坏东西，商家不处理", "legal", "全国12315"),
]


def score_cases(ai, model, cases):
    failures = []
    latencies = []
    breakdown = {}
    for prompt, expected_intent, expected_text in cases:
        breakdown.setdefault(expected_intent, [0, 0]); breakdown[expected_intent][1] += 1
        started = time.perf_counter()
        answer, intent, _ = ai.reply(model, prompt, allow_network=False)
        latencies.append(time.perf_counter() - started)
        passed = intent == expected_intent and expected_text.casefold() in answer.casefold()
        if passed:
            breakdown[expected_intent][0] += 1
        if not passed:
            failures.append({"prompt": prompt, "expected_intent": expected_intent,
                             "actual_intent": intent, "expected_text": expected_text,
                             "answer": answer[:240]})
    return {
        "passed": len(cases) - len(failures), "total": len(cases),
        "accuracy": (len(cases) - len(failures)) / len(cases),
        "latency_ms_median": statistics.median(latencies) * 1000,
        "breakdown": {name: {"passed": value[0], "total": value[1],
                             "accuracy": value[0] / value[1]}
                      for name,value in breakdown.items()},
        "failures": failures,
    }


def score_life_cases(ai, model, cases):
    failures = []
    latencies = []
    breakdown = {"CN": [0, 0], "US": [0, 0], "TW": [0, 0]}
    for prompt, expected_topic, expected_route in cases:
        region = "TW" if "臺灣" in prompt else ("US" if prompt.isascii() else "CN")
        breakdown[region][1] += 1
        started = time.perf_counter()
        answer, intent, _ = ai.reply(model, prompt, allow_network=False)
        latencies.append(time.perf_counter() - started)
        # 场景标题可能因语言和同义表达不同；真正决定回答是否可用的是法律分流
        # 是否成功，以及答案是否包含该场景必需的办理/安全关键词。
        passed = intent == "legal" and expected_route.casefold() in answer.casefold()
        if passed:
            breakdown[region][0] += 1
        if not passed:
            failures.append({"prompt": prompt, "expected_intent": "legal",
                             "actual_intent": intent, "expected_topic": expected_topic,
                             "expected_route": expected_route, "answer": answer[:360]})
    return {
        "passed": len(cases) - len(failures), "total": len(cases),
        "accuracy": (len(cases) - len(failures)) / len(cases),
        "latency_ms_median": statistics.median(latencies) * 1000,
        "scoring_rubric": "法律意图必须正确，且回答必须包含该场景的关键合法办理/安全信息。",
        "breakdown": {name: {"passed": value[0], "total": value[1],
                             "accuracy": value[0] / value[1]}
                      for name,value in breakdown.items()},
        "failures": failures,
    }


def score_daily_life_cases(ai, model, cases):
    failures = []
    latencies = []
    breakdown = {}
    for prompt, expected_topic, expected_text in cases:
        group = ai.classify_daily_life(prompt) or "unmatched"
        breakdown.setdefault(group, [0, 0]); breakdown[group][1] += 1
        started = time.perf_counter()
        answer, intent, _ = ai.reply(model, prompt, allow_network=False)
        latencies.append(time.perf_counter() - started)
        passed = (intent == "daily_life" and group == expected_topic and
                  expected_text.casefold() in answer.casefold())
        if passed:
            breakdown[group][0] += 1
        else:
            failures.append({"prompt": prompt, "expected_topic": expected_topic,
                             "actual_topic": group, "actual_intent": intent,
                             "expected_text": expected_text, "answer": answer[:320]})
    return {
        "passed": len(cases) - len(failures), "total": len(cases),
        "accuracy": (len(cases) - len(failures)) / len(cases),
        "latency_ms_median": statistics.median(latencies) * 1000,
        "coverage_topics": len({case[1] for case in cases}),
        "scoring_rubric": "必须命中正确的非法律生活主题，并包含该场景的关键安全或办理步骤。",
        "breakdown": {name: {"passed": value[0], "total": value[1],
                             "accuracy": value[0] / value[1]}
                      for name,value in breakdown.items()},
        "failures": failures,
    }


def benchmark_classifier(ai, model):
    prompts = [case[0] for case in GENERAL_HOLDOUT[:32]] * 30
    token_count = sum(len(ai.encode(prompt)) for prompt in prompts)
    started = time.perf_counter()
    for prompt in prompts:
        model.predict(prompt)
    elapsed = time.perf_counter() - started
    return {"requests": len(prompts), "elapsed_seconds": elapsed,
            "requests_per_second": len(prompts) / elapsed,
            "input_byte_tokens": token_count,
            "input_byte_tokens_per_second": token_count / elapsed}


def benchmark_end_to_end(ai, model):
    prompts = [
        "你好", "19*23", "什么是细菌", "介绍一下地震", "秦始皇是谁",
        "1 GiB to MiB", "仉是不是姓氏", "What does friend mean?",
        "什麼是滲透壓", "What is a comet?",
    ] * 30
    # 初始化数据库 schema/缓存后再计时，避免把一次性迁移混入稳态速度。
    ai.reply(model, "什么是细胞", allow_network=False)
    input_tokens = sum(len(ai.encode(prompt)) for prompt in prompts)
    output_tokens = 0
    latencies = []
    started = time.perf_counter()
    for prompt in prompts:
        one = time.perf_counter()
        answer, _, _ = ai.reply(model, prompt, allow_network=False)
        latencies.append(time.perf_counter() - one)
        output_tokens += len(ai.encode(answer))
    elapsed = time.perf_counter() - started
    return {"requests": len(prompts), "elapsed_seconds": elapsed,
            "requests_per_second": len(prompts) / elapsed,
            "latency_ms_median": statistics.median(latencies) * 1000,
            "latency_ms_p95": percentile(latencies, .95) * 1000,
            "input_byte_tokens_per_second": input_tokens / elapsed,
            "response_byte_tokens_per_second": output_tokens / elapsed,
            "note": "响应文本是模板/数据库一次性返回，不是自回归生成速度。"}


def benchmark_streaming(ai, model):
    """通过正式 delta 事件接口计数，验证流重组和 completed usage 完全一致。"""
    base = ["你好", "19*23", "什么是细菌", "秦始皇是谁", "1 GiB to MiB",
            "What is a comet?", "什麼是滲透壓", "仉是不是姓氏"]
    prompts = base * 25
    input_tokens = output_tokens = delta_tokens = chunks = 0
    ttft_values, latency_values = [], []
    valid = True; sample = None
    started = time.perf_counter()
    for prompt in prompts:
        deltas=[]; completed=None; one_delta_tokens=0
        for event in ai.stream_reply(model,prompt,allow_network=False,chunk_token_limit=24):
            if event["type"]=="response.output_text.delta":
                deltas.append(event["delta"]); one_delta_tokens += event["token_count"]; chunks += 1
            elif event["type"]=="response.completed": completed=event
        answer="".join(deltas)
        if completed is None:
            valid=False; continue
        usage,metrics=completed["usage"],completed["metrics"]
        valid &= (usage["input_tokens"]==len(ai.encode(prompt)) and
                  usage["output_tokens"]==len(ai.encode(answer)) and
                  usage["output_tokens"]==one_delta_tokens and
                  usage["total_tokens"]==usage["input_tokens"]+usage["output_tokens"])
        input_tokens += usage["input_tokens"]; output_tokens += usage["output_tokens"]
        delta_tokens += one_delta_tokens
        ttft_values.append(metrics["time_to_first_token_ms"])
        latency_values.append(metrics["total_ms"])
        if sample is None:
            sample={"prompt":prompt,"response":answer,"input_tokens":usage["input_tokens"],
                    "output_tokens":usage["output_tokens"],"total_tokens":usage["total_tokens"],
                    "chunks":metrics["chunks"]}
    elapsed=time.perf_counter()-started
    return {"event_protocol":["response.start","response.output_text.delta","response.completed"],
            "chunk_token_limit":24,"requests":len(prompts),"delta_events":chunks,
            "input_tokens":input_tokens,"output_tokens":output_tokens,
            "total_tokens":input_tokens+output_tokens,"delta_tokens":delta_tokens,
            "usage_validation_passed":valid,"elapsed_seconds":elapsed,
            "requests_per_second":len(prompts)/elapsed,
            "output_tokens_per_second":output_tokens/elapsed,
            "ttft_ms_median":statistics.median(ttft_values),
            "ttft_ms_p95":percentile(ttft_values,.95),
            "total_latency_ms_median":statistics.median(latency_values),"sample":sample,
            "note":"Token 是 LocalAI encode() 的真实 UTF-8 byte-token；这是模板/检索结果的增量传输，不是自回归解码。"}


def benchmark_concurrency(ai, model):
    base = ["你好", "2+2", "什么是细菌", "秦始皇是谁", "1 km to m",
            "What is a comet?", "什麼是滲透壓", "仉是不是姓氏"]
    prompts = (base * 40)[:320]
    results = []
    for workers in (1, 2, 4, 8):
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            answers = list(pool.map(lambda prompt: ai.reply(model, prompt, allow_network=False)[0], prompts))
        elapsed = time.perf_counter() - started
        input_tokens = sum(len(ai.encode(prompt)) for prompt in prompts)
        response_tokens = sum(len(ai.encode(answer)) for answer in answers)
        results.append({"workers": workers, "requests": len(prompts),
                        "elapsed_seconds": elapsed,
                        "requests_per_second": len(prompts) / elapsed,
                        "input_byte_tokens_per_second": input_tokens / elapsed,
                        "response_byte_tokens_per_second": response_tokens / elapsed})
    return {"mode": "Python library thread pool; interactive CLI itself is single-request",
            "results": results}


def benchmark_context(ai, model):
    sizes = (128, 512, 2048, 8192, 32768)
    timings = []
    for size in sizes:
        unit = "上下文 context 123。"
        text = (unit * (size // len(unit.encode("utf-8")) + 1)).encode("utf-8")[:size]
        # 截断可能落在 UTF-8 字符中；忽略不完整尾字节不影响近似长度测试。
        prompt = text.decode("utf-8", errors="ignore")
        samples = []
        for _ in range(5):
            started = time.perf_counter(); model.predict(prompt)
            samples.append(time.perf_counter() - started)
        timings.append({"input_byte_tokens": len(ai.encode(prompt)),
                        "median_ms": statistics.median(samples) * 1000})

    memory = ai.ConversationMemory(20)
    for index in range(25):
        memory.remember(f"用户第{index + 1}轮", f"助手第{index + 1}轮", "greeting")
    retained = [turn["user"] for turn in memory.turns]
    reference_memory = ai.ConversationMemory(20)
    first, _, _ = ai.reply(model, "秦始皇是谁", memory=reference_memory, allow_network=False)
    followup, followup_intent, _ = ai.reply(model, "那他后来怎么样了？", memory=reference_memory,
                                             allow_network=False)
    return {
        "hard_model_token_limit": None,
        "single_input_behavior": "未设硬截断；字节与字符 n-gram 全量扫描，耗时近似随输入长度线性增长。",
        "input_scaling": timings,
        "conversation_turn_limit": 20,
        "retained_after_25_turns": len(retained),
        "oldest_retained_turn": retained[0],
        "newest_retained_turn": retained[-1],
        "reference_resolution_passed": followup_intent == "history" and "秦始皇" in followup,
        "reference_answer_preview": followup[:180],
        "file_context_limit_mib": 10,
        "file_answer_line_limit": 30,
        "note": "会话不是完整 Transformer 上下文，只保存最近轮次并做主题/地区指代拼接。",
    }


def benchmark_memory_pressure(ai, model):
    typical_samples = []
    workload = ["你好", "什么是细菌", "秦始皇是谁", "2+2", "1 GiB to MiB",
                "什麼是滲透壓", "What does friend mean?"]
    for index in range(140):
        ai.reply(model, workload[index % len(workload)], allow_network=False)
        typical_samples.append(current_rss_bytes())
    representative_peak = peak_rss_bytes()

    # 压测支持上限附近的真实文本文件：约 10 MiB、约十万行。
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "context.txt"
        line = "LocalAI context benchmark line with 中文内容 and searchable marker.\n"
        target = 10 * 1024 * 1024
        repeats = target // len(line.encode("utf-8"))
        with path.open("w", encoding="utf-8") as output:
            output.write(line * repeats)
        context = ai.FileContext(); context.open(str(path))
        context.answer("searchable marker", "en")
        file_lines = len(context.lines)
        file_rss = current_rss_bytes()
        supported_file_peak = peak_rss_bytes()
    return {
        "typical_rss_mib_median": statistics.median(typical_samples) / 1048576,
        "typical_rss_mib_min": min(typical_samples) / 1048576,
        "typical_rss_mib_max": max(typical_samples) / 1048576,
        "representative_peak_rss_mib": representative_peak / 1048576,
        "near_10mib_file_lines": file_lines,
        "near_10mib_file_current_rss_mib": file_rss / 1048576,
        "near_10mib_file_peak_rss_mib": supported_file_peak / 1048576,
    }


def ram_projection(measured_peak_mib: float):
    rows = []
    for ram_gib in (2, 4, 8, 16, 32):
        if ram_gib == 2:
            note = "模型本身充足；老旧系统或同时运行大型程序时可能出现系统级内存压力。"
        elif ram_gib == 4:
            note = "单实例非常充足，常规文件问答和数据库缓存稳定。"
        elif ram_gib == 8:
            note = "内存不再是瓶颈，可承载多个轻量实例；吞吐主要受 CPU/SQLite 影响。"
        elif ram_gib == 16:
            note = "与本次测试机器一致；增加内存不会提高单题准确率。"
        else:
            note = "对单实例几乎没有收益；只有大量独立进程或更大外部数据缓存会利用额外内存。"
        rows.append({"system_ram_gib": ram_gib, "model_peak_share_percent":
                     measured_peak_mib / (ram_gib * 1024) * 100,
                     "expected_accuracy_change": "无", "expected_single_request_speed_change": "近似无",
                     "assessment": note})
    return rows


def main():
    import_started = time.perf_counter()
    ai = load_localai()
    import_seconds = time.perf_counter() - import_started
    rss_after_import = current_rss_bytes()
    load_started = time.perf_counter(); model = ai.TinyChatModel.load()
    model_load_seconds = time.perf_counter() - load_started
    rss_after_model = current_rss_bytes()

    # 先测普通工作集内存，避免线程池、长输入和 10 MiB 文件压测抬高“典型”基线。
    memory = benchmark_memory_pressure(ai, model)
    general = score_cases(ai, model, GENERAL_HOLDOUT)
    legal_life = score_life_cases(ai, model, LEGAL_LIFE_HOLDOUT)
    daily_life = score_daily_life_cases(ai, model, DAILY_LIFE_HOLDOUT)
    domestic = score_cases(ai, model, DOMESTIC_HOLDOUT)
    classifier = benchmark_classifier(ai, model)
    end_to_end = benchmark_end_to_end(ai, model)
    streaming = benchmark_streaming(ai, model)
    concurrency = benchmark_concurrency(ai, model)
    context = benchmark_context(ai, model)

    report = {
        "benchmark_version": 1,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "machine": platform.machine(), "logical_cpus": os.cpu_count()},
        "architecture_note": "字节级 n-gram 意图分类器 + SQLite 检索 + 确定性工具；不是自回归大语言模型。",
        "startup": {"python_module_import_seconds": import_seconds,
                    "model_load_seconds": model_load_seconds,
                    "rss_after_import_mib": rss_after_import / 1048576,
                    "rss_after_model_load_mib": rss_after_model / 1048576},
        "memory": memory,
        "token_speed": {"token_definition": "1 token = 1 UTF-8 byte（本模型 encode() 的真实单位）",
                        "classifier": classifier, "end_to_end": end_to_end, "streaming":streaming},
        "context": context,
        "concurrency": concurrency,
        "heldout_accuracy": general,
        "life_scenario_accuracy": daily_life,
        "legal_life_scenario_accuracy": legal_life,
        "domestic_optimization_accuracy":domestic,
        "ram_projection": ram_projection(memory["near_10mib_file_peak_rss_mib"]),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
