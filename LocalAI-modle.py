#!/usr/bin/env python3
"""可训练、离线运行的迷你简繁中英聊天模型。

这是字节级 n-gram 意图分类器，不是大语言模型。UTF-8 tokenizer 能表示所有
Unicode 字符；小型分类器负责学习基础对话。运行：train / chat / test。
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

try:
    import resource
except ImportError:  # Windows 标准库没有 resource；不因此增加第三方依赖。
    resource = None

from legal_life_guides import answer_life_legal, ensure_life_legal_guides, looks_like_life_legal
from daily_life_guides import (answer_daily_life, classify_daily_life,
                               ensure_daily_life_guides, looks_like_daily_life)

from localai_extensions import (
    ConversationMemory, FileContext, advanced_math, answer_encyclopedia,
    answer_legal_action, answer_utility, build_eval_dataset, conflict_note,
    ensure_extended_knowledge, ensure_legal_action_guides, looks_like_encyclopedia,
    looks_like_legal_action, looks_like_utility, normalize_text,
)
from knowledge_expansion import import_general_knowledge, knowledge_statistics

MODEL_PATH = Path(__file__).with_name("localai_tiny_model.npz")
DATABASE_PATH = Path(__file__).with_name("localai_knowledge.db")
TRAINING_DOCUMENT_PATH = Path(__file__).with_name("localai_multilingual_training.jsonl")
REQUIREMENTS_PATH = Path(__file__).with_name("requirements.txt")
MODEL_BACKUP_ROOT = Path(__file__).with_name("model_backups")
# v2 把哈希特征扩展到约 18 万维；21 个 float64 类别权重约 30.3 MB（十进制）。
# 未见过的桶保持 0，训练只计算实际出现的稀疏特征，避免构造数 GB 的训练矩阵。
MODEL_FORMAT_VERSION = 2
VOCAB_SIZE, NGRAM_BUCKETS = 256, 180000
FEATURE_SIZE, SEED = VOCAB_SIZE + NGRAM_BUCKETS, 42
MODEL_TARGET_MIN_BYTES, MODEL_TARGET_MAX_BYTES = 25_000_000, 35_000_000

TRAINING_DATA = {
    "greeting": ["你好", "你好呀", "您好", "嗨", "嗨呀", "哈喽", "早上好", "晚上好", "跟我说你好", "说你好",
                 "跟我打个招呼", "很高兴见到你", "早安", "晚安你好", "請向我問好", "說你好",
                 "跟我打個招呼", "很高興見到你", "向我问好", "向我問好", "有空聊聊吗", "方便聊聊嗎", "hello", "hi", "hey", "nice to meet you",
                 "good morning", "good evening", "say hello", "greet me"],
    "farewell": ["再见", "拜拜", "下次见", "我要走了", "回头见", "结束聊天", "再見", "下次見",
                 "回頭見", "結束聊天", "我们下次再聊", "我們下次再聊", "goodbye", "bye", "see you",
                 "talk to you later", "let us chat again later", "exit chat"],
    "thanks": ["谢谢", "多谢", "感谢你", "谢谢你的帮助", "太感谢了", "謝謝", "多謝", "感謝你",
               "謝謝你的幫助", "thanks", "thank you", "many thanks", "thanks for your help", "I really appreciate your help",
               "I appreciate your assistance", "I am grateful for your help"],
    "identity": ["你是谁", "你叫什么", "介绍一下自己", "你的名字是什么", "你是机器人吗", "说说你自己", "怎么称呼你", "你是誰",
                 "你叫什麼", "介紹一下自己", "你的名字是什麼", "你是機器人嗎", "who are you",
                 "what is your name", "introduce yourself", "are you a robot", "tell me about yourself", "what should I call you"],
    "ability": ["你会做什么", "你能做什么", "你有什么功能", "怎么使用你", "帮助", "你會做什麼",
                "你能做什麼", "你會做哪些事情", "你能幫我做什麼", "你有什麼功能", "怎麼使用你", "幫助", "what can you do", "help",
                "how can I use you", "what are your abilities", "how can you help me", "what tasks can you handle"],
    "wellbeing": ["你好吗", "最近怎么样", "你今天怎么样", "过得好吗", "感觉如何", "你好嗎",
                  "最近怎麼樣", "你今天怎麼樣", "過得好嗎", "感覺如何", "how are you",
                  "how are you doing", "are you okay", "how is it going", "how have you been"],
    "positive": ["我很好", "很好", "不错", "我很开心", "今天很棒", "还可以", "不錯", "我很開心", "今天一切顺利", "事情進展順利",
                 "還可以", "感觉非常好", "感覺非常好", "I am fine", "great", "not bad", "I am happy",
                 "I feel very good", "I feel wonderful", "today is good"],
    "negative": ["我不好", "我很难过", "心情不好", "今天很糟", "我累了", "我很難過", "I feel bad",
                 "I am sad", "not good", "today is awful", "I am tired"],
    "time": ["现在几点", "告诉我时间", "当前时间", "几点了", "现在是什么时间", "現在幾點",
             "告訴我時間", "目前時間", "幾點了", "报一下时间", "現在什麼時候", "what time is it", "tell me the time", "current time", "give me the time"],
    "language": ["你支持哪些语言", "你会中文吗", "你认识繁体字吗", "你会英语吗", "你支援哪些語言",
                 "你會中文嗎", "你認識繁體字嗎", "你會英語嗎", "what languages do you support",
                 "你能使用什么语言", "你能使用什麼語言", "can you speak Chinese", "can you speak English",
                 "which languages can you use"],
    "math": ["2+2", "14000**14000", "3.14×8", "1/3 + 2/5", "sqrt(2)", "sin(30°)",
             "解方程 x+3=7", "计算二加二", "请帮我算三乘以八", "一百除以四等于多少", "求平方根",
             "解方程 2*x-6=10", "計算二加二", "請幫我算三乘以八", "一百除以四等於多少",
             "求平方根", "解方程 2*x-6=10", "calculate 2 plus 2", "what is 3 times 8",
             "solve x+3=7", "find the square root of 2", "evaluate 1/3 + 2/5"],
    "history": ["讲讲秦统一六国", "秦朝是什么时候建立的", "丝绸之路的历史", "唐朝建立于哪一年",
                "秦始皇是谁", "嬴政是什么人", "介绍一下拿破仑", "孔子生活在哪个年代", "谁是汉武帝",
                "郑和下西洋", "辛亥革命发生了什么", "五四运动是什么", "中华人民共和国何时成立",
                "介绍一下这场战役", "这次战争发生在什么时候", "讲述这个历史事件", "某次起义的经过",
                "講講秦統一六國", "絲綢之路的歷史", "唐朝建立於哪一年", "鄭和下西洋",
                "秦始皇是誰", "嬴政是什麼人", "介紹一下拿破崙", "孔子生活在哪個年代", "誰是漢武帝",
                "辛亥革命發生了什麼", "五四運動是什麼", "中華人民共和國何時成立",
                "介紹一下這場戰役", "這次戰爭發生在什麼時候", "講述這個歷史事件", "某次起義的經過",
                "tell me about the Qin unification", "history of the Silk Road", "when was the Tang dynasty founded",
                "who was Qin Shi Huang", "who was Napoleon", "tell me about Confucius", "when did Julius Caesar live",
                "what happened in the French Revolution", "tell me about World War II", "when was the moon landing",
                "tell me about this battle", "when did this war happen", "describe this historical event"],
    "joke": ["讲个笑话", "说个笑话吧", "逗我开心", "来个冷笑话", "講個笑話", "說個笑話吧",
             "逗我開心", "來個冷笑話", "tell me a joke", "make me laugh", "say something funny"],
    "apology": ["对不起", "抱歉", "是我的错", "不好意思", "對不起", "抱歉啦", "是我的錯",
                "不好意思", "sorry", "I am sorry", "I am really sorry", "so sorry", "I apologize", "my mistake", "excuse me"],
    "compliment": ["你真聪明", "你很棒", "回答得不错", "我喜欢你", "你真聰明", "你很棒",
                   "回答得不錯", "我喜歡你", "you are smart", "you are great", "good answer", "I like you"],
    "creator": ["谁创造了你", "你是谁开发的", "谁训练了你", "谁制作了这个模型", "你是由谁构建的",
                "誰創造了你", "你是誰開發的", "誰訓練了你", "誰製作了這個模型", "你是由誰建立的",
                "who created you", "who made you", "who trained you", "who built this model"],
    "character": ["龘是什么意思", "仉怎么读", "这个字怎么念", "禤是不是姓氏", "解释一下爨字",
                  "生僻字亓的含义", "龘是什麼意思", "仉怎麼讀", "這個字怎麼念", "禤是不是姓氏",
                  "解釋一下爨字", "生僻字亓的含義", "what does 龘 mean", "how do you pronounce 仉",
                  "is 禤 a surname", "explain the character 爨"],
    "english_word": ["apple是什么意思", "hello这个单词怎么读", "解释一下friend", "单词beautiful的含义",
                     "apple怎么用", "用friend造句", "给一个hello的例句", "world有哪些常见用法",
                     "apple是什麼意思", "hello這個單詞怎麼讀", "解釋一下friend", "單詞beautiful的含義",
                     "apple怎麼用", "用friend造句", "給一個hello的例句", "world有哪些常見用法",
                     "what does apple mean", "how do you pronounce hello", "define friend", "meaning of beautiful",
                     "how do I use apple", "use friend in a sentence", "give me an example with hello"],
    "legal": ["中国民法典是什么", "中国大陆劳动合同适用什么法律", "中国的著作权法", "民法典诉讼时效",
              "美国联邦最低工资适用什么法", "美国版权合理使用", "美国宪法第一修正案", "加州法律和联邦法一样吗",
              "台湾民法成年年龄", "臺灣勞動基準法", "中國臺灣個人資料保護法", "台湾消费者保护法",
              "这件事适用哪里的法律", "没有地区能判断法律问题吗", "這件事適用哪個地區的法律",
              "what does Chinese civil law cover", "what is the US federal minimum wage law",
              "US federal copyright fair use", "Taiwan Civil Code", "which jurisdiction applies to this legal question",
              "公司不发工资怎么办", "中国大陆公司拖欠工资怎么处理", "美國雇主不付薪水怎麼辦",
              "臺灣公司欠薪如何申訴", "my employer has not paid me what should I do", "how can I recover unpaid wages",
              "被公司辞退怎么办", "加班费不给怎么办", "上班受伤怎么处理", "商家不退款怎么投诉",
              "我被诈骗了怎么办", "房东不退押金怎么办", "借钱不还怎么起诉", "发生车祸怎么办",
              "遭遇家暴怎么办", "个人信息泄露怎么办", "职场性骚扰如何投诉", "医疗事故怎么处理",
              "作品被抄袭如何维权", "合同违约怎么办", "离婚孩子抚养权怎么办",
              "被公司辭退怎麼辦", "房東不退押金怎麼辦", "遇到詐騙怎麼辦", "遭遇家暴怎麼辦",
              "what should I do if I am fired", "my landlord kept my deposit", "how do I report a scam",
              "what should I do after a car accident", "how can I report workplace harassment"],
    "encyclopedia": ["水的沸点是多少", "什么是光合作用", "介绍一下太平洋", "CPU 是什么", "细菌和病毒有什么区别",
                     "什麼是光合作用", "水的沸點是多少", "介紹一下太平洋", "記憶體是什麼", "細菌和病毒有什麼區別",
                     "what is photosynthesis", "where is the Pacific Ocean", "what is a CPU", "define DNA", "explain the Internet"],
    "utility": ["1千米换算成米", "0摄氏度等于多少华氏度", "1 GiB等于多少Byte", "两个日期相差几天",
                "1公里換算成公尺", "0攝氏度等於多少華氏度", "時區轉換", "兩個日期相差幾天",
                "convert 1 km to m", "convert 0 C to F", "1 GiB in bytes", "date difference", "timezone conversion"],
}

# 面向中国大陆日常表达的增量语料。保持与三语主任务相同的意图，不牺牲繁体和英语能力。
DOMESTIC_TRAINING_DATA = {
    "greeting":["在吗","在不在呀","哈喽在不","嗨喽","早啊","晚上好呀","忙不忙，聊两句","你好哈","来唠两句","有人吗"],
    "farewell":["我先撤了","回头再唠","先下了哈","改天接着聊","我去忙了","拜了个拜","先这样吧","回见","晚点聊","撤啦"],
    "thanks":["谢啦","多谢了哈","辛苦你了","麻烦你了","感谢解答","这下明白了谢谢","太谢谢你啦","谢了兄弟","劳驾了","感谢帮忙"],
    "identity":["你到底是谁呀","咋称呼你","你是干啥的","你叫啥","说说你是哪个","你是啥模型","你从哪来的","你是本地助手吗"],
    "ability":["你都会干啥","能帮我整点啥","你有啥本事","这个助手咋用","能办哪些事","你会算题不","你能查点啥","给我看看功能","使用说明有吗","你能帮啥忙"],
    "wellbeing":["最近还好不","今儿怎么样","状态咋样","你还好吗","最近过得咋样","今天感觉如何","一切都好吧","近来可好"],
    "positive":["今儿状态贼好","今天挺顺的","心情老好了","这事办成了真开心","最近感觉不错","今天可太棒了","一切顺利","挺好的呀"],
    "negative":["最近有点闹心","心里堵得慌","今天挺不顺的","我有点烦","最近压力挺大","心情有点低落","这事真让人难受","我累得不行"],
    "time":["现在啥时候了","这会儿几点","麻烦看下时间","北京时间几点了","报个点儿","现在几点钟","当前是几点","帮我看看几点了"],
    "language":["你能说普通话不","简体字能看懂吧","能用中文聊吗","英文问题能问不","繁体内容能识别吗","支持中英文不","能不能中英切换","你认识汉字吧"],
    "math":["帮我瞅瞅23*17","麻烦算下19乘23","给算算一百除以四","这道方程咋解 x+5=12","算一下三点一四乘八","求下二分之一加三分之一","开根号81是多少","30度正弦是多少"],
    "history":["给我唠唠五四运动","讲讲咱们的丝绸之路","秦始皇到底是谁","唐朝哪年建立的","抗日战争是哪几年","新中国什么时候成立","科普一下辛亥革命","说说郑和下西洋"],
    "joke":["来个段子","讲个有意思的","整一个冷笑话","逗我乐一下","来点好玩的","说个梗","讲个笑话听听","整个笑话"],
    "apology":["是我不对哈","刚才不好意思","对不住了","抱歉哈","我说错了","别介意啊","我的问题","刚刚是我搞错了"],
    "compliment":["这回答靠谱","你还挺厉害","讲得真明白","回答很到位","有点东西","你真行","解释得不错","这个答案可以"],
    "creator":["你是谁整出来的","谁把你做出来的","这模型谁训的","开发你的人是谁","你是谁写的","谁搭的这个助手","谁做的本地模型","你的作者是谁"],
    "character":["仉这字念啥","爨念什么","乜能当姓吗","龘是啥意思","这个生僻字咋读","亓这个字念啥","禤是不是一个姓","这汉字普通话咋念"],
    "english_word":["friend咋用","apple是啥意思","hello咋念","用world造个句","beautiful什么意思","这个英文词咋读","给个friend的例句","解释下computer这个词"],
    "legal":["郑州上班老板压了俩月工资","北京租房押金一直不给退","网购买到坏东西商家不处理","公司突然让我走人","加班好多天没给加班费","个人信息被平台漏出去了","接到诈骗电话转了钱","合同到期对方不办事","上班摔伤单位不管","家里有人暴力威胁我"],
    "encyclopedia":["给我科普下光合作用","说说水为啥会沸腾","细胞到底是啥","简单讲讲地震","科普一下互联网","CPU是干啥的","细菌跟病毒差在哪","太平洋在哪儿"],
    "utility":["一公里是多少米","一百摄氏度转华氏度","两个日期隔多少天","2GB换成MB","北京时间转纽约时间","一斤等于多少克","一米换算厘米","文件大小换算一下"],
}

RESPONSES = {
 "zh": {"greeting":"你好！很高兴和你聊天。", "farewell":"再见，期待下次聊天！", "thanks":"不客气！",
  "identity":"我是一个在本机运行的迷你聊天模型。", "ability":"我能用简体中文、繁体中文和英语聊天，进行确定性数学与单位计算，查询百科、历史、汉字、英语词典和分地区法律，也能读取 TXT、Markdown、代码与 CSV 文件。输入 /help 查看用法。",
  "wellbeing":"我很好，谢谢关心！你呢？", "positive":"听起来很不错！", "negative":"听起来你不太好受。休息一下，或者和信任的人聊聊吧。",
  "language":"我支持简体中文、繁体中文和英语的基础对话。", "joke":"为什么程序员分不清万圣节和圣诞节？因为 OCT 31 等于 DEC 25。",
  "apology":"没关系，我们继续聊吧。", "compliment":"谢谢你的肯定！我会继续认真回答。", "creator":"我是由这份本地训练程序和训练数据构建出来的。",
  "unknown":"抱歉，我暂时没理解。你可以换一种说法，我会再试一次。"},
 "zh_hant": {"greeting":"你好！很高興和你聊天。", "farewell":"再見，期待下次聊天！", "thanks":"不客氣！",
  "identity":"我是一個在本機運行的迷你聊天模型。", "ability":"我能用簡體中文、繁體中文和英語聊天，進行確定性數學與單位計算，查詢百科、歷史、漢字、英語詞典和分地區法律，也能讀取 TXT、Markdown、程式碼與 CSV 檔案。輸入 /help 查看用法。",
  "wellbeing":"我很好，謝謝關心！你呢？", "positive":"聽起來很不錯！", "negative":"聽起來你不太好受。休息一下，或者和信任的人聊聊吧。",
  "language":"我支援簡體中文、繁體中文和英語的基礎對話。", "joke":"為什麼程式設計師分不清萬聖節和聖誕節？因為 OCT 31 等於 DEC 25。",
  "apology":"沒關係，我們繼續聊吧。", "compliment":"謝謝你的肯定！我會繼續認真回答。", "creator":"我是由這份本地訓練程式和訓練資料建立出來的。",
  "unknown":"抱歉，我暫時沒理解。你可以換一種說法，我會再試一次。"},
 "en": {"greeting":"Hello! Nice to chat with you.", "farewell":"Goodbye! See you next time.", "thanks":"You're welcome!",
  "identity":"I'm a tiny chat model running locally.", "ability":"I can chat in Simplified Chinese, Traditional Chinese, and English; perform deterministic maths and conversions; query encyclopaedia, history, characters, words, and jurisdiction-aware law; and read TXT, Markdown, code, and CSV files. Enter /help for usage.",
  "wellbeing":"I'm doing well, thanks! How about you?", "positive":"That sounds great!", "negative":"I'm sorry you're having a hard time. Consider talking to someone you trust.",
  "language":"I support basic conversations in Simplified Chinese, Traditional Chinese, and English.",
  "joke":"Why do programmers confuse Halloween and Christmas? Because OCT 31 equals DEC 25.", "apology":"No worries. We can keep talking.",
  "compliment":"Thank you! I'll keep doing my best to give useful answers.", "creator":"I was built from this local training program and its training data.",
  "unknown":"Sorry, I didn't understand that. Try rephrasing it and I'll try again."}
}

# 同一意图的自然表达模板。根据用户原句稳定选取，避免每次只机械重复一句。
RESPONSE_TEMPLATES = {
 "zh": {
  "greeting":["你好！很高兴和你聊天。","嗨，欢迎你！今天想聊点什么？","你好呀，我在这里。有什么想问的吗？","很高兴见到你，我们开始聊天吧。"],
  "farewell":["再见，期待下次聊天！","好的，下次见。祝你今天愉快！","再会，有需要时随时来找我。","拜拜，感谢这次聊天。"],
  "thanks":["不客气！","不用谢，能帮上忙就好。","很高兴对你有帮助。","别客气，还有问题可以继续问我。"],
  "identity":["我是一个在本机运行的迷你聊天模型。","我是 LocalAI，一个轻量的本地聊天助手。","你可以把我看作一个能聊天、计算和查询本地知识的离线助手。"],
  "wellbeing":["我很好，谢谢关心！你呢？","我状态不错，随时可以陪你聊聊。","谢谢你问我。我一切正常，你今天怎么样？"],
  "positive":["听起来很不错！","真好，希望这份好心情继续保持。","听到这个我也很开心。","太棒了！愿今天接下来也一样顺利。"],
  "negative":["听起来你不太好受。休息一下，或者和信任的人聊聊吧。","抱抱你。先照顾好自己，不必急着把所有事一次解决。","这确实不容易。如果愿意，你可以慢慢说说发生了什么。"],
  "apology":["没关系，我们继续聊吧。","不用放在心上，我没有介意。","没事的，谢谢你愿意说明。"],
  "compliment":["谢谢你的肯定！我会继续认真回答。","听你这么说我很开心。","谢谢！能真正帮到你才是最重要的。"],
  "unknown":["抱歉，我暂时没理解。你可以换一种说法，我会再试一次。","这句话我还没听懂，可以说得更具体一些吗？","我不太确定你的意思。换个表达或给我一个例子吧。"]},
 "zh_hant": {
  "greeting":["你好！很高興和你聊天。","嗨，歡迎你！今天想聊點什麼？","你好呀，我在這裡。有什麼想問的嗎？","很高興見到你，我們開始聊天吧。"],
  "farewell":["再見，期待下次聊天！","好的，下次見。祝你今天愉快！","再會，有需要時隨時來找我。","拜拜，感謝這次聊天。"],
  "thanks":["不客氣！","不用謝，能幫上忙就好。","很高興對你有幫助。","別客氣，還有問題可以繼續問我。"],
  "identity":["我是一個在本機運行的迷你聊天模型。","我是 LocalAI，一個輕量的本地聊天助手。","你可以把我看作一個能聊天、計算和查詢本地知識的離線助手。"],
  "wellbeing":["我很好，謝謝關心！你呢？","我狀態不錯，隨時可以陪你聊聊。","謝謝你問我。我一切正常，你今天怎麼樣？"],
  "positive":["聽起來很不錯！","真好，希望這份好心情繼續保持。","聽到這個我也很開心。","太棒了！願今天接下來也一樣順利。"],
  "negative":["聽起來你不太好受。休息一下，或者和信任的人聊聊吧。","抱抱你。先照顧好自己，不必急著把所有事一次解決。","這確實不容易。如果願意，你可以慢慢說說發生了什麼。"],
  "apology":["沒關係，我們繼續聊吧。","不用放在心上，我沒有介意。","沒事的，謝謝你願意說明。"],
  "compliment":["謝謝你的肯定！我會繼續認真回答。","聽你這麼說我很開心。","謝謝！能真正幫到你才是最重要的。"],
  "unknown":["抱歉，我暫時沒理解。你可以換一種說法，我會再試一次。","這句話我還沒聽懂，可以說得更具體一些嗎？","我不太確定你的意思。換個表達或給我一個例子吧。"]},
 "en": {
  "greeting":["Hello! It's nice to chat with you.","Hi there! What would you like to talk about?","Hello—I'm here and ready to help.","Good to see you. Let's chat!"],
  "farewell":["Goodbye! See you next time.","Take care, and come back anytime.","See you later. I enjoyed our chat.","Bye for now! Have a good day."],
  "thanks":["You're welcome!","Glad I could help.","Anytime—feel free to ask another question.","My pleasure!"],
  "identity":["I'm a tiny chat model running locally.","I'm LocalAI, a lightweight assistant that runs on this computer.","I'm an offline assistant for basic chat, calculation, and local knowledge lookup."],
  "wellbeing":["I'm doing well, thanks! How about you?","I'm ready and doing fine. How are you today?","Thanks for asking—everything is working well on my side."],
  "positive":["That sounds great!","I'm glad to hear that.","Wonderful—I hope the rest of your day goes just as well.","That's good news!"],
  "negative":["I'm sorry you're having a hard time. Consider resting or talking to someone you trust.","That sounds difficult. Be gentle with yourself, and take things one step at a time.","I'm sorry to hear that. If you'd like, you can tell me a little more."],
  "apology":["No worries. We can keep talking.","It's okay—I don't mind.","No problem, and thank you for saying so."],
  "compliment":["Thank you! I'll keep doing my best.","That's kind of you to say.","Thanks! What matters most is being genuinely useful to you."],
  "unknown":["Sorry, I didn't understand that. Try rephrasing it and I'll try again.","I'm not quite sure what you mean. Could you be more specific?","I may need an example or a different wording to understand that."]}
}

def choose_response(language, intent, prompt):
    choices = RESPONSE_TEMPLATES.get(language, {}).get(intent)
    if not choices: return RESPONSES[language].get(intent, RESPONSES[language]["unknown"])
    digest = hashlib.blake2s(prompt.strip().encode("utf-8"), digest_size=2).digest()
    return choices[int.from_bytes(digest, "little") % len(choices)]

# 可审阅、可扩展的历史种子资料。事实与回答存进 SQLite，不依赖模型猜测。
HISTORY_EVENTS = [
 ("qin_unification", "公元前221年", "秦统一六国", "秦統一六國", "Qin unification of China",
  "公元前221年，秦王嬴政完成统一，建立秦朝并称始皇帝，推行郡县制及文字、度量衡等制度的统一。",
  "公元前221年，秦王嬴政完成統一，建立秦朝並稱始皇帝，推行郡縣制及文字、度量衡等制度的統一。",
  "In 221 BCE, King Zheng of Qin completed the conquest of the rival states, founded the Qin dynasty, and became the First Emperor. His government standardized administration, writing, weights, and measures.",
  "秦朝|秦始皇|统一六国|嬴政", "秦朝|秦始皇|統一六國|嬴政", "Qin dynasty|Qin Shi Huang|First Emperor|unification of China"),
 ("silk_road", "约公元前2世纪起", "丝绸之路形成", "絲綢之路形成", "Development of the Silk Road",
  "丝绸之路并非一条单一路线。汉代张骞出使西域后，连接东亚、中亚、西亚及欧洲的陆路交流网络逐渐发展，促进商品、技术、宗教与文化传播。",
  "絲綢之路並非一條單一路線。漢代張騫出使西域後，連接東亞、中亞、西亞及歐洲的陸路交流網絡逐漸發展，促進商品、技術、宗教與文化傳播。",
  "The Silk Road was a network rather than one road. It expanded from roughly the second century BCE after Han envoy Zhang Qian's missions, carrying goods, technologies, religions, and ideas across Eurasia.",
  "丝绸之路|张骞|西域", "絲綢之路|張騫|西域", "Silk Road|Zhang Qian"),
 ("tang_founding", "618年", "唐朝建立", "唐朝建立", "Founding of the Tang dynasty",
  "618年，李渊建立唐朝并即位为唐高祖。唐朝定都长安，后来成为中国历史上影响深远的统一王朝之一。",
  "618年，李淵建立唐朝並即位為唐高祖。唐朝定都長安，後來成為中國歷史上影響深遠的統一王朝之一。",
  "In 618, Li Yuan founded the Tang dynasty and became Emperor Gaozu. With its capital at Chang'an, the Tang became one of China's most influential dynasties.",
  "唐朝|李渊|唐高祖|长安", "唐朝|李淵|唐高祖|長安", "Tang dynasty|Li Yuan|Emperor Gaozu|Chang'an"),
 ("zheng_he", "1405—1433年", "郑和下西洋", "鄭和下西洋", "Zheng He's voyages",
  "1405年至1433年间，明朝郑和船队七次远航，抵达东南亚、南亚、阿拉伯半岛和东非多地，加强了外交与海上交流。",
  "1405年至1433年間，明朝鄭和船隊七次遠航，抵達東南亞、南亞、阿拉伯半島和東非多地，加強了外交與海上交流。",
  "Between 1405 and 1433, Ming admiral Zheng He led seven major voyages reaching Southeast and South Asia, Arabia, and East Africa, expanding diplomatic and maritime exchange.",
  "郑和|下西洋|明朝航海", "鄭和|下西洋|明朝航海", "Zheng He|Ming voyages|treasure voyages"),
 ("american_independence", "1776年", "美国《独立宣言》", "美國《獨立宣言》", "American Declaration of Independence",
  "1776年7月4日，北美十三个殖民地通过《独立宣言》，宣布脱离英国。战争延续到1783年，英国随后承认美国独立。",
  "1776年7月4日，北美十三個殖民地通過《獨立宣言》，宣布脫離英國。戰爭延續到1783年，英國隨後承認美國獨立。",
  "On July 4, 1776, the thirteen colonies adopted the Declaration of Independence from Britain. The war continued until 1783, when Britain recognized U.S. independence.",
  "美国独立|独立宣言|1776", "美國獨立|獨立宣言|1776", "American Revolution|Declaration of Independence|1776"),
 ("french_revolution", "1789—1799年", "法国大革命", "法國大革命", "French Revolution",
  "法国大革命始于1789年。它推翻旧制度与君主专制，经历共和国建立和政治动荡，并深刻传播了公民权、民族国家与平等观念。",
  "法國大革命始於1789年。它推翻舊制度與君主專制，經歷共和國建立和政治動盪，並深刻傳播了公民權、民族國家與平等觀念。",
  "The French Revolution began in 1789, overthrew the old regime and absolute monarchy, established a republic amid upheaval, and powerfully spread ideas of citizenship and equality.",
  "法国大革命|1789|巴士底狱", "法國大革命|1789|巴士底獄", "French Revolution|1789|Bastille"),
 ("xinhai_revolution", "1911年", "辛亥革命", "辛亥革命", "Xinhai Revolution",
  "辛亥革命以1911年10月10日武昌起义为重要开端，促使清朝统治结束。中华民国于1912年成立，延续两千多年的帝制由此终结。",
  "辛亥革命以1911年10月10日武昌起義為重要開端，促使清朝統治結束。中華民國於1912年成立，延續兩千多年的帝制由此終結。",
  "The Xinhai Revolution, marked by the Wuchang Uprising on October 10, 1911, led to the fall of the Qing dynasty. The Republic of China was founded in 1912, ending imperial rule.",
  "辛亥革命|武昌起义|孙中山", "辛亥革命|武昌起義|孫中山", "Xinhai Revolution|Wuchang Uprising|Sun Yat-sen"),
 ("world_war_one", "1914—1918年", "第一次世界大战", "第一次世界大戰", "World War I",
  "第一次世界大战于1914年至1918年进行，主要交战集团为协约国和同盟国。战争造成巨大伤亡，并重塑欧洲和中东的政治版图。",
  "第一次世界大戰於1914年至1918年進行，主要交戰集團為協約國和同盟國。戰爭造成巨大傷亡，並重塑歐洲和中東的政治版圖。",
  "World War I lasted from 1914 to 1918, principally between the Allies and Central Powers. It caused enormous casualties and reshaped the political map of Europe and the Middle East.",
  "一战|第一次世界大战|1914", "一戰|第一次世界大戰|1914", "World War I|First World War|WWI|1914"),
 ("may_fourth", "1919年", "五四运动", "五四運動", "May Fourth Movement",
  "1919年5月4日，北京学生因巴黎和会山东问题举行抗议，运动随后扩展到多个城市和社会群体，推动了反帝爱国运动与新文化思潮。",
  "1919年5月4日，北京學生因巴黎和會山東問題舉行抗議，運動隨後擴展到多個城市和社會群體，推動了反帝愛國運動與新文化思潮。",
  "On May 4, 1919, students in Beijing protested decisions over Shandong at the Paris Peace Conference. The movement broadened nationwide and energized anti-imperial activism and New Culture ideas.",
  "五四运动|1919|新文化运动", "五四運動|1919|新文化運動", "May Fourth Movement|1919|New Culture Movement"),
 ("world_war_two", "1939—1945年", "第二次世界大战", "第二次世界大戰", "World War II",
  "第二次世界大战通常以1939年德国入侵波兰为欧洲战场全面爆发的标志，1945年结束。它波及全球，造成数千万人死亡，并促成联合国建立。",
  "第二次世界大戰通常以1939年德國入侵波蘭為歐洲戰場全面爆發的標誌，1945年結束。它波及全球，造成數千萬人死亡，並促成聯合國建立。",
  "World War II is commonly dated from Germany's invasion of Poland in 1939 to 1945. The global conflict killed tens of millions and led to the creation of the United Nations.",
  "二战|第二次世界大战|1939|1945", "二戰|第二次世界大戰|1939|1945", "World War II|Second World War|WWII|1939|1945"),
 ("prc_founding", "1949年", "中华人民共和国成立", "中華人民共和國成立", "Founding of the People's Republic of China",
  "1949年10月1日，中华人民共和国中央人民政府成立典礼在北京举行，中华人民共和国成立。",
  "1949年10月1日，中華人民共和國中央人民政府成立典禮在北京舉行，中華人民共和國成立。",
  "On October 1, 1949, the founding of the People's Republic of China was proclaimed in Beijing.",
  "新中国成立|中华人民共和国|1949", "新中國成立|中華人民共和國|1949", "People's Republic of China|PRC|1949"),
 ("moon_landing", "1969年", "阿波罗11号登月", "阿波羅11號登月", "Apollo 11 Moon landing",
  "1969年7月20日，阿波罗11号登月舱在月球着陆。尼尔·阿姆斯特朗随后成为首位踏上月球的人，巴兹·奥尔德林也登上月面。",
  "1969年7月20日，阿波羅11號登月艙在月球著陸。尼爾·阿姆斯壯隨後成為首位踏上月球的人，巴茲·奧爾德林也登上月面。",
  "On July 20, 1969, Apollo 11 landed on the Moon. Neil Armstrong became the first person to walk there, followed by Buzz Aldrin.",
  "登月|阿波罗11号|阿姆斯特朗|1969", "登月|阿波羅11號|阿姆斯壯|1969", "Moon landing|Apollo 11|Neil Armstrong|1969"),
]

CHARACTER_SEEDS = [
 ("龘","dá","群龙腾飞的样子，由三个“龍”组成。","群龍騰飛的樣子，由三個「龍」組成。","the appearance of dragons in flight",0),
 ("𪚥","zhé","形容话多、啰嗦；由四个“龍”组成。","形容話多、囉嗦；由四個「龍」組成。","talkative; verbose; a rare character made of four dragon components",0),
 ("爨","cuàn","烧火做饭，也指灶；同时可作姓氏。","燒火做飯，也指灶；同時可作姓氏。","to cook; a stove; also a surname",1),
 ("仉","zhǎng","姓氏用字，如孟子的母亲仉氏。","姓氏用字，如孟子的母親仉氏。","a Chinese surname",1),
 ("亓","qí","姓氏用字，也可视为古代“其”字的一种写法。","姓氏用字，也可視為古代「其」字的一種寫法。","an archaic form related to 其; also a surname",1),
 ("禤","xuān","较少见的汉族姓氏用字。","較少見的漢族姓氏用字。","a rare Chinese surname",1),
 ("璩","qú","古代玉环，也用作姓氏。","古代玉環，也用作姓氏。","a jade ring; also a surname",1),
 ("郗","xī","古地名用字，也用作姓氏。","古地名用字，也用作姓氏。","an ancient place name; also a surname",1),
 ("逄","páng","姓氏用字。","姓氏用字。","a Chinese surname",1),
 ("缑","gōu","指刀剑等器物上的绳饰，也用作姓氏。","指刀劍等器物上的繩飾，也用作姓氏。","a cord or corded decoration; also a surname",1),
 ("訾","zī","有责备、诋毁等义，也可作姓氏；读音会随义项变化。","有責備、詆毀等義，也可作姓氏；讀音會隨義項變化。","to criticize; also a surname; pronunciation varies by sense",1),
 ("乜","miē / niè","读miē时可表示斜眼看；作姓氏时常读niè。","讀miē時可表示斜眼看；作姓氏時常讀niè。","to squint; as a surname often pronounced niè",1),
 ("冼","xiǎn","姓氏用字。","姓氏用字。","a Chinese surname",1),
 ("庹","tuǒ","两臂左右平伸的长度，也用作姓氏。","兩臂左右平伸的長度，也用作姓氏。","an arm-span measure; also a surname",1),
 ("厍","shè","姓氏用字，是“庫”的异体相关字。","姓氏用字，是「庫」的異體相關字。","a Chinese surname; a variant related to 庫",1),
 ("靐","bìng","雷声，字形由三个“雷”组成。","雷聲，字形由三個「雷」組成。","sound of thunder; composed of three 雷 characters",0),
 ("齉","nàng","鼻子不通气，发音不清。","鼻子不通氣，發音不清。","nasal obstruction causing indistinct speech",0),
]

ENGLISH_USAGE_SEEDS = [
 ("apple","I eat an apple every morning.","我每天早上吃一个苹果。"),
 ("friend","She is a close friend of mine.","她是我的一位好朋友。"),
 ("beautiful","The garden looks beautiful in spring.","春天的花园看起来很美。"),
 ("hello","She smiled and said hello.","她微笑着打了声招呼。"),
 ("world","People around the world use the internet.","世界各地的人都使用互联网。"),
 ("happy","I am happy to see you again.","我很高兴再次见到你。"),
 ("help","Could you help me with this question?","你能帮我解答这个问题吗？"),
 ("learn","Children learn by asking questions.","孩子们通过提问来学习。"),
 ("speak","Please speak slowly and clearly.","请说得慢一些、清楚一些。"),
 ("book","I borrowed this book from the library.","我从图书馆借了这本书。"),
 ("water","Please drink enough water every day.","请每天喝足够的水。"),
 ("time","What time does the meeting start?","会议几点开始？"),
 ("work","I usually start work at nine.","我通常九点开始工作。"),
 ("home","We went home after dinner.","晚饭后我们回家了。"),
 ("school","The children walk to school together.","孩子们一起步行去学校。"),
 ("think","I need some time to think about it.","我需要一点时间考虑这件事。"),
 ("make","Let us make a simple plan.","让我们制订一个简单的计划。"),
 ("take","Please take an umbrella with you.","请随身带一把伞。"),
 ("give","Could you give me an example?","你能给我举个例子吗？"),
 ("need","Let me know if you need help.","如果你需要帮助，请告诉我。"),
]

# 法律资料必须带司法辖区。摘要用于定位，不替代正式条文或个案法律意见。
LEGAL_SEEDS = [
 {"doc_id":"cn_constitution", "jurisdiction":"CN", "subdivision":"national", "level":"宪法",
  "title_zh":"中华人民共和国宪法", "title_hant":"中華人民共和國憲法", "title_en":"Constitution of the People's Republic of China",
  "status":"有效", "effective_date":"1982-12-04", "last_updated":"2018-03-11",
  "summary_zh":"中国大陆的根本法；查询具体权利义务时还需结合相关法律、法规和解释。",
  "summary_hant":"中國大陸的根本法；查詢具體權利義務時還需結合相關法律、法規和解釋。",
  "summary_en":"The fundamental law of mainland China; specific rights and duties also depend on implementing laws and regulations.",
  "topics":"宪法|憲法|constitution|基本权利|基本權利",
  "source_url":"https://flk.npc.gov.cn/", "source_name":"国家法律法规数据库", "license":"official-public-law", "priority":100},
 {"doc_id":"cn_civil_code", "jurisdiction":"CN", "subdivision":"national", "level":"法律",
  "title_zh":"中华人民共和国民法典", "title_hant":"中華人民共和國民法典", "title_en":"Civil Code of the People's Republic of China",
  "status":"有效", "effective_date":"2021-01-01", "last_updated":"2020-05-28",
  "summary_zh":"调整平等主体之间的人身关系和财产关系，涵盖总则、物权、合同、人格权、婚姻家庭、继承和侵权责任。第一百八十八条规定一般诉讼时效为三年，并有另有规定、起算点及最长保护期间等规则，须结合具体事实判断。",
  "summary_hant":"調整平等主體之間的人身關係和財產關係，涵蓋總則、物權、合同、人格權、婚姻家庭、繼承和侵權責任。第一百八十八條規定一般訴訟時效為三年，並有另有規定、起算點及最長保護期間等規則，須結合具體事實判斷。",
  "summary_en":"Governs civil personal and property relations. Article 188 provides a general three-year limitation period, subject to accrual, special-rule, and long-stop provisions.",
  "topics":"民法|民法典|civil code|合同|合約|侵权|侵權|婚姻|继承|繼承|诉讼时效|訴訟時效",
  "source_url":"https://flk.npc.gov.cn/", "source_name":"国家法律法规数据库", "license":"official-public-law", "priority":100},
 {"doc_id":"cn_labor_contract", "jurisdiction":"CN", "subdivision":"national", "level":"法律",
  "title_zh":"中华人民共和国劳动合同法", "title_hant":"中華人民共和國勞動合同法", "title_en":"Labor Contract Law of the People's Republic of China",
  "status":"有效", "effective_date":"2008-01-01", "last_updated":"2012-12-28",
  "summary_zh":"规定中国大陆劳动合同的订立、履行、变更、解除和终止。工资、工时和争议处理还可能涉及劳动法、社会保险法及当地规定。",
  "summary_hant":"規定中國大陸勞動合同的訂立、履行、變更、解除和終止。工資、工時和爭議處理還可能涉及勞動法、社會保險法及當地規定。",
  "summary_en":"Governs formation, performance, modification, termination, and ending of employment contracts in mainland China; local rules may also matter.",
  "topics":"劳动合同|勞動合同|employment contract|劳动法|勞動法|工资|工資|解雇|辞退|辭退",
  "source_url":"https://flk.npc.gov.cn/", "source_name":"国家法律法规数据库", "license":"official-public-law", "priority":100},
 {"doc_id":"cn_copyright", "jurisdiction":"CN", "subdivision":"national", "level":"法律",
  "title_zh":"中华人民共和国著作权法", "title_hant":"中華人民共和國著作權法", "title_en":"Copyright Law of the People's Republic of China",
  "status":"有效", "effective_date":"1991-06-01", "last_updated":"2020-11-11",
  "summary_zh":"规定中国大陆作品著作权、相关权利、权利限制、保护和侵权责任；合理使用须结合具体法定情形判断。",
  "summary_hant":"規定中國大陸作品著作權、相關權利、權利限制、保護和侵權責任；合理使用須結合具體法定情形判斷。",
  "summary_en":"Covers copyright, related rights, limitations, enforcement, and liability in mainland China; permitted uses depend on statutory conditions.",
  "topics":"著作权|著作權|版权|版權|copyright|合理使用",
  "source_url":"https://flk.npc.gov.cn/", "source_name":"国家法律法规数据库", "license":"official-public-law", "priority":100},
 {"doc_id":"us_constitution", "jurisdiction":"US-FED", "subdivision":"federal", "level":"Constitution",
  "title_zh":"美国宪法", "title_hant":"美國憲法", "title_en":"Constitution of the United States",
  "status":"current", "effective_date":"1788-06-21", "last_updated":"1992-05-07",
  "summary_zh":"美国联邦宪法确立联邦政府结构并通过修正案保障多项权利。宪法问题还受联邦法院判例解释。",
  "summary_hant":"美國聯邦憲法確立聯邦政府結構並通過修正案保障多項權利。憲法問題還受聯邦法院判例解釋。",
  "summary_en":"The U.S. Constitution establishes the federal government and protects rights through its amendments; judicial precedent is also essential.",
  "topics":"美国宪法|美國憲法|US Constitution|First Amendment|第一修正案|Fourth Amendment|第十四修正案",
  "source_url":"https://constitution.congress.gov/constitution/", "source_name":"Constitution Annotated, Congress.gov", "license":"US-government-work", "priority":100},
 {"doc_id":"us_usc_17", "jurisdiction":"US-FED", "subdivision":"federal", "level":"United States Code",
  "title_zh":"美国法典第17编：版权", "title_hant":"美國法典第17編：版權", "title_en":"Title 17, United States Code — Copyrights",
  "status":"current release", "effective_date":"", "last_updated":"",
  "summary_zh":"美国联邦版权法主要编入第17编；合理使用规定见17 U.S.C. §107，并须综合法定因素判断。州法或合同义务仍可能另外适用。",
  "summary_hant":"美國聯邦版權法主要編入第17編；合理使用規定見17 U.S.C. §107，並須綜合法定因素判斷。州法或合同義務仍可能另外適用。",
  "summary_en":"Federal copyright law is principally codified in Title 17. Fair use is addressed in 17 U.S.C. §107 and requires a fact-specific multi-factor analysis.",
  "topics":"版权|版權|copyright|fair use|合理使用|17 USC|17 U.S.C.",
  "source_url":"https://uscode.house.gov/browse/prelim@title17/front", "source_name":"U.S. House Office of the Law Revision Counsel", "license":"US-government-work", "priority":100},
 {"doc_id":"us_usc_29", "jurisdiction":"US-FED", "subdivision":"federal", "level":"United States Code",
  "title_zh":"美国法典第29编：劳动", "title_hant":"美國法典第29編：勞動", "title_en":"Title 29, United States Code — Labor",
  "status":"current release", "effective_date":"", "last_updated":"",
  "summary_zh":"美国联邦劳动法律包括《公平劳动标准法》等，涉及最低工资和加班等问题；州和地方可规定更高标准，通常须适用对劳动者更有利的有效标准。",
  "summary_hant":"美國聯邦勞動法律包括《公平勞動標準法》等，涉及最低工資和加班等問題；州和地方可規定更高標準，通常須適用對勞動者更有利的有效標準。",
  "summary_en":"Federal labor statutes include the Fair Labor Standards Act. State and local wage-and-hour rules may impose higher standards and must be checked separately.",
  "topics":"最低工资|最低工資|minimum wage|overtime|加班|FLSA|Fair Labor Standards Act|劳动法|勞動法",
  "source_url":"https://uscode.house.gov/browse/prelim@title29/front", "source_name":"U.S. House Office of the Law Revision Counsel", "license":"US-government-work", "priority":100},
 {"doc_id":"us_dol_minimum_wage", "jurisdiction":"US-FED", "subdivision":"federal", "level":"Agency guidance",
  "title_zh":"美国联邦最低工资（FLSA）", "title_hant":"美國聯邦最低工資（FLSA）", "title_en":"U.S. Federal Minimum Wage under the FLSA",
  "status":"current as checked", "effective_date":"2009-07-24", "last_updated":"2026-07-01",
  "summary_zh":"美国劳工部资料显示，适用于受《公平劳动标准法》覆盖且非豁免雇员的联邦最低工资为每小时7.25美元。州或地方标准更高时，通常适用更高标准；覆盖范围和豁免仍须核对。",
  "summary_hant":"美國勞工部資料顯示，適用於受《公平勞動標準法》涵蓋且非豁免僱員的聯邦最低工資為每小時7.25美元。州或地方標準更高時，通常適用更高標準；涵蓋範圍和豁免仍須核對。",
  "summary_en":"The U.S. Department of Labor lists a $7.25 hourly federal minimum wage for covered, nonexempt employees. A higher applicable state or local rate generally controls; coverage and exemptions must be checked.",
  "topics":"美国联邦最低工资|美國聯邦最低工資|federal minimum wage|minimum wage|FLSA|Fair Labor Standards Act",
  "source_url":"https://www.dol.gov/agencies/whd/minimum-wage/faq", "source_name":"U.S. Department of Labor, Wage and Hour Division", "license":"US-government-work", "priority":110},
 {"doc_id":"tw_civil_code", "jurisdiction":"TW", "subdivision":"Taiwan", "level":"法律",
  "title_zh":"台湾地区民法", "title_hant":"臺灣地區民法", "title_en":"Taiwan Civil Code",
  "status":"現行", "effective_date":"", "last_updated":"",
  "summary_zh":"规范台湾地区民事关系，包括总则、债、物权、亲属和继承；成年年龄自2023年1月1日起调整为18岁，个案仍须考虑施行和过渡规定。",
  "summary_hant":"規範臺灣地區民事關係，包括總則、債、物權、親屬和繼承；成年年齡自2023年1月1日起調整為18歲，個案仍須考慮施行和過渡規定。",
  "summary_en":"Governs civil relations in Taiwan, including obligations, property, family, and succession. The age of majority became 18 on January 1, 2023, subject to transition rules.",
  "topics":"台湾民法|臺灣民法|Taiwan Civil Code|成年年龄|成年年齡|债权|債權|物权|物權|继承|繼承",
  "source_url":"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=B0000001", "source_name":"全國法規資料庫（法務部）", "license":"政府資料開放授權條款第1版", "priority":100},
 {"doc_id":"tw_labor_standards", "jurisdiction":"TW", "subdivision":"Taiwan", "level":"法律",
  "title_zh":"台湾地区劳动基准法", "title_hant":"臺灣地區勞動基準法", "title_en":"Taiwan Labor Standards Act",
  "status":"現行", "effective_date":"", "last_updated":"",
  "summary_zh":"规定台湾地区劳动条件的最低标准，包括劳动合同、工资、工时、休息、休假、童工和退休等；行业及身份例外需要另行核对。",
  "summary_hant":"規定臺灣地區勞動條件的最低標準，包括勞動契約、工資、工時、休息、休假、童工和退休等；行業及身分例外需要另行核對。",
  "summary_en":"Sets minimum employment standards in Taiwan, including contracts, wages, hours, leave, child labor, and retirement; exceptions must be checked.",
  "topics":"劳动基准法|勞動基準法|Taiwan Labor Standards Act|工资|工資|工时|工時|加班|解雇",
  "source_url":"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=N0030001", "source_name":"全國法規資料庫（法務部）", "license":"政府資料開放授權條款第1版", "priority":100},
 {"doc_id":"tw_criminal_code", "jurisdiction":"TW", "subdivision":"Taiwan", "level":"法律",
  "title_zh":"台湾地区刑法", "title_hant":"臺灣地區刑法", "title_en":"Criminal Code of Taiwan",
  "status":"現行", "effective_date":"", "last_updated":"",
  "summary_zh":"规定台湾地区犯罪成立、刑罚及主要罪名；刑事责任高度依赖行为、主观要件、证据和当时有效条文。",
  "summary_hant":"規定臺灣地區犯罪成立、刑罰及主要罪名；刑事責任高度依賴行為、主觀要件、證據和當時有效條文。",
  "summary_en":"Defines criminal liability, punishments, and offenses in Taiwan; outcomes are highly fact- and evidence-dependent.",
  "topics":"台湾刑法|臺灣刑法|criminal code|刑事责任|刑事責任|犯罪",
  "source_url":"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=C0000001", "source_name":"全國法規資料庫（法務部）", "license":"政府資料開放授權條款第1版", "priority":100},
 {"doc_id":"tw_personal_data", "jurisdiction":"TW", "subdivision":"Taiwan", "level":"法律",
  "title_zh":"台湾地区个人资料保护法", "title_hant":"臺灣地區個人資料保護法", "title_en":"Taiwan Personal Data Protection Act",
  "status":"現行", "effective_date":"", "last_updated":"",
  "summary_zh":"规范台湾地区个人资料的搜集、处理、利用、当事人权利和责任；公私部门及特定资料可能适用不同要求。",
  "summary_hant":"規範臺灣地區個人資料的蒐集、處理、利用、當事人權利和責任；公私部門及特定資料可能適用不同要求。",
  "summary_en":"Regulates collection, processing, and use of personal data in Taiwan, together with data-subject rights and responsibilities.",
  "topics":"个人资料保护法|個人資料保護法|個資法|personal data|privacy|隐私|隱私",
  "source_url":"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=I0050021", "source_name":"全國法規資料庫（法務部）", "license":"政府資料開放授權條款第1版", "priority":100},
 {"doc_id":"tw_consumer", "jurisdiction":"TW", "subdivision":"Taiwan", "level":"法律",
  "title_zh":"台湾地区消费者保护法", "title_hant":"臺灣地區消費者保護法", "title_en":"Taiwan Consumer Protection Act",
  "status":"現行", "effective_date":"", "last_updated":"",
  "summary_zh":"规范台湾地区消费者权益、定型化契约、通讯交易、商品服务责任及争议处理。",
  "summary_hant":"規範臺灣地區消費者權益、定型化契約、通訊交易、商品服務責任及爭議處理。",
  "summary_en":"Covers consumer rights in Taiwan, standard-form contracts, distance transactions, product/service responsibility, and disputes.",
  "topics":"消费者保护法|消費者保護法|consumer protection|网购|網購|退货|退貨|定型化契约|定型化契約",
  "source_url":"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0170001", "source_name":"全國法規資料庫（法務部）", "license":"政府資料開放授權條款第1版", "priority":100},
]

US_CODE_TITLES = {
 1:"General Provisions", 2:"The Congress", 3:"The President", 4:"Flag and Seal, Seat of Government, and the States",
 5:"Government Organization and Employees", 6:"Domestic Security", 7:"Agriculture", 8:"Aliens and Nationality",
 9:"Arbitration", 10:"Armed Forces", 11:"Bankruptcy", 12:"Banks and Banking", 13:"Census", 14:"Coast Guard",
 15:"Commerce and Trade", 16:"Conservation", 17:"Copyrights", 18:"Crimes and Criminal Procedure",
 19:"Customs Duties", 20:"Education", 21:"Food and Drugs", 22:"Foreign Relations and Intercourse", 23:"Highways",
 24:"Hospitals and Asylums", 25:"Indians", 26:"Internal Revenue Code", 27:"Intoxicating Liquors",
 28:"Judiciary and Judicial Procedure", 29:"Labor", 30:"Mineral Lands and Mining", 31:"Money and Finance",
 32:"National Guard", 33:"Navigation and Navigable Waters", 34:"Crime Control and Law Enforcement", 35:"Patents",
 36:"Patriotic and National Observances, Ceremonies, and Organizations", 37:"Pay and Allowances of the Uniformed Services",
 38:"Veterans' Benefits", 39:"Postal Service", 40:"Public Buildings, Property, and Works", 41:"Public Contracts",
 42:"The Public Health and Welfare", 43:"Public Lands", 44:"Public Printing and Documents", 45:"Railroads",
 46:"Shipping", 47:"Telecommunications", 48:"Territories and Insular Possessions", 49:"Transportation",
 50:"War and National Defense", 51:"National and Commercial Space Programs", 52:"Voting and Elections",
 54:"National Park Service and Related Programs",
}

TAIWAN_LAW_TITLES = [
 ("tw_constitution", "台湾地区宪法", "臺灣地區憲法", "Constitution of Taiwan"),
 ("tw_administrative_procedure", "台湾地区行政程序法", "臺灣地區行政程序法", "Taiwan Administrative Procedure Act"),
 ("tw_civil_procedure", "台湾地区民事诉讼法", "臺灣地區民事訴訟法", "Taiwan Code of Civil Procedure"),
 ("tw_criminal_procedure", "台湾地区刑事诉讼法", "臺灣地區刑事訴訟法", "Taiwan Code of Criminal Procedure"),
 ("tw_company_act", "台湾地区公司法", "臺灣地區公司法", "Taiwan Company Act"),
 ("tw_copyright_act", "台湾地区著作权法", "臺灣地區著作權法", "Taiwan Copyright Act"),
 ("tw_patent_act", "台湾地区专利法", "臺灣地區專利法", "Taiwan Patent Act"),
 ("tw_trademark_act", "台湾地区商标法", "臺灣地區商標法", "Taiwan Trademark Act"),
 ("tw_employment_service", "台湾地区就业服务法", "臺灣地區就業服務法", "Taiwan Employment Service Act"),
 ("tw_gender_employment", "台湾地区性别平等工作法", "臺灣地區性別平等工作法", "Taiwan Gender Equality in Employment Act"),
 ("tw_occupational_safety", "台湾地区职业安全卫生法", "臺灣地區職業安全衛生法", "Taiwan Occupational Safety and Health Act"),
 ("tw_domestic_violence", "台湾地区家庭暴力防治法", "臺灣地區家庭暴力防治法", "Taiwan Domestic Violence Prevention Act"),
 ("tw_road_traffic", "台湾地区道路交通管理处罚条例", "臺灣地區道路交通管理處罰條例", "Taiwan Road Traffic Management and Penalty Act"),
 ("tw_tax_collection", "台湾地区税捐稽征法", "臺灣地區稅捐稽徵法", "Taiwan Tax Collection Act"),
 ("tw_income_tax", "台湾地区所得税法", "臺灣地區所得稅法", "Taiwan Income Tax Act"),
 ("tw_securities_exchange", "台湾地区证券交易法", "臺灣地區證券交易法", "Taiwan Securities and Exchange Act"),
 ("tw_banking_act", "台湾地区银行法", "臺灣地區銀行法", "Taiwan Banking Act"),
 ("tw_medical_care", "台湾地区医疗法", "臺灣地區醫療法", "Taiwan Medical Care Act"),
 ("tw_communicable_disease", "台湾地区传染病防治法", "臺灣地區傳染病防治法", "Taiwan Communicable Disease Control Act"),
 ("tw_government_information", "台湾地区政府资讯公开法", "臺灣地區政府資訊公開法", "Taiwan Freedom of Government Information Law"),
]

LEGAL_JURISDICTIONS = [
 ("CN", "中国大陆", "中國大陸", "Mainland China", "national",
  "收录全国性法律目录；地方性法规和具体实施规则须另查行为发生地。"),
 ("US-FED", "美国联邦", "美國聯邦", "United States — federal", "federal",
  "收录联邦宪法与美国法典标题；州法、地方条例和判例可能同时适用。"),
 ("TW", "中国台湾地区", "中國臺灣地區", "Taiwan", "territorial",
  "收录台湾地区主要法律知识；应以法务部全國法規資料庫最新文本为准。"),
]

def _store_legal_document(connection, record):
    columns = ("doc_id", "jurisdiction", "subdivision", "level", "title_zh", "title_hant", "title_en",
               "status", "effective_date", "last_updated", "summary_zh", "summary_hant", "summary_en",
               "topics", "source_url", "source_name", "license", "priority")
    connection.execute(f"INSERT OR REPLACE INTO legal_documents VALUES ({','.join('?' for _ in columns)})",
                       tuple(record.get(column, "") for column in columns))
    connection.execute("DELETE FROM legal_terms WHERE doc_id=?", (record["doc_id"],))
    common_terms = {term.strip() for term in record.get("topics", "").split("|") if term.strip()}
    for language, title_key in (("zh", "title_zh"), ("zh_hant", "title_hant"), ("en", "title_en")):
        terms = set(common_terms)
        if record.get(title_key):
            terms.add(record[title_key])
        connection.executemany("INSERT OR IGNORE INTO legal_terms VALUES (?,?,?)",
                               ((record["doc_id"], language, term.lower()) for term in terms))

def build_knowledge_assets():
    """生成 SQLite 历史知识库和可复用的三语 JSONL 训练文档。"""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute("""CREATE TABLE IF NOT EXISTS history_events (
        event_id TEXT PRIMARY KEY, event_date TEXT NOT NULL,
        title_zh TEXT NOT NULL, title_zh_hant TEXT NOT NULL, title_en TEXT NOT NULL,
        description_zh TEXT NOT NULL, description_zh_hant TEXT NOT NULL, description_en TEXT NOT NULL,
        aliases_zh TEXT NOT NULL, aliases_zh_hant TEXT NOT NULL, aliases_en TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS history_terms (
        event_id TEXT NOT NULL, language TEXT NOT NULL, term TEXT NOT NULL,
        PRIMARY KEY (event_id, language, term))""")
    connection.execute("CREATE INDEX IF NOT EXISTS history_terms_language ON history_terms(language)")
    connection.execute("CREATE INDEX IF NOT EXISTS history_terms_lookup ON history_terms(language,term)")
    connection.execute("""CREATE TABLE IF NOT EXISTS history_sources (
        event_id TEXT PRIMARY KEY, source_url TEXT NOT NULL,
        license TEXT NOT NULL, imported_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS history_metadata (
        event_id TEXT PRIMARY KEY, record_type TEXT NOT NULL DEFAULT 'event',
        importance INTEGER NOT NULL DEFAULT 0)""")
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.execute("""CREATE TABLE IF NOT EXISTS character_knowledge (
        character TEXT PRIMARY KEY, codepoint TEXT NOT NULL, mandarin TEXT,
        meaning_zh TEXT, meaning_zh_hant TEXT, definition_en TEXT,
        is_surname INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS english_words (
        word TEXT PRIMARY KEY COLLATE NOCASE, phonetic TEXT,
        definition_en TEXT, translation_zh TEXT, part_of_speech TEXT,
        tags TEXT, frequency_rank INTEGER, source TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS english_word_usage (
        word TEXT NOT NULL COLLATE NOCASE, example_en TEXT NOT NULL,
        example_zh TEXT, source TEXT NOT NULL,
        PRIMARY KEY (word, example_en))""")
    connection.execute("CREATE INDEX IF NOT EXISTS english_usage_word ON english_word_usage(word)")
    connection.execute("""CREATE TABLE IF NOT EXISTS legal_jurisdictions (
        jurisdiction TEXT PRIMARY KEY, name_zh TEXT NOT NULL, name_hant TEXT NOT NULL,
        name_en TEXT NOT NULL, scope_level TEXT NOT NULL, coverage_note TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS legal_documents (
        doc_id TEXT PRIMARY KEY, jurisdiction TEXT NOT NULL, subdivision TEXT NOT NULL,
        authority_level TEXT NOT NULL, title_zh TEXT NOT NULL, title_hant TEXT NOT NULL,
        title_en TEXT NOT NULL, status TEXT NOT NULL, effective_date TEXT NOT NULL,
        last_updated TEXT NOT NULL, summary_zh TEXT NOT NULL, summary_hant TEXT NOT NULL,
        summary_en TEXT NOT NULL, topics TEXT NOT NULL, source_url TEXT NOT NULL,
        source_name TEXT NOT NULL, license TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS legal_terms (
        doc_id TEXT NOT NULL, language TEXT NOT NULL, term TEXT NOT NULL,
        PRIMARY KEY(doc_id,language,term))""")
    connection.execute("CREATE INDEX IF NOT EXISTS legal_terms_lookup ON legal_terms(language,term)")
    connection.execute("CREATE INDEX IF NOT EXISTS legal_documents_jurisdiction ON legal_documents(jurisdiction)")
    connection.execute("""CREATE TABLE IF NOT EXISTS legal_sections (
        doc_id TEXT NOT NULL, section_number TEXT NOT NULL, heading TEXT NOT NULL,
        text TEXT NOT NULL, language TEXT NOT NULL, source_updated TEXT NOT NULL,
        PRIMARY KEY(doc_id,section_number,language))""")
    connection.execute("CREATE INDEX IF NOT EXISTS legal_sections_document ON legal_sections(doc_id,language)")
    connection.executemany("INSERT OR REPLACE INTO legal_jurisdictions VALUES (?,?,?,?,?,?)", LEGAL_JURISDICTIONS)
    for title_number, title_name in US_CODE_TITLES.items():
        _store_legal_document(connection, {
            "doc_id": f"us_usc_{title_number}", "jurisdiction": "US-FED", "subdivision": "federal",
            "level": "United States Code", "title_zh": f"美国法典第{title_number}编：{title_name}",
            "title_hant": f"美國法典第{title_number}編：{title_name}",
            "title_en": f"Title {title_number}, United States Code — {title_name}",
            "status": "current release", "summary_zh": f"美国联邦成文法汇编第{title_number}编，主题为 {title_name}。州法和地方规则可能另行适用。",
            "summary_hant": f"美國聯邦成文法彙編第{title_number}編，主題為 {title_name}。州法和地方規則可能另行適用。",
            "summary_en": f"Title {title_number} of the federal United States Code covers {title_name}. State and local law may also apply.",
            "topics": f"Title {title_number}|{title_number} USC|{title_number} U.S.C.|{title_name}",
            "source_url": f"https://uscode.house.gov/browse/prelim@title{title_number}/front",
            "source_name": "U.S. House Office of the Law Revision Counsel", "license": "US-government-work",
            "priority": 10})
    for doc_id, title_zh, title_hant, title_en in TAIWAN_LAW_TITLES:
        _store_legal_document(connection, {
            "doc_id":doc_id, "jurisdiction":"TW", "subdivision":"Taiwan", "level":"法律",
            "title_zh":title_zh, "title_hant":title_hant, "title_en":title_en,
            "status":"现行状态须核对", "summary_zh":f"这是台湾地区的{title_zh.removeprefix('台湾地区')}。具体适用条件、例外和最新修正应在法务部全國法規資料庫核对。",
            "summary_hant":f"這是臺灣地區的{title_hant.removeprefix('臺灣地區')}。具體適用條件、例外和最新修正應在法務部全國法規資料庫核對。",
            "summary_en":f"{title_en}. Check the Ministry of Justice Laws & Regulations Database for the current text, conditions, and exceptions.",
            "topics":f"{title_zh}|{title_hant}|{title_en}", "source_url":"https://law.moj.gov.tw/",
            "source_name":"全國法規資料庫（法務部）", "license":"政府資料開放授權條款第1版",
            "priority":20})
    for legal_record in LEGAL_SEEDS:
        _store_legal_document(connection, legal_record)
    connection.executemany("INSERT OR REPLACE INTO english_word_usage VALUES (?,?,?,'curated')",
                           ENGLISH_USAGE_SEEDS)
    connection.executemany("""INSERT OR REPLACE INTO character_knowledge
        VALUES (?,?,?,?,?,?,?,?)""",
        ((char, f"U+{ord(char):04X}", pinyin, zh, hant, en, surname, "curated")
         for char,pinyin,zh,hant,en,surname in CHARACTER_SEEDS))
    connection.executemany("INSERT OR REPLACE INTO history_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", HISTORY_EVENTS)
    for event in HISTORY_EVENTS:
        connection.execute("INSERT OR IGNORE INTO history_metadata VALUES (?,'event',1000)", (event[0],))
        connection.execute("DELETE FROM history_terms WHERE event_id=?", (event[0],))
        for language, title_index, aliases_index in (("zh",2,8), ("zh_hant",3,9), ("en",4,10)):
            terms = {event[title_index], event[1], *event[aliases_index].split("|")}
            connection.executemany("INSERT OR IGNORE INTO history_terms VALUES (?,?,?)",
                                   ((event[0], language, term.lower()) for term in terms if term))
    # 为旧版数据库中尚无类型标记的 Wikidata 事件补上元数据；人物已有 person 标记，
    # INSERT OR IGNORE 不会覆盖它们。
    connection.execute("""INSERT OR IGNORE INTO history_metadata(event_id,record_type,importance)
        SELECT event_id,'event',0 FROM history_events""")
    # Wikidata 使用天文纪年：0 表示公元前1年，-550 表示公元前551年。
    # 旧版直接 abs(year) 导致 BCE 日期少一年；仅对有 Wikidata 来源的数据做一次迁移。
    if connection.execute("SELECT 1 FROM schema_migrations WHERE name='wikidata_bce_astronomical_fix_v1'").fetchone() is None:
        for event_id,event_date in connection.execute("""SELECT e.event_id,e.event_date FROM history_events e
            JOIN history_sources s ON s.event_id=e.event_id WHERE e.event_date LIKE '%公元前%'""").fetchall():
            corrected = re.sub(r"公元前(\d+)年", lambda m:f"公元前{int(m.group(1))+1}年", event_date)
            connection.execute("UPDATE history_events SET event_date=? WHERE event_id=?",(corrected,event_id))
        connection.execute("INSERT INTO schema_migrations VALUES(?,?)",
                           ("wikidata_bce_astronomical_fix_v1",datetime.now().isoformat(timespec="seconds")))
    connection.commit()
    extended_counts = ensure_extended_knowledge(DATABASE_PATH)
    action_guide_count = ensure_legal_action_guides(DATABASE_PATH)
    life_guide_count = ensure_life_legal_guides(DATABASE_PATH)
    daily_guide_count = ensure_daily_life_guides(DATABASE_PATH)

    language_fields = {"zh": (2, 5, 8), "zh_hant": (3, 6, 9), "en": (4, 7, 10)}
    with TRAINING_DOCUMENT_PATH.open("w", encoding="utf-8") as document:
        for intent, base_prompts in TRAINING_DATA.items():
            prompts=list(base_prompts)+DOMESTIC_TRAINING_DATA.get(intent,[])
            for prompt in prompts:
                language = detect_language(prompt)
                messages = [{"role":"user", "content":prompt}]
                if intent in RESPONSES[language]:
                    messages.append({"role":"assistant", "content":choose_response(language, intent, prompt)})
                elif intent == "math":
                    try:
                        result = calculate(prompt)
                        answer = {"zh":f"计算结果：{result}", "zh_hant":f"計算結果：{result}",
                                  "en":f"Result: {result}"}[language]
                        messages.append({"role":"assistant", "content":answer})
                    except (ValueError, SyntaxError, ZeroDivisionError, InvalidOperation, OverflowError): pass
                elif intent == "legal":
                    messages.append({"role":"assistant", "content":answer_legal(prompt, language)})
                row = {"language": language, "intent": intent, "messages": messages}
                document.write(json.dumps(row, ensure_ascii=False) + "\n")
        # 用游标逐条读取，知识库再大也不会整体载入内存。
        for event in connection.execute("SELECT * FROM history_events ORDER BY event_id"):
            for language, (title_index, description_index, aliases_index) in language_fields.items():
                title, description = event[title_index], event[description_index]
                aliases = event[aliases_index].split("|")
                prompt = ({"zh":f"请介绍{aliases[0]}", "zh_hant":f"請介紹{aliases[0]}",
                           "en":f"Tell me about {aliases[0]}"})[language]
                answer = f"{title}（{event[1]}）：{description}"
                row = {"language":language, "intent":"history", "event_id":event[0],
                       "messages":[{"role":"user", "content":prompt}, {"role":"assistant", "content":answer}]}
                document.write(json.dumps(row, ensure_ascii=False) + "\n")
        for legal in connection.execute("SELECT * FROM legal_documents ORDER BY doc_id"):
            for language, title_index, summary_index in (("zh",4,10), ("zh_hant",5,11), ("en",6,12)):
                title = legal[title_index]
                if not title:
                    continue
                prompt = {"zh":f"请介绍{title}", "zh_hant":f"請介紹{title}",
                          "en":f"Tell me about {title}"}[language]
                answer = f"{title}：{legal[summary_index]} 来源：{legal[15]}"
                row = {"language":language, "intent":"legal", "doc_id":legal[0],
                       "jurisdiction":legal[1],
                       "messages":[{"role":"user", "content":prompt}, {"role":"assistant", "content":answer}]}
                document.write(json.dumps(row, ensure_ascii=False) + "\n")
        for entry in connection.execute("SELECT * FROM encyclopedia_entries ORDER BY entry_id"):
            for language,title_index,summary_index in (("zh",2,5),("zh_hant",3,6),("en",4,7)):
                title, summary = entry[title_index], entry[summary_index]
                prompt = {"zh":f"什么是{title}", "zh_hant":f"什麼是{title}",
                          "en":f"What is {title}?"}[language]
                answer = f"{title}：{summary}\n来源：{entry[8]} — {entry[9]}"
                row = {"language":language, "intent":"encyclopedia", "entry_id":entry[0],
                       "messages":[{"role":"user","content":prompt},{"role":"assistant","content":answer}]}
                document.write(json.dumps(row, ensure_ascii=False) + "\n")
    event_count = connection.execute("SELECT count(*) FROM history_events").fetchone()[0]
    character_count = connection.execute("SELECT count(*) FROM character_knowledge").fetchone()[0]
    word_count = connection.execute("SELECT count(*) FROM english_words").fetchone()[0]
    legal_count = connection.execute("SELECT count(*) FROM legal_documents").fetchone()[0]
    connection.close()
    print(f"知识库：{DATABASE_PATH}（{event_count} 个历史条目，{extended_counts['encyclopedia']} 个基础百科条目，"
          f"{character_count} 个汉字条目，{word_count} 个英语词条，{legal_count} 个法律文件，"
          f"{action_guide_count + life_guide_count} 个法律办事指南，{daily_guide_count} 个非法律生活指南，"
          f"通用知识导入上限 600 MiB）")
    print(f"多语言训练文档：{TRAINING_DOCUMENT_PATH}")

def _wikidata_date(value, language):
    match = re.match(r"(-?\d+)-", value)
    if not match: return value
    year = int(match.group(1))
    if year < 0:
        bce_year = abs(year) + 1
        return f"公元前{bce_year}年" if language != "en" else f"{bce_year} BCE"
    return f"{year}年" if language != "en" else str(year)

def _wikidata_lifespan(birth, death, language):
    """把 Wikidata 时间转换成适合显示的人物生卒年。"""
    birth_text = _wikidata_date(birth, language) if birth else "?"
    death_text = _wikidata_date(death, language) if death else "?"
    return f"{birth_text}—{death_text}"

def _split_wikidata_aliases(raw_aliases):
    """解析带语言标签的 GROUP_CONCAT 结果，并限制异常超长别名。"""
    result = {"zh": set(), "zh_hant": set(), "en": set()}
    for item in (raw_aliases or "").split("|||"):
        if "::" not in item:
            continue
        code, alias = item.split("::", 1)
        alias = " ".join(alias.replace("|", " ").split()).strip()
        if not alias or len(alias) > (80 if code == "en" else 40):
            continue
        language = "zh_hant" if code == "zh-hant" else ("zh" if code in {"zh", "zh-hans"} else "en")
        result[language].add(alias)
    return result

def import_wikidata_history(limit=1000):
    """从 Wikidata 批量导入历史事件；分页处理，不限制数据库最终容量。"""
    build_knowledge_assets()
    connection = sqlite3.connect(DATABASE_PATH)
    imported, offset = 0, 0
    # 支持断点式再次运行：已下载的实体会跳过，只统计新记录。
    seen = {row[0] for row in connection.execute(
        "SELECT event_id FROM history_events WHERE event_id LIKE 'wikidata_%'")}
    page_size = min(1000, max(1, limit))
    while imported < limit:
        query = f'''PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?event ?date ?zh ?hant ?en WHERE {{
          ?event wdt:P31/wdt:P279* wd:Q13418847.
          {{ ?event wdt:P585 ?date }} UNION {{ ?event wdt:P580 ?date }}
          ?event rdfs:label ?en FILTER(LANG(?en)="en")
          ?event rdfs:label ?zh FILTER(LANG(?zh) IN ("zh","zh-hans"))
          OPTIONAL {{ ?event rdfs:label ?hant FILTER(LANG(?hant)="zh-hant") }}
        }} ORDER BY ?event LIMIT {page_size} OFFSET {offset}'''
        # QLever 是 Wikidata 的高性能开放查询镜像；结果仍链接回 Wikidata 实体。
        url = "https://qlever.dev/api/wikidata?" + urllib.parse.urlencode({"query":query})
        request = urllib.request.Request(url, headers={"User-Agent":"LocalAI-Educational-History-Importer/1.0",
                                                       "Accept":"application/sparql-results+json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    bindings = json.load(response)["results"]["bindings"]
                break
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == 2: raise RuntimeError(f"Wikidata 第 {offset//page_size+1} 页下载失败：{error}") from error
        if not bindings: break
        offset += page_size
        for item in bindings:
            qid = item["event"]["value"].rsplit("/", 1)[-1]
            event_id = f"wikidata_{qid}"
            if event_id in seen: continue
            seen.add(event_id)
            raw_date = item["date"]["value"]
            date_zh, date_en = _wikidata_date(raw_date, "zh"), _wikidata_date(raw_date, "en")
            title_zh = item["zh"]["value"]
            title_hant = item.get("hant", {}).get("value", title_zh)
            title_en = item["en"]["value"]
            desc_zh = f"{title_zh}是发生于{date_zh}的历史事件。详细资料来源为Wikidata实体{qid}。"
            desc_hant = f"{title_hant}是發生於{date_zh}的歷史事件。詳細資料來源為Wikidata實體{qid}。"
            desc_en = f"{title_en} is a historical event dated to {date_en}. Its source is Wikidata entity {qid}."
            event = (event_id, date_zh, title_zh, title_hant, title_en, desc_zh, desc_hant, desc_en,
                     title_zh, title_hant, title_en)
            connection.execute("INSERT OR REPLACE INTO history_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", event)
            connection.execute("DELETE FROM history_terms WHERE event_id=?", (event_id,))
            for language, terms in (("zh",{date_zh,title_zh}), ("zh_hant",{date_zh,title_hant}),
                                    ("en",{date_en,title_en})):
                connection.executemany("INSERT OR IGNORE INTO history_terms VALUES (?,?,?)",
                                       ((event_id, language, term.lower()) for term in terms))
            connection.execute("INSERT OR REPLACE INTO history_sources VALUES (?,?,?,?)",
                               (event_id, item["event"]["value"], "CC0-1.0",
                                datetime.now().isoformat(timespec="seconds")))
            imported += 1
            if imported >= limit: break
        connection.commit()
        print(f"已导入 {imported}/{limit} 条……")
    total = connection.execute("SELECT count(*) FROM history_events").fetchone()[0]
    connection.close()
    build_knowledge_assets()
    print(f"导入完成：本次新增 {imported} 条事件，数据库目前共 {total} 个历史条目。")
    return imported

def import_wikidata_people(limit=10000, min_sitelinks=5, start_offset=0):
    """导入有中文与英文条目的已故历史人物，并保存三语别名和来源。

    sitelinks 下限优先保留较有资料价值的人物；分页写入 SQLite，不会把全部数据
    挂进模型内存。重复执行可继续扩大数据库，已有 QID 会安全更新而不会重复。
    """
    build_knowledge_assets()
    connection = sqlite3.connect(DATABASE_PATH)
    imported = processed = 0
    offset = max(0, int(start_offset))
    page_size = min(500, max(1, limit))
    while processed < limit:
        current_page_size = min(page_size, limit - processed)
        query = f'''PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX schema: <http://schema.org/>
        PREFIX wikibase: <http://wikiba.se/ontology#>
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT ?item ?sitelinks
          (SAMPLE(?birth0) AS ?birth) (SAMPLE(?death0) AS ?death)
          (SAMPLE(?zh0) AS ?zh) (SAMPLE(?hans0) AS ?hans)
          (SAMPLE(?hant0) AS ?hant) (SAMPLE(?en0) AS ?en)
          (SAMPLE(?descZh0) AS ?descZh) (SAMPLE(?descHans0) AS ?descHans)
          (SAMPLE(?descHant0) AS ?descHant)
          (SAMPLE(?descEn0) AS ?descEn)
          (GROUP_CONCAT(DISTINCT CONCAT(LANG(?alias0), "::", STR(?alias0)); separator="|||") AS ?aliases)
        WHERE {{
          ?item wdt:P31 wd:Q5; wdt:P570 ?death0; wikibase:sitelinks ?sitelinks.
          FILTER(YEAR(?death0) <= 2000 && ?sitelinks >= {max(0, int(min_sitelinks))})
          ?item rdfs:label ?zh0 FILTER(LANG(?zh0)="zh")
          ?item rdfs:label ?en0 FILTER(LANG(?en0)="en")
          OPTIONAL {{ ?item rdfs:label ?hans0 FILTER(LANG(?hans0)="zh-hans") }}
          OPTIONAL {{ ?item rdfs:label ?hant0 FILTER(LANG(?hant0)="zh-hant") }}
          OPTIONAL {{ ?item wdt:P569 ?birth0 }}
          OPTIONAL {{ ?item schema:description ?descZh0 FILTER(LANG(?descZh0)="zh") }}
          OPTIONAL {{ ?item schema:description ?descHans0 FILTER(LANG(?descHans0)="zh-hans") }}
          OPTIONAL {{ ?item schema:description ?descHant0 FILTER(LANG(?descHant0)="zh-hant") }}
          OPTIONAL {{ ?item schema:description ?descEn0 FILTER(LANG(?descEn0)="en") }}
          OPTIONAL {{ ?item skos:altLabel ?alias0
                      FILTER(LANG(?alias0) IN ("zh","zh-hans","zh-hant","en")) }}
        }} GROUP BY ?item ?sitelinks
        ORDER BY DESC(?sitelinks) ?item LIMIT {current_page_size} OFFSET {offset}'''
        url = "https://qlever.dev/api/wikidata?" + urllib.parse.urlencode({"query": query})
        request = urllib.request.Request(
            url, headers={"User-Agent": "LocalAI-Educational-History-Importer/2.0",
                          "Accept": "application/sparql-results+json"})
        bindings = None
        last_error = None
        for _attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    bindings = json.load(response)["results"]["bindings"]
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
        if bindings is None:
            connection.close()
            raise RuntimeError(f"Wikidata 历史人物第 {offset // page_size + 1} 页下载失败：{last_error}")
        if not bindings:
            break
        offset += len(bindings)
        processed += len(bindings)
        for item in bindings:
            qid = item["item"]["value"].rsplit("/", 1)[-1]
            event_id = f"wikidata_person_{qid}"
            birth = item.get("birth", {}).get("value", "")
            death = item.get("death", {}).get("value", "")
            title_zh = item.get("hans", item["zh"])["value"].strip()
            title_hant = item.get("hant", {}).get("value", title_zh).strip()
            title_en = item["en"]["value"].strip()
            aliases = _split_wikidata_aliases(item.get("aliases", {}).get("value", ""))
            aliases["zh"].add(title_zh)
            aliases["zh_hant"].add(title_hant)
            aliases["en"].add(title_en)
            date_zh = _wikidata_lifespan(birth, death, "zh")
            date_en = _wikidata_lifespan(birth, death, "en")
            desc_zh_raw = item.get("descHans", item.get("descZh", {})).get("value", "").strip()
            desc_hant_raw = item.get("descHant", {}).get("value", "").strip()
            desc_en_raw = item.get("descEn", {}).get("value", "").strip()
            desc_zh = f"{title_zh}是{desc_zh_raw}。" if desc_zh_raw else f"{title_zh}是一位历史人物。"
            desc_hant = (f"{title_hant}是{desc_hant_raw}。" if desc_hant_raw else
                         f"{title_hant}是一位歷史人物。")
            desc_en = (f"{title_en} was {desc_en_raw}." if desc_en_raw else
                       f"{title_en} was a historical figure.")
            record = (event_id, date_zh, title_zh, title_hant, title_en,
                      desc_zh, desc_hant, desc_en,
                      "|".join(sorted(aliases["zh"])),
                      "|".join(sorted(aliases["zh_hant"])),
                      "|".join(sorted(aliases["en"])))
            existed = connection.execute(
                "SELECT 1 FROM history_events WHERE event_id=?", (event_id,)).fetchone()
            connection.execute("INSERT OR REPLACE INTO history_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", record)
            connection.execute("DELETE FROM history_terms WHERE event_id=?", (event_id,))
            for language, terms in aliases.items():
                display_date = date_en if language == "en" else date_zh
                all_terms = set(terms)
                all_terms.add(display_date)
                connection.executemany("INSERT OR IGNORE INTO history_terms VALUES (?,?,?)",
                                       ((event_id, language, term.lower()) for term in all_terms if term))
            importance = int(item.get("sitelinks", {}).get("value", 0))
            connection.execute("INSERT OR REPLACE INTO history_metadata VALUES (?,?,?)",
                               (event_id, "person", importance))
            connection.execute("INSERT OR REPLACE INTO history_sources VALUES (?,?,?,?)",
                               (event_id, item["item"]["value"], "CC0-1.0",
                                datetime.now().isoformat(timespec="seconds")))
            imported += int(existed is None)
        connection.commit()
        print(f"已处理 {processed}/{limit} 位历史人物（远端偏移 {offset}，新增 {imported} 条）……",
              flush=True)
    total = connection.execute("SELECT count(*) FROM history_events").fetchone()[0]
    people = connection.execute(
        "SELECT count(*) FROM history_metadata WHERE record_type='person'").fetchone()[0]
    connection.close()
    build_knowledge_assets()
    print(f"历史人物导入完成：新增 {imported} 条，现有 {people} 位人物、{total} 个历史条目。")
    return imported

def import_china_law_catalog():
    """从全国人大常委会国家法律法规数据库导入全国性法律目录与时效状态。"""
    build_knowledge_assets()
    connection = sqlite3.connect(DATABASE_PATH)
    endpoint = "https://flk.npc.gov.cn/law-search/search/list"
    category_codes = [110, 120, 130, 140, 150, 155, 160, 170]
    page, imported = 1, 0
    while True:
        payload = {
            "searchRange":1, "sxrq":[], "gbrq":[], "searchType":2, "sxx":[], "gbrqYear":[],
            "flfgCodeId":category_codes, "zdjgCodeId":[], "searchContent":"",
            "orderByParam":{"order":"-1", "sort":""}, "pageNum":page, "pageSize":100,
        }
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type":"application/json", "User-Agent":"LocalAI-Legal-Importer/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
        if result.get("code") != 200:
            connection.close()
            raise RuntimeError(f"国家法律法规数据库返回错误：{result.get('msg', 'unknown')}")
        rows = result.get("rows") or []
        if not rows:
            break
        for row in rows:
            bbbs = row["bbbs"]
            title = re.sub(r"<[^>]+>", "", row.get("title", "")).strip()
            if not title:
                continue
            status = {1:"已废止", 2:"已修改", 3:"有效", 4:"尚未生效"}.get(row.get("sxx"), "状态待核对")
            authority = row.get("zdjgName") or "制定机关待核对"
            nature = row.get("flxz") or "法律"
            doc_id = f"cn_flk_{bbbs}"
            existed = connection.execute("SELECT 1 FROM legal_documents WHERE doc_id=?", (doc_id,)).fetchone()
            source_url = "https://flk.npc.gov.cn/detail?id=" + urllib.parse.quote(bbbs)
            _store_legal_document(connection, {
                "doc_id":doc_id, "jurisdiction":"CN", "subdivision":"national", "level":nature,
                "title_zh":title, "title_hant":title, "title_en":"", "status":status,
                "effective_date":row.get("sxrq") or "", "last_updated":row.get("gbrq") or "",
                "summary_zh":f"这是由{authority}公布的{nature}。查询具体权利义务时，应打开官方来源核对现行全文、适用条件及配套规定。",
                "summary_hant":f"這是由{authority}公布的{nature}。查詢具體權利義務時，應打開官方來源核對現行全文、適用條件及配套規定。",
                "summary_en":f"An official {nature} issued by {authority}. Check the official current text and implementing rules before relying on it.",
                "topics":f"{title}|{nature}|{authority}", "source_url":source_url,
                "source_name":"国家法律法规数据库（全国人大常委会办公厅）",
                "license":"official-public-law", "priority":20})
            imported += int(existed is None)
        connection.commit()
        print(f"已处理中国大陆法律目录 {min(page * 100, result.get('total', 0))}/{result.get('total', 0)} 条……",
              flush=True)
        if page * 100 >= result.get("total", 0):
            break
        page += 1
    total = connection.execute("SELECT count(*) FROM legal_documents WHERE jurisdiction='CN'").fetchone()[0]
    connection.close()
    build_knowledge_assets()
    print(f"中国大陆法律目录导入完成：新增 {imported} 条，现有 {total} 条。")
    return imported

def import_unihan_characters():
    """导入 Unicode 官方 Unihan 读音和英文释义。"""
    build_knowledge_assets()
    url = "https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip"
    print("正在下载 Unicode Unihan 数据……")
    request = urllib.request.Request(url, headers={"User-Agent":"LocalAI-Unicode-Importer/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        archive = response.read()
    records = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        with package.open("Unihan_Readings.txt") as source:
            for raw_line in source:
                line = raw_line.decode("utf-8").strip()
                if not line or line.startswith("#"): continue
                codepoint, field, value = line.split("\t", 2)
                if field not in {"kMandarin", "kDefinition"}: continue
                char = chr(int(codepoint[2:], 16))
                record = records.setdefault(char, {"mandarin":"", "definition":""})
                record["mandarin" if field == "kMandarin" else "definition"] = value
    rows = []
    for char, record in records.items():
        definition = record["definition"]
        rows.append((char, f"U+{ord(char):04X}", record["mandarin"], "", "", definition,
                     int("surname" in definition.lower()), "Unicode Unihan"))
    connection = sqlite3.connect(DATABASE_PATH)
    connection.executemany("""INSERT INTO character_knowledge VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(character) DO UPDATE SET
          mandarin=CASE WHEN excluded.mandarin<>'' THEN excluded.mandarin ELSE character_knowledge.mandarin END,
          definition_en=CASE WHEN excluded.definition_en<>'' THEN excluded.definition_en ELSE character_knowledge.definition_en END,
          is_surname=max(character_knowledge.is_surname,excluded.is_surname),
          source=CASE WHEN character_knowledge.source='curated' THEN 'curated + Unicode Unihan' ELSE excluded.source END""", rows)
    connection.commit()
    count = connection.execute("SELECT count(*) FROM character_knowledge").fetchone()[0]
    surnames = connection.execute("SELECT count(*) FROM character_knowledge WHERE is_surname=1").fetchone()[0]
    connection.close()
    build_knowledge_assets()
    print(f"Unihan导入完成：{count}个汉字条目，其中{surnames}个释义含姓氏标记。")
    return count

def import_english_words(max_length=12):
    """流式筛选 ECDICT 的常用短词，原始63MB CSV不会常驻内存或落盘。"""
    build_knowledge_assets()
    url = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
    print(f"正在下载并筛选 ECDICT（仅保留不超过 {max_length} 个字母的常用词）……")
    request = urllib.request.Request(url, headers={"User-Agent":"LocalAI-English-Dictionary-Importer/1.0"})
    connection = sqlite3.connect(DATABASE_PATH)
    imported, scanned, batch = 0, 0, []
    with urllib.request.urlopen(request, timeout=240) as response:
        text_stream = io.TextIOWrapper(response, encoding="utf-8-sig", newline="")
        for row in csv.DictReader(text_stream):
            scanned += 1
            word = (row.get("word") or "").strip().lower()
            translation = (row.get("translation") or "").strip()
            if not translation or len(word) > max_length or not re.fullmatch(r"[a-z]+(?:'[a-z]+)?", word): continue
            def rank(name):
                value = (row.get(name) or "").strip()
                return int(value) if value.isdigit() and int(value) > 0 else 10**9
            frequency = min(rank("bnc"), rank("frq"))
            tags = (row.get("tag") or "").strip()
            common = ((row.get("oxford") or "").strip() == "1" or bool(tags) or
                      (row.get("collins") or "").strip() not in {"", "0"} or frequency <= 30000)
            if not common: continue
            definition = (row.get("definition") or "").strip()
            batch.append((word, (row.get("phonetic") or "").strip(), definition, translation,
                          (row.get("pos") or "").strip(), tags,
                          None if frequency == 10**9 else frequency, "ECDICT"))
            if len(batch) >= 1000:
                connection.executemany("INSERT OR REPLACE INTO english_words VALUES (?,?,?,?,?,?,?,?)", batch)
                connection.commit(); imported += len(batch); batch.clear()
                if imported % 10000 == 0: print(f"已导入 {imported} 个常用短词……")
    if batch:
        connection.executemany("INSERT OR REPLACE INTO english_words VALUES (?,?,?,?,?,?,?,?)", batch)
        imported += len(batch)
    connection.commit()
    total = connection.execute("SELECT count(*) FROM english_words").fetchone()[0]
    connection.close()
    build_knowledge_assets()
    print(f"英语词典导入完成：扫描 {scanned} 条，本次筛选 {imported} 条，数据库共 {total} 个短词。")
    return total

def import_wordnet_usage():
    """导入 Princeton WordNet 中经过人工编纂的英语例句。"""
    build_knowledge_assets()
    url = "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip"
    print("正在下载并提取 WordNet 例句……")
    request = urllib.request.Request(url, headers={"User-Agent":"LocalAI-WordNet-Usage-Importer/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        archive = response.read()
    connection = sqlite3.connect(DATABASE_PATH)
    allowed = {row[0] for row in connection.execute("SELECT word FROM english_words")}
    batch = []
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        for filename in ("wordnet/data.noun", "wordnet/data.verb", "wordnet/data.adj", "wordnet/data.adv"):
            with package.open(filename) as source:
                for raw_line in source:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line[0].isdigit() or "|" not in line: continue
                    metadata, gloss = line.split("|", 1)
                    fields = metadata.split()
                    try: word_count = int(fields[3], 16)
                    except (ValueError, IndexError): continue
                    lemmas = {fields[4 + index * 2].replace("_", " ").lower()
                              for index in range(word_count)}
                    examples = [example.strip().replace("_", " ") for example in re.findall(r'"([^"]+)"', gloss)]
                    for lemma in lemmas & allowed:
                        for example in examples[:2]:
                            if 3 <= len(example) <= 240:
                                batch.append((lemma, example, "", "Princeton WordNet 3.0"))
                    if len(batch) >= 2000:
                        connection.executemany("INSERT OR IGNORE INTO english_word_usage VALUES (?,?,?,?)", batch)
                        connection.commit(); batch.clear()
    if batch: connection.executemany("INSERT OR IGNORE INTO english_word_usage VALUES (?,?,?,?)", batch)
    connection.commit()
    count = connection.execute("SELECT count(*) FROM english_word_usage").fetchone()[0]
    covered = connection.execute("SELECT count(DISTINCT word) FROM english_word_usage").fetchone()[0]
    connection.close()
    build_knowledge_assets()
    print(f"WordNet例句导入完成：{count}条例句，覆盖{covered}个英语短词。")
    return count

US_STATE_MARKERS = {
    "加州":"California", "加利福尼亚":"California", "california":"California",
    "纽约州":"New York", "紐約州":"New York", "new york":"New York",
    "德州":"Texas", "得克萨斯":"Texas", "德克薩斯":"Texas", "texas":"Texas",
    "佛州":"Florida", "佛罗里达":"Florida", "佛羅里達":"Florida", "florida":"Florida",
    "华盛顿州":"Washington", "華盛頓州":"Washington", "washington state":"Washington",
    "伊利诺伊":"Illinois", "伊利諾伊":"Illinois", "illinois":"Illinois",
    "马萨诸塞":"Massachusetts", "麻薩諸塞":"Massachusetts", "massachusetts":"Massachusetts",
    "亚利桑那":"Arizona", "亞利桑那":"Arizona", "arizona":"Arizona",
    "科罗拉多":"Colorado", "科羅拉多":"Colorado", "colorado":"Colorado",
    "佐治亚":"Georgia", "喬治亞":"Georgia", "georgia":"Georgia",
    "新泽西":"New Jersey", "新澤西":"New Jersey", "new jersey":"New Jersey",
    "宾夕法尼亚":"Pennsylvania", "賓夕法尼亞":"Pennsylvania", "pennsylvania":"Pennsylvania",
    "俄亥俄":"Ohio", "ohio":"Ohio", "密歇根":"Michigan", "michigan":"Michigan",
    "弗吉尼亚":"Virginia", "維吉尼亞":"Virginia", "virginia":"Virginia",
    "北卡罗来纳":"North Carolina", "北卡羅來納":"North Carolina", "north carolina":"North Carolina",
    "南卡罗来纳":"South Carolina", "南卡羅來納":"South Carolina", "south carolina":"South Carolina",
    "马里兰":"Maryland", "馬里蘭":"Maryland", "maryland":"Maryland",
    "康涅狄格":"Connecticut", "康乃狄克":"Connecticut", "connecticut":"Connecticut",
    "俄勒冈":"Oregon", "俄勒岡":"Oregon", "oregon":"Oregon",
    "内华达":"Nevada", "內華達":"Nevada", "nevada":"Nevada",
    "明尼苏达":"Minnesota", "明尼蘇達":"Minnesota", "minnesota":"Minnesota",
    "威斯康星":"Wisconsin", "wisconsin":"Wisconsin", "田纳西":"Tennessee", "田納西":"Tennessee", "tennessee":"Tennessee",
    "印第安纳":"Indiana", "印第安納":"Indiana", "indiana":"Indiana", "密苏里":"Missouri", "密蘇里":"Missouri", "missouri":"Missouri",
}

CN_LOCAL_MARKERS = ("北京", "上海", "天津", "重庆", "重慶", "广东", "廣東", "深圳", "浙江", "江苏", "江蘇",
                    "福建", "山东", "山東", "河南", "河北", "湖北", "湖南", "四川", "海南", "广西", "廣西",
                    "云南", "雲南", "贵州", "貴州", "陕西", "陝西", "新疆", "西藏", "内蒙古", "內蒙古",
                    "安徽", "江西", "辽宁", "遼寧", "吉林", "黑龙江", "黑龍江", "甘肃", "甘肅", "青海", "宁夏", "寧夏",
                    "广州", "廣州", "杭州", "南京", "苏州", "蘇州", "成都", "武汉", "武漢", "西安", "郑州", "鄭州",
                    "长沙", "長沙", "济南", "濟南", "青岛", "青島", "厦门", "廈門", "福州", "合肥", "南昌", "昆明",
                    "贵阳", "貴陽", "沈阳", "瀋陽", "大连", "大連", "长春", "長春", "哈尔滨", "哈爾濱", "石家庄", "石家莊",
                    "太原", "呼和浩特", "南宁", "南寧", "海口", "兰州", "蘭州", "银川", "銀川", "乌鲁木齐", "烏魯木齊", "拉萨", "拉薩")

def _legal_jurisdiction_from_text(text):
    value = text.lower()
    if re.search(r"香港|hong kong", value, re.I): return "HK", "香港特别行政区"
    if re.search(r"澳门|澳門|macao|macau", value, re.I): return "MO", "澳门特别行政区"
    if re.search(r"台湾|臺灣|中華民國|中华民国|\btaiwan\b|\broc\b", value, re.I):
        return "TW", "中国台湾地区"
    for marker, state in US_STATE_MARKERS.items():
        if marker in value:
            return "US-STATE", state
    if re.search(r"美国联邦|美國聯邦|美联邦|美聯邦|联邦法|聯邦法|federal law|federal statute|"
                 r"\bu\.?s\.?c\.?\b|united states code", value, re.I):
        return "US-FED", "美国联邦"
    if re.search(r"美国|美國|\bu\.?s\.?(?:a\.)?\b|united states|american law", value, re.I):
        return "US-UNSPECIFIED", "美国（未指定联邦或州）"
    for marker in CN_LOCAL_MARKERS:
        if marker in text:
            return "CN-LOCAL", marker
    if re.search(r"中国(?!台湾|臺灣)|中國(?!臺灣|台湾)|中华人民共和国|中華人民共和國|大陆法|大陸法|\bprc\b|mainland china|chinese law", value, re.I):
        return "CN", "中国大陆"
    return None, ""

def _legal_matches(connection, query, jurisdiction=None, limit=3):
    normalized = query.lower().strip()
    parameters = [normalized]
    jurisdiction_clause = ""
    if jurisdiction:
        jurisdiction_clause = "AND d.jurisdiction=?"
        parameters.append(jurisdiction)
    parameters.append(limit)
    return connection.execute(f"""SELECT DISTINCT d.* FROM legal_terms AS t
        JOIN legal_documents AS d ON d.doc_id=t.doc_id
        WHERE length(t.term)>=2 AND instr(?,lower(t.term))>0 {jurisdiction_clause}
        ORDER BY length(t.term) DESC, d.priority DESC LIMIT ?""", parameters).fetchall()

def answer_legal(query, language):
    if not DATABASE_PATH.exists():
        build_knowledge_assets()
    connection = sqlite3.connect(DATABASE_PATH)
    jurisdiction, region = _legal_jurisdiction_from_text(query)
    action_answer = answer_legal_action(DATABASE_PATH,query,language,jurisdiction,region)
    if action_answer is not None:
        connection.close()
        return action_answer
    life_answer = answer_life_legal(DATABASE_PATH,query,language,jurisdiction,region)
    if life_answer is not None:
        connection.close()
        return life_answer
    if jurisdiction in {"CN", "CN-LOCAL"} and re.search(
            r"(?:民事|民法).*(?:一般|普通)?.*(?:诉讼时效|訴訟時效|时效|時效)|"
            r"(?:诉讼时效|訴訟時效).*(?:一般|普通|多久|多長)", query, re.I):
        connection.close()
        return {
            "zh":"中国大陆《民法典》第188条规定，向人民法院请求保护民事权利的一般诉讼时效期间为三年，法律另有规定的除外。起算、中止、中断和最长保护期间会因事实而异，应尽快核对具体请求。\n来源：国家法律法规数据库 — https://flk.npc.gov.cn/；仅供一般法律信息参考。",
            "zh_hant":"中國大陸《民法典》第188條規定，向人民法院請求保護民事權利的一般訴訟時效期間為三年，法律另有規定者除外。起算、中止、中斷和最長保護期間會因事實而異，應儘快核對具體請求。\n來源：國家法律法規資料庫 — https://flk.npc.gov.cn/；僅供一般法律資訊參考。",
            "en":"Article 188 of mainland China's Civil Code sets a general three-year limitation period for requesting judicial protection of civil rights, unless another law provides otherwise. Accrual, suspension, interruption, and the long-stop period depend on the facts.\nSource: National Database of Laws and Regulations — https://flk.npc.gov.cn/; general information only.",
        }[language]
    if jurisdiction in {"HK", "MO"}:
        connection.close()
        return {
            "zh":f"你问的是{region}法律。它具有独立的法律制度，不能直接套用中国大陆法律；当前本地库尚未收录该法域的完整资料，请查当地官方法例数据库或咨询当地专业人士。",
            "zh_hant":f"你問的是{region}法律。它具有獨立的法律制度，不能直接套用中國大陸法律；目前本地庫尚未收錄該法域的完整資料，請查當地官方法例資料庫或諮詢當地專業人士。",
            "en":f"This concerns the separate legal system of {region}. Mainland Chinese law cannot simply be substituted. This local database does not yet contain that jurisdiction's complete law.",
        }[language]
    if jurisdiction == "US-STATE":
        connection.close()
        return {
            "zh":f"你问的是美国{region}州法。当前本地库收录的是美国联邦法，不能代替{region}州法；州法、地方条例和联邦法可能同时适用，请补充城市并查该州官方立法网站。",
            "zh_hant":f"你問的是美國{region}州法。目前本地庫收錄的是美國聯邦法，不能代替{region}州法；州法、地方條例和聯邦法可能同時適用，請補充城市並查該州官方立法網站。",
            "en":f"This asks about {region} law. The local database currently covers U.S. federal law, not a complete copy of {region} statutes. State, local, and federal rules may all apply.",
        }[language]
    if jurisdiction == "US-UNSPECIFIED":
        # 宪法、专利、联邦法典等问法可明确落在联邦层级；合同、婚姻、租赁、工资等
        # 只说“美国”仍不足以确定适用法，必须追问州和城市。
        clearly_federal = bool(re.search(
            r"宪法|憲法|专利|專利|版权|版權|著作权|著作權|移民|破产|破產|美国法典|美國法典|"
            r"constitution|patent|copyright|immigration|bankruptcy|united states code|u\.?s\.?c\.?", query, re.I))
        if clearly_federal:
            jurisdiction = "US-FED"
        else:
            connection.close()
            return {
                "zh":"美国法律分为联邦、州和地方层级；只说“美国”不足以判断。请说明具体州和城市，或者明确只查询“美国联邦法”。",
                "zh_hant":"美國法律分為聯邦、州和地方層級；只說「美國」不足以判斷。請說明具體州和城市，或者明確只查詢「美國聯邦法」。",
                "en":"U.S. law has federal, state, and local layers. Please identify the state and city, or explicitly ask only about U.S. federal law.",
            }[language]
    local_region = region if jurisdiction == "CN-LOCAL" else ""
    query_jurisdiction = "CN" if jurisdiction == "CN-LOCAL" else jurisdiction
    matches = _legal_matches(connection, query, query_jurisdiction)
    if query_jurisdiction is None:
        cross_matches = _legal_matches(connection, query, None, 12)
        jurisdictions = {row[1] for row in cross_matches}
        if len(jurisdictions) == 1:
            query_jurisdiction = next(iter(jurisdictions))
            matches = [row for row in cross_matches if row[1] == query_jurisdiction][:3]
        else:
            connection.close()
            return {
                "zh":"法律结论具有地域性。请先说明适用地区，例如“中国大陆”“美国联邦/具体州”或“中国台湾地区”，并说明事情发生地。",
                "zh_hant":"法律結論具有地域性。請先說明適用地區，例如「中國大陸」「美國聯邦／具體州」或「中國臺灣地區」，並說明事情發生地。",
                "en":"Legal rules are jurisdiction-specific. Please identify the applicable place—for example mainland China, U.S. federal law plus a specific state, or Taiwan—and where the events occurred.",
            }[language]
    if not matches:
        name_column = {"zh":"name_zh", "zh_hant":"name_hant", "en":"name_en"}[language]
        jurisdiction_row = connection.execute(
            f"SELECT {name_column},coverage_note FROM legal_jurisdictions WHERE jurisdiction=?",
            (query_jurisdiction,)).fetchone()
        titles = connection.execute(
            "SELECT title_zh,title_hant,title_en FROM legal_documents WHERE jurisdiction=? ORDER BY priority DESC,title_zh LIMIT 5",
            (query_jurisdiction,)).fetchall()
        connection.close()
        title_index = {"zh":0, "zh_hant":1, "en":2}[language]
        examples = "、".join(row[title_index] for row in titles if row[title_index])
        area = jurisdiction_row[0] if jurisdiction_row else query_jurisdiction
        return {
            "zh":f"已识别适用地区为{area}，但没有找到与你问题直接匹配的本地条目。可查询：{examples}等。请补充法律名称或具体主题。",
            "zh_hant":f"已識別適用地區為{area}，但沒有找到與你問題直接匹配的本地條目。可查詢：{examples}等。請補充法律名稱或具體主題。",
            "en":f"I identified {area}, but found no directly matching local entry. Available examples include {examples}. Please add the statute name or legal topic.",
        }[language]
    legal = matches[0]
    connection.close()
    title_index, summary_index = {"zh":(4,10), "zh_hant":(5,11), "en":(6,12)}[language]
    title = legal[title_index] or legal[6] or legal[4]
    summary = legal[summary_index] or legal[12] or legal[10]
    labels = {
        "zh":("状态", "施行", "更新", "来源"),
        "zh_hant":("狀態", "施行", "更新", "來源"),
        "en":("Status", "Effective", "Updated", "Source"),
    }[language]
    status_value = legal[7]
    if language == "zh_hant":
        status_value = {"有效":"有效", "已修改":"已修改", "已废止":"已廢止", "尚未生效":"尚未生效",
                        "现行状态须核对":"現行狀態須核對"}.get(status_value, status_value)
    elif language == "en":
        status_value = {"有效":"effective", "已修改":"amended", "已废止":"repealed", "尚未生效":"not yet effective",
                        "现行状态须核对":"verify current status", "現行":"current"}.get(status_value, status_value)
    date_parts = []
    if status_value: date_parts.append(f"{labels[0]}: {status_value}")
    if legal[8]: date_parts.append(f"{labels[1]}: {legal[8]}")
    if legal[9]: date_parts.append(f"{labels[2]}: {legal[9]}")
    meta = "；".join(date_parts)
    regional_note = ""
    if local_region:
        regional_note = {"zh":f" 注意：你提到{local_region}，这里是全国性法律；当地法规和实施口径仍需另查。",
                         "zh_hant":f" 注意：你提到{local_region}，這裡是全國性法律；當地法規和實施口徑仍需另查。",
                         "en":f" Note: you mentioned {local_region}; this is national law, and local rules still need checking."}[language]
    elif query_jurisdiction == "US-FED":
        regional_note = {"zh":" 注意：这是联邦法资料，州法和地方条例可能规定不同或更高标准。",
                         "zh_hant":" 注意：這是聯邦法資料，州法和地方條例可能規定不同或更高標準。",
                         "en":" Note: this is federal material; state and local law may differ or impose higher standards."}[language]
    disclaimer = {"zh":"仅供一般法律信息参考，不构成针对个案的法律意见。",
                  "zh_hant":"僅供一般法律資訊參考，不構成針對個案的法律意見。",
                  "en":"General legal information only; this is not legal advice for a specific matter."}[language]
    opening, closing, separator = ("(", ")", ": ") if language == "en" else ("（", "）", "：")
    jurisdiction_names = {
        "CN":{"zh":"中国大陆", "zh_hant":"中國大陸", "en":"Mainland China"},
        "US-FED":{"zh":"美国联邦", "zh_hant":"美國聯邦", "en":"United States — federal"},
        "TW":{"zh":"中国台湾地区", "zh_hant":"中國臺灣地區", "en":"Taiwan"},
    }
    area_label = {"zh":"适用地区", "zh_hant":"適用地區", "en":"Jurisdiction"}[language]
    area_name = jurisdiction_names.get(legal[1], {}).get(language, legal[1])
    return (f"{area_label}: {area_name}\n"
            f"{title}{opening}{meta}{closing}{separator}{summary}{regional_note}\n"
            f"{labels[3]}: {legal[15]} — {legal[14]}\n{disclaimer}")

def _find_history_record(connection, query, language, allow_fuzzy=True):
    """优先匹配问题中完整出现的标题/别名，必要时再匹配相近片段。"""
    normalized = query.lower().strip()
    asks_about_person = bool(re.search(
        r"是谁|是誰|誰是|是什么人|是什麼人|\bwho (?:is|was)\b", normalized, re.I))
    event = connection.execute("""SELECT e.* FROM history_terms AS t
        JOIN history_events AS e ON e.event_id=t.event_id
        LEFT JOIN history_metadata AS m ON m.event_id=e.event_id
        WHERE t.language=? AND length(t.term)>=2
          AND t.term NOT GLOB '[0-9]*' AND instr(?, lower(t.term))>0
        ORDER BY CASE WHEN ? AND m.record_type='person' THEN 1 ELSE 0 END DESC,
                 length(t.term) DESC, coalesce(m.importance,0) DESC LIMIT 1""",
        (language, normalized, asks_about_person)).fetchone()
    if event is not None or not allow_fuzzy:
        return event
    pieces = (re.findall(r"[a-z0-9]{3,}", normalized) if language == "en" else
              [normalized[i:i+2] for i in range(max(0, len(normalized)-1))])
    scores = {}
    for piece in pieces:
        for event_id, term in connection.execute(
                "SELECT event_id,term FROM history_terms WHERE language=? AND instr(lower(term),?)>0 LIMIT 20",
                (language, piece)):
            scores[event_id] = scores.get(event_id, 0) + max(1, len(piece))
    if scores:
        best_id, best_score = max(scores.items(), key=lambda item:item[1])
        if best_score >= (3 if language == "en" else 4):
            return connection.execute("SELECT * FROM history_events WHERE event_id=?", (best_id,)).fetchone()
    return None

def answer_history(query, language, require_known_term=False, return_subject=False):
    """回答历史问题；可同时返回实体的三语规范主题用于后续跨语言追问。"""
    if not DATABASE_PATH.exists(): build_knowledge_assets()
    connection = sqlite3.connect(DATABASE_PATH)
    event = _find_history_record(connection, query, language, allow_fuzzy=not require_known_term)
    if event is None and require_known_term:
        connection.close()
        return None
    if event is None:
        title_column = {"zh":"title_zh", "zh_hant":"title_zh_hant", "en":"title_en"}[language]
        rows = connection.execute(f"SELECT {title_column} FROM history_events ORDER BY event_id LIMIT 6").fetchall()
        connection.close()
        titles = "、".join(row[0] for row in rows)
        answer = {"zh":f"请告诉我具体事件。目前知识库可查询：{titles}等。",
                  "zh_hant":f"請告訴我具體事件。目前知識庫可查詢：{titles}等。",
                  "en":f"Please name a specific event. The database includes: {titles}, and others."}[language]
        return (answer, "") if return_subject else answer
    source = connection.execute("SELECT source_url,license,imported_at FROM history_sources WHERE event_id=?",
                                (event[0],)).fetchone()
    connection.close()
    title_index, description_index = {"zh":(2,5), "zh_hant":(3,6), "en":(4,7)}[language]
    display_date = event[1]
    if language == "en":
        display_date = re.sub(r"公元前(\d+)年", r"\1 BCE", display_date).replace("年", "")
    elif language == "zh_hant":
        display_date = display_date.replace("约", "約").replace("世纪", "世紀")
    source_text = (f"Wikidata — {source[0]}；revision/import {source[2]}；confidence 85%"
                   if source else "LocalAI curated history seed；revision 2026-09；confidence 95%")
    label = {"zh":"本地知识库来源", "zh_hant":"本地知識庫來源", "en":"Local knowledge source"}[language]
    answer = (f"{event[title_index]}（{display_date}）：{event[description_index]}\n"
              f"{label}：{source_text}" + conflict_note(DATABASE_PATH,event[0],language))
    subject = " / ".join(dict.fromkeys(title for title in event[2:5] if title))
    return (answer, subject) if return_subject else answer

def answer_character(query, language):
    stop = set("这个這個字是不是什麼么啥意思含义義怎么怎麼读讀念请請问問解释解釋一下生僻的哪能否姓氏作为作為普通话話发音發音")
    candidates = []
    for char in re.findall(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]", query):
        if char not in stop and char not in candidates: candidates.append(char)
    if not candidates:
        return {"zh":"请把想查询的汉字发给我，例如：仉怎么读？",
                "zh_hant":"請把想查詢的漢字發給我，例如：仉怎麼讀？",
                "en":"Please include the character, for example: What does 龘 mean?"}[language]
    connection = sqlite3.connect(DATABASE_PATH)
    results = []
    for char in candidates[:3]:
        row = connection.execute("SELECT * FROM character_knowledge WHERE character=?", (char,)).fetchone()
        if row: results.append(row)
    connection.close()
    if not results:
        chars = "、".join(candidates[:3])
        return {"zh":f"我能正确显示“{chars}”，但汉字知识库暂时没有它的可靠释义。",
                "zh_hant":f"我能正確顯示「{chars}」，但漢字知識庫暫時沒有它的可靠釋義。",
                "en":f"I can represent {chars} correctly, but my character database has no reliable definition for it yet."}[language]
    answers = []
    for char, codepoint, mandarin, meaning_zh, meaning_hant, definition_en, is_surname, source in results:
        if language == "en":
            meaning = definition_en or meaning_zh or "No definition is available"
            meaning = meaning[0].upper() + meaning[1:] if meaning else meaning
            punctuation = "" if meaning.endswith((".", "!", "?")) else "."
            answers.append(f"{char} ({codepoint}) is read {mandarin or 'unknown'} in Mandarin. {meaning}{punctuation}")
        else:
            meaning = (meaning_hant if language == "zh_hant" else meaning_zh)
            if not meaning:
                meaning = f"Unihan英文释义：{definition_en}" if language == "zh" else f"Unihan英文釋義：{definition_en}"
            pronunciation = ((f"普通话读作 {mandarin}" if language == "zh" else f"普通話讀作 {mandarin}")
                               if mandarin else ("读音暂缺" if language == "zh" else "讀音暫缺"))
            answers.append(f"“{char}”（{codepoint}），{pronunciation}。{meaning}")
    return "\n".join(answers)

def answer_english_word(query, language):
    stop_words = {"what","does","mean","meaning","define","definition","how","do","you","pronounce",
                  "pronunciation","word","is","the","of","please","tell","me","english","explain","use","in",
                  "a","an","sentence","example","with","give","i"}
    words = [word.lower() for word in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", query)]
    targets = [word for word in words if word not in stop_words and len(word) <= 12]
    if not targets:
        return {"zh":"请给出一个不超过12个字母的英语单词，例如：apple是什么意思？",
                "zh_hant":"請給出一個不超過12個字母的英語單詞，例如：apple是什麼意思？",
                "en":"Please provide an English word of no more than 12 letters, such as: What does apple mean?"}[language]
    connection = sqlite3.connect(DATABASE_PATH)
    row = connection.execute("SELECT * FROM english_words WHERE word=?", (targets[-1],)).fetchone()
    usages = connection.execute("""SELECT example_en,example_zh FROM english_word_usage
        WHERE word=? ORDER BY CASE source WHEN 'curated' THEN 0 ELSE 1 END LIMIT 2""", (targets[-1],)).fetchall()
    connection.close()
    if not row:
        return {"zh":f"英语短词数据库里暂时没有“{targets[-1]}”。",
                "zh_hant":f"英語短詞資料庫裡暫時沒有「{targets[-1]}」。",
                "en":f"The short-word database does not contain “{targets[-1]}” yet."}[language]
    word, phonetic, definition, translation, part_of_speech, tags, frequency, source = row
    phonetic_text = f" /{phonetic}/" if phonetic else ""
    if language == "en":
        meanings = "; ".join(part.strip() for part in definition.replace("\\n", "\n").splitlines()[:3] if part.strip())
        if not meanings: meanings = "An English definition is not available, but the Chinese entry is: " + translation
        usage_text = " ".join(f"Example: {example}" for example, _ in usages)
        if not usage_text: usage_text = "No verified usage example is stored yet."
        return f"{word}{phonetic_text}: {meanings[:500]}\n{usage_text}"
    meanings = "；".join(part.strip() for part in translation.replace("\\n", "\n").splitlines()[:4] if part.strip())
    prefix = "词义" if language == "zh" else "詞義"
    if usages:
        example, example_zh = usages[0]
        usage_label = "例句" if language == "zh" else "例句"
        usage_text = f"\n{usage_label}：{example}" + (f"（{example_zh}）" if example_zh else "")
    else:
        usage_text = "\n暂无经过验证的例句。" if language == "zh" else "\n暫無經過驗證的例句。"
    return f"{word}{phonetic_text}：{prefix}：{meanings[:500]}{usage_text}"

def encode(text: str) -> list[int]:
    """覆盖全部简繁中文、英文及其他 Unicode 的 UTF-8 tokenizer。"""
    return list(text.encode("utf-8"))

def decode(tokens: list[int]) -> str:
    return bytes(tokens).decode("utf-8", errors="replace")

def _bucket(chunk: bytes, bucket_count=NGRAM_BUCKETS) -> int:
    return int.from_bytes(hashlib.blake2s(chunk, digest_size=4).digest(), "little") % bucket_count

def sparse_vectorize(text: str, feature_size=FEATURE_SIZE):
    """返回非零特征下标和值；feature_size 来自模型本身，兼容旧版备份。"""
    if feature_size <= VOCAB_SIZE:
        raise ValueError("模型特征维度无效")
    normalized = normalize_text(text)
    raw = bytes(encode(normalized))
    bucket_count = feature_size - VOCAB_SIZE
    features = {}
    def add(index, value): features[index] = features.get(index, 0.0) + value
    for value in raw: add(value, 1.0)
    for n in (2, 3):
        for i in range(len(raw) - n + 1):
            add(VOCAB_SIZE + _bucket(raw[i:i+n], bucket_count), 1.5 if n == 3 else 1.0)
    # Unicode 字符 n-gram 能学习“你能/能做/几点”等相近句式；字节特征仍保证全字符覆盖。
    chars = list(normalized)
    for n in (2, 3):
        for i in range(len(chars) - n + 1):
            chunk = (f"c{n}:" + "".join(chars[i:i+n])).encode("utf-8")
            add(VOCAB_SIZE + _bucket(chunk, bucket_count), 2.0 if n == 2 else 2.5)
    if not features:
        return np.empty(0,dtype=np.int32),np.empty(0,dtype=np.float64)
    indices=np.fromiter(sorted(features),dtype=np.int32)
    values=np.asarray([features[int(index)] for index in indices],dtype=np.float64)
    norm=np.linalg.norm(values)
    if norm: values/=norm
    return indices,values

def vectorize(text: str, feature_size=FEATURE_SIZE) -> np.ndarray:
    """兼容调用方的稠密视图；推理和训练内部均使用 sparse_vectorize。"""
    indices,values=sparse_vectorize(text,feature_size)
    result=np.zeros(feature_size,dtype=np.float64);result[indices]=values
    return result

class TinyChatModel:
    def __init__(self, labels, weights, bias):
        self.labels, self.weights, self.bias = labels, weights, bias

    @classmethod
    def create(cls):
        labels = list(TRAINING_DATA)
        # 未使用的扩展桶保持零；实际语料出现的行会在 train() 中参与优化。
        w = np.zeros((FEATURE_SIZE, len(labels)),dtype=np.float64)
        return cls(labels, w, np.zeros(len(labels), dtype=np.float64))

    def probabilities(self, text):
        indices,values=sparse_vectorize(text,self.weights.shape[0])
        logits = values @ self.weights[indices] + self.bias
        values = np.exp(logits - logits.max())
        return values / values.sum()

    def predict(self, text):
        probs = self.probabilities(text); index = int(np.argmax(probs))
        return self.labels[index], float(probs[index])

    def save(self, path=MODEL_PATH):
        # v2 故意使用未压缩 NPZ：零值参数仍代表可继续训练的真实容量，文件约 30 MB。
        np.savez(path, weights=self.weights, bias=self.bias, labels=np.asarray(self.labels),
                 version=np.asarray([MODEL_FORMAT_VERSION]),feature_size=np.asarray([self.weights.shape[0]]),
                 tokenizer=np.asarray(["utf8-byte-char-ngram-v2"]))

    @classmethod
    def load(cls, path=MODEL_PATH):
        if not path.exists(): raise FileNotFoundError("未找到模型，请先运行 train")
        with np.load(path, allow_pickle=False) as data:
            weights=data["weights"].copy();labels=data["labels"].tolist();bias=data["bias"].copy()
        if weights.ndim!=2 or weights.shape[1]!=len(labels) or bias.shape!=(len(labels),):
            raise ValueError("模型结构或标签维度损坏")
        return cls(labels,weights,bias)

def _file_record(path):
    path = Path(path)
    if not path.exists(): return {"path":str(path), "exists":False}
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""): digest.update(chunk)
    return {"path":str(path), "exists":True, "bytes":path.stat().st_size,
            "sha256":digest.hexdigest(), "mtime":datetime.fromtimestamp(path.stat().st_mtime).isoformat()}

def _write_training_manifest(run_directory, manifest):
    path = Path(run_directory) / "manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)

def begin_training_archive(epochs, learning_rate):
    """在训练开始前归档当前模型，并记录所有可复现输入的校验值。"""
    MODEL_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    run_directory = MODEL_BACKUP_ROOT / base
    run_directory.mkdir()
    artifacts = {}
    training_source = run_directory / "training_source.py"
    shutil.copy2(Path(__file__), training_source)
    artifacts["training_source"] = _file_record(training_source)
    if REQUIREMENTS_PATH.exists():
        requirements_snapshot = run_directory / "requirements.txt"
        shutil.copy2(REQUIREMENTS_PATH, requirements_snapshot)
        artifacts["requirements"] = _file_record(requirements_snapshot)
    if MODEL_PATH.exists():
        before = run_directory / "model_before.npz"
        shutil.copy2(MODEL_PATH, before)
        artifacts["model_before"] = _file_record(before)
    manifest = {
        "schema_version":1, "run_id":base, "status":"training", "started_at":datetime.now().astimezone().isoformat(),
        "python":sys.version, "numpy":np.__version__,
        "parameters":{"epochs":epochs,"learning_rate":learning_rate,"seed":SEED,
                      "model_format_version":MODEL_FORMAT_VERSION,"feature_size":FEATURE_SIZE,
                      "labels":len(TRAINING_DATA),"parameter_count":FEATURE_SIZE*len(TRAINING_DATA)+len(TRAINING_DATA),
                      "target_model_bytes":[MODEL_TARGET_MIN_BYTES,MODEL_TARGET_MAX_BYTES],
                      "domestic_training_rows":sum(map(len,DOMESTIC_TRAINING_DATA.values()))},
        "inputs":{"source":_file_record(Path(__file__)), "requirements":_file_record(REQUIREMENTS_PATH),
                  "training_document":_file_record(TRAINING_DOCUMENT_PATH),
                  "knowledge_database":_file_record(DATABASE_PATH)}, "artifacts":artifacts,
    }
    _write_training_manifest(run_directory, manifest)
    return run_directory, manifest

def finish_training_archive(run_directory, manifest, status, error=""):
    manifest["status"] = status
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    if error: manifest["error"] = error
    if MODEL_PATH.exists() and status == "complete":
        after = Path(run_directory) / "model_after.npz"
        shutil.copy2(MODEL_PATH, after)
        manifest["artifacts"]["model_after"] = _file_record(after)
        manifest["active_model"] = _file_record(MODEL_PATH)
    _write_training_manifest(run_directory, manifest)

def list_training_archives():
    if not MODEL_BACKUP_ROOT.exists():
        print("尚无模型训练备案。")
        return
    runs = sorted((path for path in MODEL_BACKUP_ROOT.iterdir() if path.is_dir()), reverse=True)
    if not runs:
        print("尚无模型训练备案。")
        return
    for run in runs:
        try: data = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): data = {"status":"manifest-invalid"}
        print(f"{run.name}  {data.get('status','unknown')}  {run}")

def training_samples():
    """加入礼貌词、语气词和标点变体，降低模型对固定原句的依赖。"""
    samples = []
    question_intents = {"identity", "ability", "wellbeing", "time", "language", "character", "legal"}
    for label, base_rows in TRAINING_DATA.items():
        rows=list(base_rows)+DOMESTIC_TRAINING_DATA.get(label,[])
        for sentence in rows:
            variants = {sentence, sentence + "?", sentence + "！"}
            has_chinese = bool(re.search(r"[\u3400-\u9fff]", sentence))
            if label in question_intents:
                if has_chinese:
                    variants.update({"请问" + sentence, "麻烦" + sentence, sentence + "呢"})
                else:
                    variants.update({"please " + sentence, sentence + " please"})
            samples.extend((variant, label) for variant in variants)
    return samples

def train(epochs=500, learning_rate=.08):
    # 先确定性生成本次训练文档，再创建备案；模型参数尚未开始更新。
    build_knowledge_assets()
    run_directory, manifest = begin_training_archive(epochs, learning_rate)
    print(f"训练备案目录：{run_directory}")
    try:
        model = TinyChatModel.create()
        samples = training_samples()
        sparse_samples=[sparse_vectorize(sentence) for sentence,_ in samples]
        active_indices=np.unique(np.concatenate([indices for indices,_ in sparse_samples]))
        active_lookup=np.full(FEATURE_SIZE,-1,dtype=np.int32)
        active_lookup[active_indices]=np.arange(len(active_indices),dtype=np.int32)
        x=np.zeros((len(samples),len(active_indices)),dtype=np.float64)
        for row,(indices,values) in enumerate(sparse_samples):x[row,active_lookup[indices]]=values
        active_weights=model.weights[active_indices].copy()
        targets = np.asarray([model.labels.index(label) for _, label in samples])
        y = np.eye(len(model.labels), dtype=np.float64)[targets]
        mw=np.zeros_like(active_weights); vw=np.zeros_like(active_weights)
        mb=np.zeros_like(model.bias); vb=np.zeros_like(model.bias)
        for epoch in range(1, epochs + 1):
            logits=x@active_weights+model.bias; logits-=logits.max(1, keepdims=True)
            probs=np.exp(logits); probs/=probs.sum(1, keepdims=True)
            grad=(probs-y)/len(x); gw=x.T@grad+1e-5*active_weights; gb=grad.sum(0)
            mw=.9*mw+.1*gw; vw=.999*vw+.001*gw*gw
            mb=.9*mb+.1*gb; vb=.999*vb+.001*gb*gb
            active_weights -= learning_rate*(mw/(1-.9**epoch))/(np.sqrt(vw/(1-.999**epoch))+1e-8)
            model.bias -= learning_rate*(mb/(1-.9**epoch))/(np.sqrt(vb/(1-.999**epoch))+1e-8)
            if epoch == 1 or epoch % 100 == 0:
                loss=-np.log(probs[np.arange(len(x)), targets]+1e-9).mean()
                accuracy=(probs.argmax(1)==targets).mean()
                print(f"Epoch {epoch:4d}/{epochs} | loss={loss:.4f} | accuracy={accuracy:.1%}")
        model.weights[active_indices]=active_weights
        candidate = run_directory / "model_candidate.npz"
        model.save(candidate)
        if not MODEL_TARGET_MIN_BYTES <= candidate.stat().st_size <= MODEL_TARGET_MAX_BYTES:
            raise ValueError(f"候选模型大小 {candidate.stat().st_size} bytes 超出 25–35 MB 目标")
        verified = TinyChatModel.load(candidate)
        if verified.weights.shape != model.weights.shape or verified.labels != model.labels:
            raise ValueError("候选模型回读校验失败")
        os.replace(candidate, MODEL_PATH)
        finish_training_archive(run_directory, manifest, "complete")
        print(f"模型已保存：{MODEL_PATH}（{MODEL_PATH.stat().st_size/1024:.1f} KiB）；备案已完成")
        return model
    except Exception as error:
        finish_training_archive(run_directory, manifest, "failed", f"{type(error).__name__}: {error}")
        raise

def detect_language(text):
    latin_count = len(re.findall(r"[A-Za-z]", text))
    han_count = len(re.findall(r"[\u3400-\u9fff]", text))
    if latin_count and (not han_count or latin_count > han_count * 3): return "en"
    # 常见繁体专用字形；不仅依赖“誰/麼”等问句词，也覆盖专名中的“崙/臺”等。
    markers=set("體會說這為麼嗎誰請謝見時點現與簡繁國語識歡運機器幫過還難開聽錯問風門電進颱"
                "紹崙灣臺萬東絲兩嚴個豐臨麗舉義烏樂喬習鄉書買亂爭於亞產親億僅從倉"
                "儀們價眾優傳傷倫偽餘俠侶偵側僑儉儲兒兌黨蘭關興養獸內冊寫軍農馮沖"
                "決況凍淨準幾鳳凱別刪劉則剛創劃劇勸辦務動勵勞勢勳區醫華協單賣盧衛卻"
                "廠廳曆厲壓厭廁廂廈廚廢廣莊慶廬庫應廟龐")
    markers.discard("器")  # “器”是简繁共用字，不能单独作为繁体判断依据。
    return "zh_hant" if any(c in markers for c in text) else "zh"

def looks_like_math(text):
    """用结构特征辅助模型确认数学意图，避免相近表达被低置信度过滤。"""
    value = text.lower().strip()
    has_number = bool(re.search(r"\d", value))
    has_syntax = bool(re.search(r"[+\-*/×÷^=()]|\b(?:sqrt|sin|cos|tan)\b|[°√]", value))
    has_words = bool(re.search(r"解方程|計算|计算|等于多少|等於多少|solve|calculate|evaluate", value))
    return (has_number and has_syntax) or has_words

def looks_like_legal(text):
    value = text.lower()
    return bool(re.search(
        r"法律|法规|法規|法典|宪法|憲法|民法|刑法|劳动合同|勞動合同|劳动法|勞動法|"
        r"著作权|著作權|版权|版權|合同法|契约法|契約法|诉讼|訴訟|起诉|起訴|合法|违法|違法|司法辖区|司法管轄|"
        r"第[一二三四五六七八九十百千0-9]+条|第[一二三四五六七八九十百千0-9]+條|"
        r"\b(?:law|legal|statute|civil code|criminal code|constitution|copyright|fair use|"
        r"minimum wage|contract law|lawsuit|jurisdiction|u\.?s\.?c\.?)\b", value, re.I))

def looks_like_history(text):
    value = text.lower()
    return bool(re.search(r"历史|歷史|战役|戰役|战争|戰爭|世界大战|世界大戰|一战|一戰|二战|二戰|革命|起义|起義|王朝|朝代|运动|運動|条约|條約|"
                          r"\b(?:history|historical|battle|war|revolution|uprising|dynasty|movement|treaty)\b", value))

def looks_like_historical_entity_question(text):
    """识别人物/实体问法；是否真是历史对象随后由 SQLite 别名严格确认。"""
    return bool(re.search(r"是谁|是誰|誰是|是什么人|是什麼人|介绍(?:一下)?|介紹(?:一下)?|讲讲|講講|"
                          r"生活在.*年代|生于|生於|卒于|卒於|"
                          r"\b(?:who (?:is|was)|tell me about|when did .+ live)\b", text, re.I))

def looks_like_character_question(text):
    stop = set("这个這個字是不是什麼么啥意思含义義怎么怎麼读讀念请請问問解释解釋一下生僻的哪能否姓氏作为作為普通话話发音發音")
    target_han = any(char not in stop for char in re.findall(
        r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]", text))
    return target_han and bool(re.search(r"字.*(?:意思|含义|含義|怎么读|怎麼讀|怎么念|怎麼念|姓氏)|"
                          r"(?:意思|含义|含義|怎么读|怎麼讀|怎么念|怎麼念|姓氏).*字|"
                          r"是什么意思|是什麼意思|表示什么|表示什麼|怎么读|怎麼讀|怎么念|怎麼念|念啥|念什么|念什麼|咋读|咋讀|作为姓氏|作為姓氏|能当姓|能當姓|是不是姓|是否为姓|是否為姓|能不能作[为為]姓氏|能否作[为為]姓氏|普通话.*读音|普通話.*讀音|"
                          r"\b(?:what does|mean|character|pronounce|pronunciation|surname)\b", text, re.I))

def looks_like_english_word_question(text):
    stop_words = {"what","does","mean","meaning","define","definition","how","do","you","pronounce",
                  "pronunciation","word","is","the","of","please","tell","me","english","explain","use","in",
                  "a","an","sentence","example","with","give","i"}
    has_word = any(word.casefold() not in stop_words and len(word) <= 12
                   for word in re.findall(r"[A-Za-z]+", text))
    standard = re.search(r"单词|單詞|英文词|英文詞|是什么意思|是什麼意思|啥意思|怎么读|怎麼讀|咋念|怎么用|怎麼用|咋用|造句|造(?:个|個).*句子|例句|用法|"
                         r"\b(?:what does|meaning of|define|pronounce|pronunciation|how do i use|use .+ in a sentence|example with)\b", text, re.I)
    explain_one_word = re.search(r"^\s*explain(?:\s+the\s+word)?\s+[a-z]{1,12}\s*[?.!]*\s*$", text, re.I)
    return has_word and bool(standard or explain_one_word)

def _to_decimal(value):
    if isinstance(value, Decimal): return value
    if isinstance(value, Fraction): return Decimal(value.numerator) / Decimal(value.denominator)
    return Decimal(value)

def _binary(left, right, op):
    if op is ast.Pow:
        if isinstance(right, Fraction) and right.denominator == 1: exponent = right.numerator
        elif isinstance(right, int): exponent = right
        else: raise ValueError("指数必须是整数")
        if abs(exponent) > 100000: raise ValueError("指数过大（最大 100000）")
        return left ** exponent
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        left, right = _to_decimal(left), _to_decimal(right)
    if op is ast.Add: return left + right
    if op is ast.Sub: return left - right
    if op is ast.Mult: return left * right
    if op is ast.Div: return left / right
    if op is ast.FloorDiv: return left // right
    if op is ast.Mod: return left % right
    raise ValueError("不支持这个运算符")

def _eval_math_node(node, variables=None):
    variables = variables or {}
    if isinstance(node, ast.Expression): return _eval_math_node(node.body, variables)
    if isinstance(node, ast.Constant) and isinstance(node.value, int): return Fraction(node.value)
    if isinstance(node, ast.Name) and node.id in variables: return variables[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_math_node(node.operand, variables)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}:
        return _binary(_eval_math_node(node.left, variables), _eval_math_node(node.right, variables), type(node.op))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "D" and len(node.args) == 1 and isinstance(node.args[0], ast.Constant):
            return Decimal(node.args[0].value)
        args = [_eval_math_node(arg, variables) for arg in node.args]
        if len(args) != 1: raise ValueError("函数只接受一个参数")
        value = args[0]
        if node.func.id == "sqrt":
            decimal_value = _to_decimal(value)
            if decimal_value < 0: raise ValueError("实数范围内不能对负数开平方")
            return decimal_value.sqrt()
        if node.func.id in {"sin", "cos", "tan", "sin_deg", "cos_deg", "tan_deg"}:
            number = float(_to_decimal(value)); name = node.func.id
            if name.endswith("_deg"):
                exact = {("sin_deg", 0.0): "0", ("sin_deg", 30.0): "0.5", ("sin_deg", 90.0): "1",
                         ("cos_deg", 0.0): "1", ("cos_deg", 60.0): "0.5", ("cos_deg", 90.0): "0",
                         ("tan_deg", 0.0): "0", ("tan_deg", 45.0): "1"}
                if (name, number % 360) in exact: return Decimal(exact[(name, number % 360)])
                number, name = math.radians(number), name[:-4]
            return Decimal(str(getattr(math, name)(number)))
        if node.func.id == "abs": return abs(value)
    raise ValueError("表达式含有不支持的内容")

def _prepare_expression(text):
    value = normalize_text(text).replace("×", "*").replace("÷", "/").replace("−", "-").replace("^", "**")
    value = re.sub(r"^(?:请|請)?(?:帮我|幫我|帮忙|幫忙)?(?:瞅瞅|看下|看看|计算|計算|求值|算(?:一下|一算|下)?|calculate|evaluate)\s*[:：]?\s*", "", value)
    value = re.sub(r"(sin|cos|tan)\s*\(\s*([^()]*)\s*°\s*\)", r"\1_deg(\2)", value)
    # 保留小数字面量的十进制精度，避免先变成二进制 float。
    value = re.sub(r"(?<![\w.])(\d+\.\d+(?:[eE][+\-]?\d+)?)(?![\w.])", r"D('\1')", value)
    return value

def evaluate_expression(expression, variables=None):
    tree = ast.parse(_prepare_expression(expression), mode="eval")
    with localcontext() as context:
        context.prec = 50
        return _eval_math_node(tree, variables)

def solve_equation(text):
    equation = re.sub(r"^(?:请|請)?(?:解方程|求解|solve(?:\s+the)?(?:\s+equation)?)\s*[:：]?\s*", "", text.strip(), flags=re.I)
    if equation.count("=") != 1: raise ValueError("方程必须包含一个等号")
    left, right = equation.split("=", 1)
    def difference(x): return _binary(evaluate_expression(left, {"x": x}), evaluate_expression(right, {"x": x}), ast.Sub)
    f0, f1, f2 = difference(Fraction(0)), difference(Fraction(1)), difference(Fraction(2))
    slope = _binary(f1, f0, ast.Sub)
    if slope == 0: raise ValueError("方程没有唯一解")
    if _binary(f2, f1, ast.Sub) != slope: raise ValueError("目前只支持一元一次方程")
    return _binary(-f0, slope, ast.Div)

def _integer_digits(value):
    """返回整数的完整十进制文本；非整数返回 None。"""
    if isinstance(value, Fraction) and value.denominator == 1: value = value.numerator
    if not isinstance(value, int): return None
    if hasattr(sys, "set_int_max_str_digits"): sys.set_int_max_str_digits(1000000)
    return str(value)

def format_number(value):
    if isinstance(value, Fraction):
        if value.denominator != 1: return f"{value.numerator}/{value.denominator}（≈ {float(value):.12g}）"
        value = value.numerator
    if isinstance(value, int):
        digits = _integer_digits(value)
        if len(digits) > 500:
            sign = "-" if digits.startswith("-") else ""; body = digits.lstrip("-")
            return f"{sign}{body[:40]}…{body[-40:]}（共 {len(body)} 位）"
        return digits
    if isinstance(value, Decimal):
        rendered = format(value, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return str(value)

def calculate(text):
    is_equation = "=" in text or bool(re.search(r"解方程|求解|\bsolve\b", text, re.I))
    value = solve_equation(text) if is_equation else evaluate_expression(text)
    return ("x = " if is_equation else "") + format_number(value)

def calculate_for_chat(text, language, output_directory=None):
    """计算并输出；超过 500 位的完整整数写入桌面文档。"""
    is_equation = "=" in text or bool(re.search(r"解方程|求解|\bsolve\b", text, re.I))
    value = solve_equation(text) if is_equation else evaluate_expression(text)
    digits = _integer_digits(value)
    digit_count = len(digits.lstrip("-")) if digits is not None else 0
    if digits is not None and digit_count > 500:
        destination = Path(output_directory) if output_directory else Path.home() / "Desktop"
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = destination / f"LocalAI_数学计算结果_{stamp}.txt"
        document = ("LocalAI 数学计算结果\n"
                    f"表达式：{text}\n"
                    f"结果位数：{digit_count}\n\n"
                    f"完整结果：\n{digits}\n")
        path.write_text(document, encoding="utf-8")
        messages = {
            "zh": f"结果是一个 {digit_count} 位整数，完整结果已保存到桌面文档：{path}",
            "zh_hant": f"結果是一個 {digit_count} 位整數，完整結果已儲存到桌面文件：{path}",
            "en": f"The result is a {digit_count}-digit integer. The complete result was saved to: {path}",
        }
        return messages[language]
    prefix = "x = " if is_equation else ""
    return {"zh":f"计算结果：{prefix}{format_number(value)}",
            "zh_hant":f"計算結果：{prefix}{format_number(value)}",
            "en":f"Result: {prefix}{format_number(value)}"}[language]

def reply(model, text, memory=None, file_context=None, allow_network=True):
    if not text.strip():
        return "请输入内容后再发送。", "unknown", 0.0
    original_text = text
    text = memory.resolve(text) if memory else text
    intent, confidence = model.predict(text); language = detect_language(original_text)

    def finish(answer, final_intent, subject=None):
        if memory:
            if subject is None:
                subject = original_text if final_intent in {"history","encyclopedia"} else ""
            memory.remember(original_text, answer, final_intent, subject)
        return answer, final_intent, confidence

    if file_context and file_context.path:
        file_answer = file_context.answer(original_text, language)
        if file_answer:
            return finish(file_answer, "file_qa")
    utility_answer = answer_utility(original_text, language)
    if utility_answer is not None or looks_like_utility(original_text):
        if utility_answer is None:
            utility_answer = {"zh":"我识别到换算意图，但没有找到完整的数值、源单位和目标单位。",
                              "zh_hant":"我識別到換算意圖，但沒有找到完整的數值、來源單位和目標單位。",
                              "en":"I detected a conversion request but could not find a complete value, source unit, and target unit."}[language]
        return finish(utility_answer, "utility")
    advanced_answer = advanced_math(original_text, language)
    if advanced_answer is not None:
        return finish(advanced_answer, "math")
    if looks_like_math(original_text) or (intent == "math" and confidence >= .55 and re.search(r"\d",original_text)):
        intent = "math"
        try:
            answer = calculate_for_chat(original_text, language)
        except (ValueError, SyntaxError, ZeroDivisionError, InvalidOperation, OverflowError) as error:
            answer = {"zh":f"这个数学表达式暂时无法计算：{error}", "zh_hant":f"這個數學表達式暫時無法計算：{error}",
                      "en":f"I could not calculate that expression: {error}"}[language]
        return finish(answer, intent)
    if (looks_like_legal(original_text) or looks_like_legal_action(original_text)
            or looks_like_life_legal(original_text)
            or (memory and memory.resolved_intent=="legal")):
        return finish(answer_legal(text, language), "legal")
    daily_answer = answer_daily_life(DATABASE_PATH, original_text, language) if looks_like_daily_life(original_text) else None
    if daily_answer is not None:
        return finish(daily_answer, "daily_life")
    # 旧分类器没有 daily_life 标签，未知生活句有时会被猜成 legal；只有出现明确法律
    # 特征时才允许分类器的 legal 结果接管，避免“冰箱坏了”被要求补充司法辖区。
    if intent == "legal" and confidence >= .75 and re.search(
            r"纠纷|爭議|维权|維權|投诉|投訴|赔偿|賠償|仲裁|法院|律师|律師|"
            r"最低工资|最低工資|联邦|聯邦|州法|适用.*法|適用.*法|"
            r"employer|landlord|refund|complaint|compensation|court|lawyer", original_text, re.I):
        return finish(answer_legal(text, language), "legal")
    # 明确的“单词怎么用/汉字怎么读”结构优先于统计分类，防止大模型容量提升后
    # 高置信度基础聊天标签抢走这些确定性数据库查询。
    if looks_like_english_word_question(original_text):
        return finish(answer_english_word(original_text, language), "english_word")
    if looks_like_character_question(original_text):
        return finish(answer_character(original_text, language), "character")
    # 高置信度基础聊天优先于宽泛的“what/介绍/解释”百科问句，避免把“介绍你自己”
    # 或“what time is it”当成百科检索。
    basic_intents={"greeting","farewell","thanks","identity","ability","wellbeing","positive",
                   "negative","time","language","joke","apology","compliment","creator"}
    explicit_basic = None
    normalized_basic = normalize_text(original_text)
    if re.search(r"(?:^|[\s,，.!！?？])(?:你好|您好|嗨|哈喽|哈囉|hello|hi|hey)(?:$|[\s,，.!！?？呀啊])", normalized_basic, re.I):
        explicit_basic = "greeting"
    elif re.search(r"\b(?:appreciate|grateful|thank(?:s| you)?)\b|感谢|感謝|谢谢|謝謝|多谢|多謝", normalized_basic, re.I):
        explicit_basic = "thanks"
    elif re.search(r"下线|下線|先走|离开|離開|改天再聊|下次再聊|回头见|回頭見|\b(?:goodbye|bye|see you|signing off)\b", normalized_basic, re.I):
        explicit_basic = "farewell"
    elif re.search(r"不好受|难过|難過|伤心|傷心|心情不好|糟糕|很累|\b(?:sad|upset|awful|feel bad)\b", normalized_basic, re.I):
        explicit_basic = "negative"
    elif re.search(r"顺利|順利|开心|開心|高兴|高興|很棒|很好|不错|不錯|\b(?:great|wonderful|happy)\b", normalized_basic, re.I):
        explicit_basic = "positive"
    if explicit_basic: intent = explicit_basic
    if intent in basic_intents and (confidence >= .55 or explicit_basic):
        if intent=="time":
            now=datetime.now().strftime("%Y-%m-%d %H:%M")
            answer={"zh":f"现在时间是 {now}。","zh_hant":f"現在時間是 {now}。","en":f"The current time is {now}."}[language]
        else: answer=choose_response(language,intent,original_text)
        return finish(answer,intent)
    # 人物/实体问法必须先做严格的本地历史别名匹配。百科库扩充后，宽泛的
    # “who was …”若先走 FTS，可能因正文中的弱关联词误命中非目标百科条目。
    # 只有 SQLite 历史词条完整命中时才接管，因而不会把“你是谁”一类聊天误判为历史。
    if looks_like_historical_entity_question(text):
        history_result = answer_history(text, language, require_known_term=True, return_subject=True)
        if history_result is not None:
            history_answer, history_subject = history_result
            return finish(history_answer, "history", history_subject)
    # “介绍一下地震/细菌”与历史训练句式很像，小分类器可能给出 history。
    # 若句中没有战争、革命、王朝等显式历史特征，先尝试严格的本地百科命中；
    # 此处关闭网络回退，避免未知实体被网页百科提前截获。
    if looks_like_encyclopedia(original_text) and not looks_like_history(text):
        encyclopedia_answer, encyclopedia_id = answer_encyclopedia(
            DATABASE_PATH, original_text, language, allow_network=False)
        if encyclopedia_answer is not None and encyclopedia_id is not None:
            return finish(encyclopedia_answer, "encyclopedia")
    if (intent == "history" and confidence >= .55) or looks_like_history(text):
        intent = "history"
        history_answer, history_subject = answer_history(text, language, return_subject=True)
        return finish(history_answer, intent, history_subject)
    if intent == "encyclopedia" or looks_like_encyclopedia(original_text):
        encyclopedia_answer, _ = answer_encyclopedia(DATABASE_PATH, original_text, language, allow_network)
        if encyclopedia_answer is not None:
            return finish(encyclopedia_answer, "encyclopedia")
        refusal = {"zh":"本地知识库没有找到可靠答案，联网检索也没有获得足够资料；我不猜测。",
                   "zh_hant":"本機知識庫沒有找到可靠答案，聯網檢索也沒有取得足夠資料；我不猜測。",
                   "en":"I found no reliable answer in the local database or web fallback, so I will not guess."}[language]
        return finish(refusal,"unknown")
    # 动态数据库意图若未满足各自路由条件，应安全降级，而不是显示错误标签。
    if intent in {"history", "character", "english_word", "legal", "encyclopedia", "utility"}: intent = "unknown"
    explicit_time = bool(re.search(r"几点|幾點|时间|時間|\btime\b", original_text, re.I))
    if explicit_time:
        intent = "time"
    if (confidence < .35 and not explicit_time) or not text.strip(): intent = "unknown"
    if intent == "time":
        now=datetime.now().strftime("%Y-%m-%d %H:%M")
        answer={"zh":f"现在时间是 {now}。", "zh_hant":f"現在時間是 {now}。", "en":f"The current time is {now}."}[language]
    else: answer=choose_response(language, intent, original_text)
    return finish(answer, intent)

def stream_text_chunks(text, max_token_bytes=24):
    """按本模型真实 byte-token 上限分块，并确保不拆开 Unicode 字符。"""
    if max_token_bytes < 1:
        raise ValueError("max_token_bytes 必须大于 0")
    chunk, chunk_tokens = [], 0
    for character in text:
        character_tokens = len(encode(character))
        if chunk and chunk_tokens + character_tokens > max_token_bytes:
            yield "".join(chunk)
            chunk, chunk_tokens = [], 0
        chunk.append(character)
        chunk_tokens += character_tokens
        # 单个 Unicode 字符可能超过非常小的人工上限；仍完整输出，绝不切坏 UTF-8。
        if chunk_tokens >= max_token_bytes:
            yield "".join(chunk)
            chunk, chunk_tokens = [], 0
    if chunk:
        yield "".join(chunk)

def stream_reply(model, text, memory=None, file_context=None, allow_network=True,
                 chunk_token_limit=24):
    """产生类似 LLM 的增量事件，并在完成事件中返回精确 byte-token 用量。"""
    started = time.perf_counter()
    answer, intent, confidence = reply(model, text, memory, file_context, allow_network)
    prepared = time.perf_counter()
    input_tokens, output_tokens = len(encode(text)), len(encode(answer))
    yield {"type":"response.start", "intent":intent, "confidence":confidence,
           "usage":{"input_tokens":input_tokens}}
    first_delta_at = None
    chunk_count = 0
    for chunk_count, delta in enumerate(stream_text_chunks(answer, chunk_token_limit), 1):
        if first_delta_at is None: first_delta_at = time.perf_counter()
        yield {"type":"response.output_text.delta", "index":chunk_count - 1,
               "delta":delta, "token_count":len(encode(delta))}
    finished = time.perf_counter()
    if first_delta_at is None: first_delta_at = finished
    elapsed = max(finished - started, 1e-12)
    yield {"type":"response.completed", "intent":intent, "confidence":confidence,
           "usage":{"input_tokens":input_tokens, "output_tokens":output_tokens,
                    "total_tokens":input_tokens + output_tokens},
           "metrics":{"time_to_first_token_ms":(first_delta_at-started)*1000,
                      "preparation_ms":(prepared-started)*1000,
                      "total_ms":elapsed*1000,
                      "output_tokens_per_second":output_tokens/elapsed,
                      "chunks":chunk_count}}

def run_tests(model):
    # 这些改写句不在训练数据中，用于检查相近语法，而不是检查机械记忆。
    cases={"哈喽啊，很高兴见到你":"greeting", "請跟我打個招呼":"greeting", "hello there":"greeting",
           "能告诉我该怎么称呼你吗":"identity", "可否介紹一下你自己":"identity",
           "请问你都能帮我干什么呀":"ability", "你可以協助我做哪些事呢":"ability",
           "could you explain what you can help with":"ability", "thanks a lot":"thanks",
           "麻烦报一下现在的时间":"time", "請問此刻是幾點鐘":"time",
           "could you tell me what time it is":"time", "see you soon":"farewell",
           "我今天有点难过":"negative", "I am doing really great today":"positive",
           "你能看懂繁體字和英文不":"language"}
    ok=True; print("\n自测结果：")
    empty_answer, empty_intent, _ = reply(model, "   ")
    empty_passed = empty_intent == "unknown" and "请输入" in empty_answer; ok &= empty_passed
    print(f"  {'✓' if empty_passed else '✗'} 空输入安全处理")
    stream_events=list(stream_reply(model,"你好",allow_network=False,chunk_token_limit=7))
    streamed="".join(event["delta"] for event in stream_events if event["type"]=="response.output_text.delta")
    stream_done=stream_events[-1]; expected_answer=reply(model,"你好",allow_network=False)[0]
    stream_passed=(streamed==expected_answer and stream_done["type"]=="response.completed" and
                   stream_done["usage"]["input_tokens"]==len(encode("你好")) and
                   stream_done["usage"]["output_tokens"]==len(encode(streamed)) and
                   stream_done["usage"]["total_tokens"]==len(encode("你好"))+len(encode(streamed)))
    ok &= stream_passed
    print(f"  {'✓' if stream_passed else '✗'} 流式 delta 无损重组与真实 byte-token 计数")
    for prompt, expected in cases.items():
        _, actual, confidence=reply(model, prompt); passed=actual==expected; ok &= passed
        print(f"  {'✓' if passed else '✗'} {prompt!r}: {actual} ({confidence:.1%})，期望 {expected}")
    sample="你好，世界！繁體中文：龍龜。English: Hello! 😀"
    passed=decode(encode(sample))==sample; ok &= passed
    print(f"  {'✓' if passed else '✗'} UTF-8 简繁英及 emoji 无损编码")
    cjk_ranges = ((0x3400,0x4DC0), (0x4E00,0xA000), (0xF900,0xFB00),
                  (0x20000,0x2EBF0), (0x30000,0x323B0))
    cjk_text = "".join(chr(codepoint) for start,end in cjk_ranges for codepoint in range(start,end))
    cjk_passed = decode(encode(cjk_text)) == cjk_text; ok &= cjk_passed
    print(f"  {'✓' if cjk_passed else '✗'} {len(cjk_text):,} 个CJK码位无损编码与输出")
    parity_cases = {
        "greeting": ("麻烦向我问好", "麻煩向我問好", "please greet me"),
        "farewell": ("我们下次再聊吧", "我們下次再聊吧", "let us chat again later"),
        "thanks": ("真的很感谢你的帮助", "真的很感謝你的幫助", "I really appreciate your help"),
        "identity": ("能说一下你是谁吗", "能說一下你是誰嗎", "could you tell me who you are"),
        "ability": ("你可以帮我做哪些事", "你可以幫我做哪些事", "which tasks can you help me with"),
        "wellbeing": ("你最近过得好不好", "你最近過得好不好", "have you been doing well"),
        "positive": ("我今天感觉非常好", "我今天感覺非常好", "I feel very good today"),
        "negative": ("今天心情有些糟糕", "今天心情有些糟糕", "I feel a little sad today"),
        "time": ("请告诉我此刻几点", "請告訴我此刻幾點", "could you give me the current time"),
        "language": ("你能使用什么语言", "你能使用什麼語言", "which languages can you use"),
        "math": ("请计算 7/8+1/4", "請計算 7/8+1/4", "calculate 7/8+1/4"),
        "history": ("介绍一下五四运动", "介紹一下五四運動", "tell me about the May Fourth Movement"),
        "joke": ("说个好笑的", "說個好笑的", "tell me something funny"),
        "apology": ("真的很抱歉", "真的很抱歉", "I am really sorry"),
        "compliment": ("你的回答很棒", "你的回答很棒", "your answer is great"),
        "creator": ("你是由谁制作的", "你是由誰製作的", "who made this model"),
        "character": ("仉这个字怎么读", "仉這個字怎麼讀", "how do you pronounce 仉"),
        "english_word": ("friend这个单词什么意思", "friend這個單詞什麼意思", "what does friend mean"),
        "legal": ("中国民法典管什么", "臺灣民法管什麼", "what does US federal copyright law cover"),
    }
    print("\n简体 / 繁体 / 英语功能对等测试：")
    for expected, prompts in parity_cases.items():
        results = [model.predict(prompt)[0] for prompt in prompts]
        passed = all(result == expected for result in results); ok &= passed
        print(f"  {'✓' if passed else '✗'} {expected}: {' / '.join(results)}")
    math_cases = {
        "2+2": "4", "3.14×8": "25.12", "0.1+0.2": "0.3",
        "1/3 + 2/5": "11/15（≈ 0.733333333333）", "sin(30°)": "0.5",
        "解方程 x+3=7": "x = 4", "解方程 2*x-6=10": "x = 8",
    }
    print("\nPython 精确计算测试：")
    for expression, expected in math_cases.items():
        actual = calculate(expression); passed = actual == expected; ok &= passed
        print(f"  {'✓' if passed else '✗'} {expression} = {actual}")
    huge = calculate("14000**14000")
    passed = "共 58046 位" in huge; ok &= passed
    print(f"  {'✓' if passed else '✗'} 14000**14000 已实际计算（58046 位，界面缩略显示）")
    with tempfile.TemporaryDirectory() as temporary_directory:
        notice = calculate_for_chat("14000**14000", "zh", temporary_directory)
        documents = list(Path(temporary_directory).glob("LocalAI_数学计算结果_*.txt"))
        content = documents[0].read_text(encoding="utf-8") if documents else ""
        exported = (len(documents) == 1 and "58046 位整数" in notice and
                    "结果位数：58046" in content and "完整结果：" in content)
        ok &= exported
        print(f"  {'✓' if exported else '✗'} 超大结果已完整导出为 UTF-8 文档")
    try:
        calculate("__import__('os').system('echo unsafe')")
        safe = False
    except (ValueError, SyntaxError):
        safe = True
    ok &= safe
    print(f"  {'✓' if safe else '✗'} 非白名单 Python 代码已拒绝")
    history_cases = {
        "秦始皇如何统一中国":"秦统一六国", "鄭和的航海發生了什麼":"鄭和下西洋",
        "What happened during the French Revolution?":"French Revolution",
        "二战是哪几年":"1939—1945年", "When did Apollo 11 land on the Moon?":"1969",
        "秦始皇是谁":"中国历史上第一位", "誰是秦始皇":"秦朝第一代皇帝",
        "Who was Napoleon?":"French military leader", "孔夫子生活在哪个年代":"中国古代思想家",
        "介紹一下拿破崙":"法國軍事家",
    }
    print("\nSQLite 三语历史知识测试：")
    for question, expected_text in history_cases.items():
        answer, intent, _ = reply(model, question)
        passed = intent == "history" and expected_text in answer; ok &= passed
        print(f"  {'✓' if passed else '✗'} {question} -> {answer[:72]}")
    context_memory = ConversationMemory()
    reply(model, "Who was Napoleon?", memory=context_memory, allow_network=False)
    followup_answer, followup_intent, _ = reply(model, "他后来怎样？", memory=context_memory, allow_network=False)
    cross_language_context = (followup_intent == "history" and "拿破仑" in followup_answer and
                              "Napoleon" in context_memory.last_subject)
    ok &= cross_language_context
    print(f"  {'✓' if cross_language_context else '✗'} 英文历史实体到中文代词追问")
    for _ in range(66):
        context_memory.remember("你好", "你好。", "greeting")
    window_context = len(context_memory.turns) == context_memory.max_turns == 64
    ok &= window_context
    print(f"  {'✓' if window_context else '✗'} 64 轮轻量会话窗口")
    legal_memory = ConversationMemory()
    reply(model, "最低工资法律是多少？", memory=legal_memory, allow_network=False)
    for _ in range(10):
        reply(model, "你好", memory=legal_memory, allow_network=False)
    legal_answer, legal_intent, _ = reply(model, "中国大陆", memory=legal_memory, allow_network=False)
    long_legal_context = legal_intent == "legal" and "中国大陆" in legal_answer
    ok &= long_legal_context
    print(f"  {'✓' if long_legal_context else '✗'} 远程法律地区补充追问")
    encyclopedia_cases = {
        "介绍一下地震": "地壳", "什麼是滲透壓": "半透膜",
        "What is cell biology?": "basic unit of life", "什么是光锥": "狭义相对论",
        "什麼是播客": "RSS", "What is a comet?": "Solar System",
    }
    print("\n扩充后三语百科与路由测试：")
    for question, expected_text in encyclopedia_cases.items():
        answer, intent, _ = reply(model, question, allow_network=False)
        passed = intent == "encyclopedia" and expected_text.lower() in answer.lower(); ok &= passed
        print(f"  {'✓' if passed else '✗'} {question} -> {intent}: {answer[:90]}")
    legal_cases = {
        "中国民法典管什么":"中国大陆", "美国联邦最低工资适用什么法":"7.25美元",
        "加州最低工资法律是什么":"不能代替California州法", "臺灣民法的成年年齡":"18歲",
        "Who regulates fair use under US federal law?":"17 U.S.C. §107",
        "最低工资法律是多少":"地域性", "香港劳动法适用什么规定":"独立的法律制度",
        "美国合同法怎么规定":"具体州",
    }
    print("\nSQLite 地域化法律知识测试：")
    for question, expected_text in legal_cases.items():
        answer, intent, _ = reply(model, question)
        passed = intent == "legal" and expected_text.lower() in answer.lower(); ok &= passed
        print(f"  {'✓' if passed else '✗'} {question} -> {answer[:110]}")
    character_cases = {"龘是什么意思":"群龙腾飞", "仉是不是姓氏":"姓氏", "爨怎么读":"cuàn",
                       "乜作为姓氏怎么读":"niè", "What does 龘 mean?":"dragons in flight"}
    print("\n生僻字知识测试：")
    for question, expected_text in character_cases.items():
        answer, intent, _ = reply(model, question)
        passed = intent == "character" and expected_text in answer; ok &= passed
        print(f"  {'✓' if passed else '✗'} {question} -> {answer[:100]}")
    word_cases = {"apple是什么意思":"I eat an apple", "hello这个单词怎么读":"said hello",
                  "用friend造句":"close friend", "What does friend mean?":"Example:",
                  "define beautiful":"beautiful", "use world in a sentence":"Example:"}
    print("\n英语短词数据库测试：")
    for question, expected_text in word_cases.items():
        answer, intent, _ = reply(model, question)
        passed = intent == "english_word" and expected_text.lower() in answer.lower(); ok &= passed
        print(f"  {'✓' if passed else '✗'} {question} -> {answer[:100]}")
    return ok

def run_systematic_eval(model):
    """运行可复现的千项回归集；联网在评测中始终关闭。"""
    count=build_eval_dataset(); passed=0; failed=[]; categories={}
    with Path(__file__).with_name("localai_systematic_eval.jsonl").open(encoding="utf-8") as source:
        for line_number,line in enumerate(source,1):
            case=json.loads(line); category=case["category"]; categories.setdefault(category,[0,0]); categories[category][1]+=1
            try:
                if category=="math":
                    actual=calculate(case["prompt"]); ok=actual==case["expected"]
                elif category=="utility":
                    language=detect_language(case["prompt"]); answer=answer_utility(case["prompt"],language) or ""; ok=case["expected"] in answer
                elif category in {"encyclopedia","expanded_encyclopedia"}:
                    answer,entry_id=answer_encyclopedia(DATABASE_PATH,case["prompt"],case["language"],False)
                    ok=entry_id==case["expected_id"] and bool(answer)
                elif category=="advanced":
                    answer=advanced_math(case["prompt"],case["language"]) or ""; ok=case["expected"] in answer
                elif category in {"legal_action","life_legal"}:
                    answer,intent,_=reply(model,case["prompt"],allow_network=False); ok=intent=="legal" and case["expected"].casefold() in answer.casefold()
                elif category=="daily_life":
                    answer,intent,_=reply(model,case["prompt"],allow_network=False); ok=intent=="daily_life" and case["expected"].casefold() in answer.casefold()
                else:
                    reply(model,case["prompt"],allow_network=False); ok=True
            except Exception as error:
                ok=False; actual=f"{type(error).__name__}: {error}"
            if ok: passed+=1; categories[category][0]+=1
            elif len(failed)<12: failed.append((line_number,case.get("prompt",""),locals().get("actual","mismatch")))
    print(f"\n系统评测：{passed}/{count} 通过")
    for category,(good,total) in categories.items(): print(f"  {category}: {good}/{total}")
    for line_number,prompt,actual in failed: print(f"  ✗ L{line_number} {prompt!r}: {actual}")
    return passed==count

def run_memory_benchmark(model):
    """在当前进程执行代表性离线工作负载并报告操作系统记录的峰值 RSS。"""
    memory=ConversationMemory(); context=FileContext()
    workload=["你好","水的沸点是多少","秦始皇是谁","中国大陆网购商品坏了商家不给退款怎么办",
              "美国加州房东不退押金怎么办","臺灣遇到網路詐騙怎麼辦","解方程 x^2-5x+6=0",
              "因式分解 x^2-5x+6","1 GiB to B"]
    for prompt in workload: reply(model,prompt,memory=memory,allow_network=False)
    context.open(str(Path(__file__).with_name("README.md")))
    reply(model,"如何运行系统评测",memory=memory,file_context=context,allow_network=False)
    if resource is None:
        print("当前平台无法用 Python 标准库读取进程峰值 RSS；请使用系统任务管理器测量。")
        return True
    raw=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 返回字节，Linux/BSD 常见实现返回 KiB。
    bytes_used=raw if sys.platform=="darwin" else raw*1024
    print(f"代表性离线聊天工作负载峰值 RSS：{bytes_used/1048576:.2f} MiB（{bytes_used:,} bytes）")
    print("说明：RSS 包含 Python、NumPy、SQLite、模型及页面缓存；不同系统和查询会波动。")
    return True

def chat(model, debug=False, allow_network=True, stream_enabled=True, show_stats=False,
         stream_chunk_tokens=24):
    memory, file_context = ConversationMemory(), FileContext()
    print("LocalAI 迷你模型已启动。输入 /help 查看用法，/quit 退出。")
    while True:
        try: text=input("你: ").strip()
        except (EOFError, KeyboardInterrupt): print("\nAI: 再见！"); return
        if text.lower() in {"/quit", "/exit", "退出"}: print("AI: 再见！"); return
        if text.lower()=="/debug": debug=not debug; print(f"诊断信息：{'开' if debug else '关'}"); continue
        if text.lower()=="/stats": show_stats=not show_stats; print(f"Token统计：{'开' if show_stats else '关'}"); continue
        if text.lower()=="/stream": stream_enabled=not stream_enabled; print(f"流式输出：{'开' if stream_enabled else '关'}"); continue
        if text.lower()=="/help":
            print("AI: 直接输入聊天、百科、历史、法律、数学或换算问题。\n"
                  "    /open 文件路径  读取 TXT/Markdown/代码/CSV（最大 10 MiB）\n"
                  "    /forget-file    关闭当前文件问答\n"
                  "    /memory         查看最近会话轮数\n"
                  "    /debug          显示意图与置信度\n"
                  "    /stats          显示真实 byte-token、TTFT 与吞吐\n"
                  "    /stream         切换流式/整段显示\n"
                  "    /quit           退出")
            continue
        if text.lower().startswith(("/open ","/file ")):
            raw_path=text.split(maxsplit=1)[1].strip()
            try:
                parsed=shlex.split(raw_path); path=parsed[0] if len(parsed)==1 else raw_path
                print(f"AI: {file_context.open(path)}")
            except (ValueError,OSError) as error: print(f"AI: 无法读取文件：{error}")
            continue
        if text.lower()=="/forget-file": file_context.clear(); print("AI: 已关闭当前文件问答。"); continue
        if text.lower()=="/memory": print(f"AI: 当前保留最近 {len(memory.turns)}/{memory.max_turns} 轮对话；/quit 后不会写入磁盘。"); continue
        events=stream_reply(model,text,memory,file_context,allow_network,stream_chunk_tokens)
        parts=[]; completed=None
        if stream_enabled: print("AI: ",end="",flush=True)
        for event in events:
            if event["type"]=="response.output_text.delta":
                parts.append(event["delta"])
                if stream_enabled: print(event["delta"],end="",flush=True)
            elif event["type"]=="response.completed": completed=event
        suffix=(f"  [intent={completed['intent']}, confidence={completed['confidence']:.1%}]"
                if debug and completed else "")
        if stream_enabled: print(suffix)
        else: print(f"AI: {''.join(parts)}{suffix}")
        if show_stats and completed:
            usage,metrics=completed["usage"],completed["metrics"]
            print(f"    [byte-token: input={usage['input_tokens']}, output={usage['output_tokens']}, "
                  f"total={usage['total_tokens']}; TTFT={metrics['time_to_first_token_ms']:.2f} ms; "
                  f"total={metrics['total_ms']:.2f} ms; output={metrics['output_tokens_per_second']:.1f} token/s; "
                  f"chunks={metrics['chunks']}]")

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("train","chat","stream","test","build-data","build-eval","eval","benchmark-memory","ask-file","import-history","import-history-people","import-china-laws","import-unihan","import-english","import-word-usage","import-knowledge","knowledge-stats","model-backups"), default="chat")
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--learning-rate", type=float, default=.08)
    parser.add_argument("--history-limit", type=int, default=1000, help="本次从 Wikidata 处理的最大历史条目数")
    parser.add_argument("--min-sitelinks", type=int, default=5, help="历史人物至少拥有的 Wikimedia 站点链接数")
    parser.add_argument("--history-offset", type=int, default=0, help="历史人物远端分页起点，用于从上次位置继续扩充")
    parser.add_argument("--max-word-length", type=int, default=12, help="英语词典允许的最长单词字母数")
    parser.add_argument("--knowledge-limit", type=int, default=1500,
                        help="本次从 Wikimedia 处理的最大通用知识候选数；数据库硬上限为 600 MiB")
    parser.add_argument("--knowledge-topics", default="",
                        help="只导入逗号分隔的指定领域；留空导入全部领域")
    parser.add_argument("--offline", action="store_true", help="禁止数据库 MISS 后联网检索（已有缓存仍可使用）")
    parser.add_argument("--no-stream", action="store_true", help="聊天时整段输出；默认使用 LLM 风格增量输出")
    parser.add_argument("--show-stats", action="store_true", help="每轮显示真实 byte-token、首 Token 延迟和吞吐")
    parser.add_argument("--stream-chunk-tokens", type=int, default=24,
                        help="每个流式文本块的目标 byte-token 上限（默认 24，不拆 Unicode 字符）")
    parser.add_argument("--file", help="ask-file 模式下的本地文件路径")
    parser.add_argument("--question", help="ask-file 模式下的问题")
    parser.add_argument("--debug", action="store_true"); args=parser.parse_args()
    if args.stream_chunk_tokens < 1: parser.error("--stream-chunk-tokens 必须大于 0")
    if args.command=="build-data": build_knowledge_assets(); return 0
    if args.command=="build-eval": print(f"已生成 {build_eval_dataset():,} 条评测：{Path(__file__).with_name('localai_systematic_eval.jsonl')}"); return 0
    if args.command=="import-history": import_wikidata_history(args.history_limit); return 0
    if args.command=="import-history-people":
        import_wikidata_people(args.history_limit,args.min_sitelinks,args.history_offset); return 0
    if args.command=="import-china-laws": import_china_law_catalog(); return 0
    if args.command=="import-unihan": import_unihan_characters(); return 0
    if args.command=="import-english": import_english_words(args.max_word_length); return 0
    if args.command=="import-word-usage": import_wordnet_usage(); return 0
    if args.command=="import-knowledge":
        topics=tuple(item.strip() for item in args.knowledge_topics.split(",") if item.strip()) or None
        result=import_general_knowledge(DATABASE_PATH,args.knowledge_limit,topics=topics)
        print(f"通用知识扩充完成：本次写入/更新 {result['imported']:,} 条，"
              f"当前共 {result['total']:,} 条；数据库 {result['database_bytes']/1048576:.2f} MiB / 600 MiB。")
        if result["errors"]:
            print("部分主题暂未完成："+"；".join(result["errors"]))
        build_knowledge_assets()
        return 0
    if args.command=="knowledge-stats":
        stats=knowledge_statistics(DATABASE_PATH)
        print(f"通用百科：{stats['entries']:,} 条，检索词 {stats['terms']:,} 个，"
              f"FTS 行 {stats['fts_rows']:,} 条，来源记录 {stats['sources']:,} 条")
        for category,count in stats["categories"].items(): print(f"  {category}: {count:,}")
        print(f"数据库：{stats['database_bytes']/1048576:.2f} MiB / {stats['max_bytes']/1048576:.0f} MiB")
        return 0
    if args.command=="model-backups": list_training_archives(); return 0
    if args.command=="train":
        model=train(args.epochs,args.learning_rate)
        return 0 if (run_tests(model) and run_systematic_eval(model)) else 1
    try: model=TinyChatModel.load()
    except FileNotFoundError: print("尚未发现已训练模型，正在首次训练……"); model=train(args.epochs,args.learning_rate)
    if args.command=="test": return 0 if (run_tests(model) and run_systematic_eval(model)) else 1
    if args.command=="eval": return 0 if run_systematic_eval(model) else 1
    if args.command=="benchmark-memory": return 0 if run_memory_benchmark(model) else 1
    if args.command=="stream":
        if not args.question: parser.error("stream 需要 --question")
        context=None
        if args.file:
            context=FileContext(); context.open(args.file)
        for event in stream_reply(model,args.question,file_context=context,
                                  allow_network=not args.offline,
                                  chunk_token_limit=args.stream_chunk_tokens):
            print(json.dumps(event,ensure_ascii=False),flush=True)
        return 0
    if args.command=="ask-file":
        if not args.file or not args.question: parser.error("ask-file 需要 --file 和 --question")
        context=FileContext(); context.open(args.file)
        print(context.answer(args.question,detect_language(args.question))); return 0
    chat(model,args.debug,not args.offline,not args.no_stream,args.show_stats,args.stream_chunk_tokens); return 0

if __name__ == "__main__": raise SystemExit(main())
