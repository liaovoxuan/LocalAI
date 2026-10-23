#!/usr/bin/env python3
"""LocalAI Tiny 的可审阅扩展：百科、检索、工具、文件问答与评测。"""
from __future__ import annotations

import ast
import csv
import json
import math
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
SEARCH_CACHE_PATH = ROOT / "search_cache.db"
EVAL_PATH = ROOT / "localai_systematic_eval.jsonl"
_READY_ENCYCLOPEDIA_DATABASES: set[str] = set()

# 仅用于检索，不改写展示给用户的原文。这里包含高频简繁、口语和错别字；NFKC
# 处理全角字符及兼容字，casefold 比 lower 更适合 Unicode 英文检索。
EQUIVALENTS = str.maketrans({
    "臺": "台", "裡": "里", "瞭": "了", "著": "着", "麼": "么", "為": "为",
    "與": "与", "後": "后", "發": "发", "歷": "历", "國": "国", "學": "学",
    "體": "体", "計": "计", "算": "算", "轉": "转", "換": "换", "問": "问",
    "資": "资", "訊": "讯", "網": "网", "絡": "络", "電": "电", "腦": "脑",
    "溫": "温", "華": "华", "萬": "万", "億": "亿", "說": "说", "識": "识", "費": "费", "點": "点", "積": "积",
})
TYPO_REPLACEMENTS = {
    "沸腾点": "沸点", "水的费点": "水的沸点", "计祘": "计算", "換祘": "换算",
    "怎末": "怎么", "在嘛": "在吗", "啥时侯": "啥时候", "因该": "应该",
    "工資": "工资", "身分证": "身份证", "社保卡丢失了咋办": "社保卡丢失怎么办",
    "什麼事": "什么是", "what's": "what is", "whats": "what is", "whta": "what",
    "computor": "computer", "temprature": "temperature", "definately": "definitely",
}


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().translate(EQUIVALENTS)
    for wrong, right in TYPO_REPLACEMENTS.items():
        value = value.replace(wrong, right)
    value = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


# id, category, zh, hant, en, zh summary, hant summary, en summary, aliases
ENCYCLOPEDIA = [
 ("earth","geography","地球","地球","Earth","地球是太阳系第三颗行星，也是目前已知唯一存在生命的天体；表面约71%被海洋覆盖。","地球是太陽系第三顆行星，也是目前已知唯一存在生命的天體；表面約71%被海洋覆蓋。","Earth is the third planet from the Sun and the only world known to support life; oceans cover about 71% of its surface.","蓝色星球|藍色星球|world|planet earth"),
 ("china","geography","中国","中國","China","中国位于东亚，陆地面积约960万平方千米，首都是北京。","中國位於東亞，陸地面積約960萬平方公里，首都是北京。","China is in East Asia, covers about 9.6 million square kilometres of land, and has Beijing as its capital.","中华人民共和国|中華人民共和國|PRC"),
 ("united_states","geography","美国","美國","United States","美国位于北美洲，由50个州和一个联邦特区组成，首都是华盛顿哥伦比亚特区。","美國位於北美洲，由50個州和一個聯邦特區組成，首都是華盛頓哥倫比亞特區。","The United States is in North America, consists of 50 states and a federal district, and has Washington, D.C. as its capital.","美利坚合众国|美利堅合眾國|USA|US|United States of America"),
 ("taiwan_geography","geography","台湾岛","臺灣島","Taiwan island","台湾岛位于东亚西太平洋，西隔台湾海峡与中国大陆相望；最高峰是玉山。","臺灣島位於東亞西太平洋，西隔臺灣海峽與中國大陸相望；最高峰是玉山。","Taiwan is an island in the western Pacific off East Asia; the Taiwan Strait separates it from mainland China, and Yushan is its highest peak.","台湾地理|臺灣地理|Formosa"),
 ("asia","geography","亚洲","亞洲","Asia","亚洲是面积最大、人口最多的洲，与欧洲共同构成欧亚大陆。","亞洲是面積最大、人口最多的洲，與歐洲共同構成歐亞大陸。","Asia is the largest and most populous continent and forms Eurasia together with Europe.","亚细亚洲|亞細亞洲"),
 ("pacific","geography","太平洋","太平洋","Pacific Ocean","太平洋是地球上面积最大、最深的海洋，位于亚洲和大洋洲以东、美洲以西。","太平洋是地球上面積最大、最深的海洋，位於亞洲和大洋洲以東、美洲以西。","The Pacific is Earth's largest and deepest ocean, between Asia and Oceania to the west and the Americas to the east.","Pacific"),
 ("everest","geography","珠穆朗玛峰","聖母峰","Mount Everest","珠穆朗玛峰位于喜马拉雅山脉，是按海拔计算的地球最高峰，公认高程为8848.86米。","聖母峰位於喜馬拉雅山脈，是按海拔計算的地球最高峰，公認高程為8848.86公尺。","Mount Everest in the Himalayas is Earth's highest peak above sea level, with an officially accepted elevation of 8,848.86 metres.","珠峰|珠穆朗瑪峰|圣母峰|Everest"),
 ("yangtze","geography","长江","長江","Yangtze River","长江发源于青藏高原，向东注入东海，是中国最长的河流。","長江發源於青藏高原，向東注入東海，是中國最長的河流。","The Yangtze rises on the Tibetan Plateau and flows east to the East China Sea; it is China's longest river.","扬子江|揚子江|Chang Jiang"),
 ("equator","geography","赤道","赤道","Equator","赤道是与地球自转轴垂直、到南北两极距离相等的假想大圆，纬度为0度。","赤道是與地球自轉軸垂直、到南北兩極距離相等的假想大圓，緯度為0度。","The Equator is the imaginary great circle midway between the poles, perpendicular to Earth's rotation axis, at 0° latitude.","零度纬线|零度緯線"),
 ("water","chemistry","水","水","Water","水的化学式是H₂O。在1个标准大气压下，纯水约在0℃结冰、100℃沸腾；沸点会随气压变化。","水的化學式是H₂O。在1個標準大氣壓下，純水約在0℃結冰、100℃沸騰；沸點會隨氣壓變化。","Water has formula H₂O. At one standard atmosphere, pure water freezes near 0°C and boils near 100°C; boiling point changes with pressure.","H2O|水的沸点|水的沸點|boiling point of water"),
 ("atom","chemistry","原子","原子","Atom","原子是保持元素化学性质的基本粒子，由原子核和核外电子构成。","原子是保持元素化學性質的基本粒子，由原子核和核外電子構成。","An atom is the basic unit retaining an element's chemical properties, made of a nucleus and surrounding electrons.","atomic structure|原子结构|原子結構"),
 ("element","chemistry","化学元素","化學元素","Chemical element","化学元素由质子数相同的原子组成；元素按原子序数排列在元素周期表中。","化學元素由質子數相同的原子組成；元素按原子序排列在元素週期表中。","A chemical element consists of atoms with the same proton count; elements are ordered by atomic number in the periodic table.","元素|element"),
 ("periodic_table","chemistry","元素周期表","元素週期表","Periodic table","元素周期表按原子序数和电子结构排列元素，使周期性化学性质清晰可见。","元素週期表按原子序和電子結構排列元素，使週期性化學性質清晰可見。","The periodic table arranges elements by atomic number and electron structure, revealing recurring chemical properties.","化学周期表|化學週期表"),
 ("oxygen","chemistry","氧气","氧氣","Oxygen","氧气通常以双原子分子O₂存在，约占地球干燥空气体积的20.95%，支持多数生物呼吸和燃烧。","氧氣通常以雙原子分子O₂存在，約占地球乾燥空氣體積的20.95%，支持多數生物呼吸和燃燒。","Oxygen commonly occurs as O₂, makes up about 20.95% of dry air by volume, and supports most respiration and combustion.","O2|氧"),
 ("carbon_dioxide","chemistry","二氧化碳","二氧化碳","Carbon dioxide","二氧化碳的化学式为CO₂，是无色气体，也是温室气体和光合作用的原料之一。","二氧化碳的化學式為CO₂，是無色氣體，也是溫室氣體和光合作用的原料之一。","Carbon dioxide, CO₂, is a colourless greenhouse gas and one of the raw materials of photosynthesis.","CO2|碳酸气|碳酸氣"),
 ("ph","chemistry","pH值","pH值","pH","pH衡量水溶液的酸碱程度；通常25℃时小于7偏酸、大于7偏碱、等于7为中性。","pH衡量水溶液的酸鹼程度；通常25℃時小於7偏酸、大於7偏鹼、等於7為中性。","pH measures acidity or basicity in aqueous solution; at about 25°C, values below 7 are acidic, above 7 basic, and 7 neutral.","酸碱度|酸鹼度"),
 ("speed_of_light","physics","光速","光速","Speed of light","真空光速定义为每秒299,792,458米，是现代米定义的基础，也是相对论中的速度上限。","真空光速定義為每秒299,792,458公尺，是現代公尺定義的基礎，也是相對論中的速度上限。","The speed of light in vacuum is defined as exactly 299,792,458 metres per second and is the relativistic speed limit.","c|真空光速"),
 ("gravity","physics","重力","重力","Gravity","重力是有质量的物体之间相互吸引的现象；地球表面附近标准重力加速度约为9.80665 m/s²。","重力是有質量的物體之間相互吸引的現象；地球表面附近標準重力加速度約為9.80665 m/s²。","Gravity is attraction between masses; standard gravitational acceleration near Earth's surface is about 9.80665 m/s².","引力|万有引力|萬有引力|gravitational force"),
 ("newton_laws","physics","牛顿运动定律","牛頓運動定律","Newton's laws of motion","牛顿三大运动定律描述惯性、力与加速度的关系及作用力和反作用力，是经典力学基础。","牛頓三大運動定律描述慣性、力與加速度的關係及作用力和反作用力，是經典力學基礎。","Newton's three laws describe inertia, the relation between force and acceleration, and action-reaction pairs, forming a basis of classical mechanics.","牛顿三定律|牛頓三定律|F=ma"),
 ("energy","physics","能量","能量","Energy","能量是物理系统做功或引起变化的能力，可在不同形式间转换；封闭系统总能量守恒。","能量是物理系統做功或引起變化的能力，可在不同形式間轉換；封閉系統總能量守恆。","Energy is a system's capacity to do work or cause change; it changes form while total energy is conserved in a closed system.","能量守恒|能量守恆"),
 ("electric_current","physics","电流","電流","Electric current","电流是电荷的定向流动，国际单位是安培（A）；电压推动电流，电阻会阻碍电流。","電流是電荷的定向流動，國際單位是安培（A）；電壓推動電流，電阻會阻礙電流。","Electric current is directed flow of charge, measured in amperes; voltage drives current and resistance opposes it.","电|電|current|ampere"),
 ("sound","physics","声音","聲音","Sound","声音是机械振动在介质中传播形成的波；真空中没有介质，因此声音不能在真空传播。","聲音是機械振動在介質中傳播形成的波；真空中沒有介質，因此聲音不能在真空傳播。","Sound is a mechanical wave travelling through a medium; it cannot travel through a vacuum.","声波|聲波|sound wave"),
 ("cell","biology","细胞","細胞","Cell","细胞是生物体结构和功能的基本单位；所有已知生物由一个或多个细胞构成。","細胞是生物體結構和功能的基本單位；所有已知生物由一個或多個細胞構成。","The cell is the basic structural and functional unit of life; every known organism consists of one or more cells.","生物细胞|生物細胞"),
 ("dna","biology","DNA","DNA","DNA","DNA即脱氧核糖核酸，通常以双螺旋形式储存生物的遗传信息。","DNA即脫氧核糖核酸，通常以雙螺旋形式儲存生物的遺傳資訊。","DNA, deoxyribonucleic acid, usually stores biological genetic information in a double helix.","脱氧核糖核酸|脫氧核糖核酸"),
 ("gene","biology","基因","基因","Gene","基因是具有功能的遗传信息单位，通常指能产生功能性RNA或影响性状的一段DNA。","基因是具有功能的遺傳資訊單位，通常指能產生功能性RNA或影響性狀的一段DNA。","A gene is a functional unit of heredity, usually a DNA sequence that produces functional RNA or influences a trait.","遗传因子|遺傳因子"),
 ("photosynthesis","biology","光合作用","光合作用","Photosynthesis","植物、藻类等利用光能把二氧化碳和水转化为有机物，并通常释放氧气。","植物、藻類等利用光能把二氧化碳和水轉化為有機物，並通常釋放氧氣。","Plants, algae and some microbes use light energy to convert carbon dioxide and water into organic matter, usually releasing oxygen.","光合"),
 ("ecosystem","biology","生态系统","生態系統","Ecosystem","生态系统由一个区域内的生物群落及其非生物环境和相互作用组成。","生態系統由一個區域內的生物群落及其非生物環境和相互作用組成。","An ecosystem consists of a community of organisms, its non-living environment, and their interactions.","生态圈|生態系"),
 ("virus_bacteria","biology","病毒与细菌","病毒與細菌","Viruses and bacteria","细菌是能独立代谢和繁殖的细胞生物；病毒没有完整细胞结构，必须借助宿主细胞复制。抗生素针对细菌，不直接治疗病毒感染。","細菌是能獨立代謝和繁殖的細胞生物；病毒沒有完整細胞結構，必須借助宿主細胞複製。抗生素針對細菌，不直接治療病毒感染。","Bacteria are cellular organisms able to metabolise and reproduce; viruses lack full cellular machinery and replicate in host cells. Antibiotics target bacteria, not viruses directly.","细菌和病毒|細菌和病毒|virus vs bacteria"),
 ("computer","computing","计算机","電腦","Computer","计算机按照程序接收、处理、存储并输出数据，硬件和软件共同完成任务。","電腦按照程式接收、處理、儲存並輸出資料，硬體和軟體共同完成任務。","A computer follows programs to receive, process, store and output data through cooperating hardware and software.","电脑|電腦|computer"),
 ("cpu","computing","中央处理器","中央處理器","CPU","CPU执行程序指令并协调计算机部件，主要指标包括架构、核心数和时钟频率，但性能不能只看频率。","CPU執行程式指令並協調電腦部件，主要指標包括架構、核心數和時脈頻率，但效能不能只看頻率。","A CPU executes program instructions and coordinates computer components; architecture, cores and clock rate matter, but clock rate alone does not determine performance.","处理器|處理器|中央处理单元|processor"),
 ("memory","computing","内存","記憶體","Computer memory","内存通常指RAM，为正在运行的程序暂存数据；断电后普通RAM中的内容会丢失。","記憶體通常指RAM，為正在執行的程式暫存資料；斷電後普通RAM中的內容會遺失。","Memory commonly means RAM, temporary working storage for running programs; ordinary RAM loses its contents without power.","RAM|随机存取存储器|隨機存取記憶體"),
 ("operating_system","computing","操作系统","作業系統","Operating system","操作系统管理硬件资源并为应用提供服务，常见例子包括Windows、macOS、Linux、Android和iOS。","作業系統管理硬體資源並為應用程式提供服務，常見例子包括Windows、macOS、Linux、Android和iOS。","An operating system manages hardware resources and services for applications; examples include Windows, macOS, Linux, Android and iOS.","OS|系统软件|系統軟體"),
 ("internet","computing","互联网","網際網路","Internet","互联网是使用互联网协议族连接的大规模全球网络；万维网只是运行在互联网上的一项服务。","網際網路是使用網際網路協定族連接的大型全球網路；全球資訊網只是運行在其上的一項服務。","The Internet is a global network using the Internet protocol suite; the World Wide Web is one service operating on it.","因特网|互聯網|网络|網路|Internet"),
 ("http","computing","HTTP","HTTP","HTTP","HTTP是浏览器和服务器交换网页等资源的应用层协议；HTTPS在HTTP外加入加密和身份验证。","HTTP是瀏覽器和伺服器交換網頁等資源的應用層協定；HTTPS在HTTP外加入加密和身分驗證。","HTTP is an application protocol for exchanging web resources; HTTPS adds encryption and authentication.","HTTPS|超文本传输协议|超文字傳輸協定"),
 ("python_language","computing","Python语言","Python語言","Python programming language","Python是一种强调可读性的通用编程语言，支持多种编程范式，拥有丰富的标准库和第三方生态。","Python是一種強調可讀性的通用程式語言，支援多種程式設計範式，擁有豐富的標準函式庫和第三方生態。","Python is a general-purpose programming language emphasizing readability, with multiple paradigms, a broad standard library and ecosystem.","Python|python编程|python程式設計"),
 ("algorithm","computing","算法","演算法","Algorithm","算法是解决一类问题的明确、有限步骤；常用时间复杂度和空间复杂度衡量资源需求。","演算法是解決一類問題的明確、有限步驟；常用時間複雜度和空間複雜度衡量資源需求。","An algorithm is a finite, unambiguous procedure for solving a class of problems; time and space complexity measure resource needs.","演算法|algorithm"),
 ("database","computing","数据库","資料庫","Database","数据库是有组织的数据集合；数据库管理系统负责存储、查询、更新、并发和权限等工作。","資料庫是有組織的資料集合；資料庫管理系統負責儲存、查詢、更新、並行和權限等工作。","A database is an organised collection of data; a DBMS manages storage, queries, updates, concurrency and access control.","DB|DBMS|资料库|資料庫"),
 ("binary","computing","二进制","二進位","Binary","二进制只使用0和1两个数字。计算机常用位表示二进制状态，8位通常组成1字节。","二進位只使用0和1兩個數字。電腦常用位元表示二進位狀態，8位元通常組成1位元組。","Binary uses only 0 and 1. Computers represent binary states as bits, and eight bits usually form one byte.","二进位|bit|位元"),
 ("byte","computing","字节","位元組","Byte","字节是数字信息单位，现代计算中通常等于8位；1 KiB等于1024字节，1 kB按SI定义为1000字节。","位元組是數位資訊單位，現代計算中通常等於8位元；1 KiB等於1024位元組，1 kB按SI定義為1000位元組。","A byte is usually eight bits. One KiB is 1,024 bytes, while one kB is 1,000 bytes under SI.","Byte|B|字节和位|位元組"),
 ("si_units","units","国际单位制","國際單位制","International System of Units","国际单位制（SI）的七个基本单位是秒、米、千克、安培、开尔文、摩尔和坎德拉。","國際單位制（SI）的七個基本單位是秒、公尺、公斤、安培、克耳文、莫耳和燭光。","The seven SI base units are the second, metre, kilogram, ampere, kelvin, mole and candela.","SI|公制|metric system"),
 ("refrigerator","objects","冰箱","冰箱","Refrigerator","冰箱利用制冷剂循环把内部热量转移到外部。冷藏可减慢微生物繁殖，但不能无限期保鲜或杀灭所有微生物。","冰箱利用冷媒循環把內部熱量轉移到外部。冷藏可減慢微生物繁殖，但不能無限期保鮮或殺滅所有微生物。","A refrigerator uses a refrigerant cycle to move heat outside. Cooling slows microbial growth but does not preserve food indefinitely or kill every microbe.","电冰箱|電冰箱|fridge"),
 ("battery","objects","电池","電池","Battery","电池通过电化学反应在电极间产生电势差，将化学能转化为电能；不同化学体系不可随意混充。","電池透過電化學反應在電極間產生電位差，將化學能轉化為電能；不同化學體系不可任意混充。","A battery creates voltage through electrochemical reactions and converts chemical energy into electrical energy; different chemistries require appropriate charging.","battery|蓄电池|蓄電池"),
 ("thermometer","objects","温度计","溫度計","Thermometer","温度计利用物质随温度变化的性质测量温度；接触式和非接触式温度计有不同的使用条件。","溫度計利用物質隨溫度變化的性質測量溫度；接觸式和非接觸式溫度計有不同的使用條件。","A thermometer measures temperature through a temperature-dependent property; contact and non-contact types have different requirements.","体温计|體溫計|thermometer"),
]

SOURCE_BY_CATEGORY = {
    "geography": ("Encyclopaedia Britannica / public reference facts", "https://www.britannica.com/", "2026-09"),
    "physics": ("NIST / BIPM reference facts", "https://www.nist.gov/pml/owm/si-units", "SI Brochure 9th ed."),
    "chemistry": ("IUPAC Gold Book / NIST reference facts", "https://goldbook.iupac.org/", "2026-09"),
    "biology": ("OpenStax Biology 2e", "https://openstax.org/details/books/biology-2e", "2e"),
    "computing": ("MDN / Python documentation / standard definitions", "https://developer.mozilla.org/", "2026-09"),
    "units": ("BIPM SI Brochure", "https://www.bipm.org/en/publications/si-brochure", "9th ed."),
    "objects": ("Curated common-knowledge reference", "https://www.britannica.com/technology", "2026-09"),
}


def ensure_extended_knowledge(db_path: Path) -> dict[str, int]:
    """幂等创建带来源、冲突记录和 FTS5 的通用百科层。"""
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS encyclopedia_entries(
      entry_id TEXT PRIMARY KEY, category TEXT NOT NULL,
      title_zh TEXT NOT NULL, title_hant TEXT NOT NULL, title_en TEXT NOT NULL,
      summary_zh TEXT NOT NULL, summary_hant TEXT NOT NULL, summary_en TEXT NOT NULL,
      source TEXT NOT NULL, source_url TEXT NOT NULL, revision TEXT NOT NULL,
      confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1))""")
    con.execute("""CREATE TABLE IF NOT EXISTS encyclopedia_terms(
      entry_id TEXT NOT NULL, language TEXT NOT NULL, term TEXT NOT NULL,
      normalized_term TEXT NOT NULL, term_type TEXT NOT NULL,
      PRIMARY KEY(entry_id,language,normalized_term))""")
    con.execute("CREATE INDEX IF NOT EXISTS encyclopedia_terms_lookup ON encyclopedia_terms(language,normalized_term)")
    con.execute("""CREATE TABLE IF NOT EXISTS knowledge_claims(
      claim_id INTEGER PRIMARY KEY AUTOINCREMENT, subject_type TEXT NOT NULL,
      subject_id TEXT NOT NULL, predicate TEXT NOT NULL, value TEXT NOT NULL,
      qualifier TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, source_url TEXT NOT NULL,
      revision TEXT NOT NULL, confidence REAL NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
      UNIQUE(subject_type,subject_id,predicate,value,source_url))""")
    con.execute("""CREATE TABLE IF NOT EXISTS knowledge_sources(
      entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source TEXT NOT NULL,
      source_url TEXT NOT NULL, revision TEXT NOT NULL, confidence REAL NOT NULL,
      imported_at TEXT NOT NULL, PRIMARY KEY(entity_type,entity_id,source_url))""")
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    try:
        con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS encyclopedia_fts USING fts5(
          entry_id UNINDEXED, language UNINDEXED, title, aliases, body,
          tokenize='unicode61 remove_diacritics 2', prefix='2 3 4')""")
        fts = True
    except sqlite3.OperationalError:
        fts = False
    now = datetime.now().isoformat(timespec="seconds")
    for row in ENCYCLOPEDIA:
        entry_id, category, zh, hant, en, s_zh, s_hant, s_en, aliases = row
        source, url, revision = SOURCE_BY_CATEGORY[category]
        con.execute("INSERT OR REPLACE INTO encyclopedia_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entry_id,category,zh,hant,en,s_zh,s_hant,s_en,source,url,revision,.94))
        con.execute("DELETE FROM encyclopedia_terms WHERE entry_id=?", (entry_id,))
        terms = {"zh":[zh], "zh_hant":[hant], "en":[en]}
        for alias in aliases.split("|"):
            lang = "en" if re.fullmatch(r"[A-Za-z0-9 ._+-]+", alias) else ("zh_hant" if any(c in alias for c in "臺灣體資訊電腦網絡與為") else "zh")
            terms[lang].append(alias)
            # 简繁归一检索时允许中文别名跨两套界面。
            if lang != "en": terms["zh_hant" if lang == "zh" else "zh"].append(alias)
        for lang, values in terms.items():
            title = {"zh":zh,"zh_hant":hant,"en":en}[lang]
            for term in set(values):
                con.execute("INSERT OR REPLACE INTO encyclopedia_terms VALUES(?,?,?,?,?)",
                            (entry_id,lang,term,normalize_text(term),"title" if term == title else "alias"))
        con.execute("INSERT OR REPLACE INTO knowledge_sources VALUES(?,?,?,?,?,?,?)",
                    ("encyclopedia",entry_id,source,url,revision,.94,now))
        if fts:
            con.execute("DELETE FROM encyclopedia_fts WHERE entry_id=?", (entry_id,))
            for lang,title,body in (("zh",zh,s_zh),("zh_hant",hant,s_hant),("en",en,s_en)):
                con.execute("INSERT INTO encyclopedia_fts VALUES(?,?,?,?,?)",
                            (entry_id,lang,title,aliases.replace("|"," "),body))
    claims = [
      ("history","wikidata_person_Q4604","birth_year","公元前551年","传统通行纪年","Encyclopaedia Britannica","https://www.britannica.com/biography/Confucius","2026-09",.90,1),
      ("history","wikidata_person_Q4604","birth_year","公元前552年","部分古籍推算说法","Stanford Encyclopedia of Philosophy","https://plato.stanford.edu/entries/confucius/","2024 archive",.72,0),
    ]
    con.executemany("""INSERT OR IGNORE INTO knowledge_claims
      (subject_type,subject_id,predicate,value,qualifier,source,source_url,revision,confidence,is_primary)
      VALUES(?,?,?,?,?,?,?,?,?,?)""", claims)
    # 用通用来源表补齐旧表，不改动原表结构。一次迁移即可覆盖约十万条历史、
    # 汉字、短词和法律记录，之后查询不需要重复执行。
    if con.execute("SELECT 1 FROM schema_migrations WHERE name='unified_provenance_v1'").fetchone() is None:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_events'").fetchone():
            con.execute("""INSERT OR IGNORE INTO knowledge_sources
              SELECT 'history',e.event_id,
                CASE WHEN s.source_url IS NULL THEN 'LocalAI curated history seed' ELSE 'Wikidata' END,
                coalesce(s.source_url,'local://LocalAI-modle.py/HISTORY_EVENTS'),
                coalesce(s.imported_at,'2026-09'),CASE WHEN s.source_url IS NULL THEN .95 ELSE .85 END,?
              FROM history_events e LEFT JOIN history_sources s ON s.event_id=e.event_id""",(now,))
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='legal_documents'").fetchone():
            con.execute("""INSERT OR IGNORE INTO knowledge_sources
              SELECT 'legal',doc_id,source_name,source_url,
                CASE WHEN last_updated<>'' THEN last_updated ELSE status END,.90,?
              FROM legal_documents""",(now,))
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_knowledge'").fetchone():
            con.execute("""INSERT OR IGNORE INTO knowledge_sources
              SELECT 'character',character,source,
                CASE WHEN source='Unicode Unihan' THEN 'https://www.unicode.org/reports/tr38/' ELSE 'local://LocalAI-modle.py/CHARACTER_SEEDS' END,
                CASE WHEN source='Unicode Unihan' THEN 'Unicode local import' ELSE '2026-09' END,
                CASE WHEN source='Unicode Unihan' THEN .88 ELSE .96 END,? FROM character_knowledge""",(now,))
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='english_words'").fetchone():
            con.execute("""INSERT OR IGNORE INTO knowledge_sources
              SELECT 'english_word',word,source,
                CASE WHEN source='ECDICT' THEN 'https://github.com/skywind3000/ECDICT' ELSE 'local://LocalAI' END,
                'local import',.85,? FROM english_words""",(now,))
        con.execute("INSERT INTO schema_migrations VALUES('unified_provenance_v1',?)",(now,))
    con.commit()
    counts = {"encyclopedia":con.execute("SELECT count(*) FROM encyclopedia_entries").fetchone()[0],
              "terms":con.execute("SELECT count(*) FROM encyclopedia_terms").fetchone()[0],
              "claims":con.execute("SELECT count(*) FROM knowledge_claims").fetchone()[0]}
    con.close()
    _READY_ENCYCLOPEDIA_DATABASES.add(str(db_path.resolve()))
    return counts


def _cache_connection() -> sqlite3.Connection:
    con = sqlite3.connect(SEARCH_CACHE_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS search_cache(
      query_norm TEXT NOT NULL, language TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
      summary TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
      fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL, confidence REAL NOT NULL,
      status TEXT NOT NULL, PRIMARY KEY(query_norm,language))""")
    con.commit()
    return con


def _clean_question(query: str) -> str:
    q = normalize_text(query)
    q = re.sub(r"^(?:请|請)?(?:告诉我|告訴我|介绍一下|介紹一下|解释一下|解釋一下|说说|說說|什么是|什麼是)\s*", "", q)
    q = re.sub(r"^(?:what is|who is|who was|tell me about|explain)\s+", "", q)
    q = re.sub(r"(?:是什么|是什麼|什么意思|什麼意思|吗|嗎|呢)[?？。!！]*$", "", q)
    return q.strip(" ?？。!！")[:180]


def _internet_lookup(query: str, language: str) -> tuple[str,str,str] | None:
    search = _clean_question(query)
    if len(search) < 2: return None
    wiki = "en" if language == "en" else "zh"
    params = urllib.parse.urlencode({"action":"query","generator":"search","gsrsearch":search,
        "gsrlimit":1,"prop":"extracts|info","exintro":1,"explaintext":1,"inprop":"url","format":"json","utf8":1})
    request = urllib.request.Request(f"https://{wiki}.wikipedia.org/w/api.php?{params}",
                                     headers={"User-Agent":"LocalAI-Tiny/1.0 (local knowledge fallback)"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.load(response)
    pages = list(payload.get("query",{}).get("pages",{}).values())
    if not pages: return None
    page = pages[0]; extract = re.sub(r"\s+", " ", page.get("extract","")).strip()
    if len(extract) < 60: return None
    title=page.get("title",search); normalized_title=normalize_text(title)
    # MediaWiki 搜索对自然聊天句可能给出完全无关的热门页面。至少要求标题与
    # 查询共享一个有意义的英文词或中文双字片段，防止垃圾结果进入缓存。
    if language=="en":
        significant=[word for word in re.findall(r"[a-z0-9]+",search) if len(word)>=3 and word not in {"the","what","who","how","and","for"}]
        if significant and not any(word in normalized_title for word in significant):return None
    else:
        han="".join(re.findall(r"[\u3400-\u9fff]",search)); bigrams={han[i:i+2] for i in range(len(han)-1)}
        if bigrams and not any(piece in normalized_title for piece in bigrams):return None
    return title, extract[:1200], page.get("fullurl","")


def cached_web_fallback(query: str, language: str, allow_network: bool=True) -> str | None:
    key = _clean_question(query); now = datetime.now()
    con = _cache_connection()
    row = con.execute("SELECT title,summary,source_url,expires_at,status,confidence FROM search_cache WHERE query_norm=? AND language=?",
                      (key,language)).fetchone()
    if row and datetime.fromisoformat(row[3]) > now:
        con.close()
        if row[4] != "ok": return None
        return _format_web(row[0],row[1],row[2],row[5],language,True)
    if not allow_network:
        con.close(); return None
    try: result = _internet_lookup(query,language)
    except Exception: result = None
    lifetime = 30 if result else 1
    expires = now.fromtimestamp(now.timestamp()+lifetime*86400).isoformat(timespec="seconds")
    if result:
        title, summary, url = result; status="ok"; confidence=.68
    else:
        title=summary=url=""; status="miss"; confidence=0
    con.execute("INSERT OR REPLACE INTO search_cache VALUES(?,?,?,?,?,?,?,?,?)",
                (key,language,title,summary,url,now.isoformat(timespec="seconds"),expires,confidence,status))
    con.commit(); con.close()
    return _format_web(title,summary,url,confidence,language,False) if result else None


def _format_web(title, summary, url, confidence, language, cached):
    labels = {"zh":("联网缓存结果" if cached else "联网检索结果","来源","置信度"),
              "zh_hant":("聯網快取結果" if cached else "聯網檢索結果","來源","信心度"),
              "en":("Cached web result" if cached else "Web result","Source","confidence")}[language]
    return f"{labels[0]} — {title}：{summary}\n{labels[1]}：Wikipedia — {url}；{labels[2]} {confidence:.0%}"


def answer_encyclopedia(db_path: Path, query: str, language: str, allow_network=True) -> tuple[str|None,str|None]:
    """返回 (答案, entry_id)。排序严格为精确别名 > 精确标题 > 正文 BM25。"""
    if not db_path.exists(): return cached_web_fallback(query,language,allow_network), None
    database_key = str(db_path.resolve())
    if database_key not in _READY_ENCYCLOPEDIA_DATABASES:
        ensure_extended_knowledge(db_path)
        _READY_ENCYCLOPEDIA_DATABASES.add(database_key)
    con=sqlite3.connect(db_path); norm=normalize_text(_clean_question(query))
    lang = language
    # 问题包含术语，也允许纯术语。term_type=alias 优先级 3，title 为 2。
    rows=con.execute("""SELECT e.*,t.term_type,length(t.normalized_term) AS n
      FROM encyclopedia_terms t JOIN encyclopedia_entries e ON e.entry_id=t.entry_id
      WHERE t.language=? AND (t.normalized_term=? OR (length(t.normalized_term)>=? AND instr(?,t.normalized_term)>0))
      ORDER BY CASE WHEN t.normalized_term=? THEN 10 ELSE 0 END DESC,
               CASE t.term_type WHEN 'alias' THEN 3 ELSE 2 END DESC,
               e.confidence DESC,n DESC LIMIT 1""",
      (lang,norm,3 if lang=="en" else 2,norm,norm)).fetchone()
    if rows is None and lang != "en":
        rows=con.execute("""SELECT e.*,t.term_type,length(t.normalized_term) FROM encyclopedia_terms t
          JOIN encyclopedia_entries e ON e.entry_id=t.entry_id WHERE t.language IN('zh','zh_hant')
          AND (t.normalized_term=? OR (length(t.normalized_term)>=2 AND instr(?,t.normalized_term)>0))
          ORDER BY CASE WHEN t.normalized_term=? THEN 10 ELSE 0 END DESC,
                   CASE t.term_type WHEN 'alias' THEN 3 ELSE 2 END DESC,
                   e.confidence DESC,length(t.normalized_term) DESC LIMIT 1""",(norm,norm,norm)).fetchone()
    if rows is None:
        stop={"what","is","the","a","an","of","about","please","tell","me","什么","怎么","如何","为何","為何"}
        tokens = [token for token in re.findall(r"[\w\u3400-\u9fff]+", norm) if len(token)>1 and token not in stop]
        match = " AND ".join('"'+token.replace('"','')+'"' for token in tokens[:8])
        if match:
            try:
                hit=con.execute("SELECT entry_id,bm25(encyclopedia_fts,0,3,1) score FROM encyclopedia_fts WHERE language=? AND encyclopedia_fts MATCH ? ORDER BY score LIMIT 1",(lang,match)).fetchone()
                if hit: rows=con.execute("SELECT * FROM encyclopedia_entries WHERE entry_id=?",(hit[0],)).fetchone()
            except sqlite3.OperationalError: pass
    if rows is None:
        con.close(); return cached_web_fallback(query,language,allow_network),None
    entry_id=rows[0]; title_i,summary_i={"zh":(2,5),"zh_hant":(3,6),"en":(4,7)}[language]
    title,summary=rows[title_i],rows[summary_i]
    source,url,revision,confidence=rows[8:12]
    con.close()
    source_label={"zh":"本地知识库来源","zh_hant":"本地知識庫來源","en":"Local knowledge source"}[language]
    revision_label={"zh":"版本","zh_hant":"版本","en":"revision"}[language]
    confidence_label={"zh":"置信度","zh_hant":"信心度","en":"confidence"}[language]
    return f"{title}：{summary}\n{source_label}：{source} — {url}；{revision_label} {revision}；{confidence_label} {confidence:.0%}",entry_id


def looks_like_encyclopedia(text: str) -> bool:
    q=normalize_text(text)
    return bool(re.search(r"什么是|什麼是|是什么|是什麼|介绍|介紹|解释|解釋|多少|哪里|哪裡|为什么|為什麼|"
                          r"\b(?:what|where|why|who|define|explain|tell me about|how (?:does|do))\b",q))


def conflict_note(db_path: Path, subject_id: str, language: str) -> str:
    con=sqlite3.connect(db_path)
    rows=con.execute("SELECT value,qualifier,source,source_url,is_primary FROM knowledge_claims WHERE subject_id=? ORDER BY is_primary DESC,confidence DESC",(subject_id,)).fetchall()
    con.close()
    if len(rows)<2:return ""
    if language=="en":
        return "\nSources differ: the common account is " + rows[0][0] + f" ({rows[0][2]}); another account gives {rows[1][0]} ({rows[1][2]})."
    lead="常见说法" if language=="zh" else "常見說法"
    other="另有说法" if language=="zh" else "另有說法"
    return f"\n{lead}是{rows[0][0]}（{rows[0][2]}）；{other}为{rows[1][0]}（{rows[1][2]}）。"


LEGAL_ACTION_GUIDES = [
 {"id":"CN_UNPAID_WAGES","jurisdiction":"CN","topic":"unpaid_wages",
  "triggers":"欠薪|拖欠工资|不发工资|工资没发|克扣工资|未足额支付|unpaid wages|withheld wages",
  "title_zh":"中国大陆欠薪处理步骤","title_hant":"中國大陸欠薪處理步驟","title_en":"Mainland China unpaid-wage steps",
  "steps_zh":["核对劳动合同约定的发薪日、欠付月份和金额，列一张欠薪明细。","保存劳动合同、考勤/排班、工资条、银行流水、工作聊天、工牌和社保记录；只保存你有权取得的材料，不伪造或非法侵入系统。","用短信、邮件或书面通知向公司明确催告：欠薪期间、金额、付款期限和收款方式，并保留送达记录。","仍不支付时，可向工作地人力资源社会保障部门或劳动保障监察机构咨询、投诉；也可申请劳动争议调解。","可向有管辖权的劳动人事争议仲裁委员会申请仲裁。劳动关系存续期间的欠薪争议不受普通一年期间限制；劳动关系终止后，应自终止之日起一年内提出。","《劳动合同法》第30条还规定，拖欠或未足额支付劳动报酬时可依法向当地人民法院申请支付令；是否适合应结合证据及对方是否可能提出异议判断。"],
  "steps_hant":["核對勞動合同約定的發薪日、欠付月份和金額，列出欠薪明細。","保存勞動合同、考勤／排班、工資條、銀行流水、工作聊天、工牌和社保記錄；只保存你有權取得的材料，不偽造或非法侵入系統。","以短信、電郵或書面通知向公司明確催告欠薪期間、金額、付款期限和收款方式，並保存送達記錄。","仍不支付時，可向工作地人力資源社會保障部門或勞動保障監察機構諮詢、投訴；也可申請勞動爭議調解。","可向有管轄權的勞動人事爭議仲裁委員會申請仲裁。勞動關係存續期間的欠薪爭議不受普通一年期間限制；勞動關係終止後，應自終止日起一年內提出。","《勞動合同法》第30條亦規定，拖欠或未足額支付勞動報酬時可依法向當地人民法院申請支付令；是否適合應結合證據及對方是否可能提出異議判斷。"],
  "steps_en":["Check the contractual payday and make an itemised list of unpaid pay periods and amounts.","Preserve the employment contract, schedules or time records, payslips, bank statements, work messages, ID badge and social-insurance records. Keep only material you may lawfully access; do not fabricate evidence or enter systems without permission.","Send a traceable written demand stating the pay periods, amount, payment deadline and payment method.","If payment is still withheld, contact the labour administration or labour-inspection body where the work was performed, or request labour-dispute mediation.","A claim may be filed with the competent labour-dispute arbitration commission. During employment, the special rule for unpaid remuneration is not restricted by the ordinary one-year period; after employment ends, file within one year of termination.","Article 30 of the Labour Contract Law also permits an application to the local people's court for a payment order. Suitability depends on the evidence and whether the employer is likely to object."],
  "deadline_zh":"不要因协商而放任期限经过；离职后欠薪仲裁的一年期间尤其重要。",
  "deadline_hant":"不要因協商而放任期限經過；離職後欠薪仲裁的一年期間尤其重要。",
  "deadline_en":"Do not let informal negotiation consume a filing period; the one-year period after termination is especially important.",
  "sources":[("中华人民共和国人力资源和社会保障部：《劳动合同法》第30条","https://www.mohrss.gov.cn/xxgk2020/fdzdgknr/zcfg/fl/202011/t20201102_394622_wap.html","2026-09"),
             ("中华人民共和国人力资源和社会保障部：《劳动争议调解仲裁法》第5、27、28条","https://www.mohrss.gov.cn/SYrlzyhshbzb/ztzl/ldrszytjzc/fgzc/202207/t20220714_457857.html","2026-09"),
             ("中华人民共和国人力资源和社会保障部：《劳动保障监察条例》","https://www.mohrss.gov.cn/SYrlzyhshbzb/rdzt/gznmgqxwt/zcfg/201905/t20190531_318875.html","2026-09")]},
 {"id":"US_FED_UNPAID_WAGES","jurisdiction":"US-FED","topic":"unpaid_wages",
  "triggers":"unpaid wages|not paid|withheld wages|missing paycheck|欠薪|不发工资|拖欠工资|未付薪資|積欠工資",
  "title_zh":"美国联邦欠薪处理步骤","title_hant":"美國聯邦欠薪處理步驟","title_en":"U.S. federal unpaid-wage steps",
  "steps_zh":["记录雇主法定名称、工作地点、主管姓名、工作日期和时数、约定工资、实际付款及欠付金额，并保存工资单、排班、打卡、银行流水和沟通记录。","向雇主发送可留痕的书面付款要求；不要篡改工时或获取无权访问的公司材料。","如果涉及联邦最低工资或加班费，可向美国劳工部工资工时部门（WHD）投诉：1-866-487-9243。WHD说明投诉通常保密，雇主不得因投诉或配合调查而报复。","同时查询工作所在州及城市的劳工部门；州或地方规则可能适用更高最低工资、更短发薪周期或不同追偿程序。","FLSA通常有两年时效，故意违法通常为三年；应尽快行动。个案还可能通过州机关、法院或律师处理。"],
  "steps_hant":["記錄僱主法定名稱、工作地點、主管姓名、工作日期和時數、約定工資、實際付款及欠付金額，並保存工資單、排班、打卡、銀行流水和溝通記錄。","向僱主發送可留痕的書面付款要求；不要篡改工時或取得無權存取的公司材料。","若涉及聯邦最低工資或加班費，可向美國勞工部工資工時部門（WHD）投訴：1-866-487-9243。WHD說明投訴通常保密，僱主不得因投訴或配合調查而報復。","同時查詢工作所在州及城市的勞工部門；州或地方規則可能適用更高最低工資、更短發薪週期或不同追償程序。","FLSA通常有兩年時效，故意違法通常為三年；應儘快行動。個案還可能透過州機關、法院或律師處理。"],
  "steps_en":["Record the employer's legal name and work location, supervisor, dates and hours worked, promised rate, payments received and amount missing. Preserve pay stubs, schedules, time records, bank records and messages.","Send a traceable written payment demand. Do not alter time records or take company material you are not authorised to access.","For federal minimum-wage or overtime issues, a worker may contact the U.S. Department of Labor Wage and Hour Division at 1-866-487-9243. WHD states that complaints are generally confidential and retaliation for complaining or cooperating is prohibited.","Also check the labour agency for the state and city where the work occurred; state or local law may require a higher wage, a different payday, or another recovery procedure.","The FLSA generally has a two-year limitation period, extended to three years for wilful violations. Act promptly; state-agency, court, or attorney routes may also be available."],
  "deadline_zh":"联邦层面通常为两年，故意违法通常为三年；州法期限可能不同。",
  "deadline_hant":"聯邦層面通常為兩年，故意違法通常為三年；州法期限可能不同。",
  "deadline_en":"The usual federal period is two years and generally three years for wilful violations; state deadlines may differ.",
  "sources":[("U.S. Department of Labor, How to File a Complaint","https://www.dol.gov/agencies/whd/contact/complaints","2026-09"),
             ("U.S. Department of Labor, Complaints and Investigation FAQ","https://www.dol.gov/agencies/whd/faq/workers","2026-09"),
             ("U.S. Department of Labor, FLSA retaliation Fact Sheet 77A","https://www.dol.gov/agencies/whd/fact-sheets/77a-flsa-prohibiting-retaliation","2026-09")]},
 {"id":"TW_UNPAID_WAGES","jurisdiction":"TW","topic":"unpaid_wages",
  "triggers":"欠薪|不發薪水|不发工资|積欠工資|未付薪資|拖欠工资|unpaid wages|not paid salary",
  "title_zh":"中国台湾地区欠薪处理步骤","title_hant":"中國臺灣地區欠薪處理步驟","title_en":"Taiwan unpaid-wage steps",
  "steps_zh":["整理劳动契约、出勤和排班、薪资单、银行记录、工作讯息及欠薪月份和金额。只保存有权取得的资料。","向雇主发出可证明送达的书面催告，写明欠薪期间、金额和付款期限。","可向劳务提供地的直辖市或县（市）劳动行政主管机关申请免费劳资争议调解；可选择由调解人或调解委员会处理。","若雇主行为可能违反劳动法令，也可向地方劳动主管机关或劳动检查机构洽询、申诉或检举。","调解不成立后可评估仲裁或劳动诉讼；劳动部和部分地方政府设有符合条件者可申请的诉讼扶助。"],
  "steps_hant":["整理勞動契約、出勤和排班、薪資單、銀行記錄、工作訊息及欠薪月份和金額。只保存有權取得的資料。","向僱主發出可證明送達的書面催告，寫明欠薪期間、金額和付款期限。","可向勞務提供地的直轄市或縣（市）勞動行政主管機關申請免費勞資爭議調解；可選擇由調解人或調解委員會處理。","若僱主行為可能違反勞動法令，也可向地方勞動主管機關或勞動檢查機構洽詢、申訴或檢舉。","調解不成立後可評估仲裁或勞動訴訟；勞動部和部分地方政府設有符合條件者可申請的訴訟扶助。"],
  "steps_en":["Organise the employment agreement, attendance and schedules, payslips, bank records, work messages, and an itemised list of unpaid periods and amounts. Keep only information you may lawfully access.","Send the employer a traceable written demand specifying the period, amount and payment deadline.","Apply for free labour-dispute mediation with the municipal or county labour authority where the work was performed; the applicant may choose a mediator or mediation committee.","If labour law may have been violated, a worker may also enquire, complain or report to the local labour authority or labour-inspection body.","If mediation fails, assess arbitration or labour litigation. Ministry of Labor and some local-government assistance may be available to eligible workers."],
  "deadline_zh":"具体请求期限取决于请求类型和事实；应尽快向劳务提供地主管机关确认，不要只依赖口头协商。",
  "deadline_hant":"具體請求期限取決於請求類型和事實；應儘快向勞務提供地主管機關確認，不要只依賴口頭協商。",
  "deadline_en":"Deadlines depend on the claim and facts; promptly confirm them with the authority where the work was performed rather than relying only on oral negotiation.",
  "sources":[("臺灣勞動部：發生勞資爭議怎麼辦","https://www.mol.gov.tw/1607/28690/89680/","2026-09"),
             ("臺灣勞動部：勞資爭議調解","https://www.mol.gov.tw/1607/28162/28296/28410/28412/","2026-09"),
             ("臺灣勞動部：調解、仲裁、訴訟扶助","https://www.mol.gov.tw/1607/28162/28296/81778/81802/81951/","2026-09")]},
]


def ensure_legal_action_guides(db_path: Path) -> int:
    con=sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS legal_action_guides(
      guide_id TEXT PRIMARY KEY,jurisdiction TEXT NOT NULL,topic TEXT NOT NULL,triggers TEXT NOT NULL,
      title_zh TEXT NOT NULL,title_hant TEXT NOT NULL,title_en TEXT NOT NULL,
      steps_zh TEXT NOT NULL,steps_hant TEXT NOT NULL,steps_en TEXT NOT NULL,
      deadline_zh TEXT NOT NULL,deadline_hant TEXT NOT NULL,deadline_en TEXT NOT NULL,
      revision TEXT NOT NULL,confidence REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS legal_action_sources(
      guide_id TEXT NOT NULL,source_name TEXT NOT NULL,source_url TEXT NOT NULL,revision TEXT NOT NULL,
      confidence REAL NOT NULL,PRIMARY KEY(guide_id,source_url))""")
    for guide in LEGAL_ACTION_GUIDES:
        con.execute("INSERT OR REPLACE INTO legal_action_guides VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (guide["id"],guide["jurisdiction"],guide["topic"],guide["triggers"],guide["title_zh"],guide["title_hant"],guide["title_en"],
           json.dumps(guide["steps_zh"],ensure_ascii=False),json.dumps(guide["steps_hant"],ensure_ascii=False),json.dumps(guide["steps_en"],ensure_ascii=False),
           guide["deadline_zh"],guide["deadline_hant"],guide["deadline_en"],"2026-09",.94))
        con.execute("DELETE FROM legal_action_sources WHERE guide_id=?",(guide["id"],))
        con.executemany("INSERT INTO legal_action_sources VALUES(?,?,?,?,?)",
          ((guide["id"],name,url,revision,.96) for name,url,revision in guide["sources"]))
    con.commit();count=con.execute("SELECT count(*) FROM legal_action_guides").fetchone()[0];con.close();return count


def looks_like_legal_action(text: str) -> bool:
    q=normalize_text(text)
    wage=bool(re.search(r"欠薪|积欠.*(?:工资|薪资|薪水)|拖欠.*(?:工资|薪资|薪水)|压了?.*(?:工资|薪资|薪水)|不(?:发|付).*(?:工资|薪资|薪水)|"
                        r"(?:工资|薪资|薪水).*没发|未付.*(?:工资|薪资|薪水)|克扣.*(?:工资|薪资)|"
                        r"unpaid wages|not (?:been )?paid|missing paycheck|withheld.*(?:wages|paychecks?)|"
                        r"(?:paychecks?|wages).*(?:withheld|missing|not paid)",q,re.I))
    action=bool(re.search(r"怎么办|怎麼辦|怎么处理|怎麼處理|如何维权|如何維權|如何投诉|如何投訴|"
                          r"what (?:can|should) i do|how (?:can|do) i (?:file|recover|complain)",q,re.I))
    return wage and (action or bool(re.search(r"公司|老板|老闆|雇主|僱主|employer|boss",q,re.I)))


def answer_legal_action(db_path: Path, query: str, language: str, jurisdiction: str|None, region: str="") -> str|None:
    if not looks_like_legal_action(query):return None
    ensure_legal_action_guides(db_path)
    # 未说明地区时仍给出立即可做且跨法域安全的步骤，再明确追问，避免只回复一句“请补充地区”。
    if jurisdiction is None:
        return {"zh":"欠薪处理高度依赖工作地。请告诉我工作实际发生在中国大陆哪个城市、美国哪个州和城市，还是中国台湾地区。你现在可以先：保存合同、考勤、工资条、银行流水和沟通记录；列明欠薪月份与金额；以可留痕的书面方式催告。不要伪造证据、威胁他人或擅自拿走无权取得的公司资料。",
                "zh_hant":"欠薪處理高度依賴工作地。請告訴我工作實際發生在中國大陸哪個城市、美國哪個州和城市，還是中國臺灣地區。你現在可以先：保存契約、考勤、薪資單、銀行流水和溝通記錄；列明欠薪月份與金額；以可留痕的書面方式催告。不要偽造證據、威脅他人或擅自拿走無權取得的公司資料。",
                "en":"Unpaid-wage remedies depend on where the work occurred. Please give the mainland-China city, the U.S. state and city, or Taiwan. For now, preserve the contract, time and pay records, bank records and messages; itemise each missing pay period and amount; and send a traceable written demand. Do not fabricate evidence, threaten anyone, or take company material you may not access."}[language]
    requested=jurisdiction
    if jurisdiction=="CN-LOCAL":jurisdiction="CN"
    if jurisdiction in {"US-STATE","US-UNSPECIFIED"}:jurisdiction="US-FED"
    if jurisdiction not in {"CN","US-FED","TW"}:return None
    con=sqlite3.connect(db_path)
    row=con.execute("SELECT * FROM legal_action_guides WHERE jurisdiction=? AND topic='unpaid_wages'",(jurisdiction,)).fetchone()
    sources=con.execute("SELECT source_name,source_url,revision FROM legal_action_sources WHERE guide_id=? ORDER BY source_url",(row[0],)).fetchall() if row else []
    con.close()
    if not row:return None
    title_i,steps_i,deadline_i={"zh":(4,7,10),"zh_hant":(5,8,11),"en":(6,9,12)}[language]
    title=row[title_i];steps=json.loads(row[steps_i]);deadline=row[deadline_i]
    lines=[title]+[f"{i}. {step}" for i,step in enumerate(steps,1)]
    if requested in {"US-STATE","US-UNSPECIFIED"}:
        place=region if requested=="US-STATE" else ""
        lines.append({"zh":f"地域提醒：以上是联邦路径；还必须核对{place or '工作所在州和城市'}的工资法和投诉机关。",
                      "zh_hant":f"地域提醒：以上是聯邦途徑；還必須核對{place or '工作所在州和城市'}的工資法和申訴機關。",
                      "en":f"Location note: this is the federal route. You must also check wage law and the enforcement agency for {place or 'the state and city where the work occurred'}."}[language])
    elif requested=="CN-LOCAL":
        lines.append({"zh":f"地域提醒：请向{region}实际工作地有管辖权的机关核实受理窗口和本地规则。",
                      "zh_hant":f"地域提醒：請向{region}實際工作地有管轄權的機關核實受理窗口和本地規則。",
                      "en":f"Location note: confirm the competent filing office and local rules where the work was performed in {region}."}[language])
    lines.append(({"zh":"期限提醒：","zh_hant":"期限提醒：","en":"Deadline note: "}[language])+deadline)
    lines.append({"zh":"官方来源：","zh_hant":"官方來源：","en":"Official sources:"}[language])
    lines.extend(f"- {name} — {url}（{revision}）" for name,url,revision in sources)
    lines.append({"zh":"这是一份一般办事指南，不替代律师根据合同、金额、身份和所在地提供的个案意见。若公司倒闭、转移财产、以暴力威胁或期限临近，应尽快联系当地主管机关或律师。",
                  "zh_hant":"這是一份一般辦事指南，不替代律師依契約、金額、身分和所在地提供的個案意見。若公司倒閉、轉移財產、以暴力威脅或期限臨近，應儘快聯絡當地主管機關或律師。",
                  "en":"This is a general procedural guide, not case-specific legal advice. If the employer is insolvent, moving assets, making threats, or a deadline is near, promptly contact the local authority or a lawyer."}[language])
    return "\n".join(lines)


UNIT_ALIASES={
 "mm":("length",Decimal("0.001")),"毫米":("length",Decimal("0.001")),
 "cm":("length",Decimal("0.01")),"厘米":("length",Decimal("0.01")),"公分":("length",Decimal("0.01")),
 "m":("length",Decimal(1)),"米":("length",Decimal(1)),"公尺":("length",Decimal(1)),
 "km":("length",Decimal(1000)),"千米":("length",Decimal(1000)),"公里":("length",Decimal(1000)),
 "in":("length",Decimal("0.0254")),"inch":("length",Decimal("0.0254")),"英寸":("length",Decimal("0.0254")),
 "ft":("length",Decimal("0.3048")),"foot":("length",Decimal("0.3048")),"英尺":("length",Decimal("0.3048")),
 "mile":("length",Decimal("1609.344")),"mi":("length",Decimal("1609.344")),"英里":("length",Decimal("1609.344")),
 "mg":("mass",Decimal("0.000001")),"毫克":("mass",Decimal("0.000001")),
 "g":("mass",Decimal("0.001")),"克":("mass",Decimal("0.001")),
 "kg":("mass",Decimal(1)),"千克":("mass",Decimal(1)),"公斤":("mass",Decimal(1)),
 "lb":("mass",Decimal("0.45359237")),"pound":("mass",Decimal("0.45359237")),"磅":("mass",Decimal("0.45359237")),
 "b":("data",Decimal(1)),"byte":("data",Decimal(1)),"字节":("data",Decimal(1)),"位元组":("data",Decimal(1)),
 "kb":("data",Decimal(1000)),"mb":("data",Decimal(1000000)),"gb":("data",Decimal(1000000000)),
 "kib":("data",Decimal(1024)),"mib":("data",Decimal(1048576)),"gib":("data",Decimal(1073741824)),
}


def _nice_decimal(value: Decimal) -> str:
    with localcontext() as ctx: ctx.prec=28; rendered=format(+value,"f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _safe_numeric_eval(expression: str, variables: dict[str, Decimal]) -> Decimal:
    """只计算数字、x/y 和基础运算；不允许属性、下标、调用或任意 Python。"""
    def visit(node):
        if isinstance(node,ast.Expression):return visit(node.body)
        if isinstance(node,ast.Constant) and isinstance(node.value,(int,float)):
            return Decimal(str(node.value))
        if isinstance(node,ast.Name) and node.id in variables:return variables[node.id]
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            value=visit(node.operand);return value if isinstance(node.op,ast.UAdd) else -value
        if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Pow)):
            left,right=visit(node.left),visit(node.right)
            if isinstance(node.op,ast.Add):return left+right
            if isinstance(node.op,ast.Sub):return left-right
            if isinstance(node.op,ast.Mult):return left*right
            if isinstance(node.op,ast.Div):return left/right
            if right!=right.to_integral_value() or abs(right)>10:raise ValueError("exponent rejected")
            return left**int(right)
        raise ValueError("unsupported expression")
    return visit(ast.parse(expression,mode="eval"))


def looks_like_utility(text: str) -> bool:
    q=normalize_text(text)
    return bool(re.search(r"换算|转换|等于多少|convert|difference between|相差几|相差幾|时区|時區|timezone|"
                          r"(?:℃|°c|°f|华氏|華氏|摄氏|攝氏|\b(?:km|cm|mm|kg|lb|gib|mib|gb|mb)\b)",q))


def answer_utility(text: str, language: str) -> str | None:
    q=normalize_text(text).replace("，",",")
    dates=re.findall(r"(?<!\d)(\d{4}-\d{1,2}-\d{1,2})(?!\d)",q)
    if len(dates)>=2 and re.search(r"相差|difference|间隔|間隔",q):
        try: days=abs((datetime.fromisoformat(dates[1])-datetime.fromisoformat(dates[0])).days)
        except ValueError:return None
        return {"zh":f"日期相差 {days} 天。","zh_hant":f"日期相差 {days} 天。","en":f"The dates are {days} days apart."}[language]
    tm=re.search(r"(\d{4}-\d\d?-\d\d?[ t]\d\d?:\d\d?)",q)
    zones={"北京时间":"Asia/Shanghai","北京":"Asia/Shanghai","上海":"Asia/Shanghai","台北":"Asia/Taipei","臺北":"Asia/Taipei",
           "纽约":"America/New_York","紐約":"America/New_York","new york":"America/New_York","伦敦":"Europe/London","倫敦":"Europe/London","london":"Europe/London",
           "东京":"Asia/Tokyo","東京":"Asia/Tokyo","tokyo":"Asia/Tokyo","utc":"UTC"}
    found=[(name,zone) for name,zone in zones.items() if normalize_text(name) in q]
    if tm and len(found)>=2:
        dt=datetime.fromisoformat(tm.group(1).replace(" ","T")); converted=dt.replace(tzinfo=ZoneInfo(found[0][1])).astimezone(ZoneInfo(found[1][1]))
        return {"zh":f"换算结果：{converted:%Y-%m-%d %H:%M}（{found[1][1]}）。",
                "zh_hant":f"換算結果：{converted:%Y-%m-%d %H:%M}（{found[1][1]}）。",
                "en":f"Converted time: {converted:%Y-%m-%d %H:%M} ({found[1][1]})."}[language]
    temp=re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*)?(c|f|k|℃|℉|摄氏(?:度)?|攝氏(?:度)?|華氏(?:度)?|华氏(?:度)?|開爾文|开尔文).*?(?:到|为|爲|换成|換成|换算(?:成)?|換算(?:成)?|转换(?:成)?|轉換(?:成)?|to|in)\s*(?:°\s*)?(c|f|k|℃|℉|摄氏(?:度)?|攝氏(?:度)?|華氏(?:度)?|华氏(?:度)?|開爾文|开尔文)",q)
    if temp:
        n=Decimal(temp.group(1)); src=temp.group(2); dst=temp.group(3)
        kind=lambda u:"c" if u in {"c","℃","摄氏","摄氏度","攝氏","攝氏度"} else ("f" if u in {"f","℉","华氏","華氏","华氏度","華氏度"} else "k")
        s,d=kind(src),kind(dst); kelvin=n+Decimal("273.15") if s=="c" else ((n-32)*Decimal(5)/9+Decimal("273.15") if s=="f" else n)
        out=kelvin-Decimal("273.15") if d=="c" else ((kelvin-Decimal("273.15"))*Decimal(9)/5+32 if d=="f" else kelvin)
        symbol={"c":"°C","f":"°F","k":"K"}[d]
        return {"zh":f"换算结果：{_nice_decimal(out)} {symbol}。","zh_hant":f"換算結果：{_nice_decimal(out)} {symbol}。","en":f"Converted value: {_nice_decimal(out)} {symbol}."}[language]
    match=re.search(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z]+|毫米|厘米|公分|千米|公里|公尺|米|英寸|英尺|英里|毫克|千克|公斤|克|磅|字节|位元组)\s*(?:换算(?:成)?|轉換(?:成)?|转换(?:成)?|等于|等於|to|in)\s*([a-zA-Z]+|毫米|厘米|公分|千米|公里|公尺|米|英寸|英尺|英里|毫克|千克|公斤|克|磅|字节|位元组)",q)
    if match:
        number=Decimal(match.group(1)); src=match.group(2).casefold(); dst=match.group(3).casefold()
        if src in UNIT_ALIASES and dst in UNIT_ALIASES and UNIT_ALIASES[src][0]==UNIT_ALIASES[dst][0]:
            out=number*UNIT_ALIASES[src][1]/UNIT_ALIASES[dst][1]
            return {"zh":f"换算结果：{_nice_decimal(number)} {match.group(2)} = {_nice_decimal(out)} {match.group(3)}。",
                    "zh_hant":f"換算結果：{_nice_decimal(number)} {match.group(2)} = {_nice_decimal(out)} {match.group(3)}。",
                    "en":f"Converted value: {_nice_decimal(number)} {match.group(2)} = {_nice_decimal(out)} {match.group(3)}."}[language]
    return None


def advanced_math(text: str, language: str) -> str | None:
    """不猜答案的中学数学模式；只有明确匹配时才返回。"""
    q=normalize_text(text).replace("×","*").replace("−","-").replace("²","^2")
    prefix={"zh":"计算结果：","zh_hant":"計算結果：","en":"Result: "}[language]
    function=re.search(r"f\s*\(\s*x\s*\)\s*=\s*([0-9x+\-*/ ().]+).*?f\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)",q,re.I)
    if function:
        expr=re.sub(r"(?<=\d)x\b","*x",function.group(1).strip())
        try: value=_safe_numeric_eval(expr,{"x":Decimal(function.group(2))})
        except Exception:return None
        return prefix+_nice_decimal(Decimal(value))
    proportion=re.search(r"(-?\d+(?:\.\d+)?)\s*:\s*(-?\d+(?:\.\d+)?)\s*=\s*x\s*:\s*(-?\d+(?:\.\d+)?)",q,re.I)
    if proportion:
        a,b,c=map(Decimal,proportion.groups())
        if b==0:return None
        return prefix+"x = "+_nice_decimal(a*c/b)
    radical=re.search(r"(?:化简|化簡|simplify)\s*(?:根式|radical)?\s*(?:sqrt|√)\s*\(?\s*(\d+)\s*\)?",q,re.I)
    if radical:
        n=int(radical.group(1)); outside=1
        for factor in range(math.isqrt(n),1,-1):
            if n%(factor*factor)==0: outside=factor; break
        inside=n//(outside*outside)
        rendered=str(outside) if inside==1 else (("" if outside==1 else str(outside))+f"√{inside}")
        return prefix+rendered
    factor_match=re.search(r"(?:因式分解|因式分解為|factor(?:ize)?)\s*[:：]?\s*x(?:\^2|\*\*2)\s*([+\-])\s*(\d+)\s*x\s*([+\-])\s*(\d+)",q,re.I)
    if factor_match:
        b=Decimal(("-" if factor_match.group(1)=="-" else "")+factor_match.group(2))
        c=Decimal(("-" if factor_match.group(3)=="-" else "")+factor_match.group(4))
        disc=b*b-4*c
        if disc<0 or disc!=disc.to_integral_value():return None
        root=disc.sqrt()
        r1=(-b+root)/2; r2=(-b-root)/2
        if r1!=r1.to_integral_value() or r2!=r2.to_integral_value():return None
        def term(root): return f"(x {'-' if root>=0 else '+'} {_nice_decimal(abs(root))})"
        return prefix+term(r1)+term(r2)
    # 二元一次方程组：2x+y=5, x-y=1
    system_text=re.sub(r"^(?:请|請)?(?:解|求解)?(?:二元一次)?方程(?:组|組)\s*[:：]?\s*","",q)
    system_text=re.sub(r"^solve(?: the)? system\s*[:：]?\s*","",system_text)
    equations=re.findall(r"([^,，;；\n]+=[^,，;；\n]+)",system_text)
    if len(equations)==2 and re.search(r"方程组|方程組|system",q):
        def coeff(eq,var):
            left,right=eq.split("=",1); expr=(left+"-("+right+")").replace(" ","")
            expr=re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([xy])",r"\1*\2",expr)
            def val(x,y): return _safe_numeric_eval(expr,{"x":Decimal(x),"y":Decimal(y)})
            c=val(0,0); return val(1,0)-c,val(0,1)-c,-c
        try:
            a,b,e=coeff(equations[0],"x"); c,d,f=coeff(equations[1],"x"); det=a*d-b*c
            if det==0:return None
            x=(e*d-b*f)/det; y=(a*f-e*c)/det
            return prefix+f"x = {x:g}, y = {y:g}"
        except Exception:return None
    # 一元二次方程，通过 f(0), f(1), f(2) 恢复系数，AST安全性由只允许字符保证。
    if "=" in q and re.search(r"x\s*(?:\^\s*2|\*\*\s*2)",q) and re.fullmatch(r"[\d.xX+\-*/^=()\s解方程求solve]+",q,re.I):
        raw=re.sub(r"^(?:解方程|求解|solve(?: the)? equation)[:： ]*","",q,flags=re.I)
        left,right=raw.split("=",1); expr=(left+"-("+right+")").replace("^","**")
        expr=re.sub(r"(?<=\d)x\b","*x",expr)
        try:
            f0=_safe_numeric_eval(expr,{"x":Decimal(0)}); f1=_safe_numeric_eval(expr,{"x":Decimal(1)}); f2=_safe_numeric_eval(expr,{"x":Decimal(2)})
            c=f0; a=(f2-2*f1+c)/2; b=f1-a-c; disc=b*b-4*a*c
            if a==0 or disc<0:return prefix+({"zh":"实数范围内无解","zh_hant":"實數範圍內無解","en":"no real solution"}[language])
            root=disc.sqrt(); x1=(-b+root)/(2*a); x2=(-b-root)/(2*a)
            return prefix+f"x = {_nice_decimal(x1)}"+(f" 或 x = {_nice_decimal(x2)}" if x1!=x2 and language!="en" else (f" or x = {_nice_decimal(x2)}" if x1!=x2 else ""))
        except Exception:return None
    nums=[Decimal(n) for n in re.findall(r"-?\d+(?:\.\d+)?",q)]
    if re.search(r"平均数|平均數|mean|average",q) and nums:
        return prefix+_nice_decimal(sum(nums)/len(nums))
    if re.search(r"中位数|中位數|median",q) and nums:
        values=sorted(nums); n=len(values); result=values[n//2] if n%2 else (values[n//2-1]+values[n//2])/2
        return prefix+_nice_decimal(result)
    if re.search(r"圆.*面积|圓.*面積|area of (?:a )?circle",q) and nums:
        area=Decimal(str(math.pi))*nums[0]*nums[0]; return prefix+_nice_decimal(area)+"（π 取 Python math.pi）"
    if re.search(r"三角形.*面积|三角形.*面積|area of (?:a )?triangle",q) and len(nums)>=2:
        return prefix+_nice_decimal(nums[0]*nums[1]/2)
    if re.search(r"长方形.*面积|長方形.*面積|area of (?:a )?rectangle",q) and len(nums)>=2:
        return prefix+_nice_decimal(nums[0]*nums[1])
    if re.search(r"长方体.*体积|長方體.*體積|volume of (?:a )?(?:rectangular|cuboid)",q) and len(nums)>=3:
        return prefix+_nice_decimal(nums[0]*nums[1]*nums[2])
    if re.search(r"(?:抛|擲|掷).*硬币|coin.*probab",q):
        return prefix+"1/2（50%）"
    if re.search(r"(?:抛|擲|掷).*骰子|die.*probab",q):
        return prefix+"1/6（≈ 16.6667%）"
    return None


SUPPORTED_FILE_SUFFIXES={".txt",".md",".markdown",".py",".js",".ts",".json",".yaml",".yml",".csv",".c",".cpp",".h",".hpp",".java",".rs",".go",".sh"}


class FileContext:
    def __init__(self): self.path=None; self.lines=[]
    def open(self,path: str) -> str:
        candidate=Path(path).expanduser().resolve()
        if not candidate.is_file(): raise ValueError("文件不存在或不是普通文件")
        if candidate.suffix.casefold() not in SUPPORTED_FILE_SUFFIXES: raise ValueError("目前支持 TXT、Markdown、代码、JSON/YAML 和 CSV")
        if candidate.stat().st_size>10*1024*1024: raise ValueError("文件超过 10 MiB 安全上限")
        self.lines=candidate.read_text(encoding="utf-8-sig",errors="replace").splitlines(); self.path=candidate
        return f"已读取 {candidate.name}（{len(self.lines)} 行）。现在可以询问内容；回答会标出行号。"
    def clear(self): self.path=None; self.lines=[]
    def answer(self,question: str,language: str) -> str | None:
        if not self.path:return None
        if re.search(r"总结|總結|概括|summary|summarize",question,re.I):
            selected=[(i,l) for i,l in enumerate(self.lines,1) if l.strip()][:25]
        else:
            norm_question=normalize_text(question)
            terms=set(re.findall(r"[a-z0-9_]{2,}",norm_question))
            han_runs=re.findall(r"[\u3400-\u9fff]{2,}",norm_question)
            terms.update(run[i:i+2] for run in han_runs for i in range(len(run)-1))
            scored=[]
            for i,line in enumerate(self.lines,1):
                norm=normalize_text(line); score=sum(3 if t in norm else 0 for t in terms)
                if score: scored.append((score,i,line))
            if not scored:return {"zh":"在当前文件中没有找到足够相关的内容，我不编造答案。","zh_hant":"在目前檔案中沒有找到足夠相關的內容，我不編造答案。","en":"I could not find sufficiently relevant content in the current file, so I will not invent an answer."}[language]
            scored.sort(reverse=True); selected=[]
            for _,line_no,_ in scored[:4]:
                for n in range(max(1,line_no-2),min(len(self.lines),line_no+2)+1):
                    if (n,self.lines[n-1]) not in selected:selected.append((n,self.lines[n-1]))
        excerpt="\n".join(f"[{self.path.name}:L{n}] {line}" for n,line in selected[:30])
        lead={"zh":"根据本地文件，最相关的原文如下：","zh_hant":"根據本機檔案，最相關的原文如下：","en":"The most relevant passages in the local file are:"}[language]
        return lead+"\n"+excerpt


class ConversationMemory:
    """保留有限会话窗口，并以轻量主题索引支持跨语言追问。"""
    def __init__(self,max_turns=64):
        self.max_turns=max_turns
        self.turns=deque(maxlen=max_turns)
        self.last_subject=""
        self.last_intent=""
        self.resolved_intent=""

    def _recent_turn(self,intent=""):
        for turn in reversed(self.turns):
            if not intent or turn["intent"]==intent:
                return turn
        return None

    def _recent_subject(self):
        turn=self._recent_turn()
        for candidate in reversed(self.turns):
            if candidate.get("subject"):
                return candidate["subject"]
        return self.last_subject

    def resolve(self,text: str) -> str:
        self.resolved_intent=""
        jurisdiction_pattern=(r"中国大陆|中國大陸|北京|上海|广东|廣東|美国|美國|[A-Za-z ]+州|臺灣|台湾|"
                              r"\b(?:china|u\.?s\.?|united states|taiwan|california|texas|new york)\b")
        if re.fullmatch(r"\s*(?:"+jurisdiction_pattern+r")\s*[。.!！?？]*\s*",text,re.I):
            legal_turn=self._recent_turn("legal")
            if legal_turn:
                self.resolved_intent="legal"
                return f"{legal_turn['user']}；适用地区：{text}"
        subject=self._recent_subject()
        if subject and re.search(r"他|她|它|他们|她們|那件事|后来|後來|that person|what happened next|\bit\b",text,re.I):
            return f"{text}（上下文主题：{subject}）"
        return text

    def remember(self,user,assistant,intent,subject=""):
        self.turns.append({"user":user,"assistant":assistant,"intent":intent,"subject":subject})
        if subject:
            self.last_subject=subject
        self.last_intent=intent


def build_eval_dataset(path: Path=EVAL_PATH) -> int:
    records=[]
    for a in range(-20,31):
        for b in range(1,21):
            records.append({"category":"math","language":"neutral","prompt":f"{a}+{b}","expected":str(a+b)})
    for unit_prompt,expected in [("1 km换算成m","1000"),("0°C to °F","32"),("1 GiB to B","1073741824"),("2024-01-01和2024-01-31相差几天","30")]:
        for variant in (unit_prompt,unicodedata.normalize("NFKC",unit_prompt),unit_prompt.upper()):
            records.append({"category":"utility","language":"mixed","prompt":variant,"expected":expected})
    for row in ENCYCLOPEDIA:
        for lang,title in (("zh",row[2]),("zh_hant",row[3]),("en",row[4])):
            prompt=(f"什么是{title}" if lang=="zh" else (f"什麼是{title}" if lang=="zh_hant" else f"What is {title}?"))
            records.append({"category":"encyclopedia","language":lang,"prompt":prompt,"expected_id":row[0]})
    # 对实际下载的知识做确定性抽样，而不把整个数据库一次性读入内存。
    # 只选择三语标题在各自索引中唯一的条目，避免同义重复页让固定 ID 测试产生假失败。
    database_path = ROOT / "localai_knowledge.db"
    if database_path.exists():
        con = sqlite3.connect(database_path)
        sampled = 0
        for entry_id,title_zh,title_hant,title_en in con.execute("""SELECT entry_id,title_zh,title_hant,title_en
            FROM encyclopedia_entries WHERE entry_id LIKE 'wikipedia_zh_%'
            ORDER BY entry_id"""):
            cases = []
            for lang,title in (("zh",title_zh),("zh_hant",title_hant),("en",title_en)):
                norm = normalize_text(title)
                matches = con.execute("""SELECT count(DISTINCT entry_id) FROM encyclopedia_terms
                    WHERE language=? AND normalized_term=?""",(lang,norm)).fetchone()[0]
                if matches != 1:
                    break
                prompt = (f"什么是{title}" if lang=="zh" else
                          (f"什麼是{title}" if lang=="zh_hant" else f"What is {title}?"))
                cases.append({"category":"expanded_encyclopedia","language":lang,
                              "prompt":prompt,"expected_id":entry_id})
            if len(cases) == 3:
                records.extend(cases); sampled += 1
                if sampled >= 150:
                    break
        con.close()
    advanced_cases=[("解方程 x^2-5x+6=0","x = 3"),("解方程组 2x+y=5, x-y=1","x = 2"),
      ("因式分解 x^2-5x+6","(x - 3)(x - 2)"),("已知 f(x)=2x+3，求 f(4)","11"),
      ("比例 3:5=x:20","x = 12"),("化简根式 sqrt(72)","6√2"),
      ("半径3的圆面积","28.274"),("底为6高为4的三角形面积","12"),
      ("平均数 1,2,3,4","2.5"),("中位数 1,8,3","3"),
      ("抛一枚硬币正面概率","1/2"),("掷骰子得到6的概率","1/6")]
    records += [{"category":"advanced","language":"zh","prompt":q,"expected":expected} for q,expected in advanced_cases]
    legal_action_cases=[("公司不发工资怎么办","工作实际发生"),
      ("中国大陆公司拖欠工资怎么办","劳动人事争议仲裁委员会"),
      ("上海公司不发工资怎么办","上海实际工作地"),
      ("美國聯邦雇主不付薪水怎麼辦","1-866-487-9243"),
      ("美国加州雇主拖欠工资怎么办","California"),
      ("臺灣公司積欠工資怎麼辦","免費勞資爭議調解"),
      ("My employer in the United States has not paid me. What should I do?","state and city"),
      ("Under U.S. federal law, how can I recover unpaid wages?","Wage and Hour Division"),
      ("My employer in Taiwan has not paid my salary. What should I do?","free labour-dispute mediation")]
    records += [{"category":"legal_action","language":"mixed","prompt":q,"expected":expected} for q,expected in legal_action_cases]
    from legal_life_guides import life_eval_cases
    records += [{"category":"life_legal","language":"mixed","prompt":q,"expected":expected}
                for q,expected in life_eval_cases()]
    from daily_life_guides import daily_life_eval_cases
    records += [{"category":"daily_life","language":"mixed","prompt":q,"expected":expected}
                for q,_,expected in daily_life_eval_cases()]
    edge=["","　","🤔","不存在的虚构概念ZXQ-1947是什么","DROP TABLE encyclopedia_entries;","2**100001"]
    records += [{"category":"edge","language":"mixed","prompt":q,"expected":"safe"} for q in edge]
    with path.open("w",encoding="utf-8") as out:
        for item in records: out.write(json.dumps(item,ensure_ascii=False)+"\n")
    return len(records)
