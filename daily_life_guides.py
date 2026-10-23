#!/usr/bin/env python3
"""非法律日常生活指南：三语、数据库驱动、带安全升级边界。"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path


# 每个场景只保存场景特有的第一步；检查清单、禁忌和升级条件由分组模板统一提供，
# 最终仍会展开后写入 SQLite，方便后续单独修订某条记录。
GUIDES = {
    "gas_smell": ("home_safety", r"煤气味|燃气味|瓦斯味|疑似漏气|闻到.*(?:煤气|燃气|瓦斯)|smell gas|gas leak",
        ("疑似燃气泄漏时不要开关电器或点火；能安全做到就关闭气源，立即离开并在室外联系燃气抢修和消防。",
         "疑似燃氣洩漏時不要開關電器或點火；能安全做到就關閉氣源，立即離開並在室外聯絡燃氣搶修和消防。",
         "Do not operate switches or flames. If safe, shut off the gas, leave immediately, and call the gas emergency service and fire service from outside.")),
    "home_fire": ("home_safety", r"家里.*(?:着火|著火|起火)|屋里.*烟|房间.*冒烟|火灾怎么办|住宅火警|house fire|room.*smoke|smoke.*(?:apartment|house|room|home)|home.*on fire",
        ("立即撤离并提醒他人，走楼梯而不是电梯；到安全地点后联系消防，不要返回取物。",
         "立即撤離並提醒他人，走樓梯而不是電梯；到安全地點後聯絡消防，不要返回取物。",
         "Evacuate, alert others, use stairs rather than lifts, call the fire service from safety, and do not return for belongings.")),
    "power_outage": ("home", r"停电了|停電了|家里没电|家裡沒電|突然断电|突然斷電|power outage|power went out|no electricity at home",
        ("先确认是否只有本户停电，关闭高功率电器并保留一盏灯；用手电照明，查看物业或供电方通知。",
         "先確認是否只有本戶停電，關閉高功率電器並保留一盞燈；用手電照明，查看物業或供電方通知。",
         "Check whether only your home is affected, switch off high-load appliances, leave one light on, use a torch, and check utility notices.")),
    "water_leak": ("home", r"水管.*漏|家里漏水|家裡漏水|天花板.*(?:滴水|漏水)|房间进水|房間進水|burst pipe|water leak|ceiling.*drip",
        ("先关闭附近阀门或总水阀，把电器和贵重物品移离积水；若水靠近插座或配电箱，不要涉水操作电源。",
         "先關閉附近閥門或總水閥，把電器和貴重物品移離積水；若水靠近插座或配電箱，不要涉水操作電源。",
         "Shut the local or main water valve, move electronics and valuables, and do not enter water to handle power if water is near sockets or the panel.")),
    "clogged_drain": ("home", r"下水道堵|马桶堵|馬桶堵|(?:水池|洗手池|洗手盆).*(?:堵|不下水)|排水.*不下去|clogged (?:drain|toilet|sink)|toilet.*blocked",
        ("停止继续放水，先清除可见杂物并使用皮搋子；不要混合不同管道疏通剂，记录是否多处同时返水。",
         "停止繼續放水，先清除可見雜物並使用吸盤；不要混合不同管道疏通劑，記錄是否多處同時返水。",
         "Stop adding water, remove visible debris, and try a plunger. Never mix drain chemicals; note whether several drains back up together.")),
    "mold_damp": ("home", r"(?:墙|牆|墙角|牆角|房间|房間).*(?:发霉|發霉|长霉|長霉)|屋里潮湿|屋裡潮濕|mould|mold on (?:wall|ceiling)|damp room",
        ("先找漏水、结露或通风不足的来源并降低湿度；小面积清理时通风并戴手套，反复出现要先解决水源。",
         "先找漏水、結露或通風不足的來源並降低濕度；小面積清理時通風並戴手套，反覆出現要先解決水源。",
         "Find and fix leaks, condensation, or poor ventilation and reduce humidity. Ventilate and wear gloves for a small area; recurring mould requires fixing the moisture source.")),
    "fridge_food": ("food", r"冰箱停电|冰箱停電|冰箱.*(?:坏|壞).*(?:食物|东西|東西|肉|菜|还能吃|還能吃)|食物放冰箱.*还能吃|食物.*變質|food.*fridge.*power|refrigerator.*food safe|food spoiled",
        ("尽量不要开冰箱门；记录断电时长和食物温度。易腐食物若温度或离开冷藏的时间无法确认，宁可丢弃。",
         "儘量不要開冰箱門；記錄停電時長和食物溫度。易腐食物若溫度或離開冷藏的時間無法確認，寧可丟棄。",
         "Keep the door closed and track outage time and food temperature. Discard perishable food when its temperature or time outside safe refrigeration is uncertain.")),
    "grease_fire": ("home_safety", r"(?:油锅|油鍋|锅里|鍋裡).*(?:着火|著火|起火|冒火)|厨房油火|廚房油火|grease fire|pan.*on fire|cooking oil.*fire",
        ("关闭热源，在安全情况下用锅盖或烤盘覆盖；绝对不要泼水，也不要端着着火的锅移动。无法立即控制就撤离并联系消防。",
         "關閉熱源，在安全情況下用鍋蓋或烤盤覆蓋；絕對不要潑水，也不要端著著火的鍋移動。無法立即控制就撤離並聯絡消防。",
         "Turn off the heat and, only if safe, cover the pan with a lid or baking sheet. Never use water or carry the burning pan; evacuate and call the fire service if it is not immediately controlled.")),
    "meal_planning": ("food", r"不知道吃(?:什么|什麼|啥)|一周菜谱|一週菜譜|怎么安排三餐|怎麼安排三餐|meal plan|what should I cook|plan meals",
        ("先列出人数、预算、过敏或忌口、现有食材和可用时间；每餐按主食、蛋白质、蔬菜组合，再安排一两顿处理剩余食材。",
         "先列出人數、預算、過敏或忌口、現有食材和可用時間；每餐按主食、蛋白質、蔬菜組合，再安排一兩餐處理剩餘食材。",
         "List diners, budget, allergies, ingredients, and cooking time. Build meals from a staple, protein, and vegetables, with one or two meals using leftovers.")),
    "food_allergy": ("health_urgent", r"吃.*过敏|吃.*過敏|食物过敏|食物過敏|(?:吃完|吃了).*(?:嘴唇|喉咙|喉嚨).*(?:肿|腫|痒|癢)|嘴唇.*(?:肿|腫)|food allergy|allergic reaction.*food",
        ("立即停止食用并观察呼吸、喉咙、意识和全身反应；呼吸困难、喉头紧、晕厥或迅速加重时立即联系急救。",
         "立即停止食用並觀察呼吸、喉嚨、意識和全身反應；呼吸困難、喉頭緊、昏厥或迅速加重時立即聯絡急救。",
         "Stop eating it and monitor breathing, throat symptoms, consciousness, and widespread reaction. Call emergency services for breathing difficulty, throat tightness, fainting, or rapid worsening.")),
    "urgent_symptoms": ("health_urgent", r"胸痛|胸口剧痛|胸口劇痛|喘不过气|喘不過氣|呼吸困难|呼吸困難|突然昏迷|一侧无力|一側無力|说话含糊|說話含糊|chest pain|cannot breathe|difficulty breathing|sudden weakness|unconscious",
        ("这可能是急症：立即联系当地急救并说明症状、地址和开始时间；不要自行驾车，也不要等待在线回复。",
         "這可能是急症：立即聯絡當地急救並說明症狀、地址和開始時間；不要自行駕車，也不要等待線上回覆。",
         "This may be an emergency. Call local emergency services now with the symptoms, location, and onset time; do not drive yourself or wait for an online reply.")),
    "minor_burn": ("health", r"轻微烫伤|輕微燙傷|(?:被)?(?:热水|熱水|开水|開水).*(?:烫|燙)|小面积烧伤|小面積燒傷|minor burn|scalded my|small burn",
        ("移开热源，用流动凉水持续冷却，取下附近不粘连的饰物和衣物，再用干净不粘敷料松盖。不要用冰、牙膏或油脂。",
         "移開熱源，用流動涼水持續冷卻，取下附近不黏連的飾物和衣物，再用乾淨不黏敷料鬆蓋。不要用冰、牙膏或油脂。",
         "Remove the heat source, cool under cool running water, remove nearby non-stuck jewellery or clothing, and loosely cover with a clean non-stick dressing. Do not use ice, toothpaste, or grease.")),
    "minor_cut": ("health", r"手指割伤|手指割傷|小伤口流血|小傷口流血|不小心切到手|切菜.*(?:手|划|劃|伤|傷)|minor cut|small cut.*bleeding|cut my finger",
        ("先洗手，用干净纱布持续直接按压止血；止血后用清水冲洗并覆盖。不要把深扎物自行拔出。",
         "先洗手，用乾淨紗布持續直接按壓止血；止血後用清水沖洗並覆蓋。不要把深扎物自行拔出。",
         "Wash your hands and apply continuous direct pressure with clean gauze. Once bleeding stops, rinse with clean water and cover it. Do not remove a deeply embedded object yourself.")),
    "fever": ("health", r"发烧怎么办|發燒怎麼辦|一直发热|一直發熱|孩子发烧|孩子發燒|有点发烧|有點發燒|fever.*what|running a fever",
        ("测量并记录体温、持续时间、伴随症状、饮水和用药；休息补液。婴幼儿、孕妇、老人或有基础病者应更早咨询医生。",
         "測量並記錄體溫、持續時間、伴隨症狀、飲水和用藥；休息補液。嬰幼兒、孕婦、長者或有基礎病者應更早諮詢醫師。",
         "Measure and record temperature, duration, other symptoms, fluids, and medicines; rest and hydrate. Seek earlier clinical advice for infants, pregnancy, older age, or major underlying illness.")),
    "missed_medicine": ("health", r"忘(?:记|記|了).*(?:吃|服).*(?:药|藥)|漏服.*药|漏服.*藥|药少吃一顿|藥少吃一頓|missed (?:a )?dose|forgot.*medication",
        ("先查看药品标签和医生或药师给的漏服说明；不要自行加倍补服。记录药名、剂量、原定时间和发现时间后联系药师或开药医生。",
         "先查看藥品標籤和醫師或藥師提供的漏服說明；不要自行加倍補服。記錄藥名、劑量、原定時間和發現時間後聯絡藥師或開藥醫師。",
         "Check the label and missed-dose instructions from the prescriber or pharmacist. Do not double a dose unless specifically instructed; note the medicine, dose, scheduled time, and discovery time and contact a pharmacist or prescriber.")),
    "sleep_problem": ("wellbeing", r"睡不着|睡不著|失眠|总是熬夜|總是熬夜|作息乱|作息亂|cannot sleep|insomnia|sleep schedule",
        ("固定起床时间，睡前降低光线和刺激，下午后减少咖啡因；床上长时间清醒时先起身做安静活动，困了再回去。",
         "固定起床時間，睡前降低光線和刺激，下午後減少咖啡因；在床上長時間清醒時先起身做安靜活動，睏了再回去。",
         "Keep a fixed wake time, reduce light and stimulation before bed, and limit late caffeine. If awake in bed for a long time, do something quiet and return when sleepy.")),
    "stress_overload": ("wellbeing", r"压力太大|壓力太大|(?:快|感觉要|感覺要)?(?:撑不住|撐不住)|事情太多.*(?:焦虑|焦慮)|overwhelmed|too stressed|cannot cope",
        ("先暂停并确认当前是否安全，把任务分成“今天必须、可以推迟、可以求助”三类；只选择一个十分钟内能开始的小步骤，并联系可信的人。",
         "先暫停並確認目前是否安全，把任務分成「今天必須、可以延後、可以求助」三類；只選一個十分鐘內能開始的小步驟，並聯絡可信任的人。",
         "Pause and check immediate safety. Sort tasks into must do today, can wait, and can ask for help; choose one step that takes under ten minutes and contact someone you trust.")),
    "no_internet": ("digital", r"连不上网|連不上網|家里没网|家裡沒網|网络断了|網路斷了|(?:连不上|連不上).*wifi|wifi.*(?:不行|断|斷|连不上|連不上)|no internet|wifi.*not working|cannot connect.*internet",
        ("先确认是一台设备还是全部设备受影响，检查飞行模式、网线和运营商通知；依次重启终端、路由器和光猫，每次等待其完全启动。",
         "先確認是一台裝置還是全部裝置受影響，檢查飛航模式、網路線和營運商通知；依次重啟終端、路由器和光纖數據機，每次等待其完全啟動。",
         "Check whether one or all devices are affected, flight mode, cables, and provider notices. Restart the device, router, and modem in order, waiting for each to finish booting.")),
    "slow_computer": ("digital", r"(?:电脑|電腦|笔记本|筆記本).*(?:很卡|卡得|变慢|變慢|卡死)|computer.*slow|laptop.*slow|PC.*freezing",
        ("先保存工作并重启，查看存储空间和任务管理器中的高占用程序；暂停不明启动项并完成系统和安全更新。不要下载所谓一键加速工具。",
         "先儲存工作並重新啟動，查看儲存空間和工作管理員中的高佔用程式；暫停不明啟動項並完成系統和安全更新。不要下載所謂一鍵加速工具。",
         "Save work and restart, then check free storage and high-usage processes. Disable unknown startup items and install system and security updates; avoid dubious one-click optimiser tools.")),
    "lost_phone": ("digital_security", r"手机丢了|手機丟了|手机找不到|手機找不到|手机被偷|手機被偷|lost my phone|phone was stolen|cannot find my phone",
        ("用可信设备启动官方查找、锁定或丢失模式，联系运营商暂停 SIM，并优先保护邮箱、支付和密码管理器；不要独自去陌生地点取机。",
         "用可信裝置啟動官方尋找、鎖定或遺失模式，聯絡電信業者暫停 SIM，並優先保護信箱、支付和密碼管理器；不要獨自去陌生地點取機。",
         "Use a trusted device to enable the official find, lock, or lost mode; suspend the SIM; and secure email, payment, and password-manager accounts first. Do not retrieve it alone from an unfamiliar location.")),
    "hacked_account": ("digital_security", r"账号被盗|帳號被盜|账号被黑|帳號被駭|有人登录我的账号|有人登入我的帳號|陌生设备.*(?:登录|登入)|account hacked|account compromised|unknown login|login.*(?:do not recognize|don't recognize)",
        ("从可信设备改密码并退出其他会话，启用多因素验证，检查恢复邮箱、转发规则和已授权应用；相同密码用在别处的也要更换。",
         "從可信裝置更改密碼並登出其他工作階段，啟用多因素驗證，檢查復原信箱、轉寄規則和已授權應用程式；相同密碼用在別處的也要更換。",
         "From a trusted device, change the password, sign out other sessions, enable MFA, and inspect recovery details, forwarding rules, and authorised apps. Change reused passwords elsewhere too.")),
    "storage_full": ("digital", r"存储空间满|儲存空間滿|硬盘.*(?:满|沒空間|没空间|空间.*没有)|硬碟.*(?:滿|沒空間)|手机空间不足|手機空間不足|storage full|disk full|out of storage",
        ("先查看系统的分类统计，清理回收站、下载和可重新获取的缓存；移动照片前先确认备份可打开，不要删除不认识的系统目录。",
         "先查看系統的分類統計，清理垃圾桶、下載項目和可重新取得的快取；移動照片前先確認備份可開啟，不要刪除不認識的系統目錄。",
         "Check the system storage breakdown, clear trash, downloads, and reproducible caches. Verify backups open before moving photos, and do not delete unfamiliar system folders.")),
    "phone_not_charging": ("digital", r"(?:手机|手機).*(?:充不进电|充不進電|不充电|不充電|插线.*没反应|插線.*沒反應)|充电没反应|充電沒反應|phone not charging|will not charge",
        ("更换确认正常的插座、线缆和充电器，检查接口是否有可见异物；设备进水、鼓包、异常发热或有焦味时立即停用且不要继续充电。",
         "更換確認正常的插座、線材和充電器，檢查連接埠是否有可見異物；裝置進水、膨脹、異常發熱或有焦味時立即停用且不要繼續充電。",
         "Try a known-good outlet, cable, and charger and inspect the port for visible debris. Stop using and charging a wet, swollen, unusually hot, or burnt-smelling device.")),
    "missed_transport": ("travel", r"(?:没赶上|沒趕上|错过|錯過).*(?:飞机|飛機|火车|火車|高铁|高鐵|班车|班車)|误机|誤機|missed (?:my )?(?:flight|train|bus)",
        ("立即在承运方官方应用、柜台或客服查询改签和候补，不要通过搜索广告里的陌生号码付款；同时核对后续联程、住宿和保险通知期限。",
         "立即在承運方官方應用程式、櫃檯或客服查詢改簽和候補，不要透過搜尋廣告裡的陌生號碼付款；同時核對後續聯程、住宿和保險通知期限。",
         "Immediately check rebooking and standby through the carrier's official app, desk, or support. Do not pay numbers found in search adverts; also review connections, accommodation, and insurance notice periods.")),
    "lost_documents": ("travel", r"(?:身份证|身分證|护照|護照|钱包|錢包).*(?:丢|丟|弄丢|弄丟|找不到)|lost (?:my )?(?:passport|ID|wallet)|passport.*missing",
        ("先冻结银行卡和支付工具，列出丢失证件并记录最后出现地点；通过当地警察、证件签发机关或使领馆的官方渠道办理挂失和临时证明。",
         "先凍結銀行卡和支付工具，列出遺失證件並記錄最後出現地點；透過當地警察、證件核發機關或使領館的官方管道辦理掛失和臨時證明。",
         "Freeze cards and payment tools, list missing documents and last-seen locations, and use official police, issuing-authority, or consular channels for reports and temporary documents.")),
    "bad_weather_trip": ("travel", r"(?:台风|颱風).*(?:出行|出门|出門)|暴雨.*(?:出门|出門)|大雪.*旅行|恶劣天气.*出行|惡劣天氣.*出行|travel.*severe weather|storm.*trip",
        ("查看气象部门和承运方官方预警，能推迟就不要冒险；准备充电、饮水、保暖、常用药和联系人，并把行程告知可信的人。",
         "查看氣象部門和承運方官方預警，能延後就不要冒險；準備充電、飲水、保暖、常用藥和聯絡人，並把行程告知可信任的人。",
         "Check official weather and carrier alerts and postpone when possible. Prepare power, water, warmth, regular medicines, and contacts, and share the plan with someone you trust.")),
    "budgeting": ("money", r"怎么做预算|怎麼做預算|(?:工资|工資|薪水).*(?:不够花|不夠花|不够用|不夠用)|每月怎么存钱|每月怎麼存錢|make a budget|monthly budget|save money each month",
        ("先记录一个月的到手收入、固定支出、最低还款和可变支出；优先保障住房、食物、医疗和交通，再设小额自动储蓄并每周复盘。",
         "先記錄一個月的實領收入、固定支出、最低還款和變動支出；優先保障住房、食物、醫療和交通，再設小額自動儲蓄並每週檢視。",
         "Record one month of take-home income, fixed costs, minimum debt payments, and variable spending. Protect housing, food, health, and transport first, then automate a small saving and review weekly.")),
    "unexpected_bill": ("money", r"突然收到.*账单|突然收到.*帳單|(?:账单|帳單|电费|電費|水费|水費).*(?:太高|高得|离谱|離譜)|交不起.*费用|繳不起.*費用|unexpected bill|cannot pay.*bill|bill is too high",
        ("先核对户名、周期、用量、费率和重复收费，并通过账单上的官方渠道要求明细；在到期前询问分期、困难援助或争议流程。",
         "先核對戶名、週期、用量、費率和重複收費，並透過帳單上的官方管道要求明細；在到期前詢問分期、困難援助或爭議流程。",
         "Verify the account, period, usage, tariff, and duplicate charges and request an itemisation through the official contact on the bill. Ask about instalments, hardship help, or disputes before the due date.")),
    "study_plan": ("study", r"怎么制定学习计划|怎麼制定學習計畫|帮我安排复习|幫我安排複習|学习计划怎么做|學習計畫怎麼做|考试.*(?:排复习|排複習)|study plan|revision plan|plan my studying",
        ("列出考试日期、科目范围和当前薄弱点，把任务拆成可完成的小块；每天安排主动回忆、练习题和短复盘，并预留补漏时间。",
         "列出考試日期、科目範圍和目前薄弱點，把任務拆成可完成的小塊；每天安排主動回憶、練習題和短檢討，並預留補漏時間。",
         "List dates, syllabus, and weak areas and split work into small tasks. Schedule active recall, practice questions, and a short review each day, with catch-up time.")),
    "procrastination": ("study", r"总是拖延|總是拖延|不想开始学习|不想開始學習|(?:作业|作業).*(?:拖到最后|拖到最後)|procrastinat|cannot start studying|keep putting.*off",
        ("把目标缩成两分钟启动动作，例如打开资料并写第一题；关闭一个干扰源，设置二十五分钟专注段，结束后只记录下一步。",
         "把目標縮成兩分鐘啟動動作，例如打開資料並寫第一題；關閉一個干擾源，設定二十五分鐘專注時段，結束後只記錄下一步。",
         "Shrink the goal to a two-minute start, such as opening notes and attempting one question. Remove one distraction, use a 25-minute focus block, and record only the next step afterward.")),
    "exam_anxiety": ("wellbeing", r"考试紧张|考試緊張|考试焦虑|考試焦慮|(?:考试|考試|考场|考場).*(?:慌|紧张|緊張|空白)|exam anxiety|nervous about.*exam|test anxiety",
        ("先用模拟题熟悉时间和流程，考前维持睡眠和正常饮食；现场先慢呼气、读清要求、从能做的题开始，卡住就标记后跳过。",
         "先用模擬題熟悉時間和流程，考前維持睡眠和正常飲食；現場先慢慢吐氣、讀清要求、從能做的題開始，卡住就標記後跳過。",
         "Use timed practice to rehearse the format, and keep normal sleep and meals. During the exam, exhale slowly, read instructions, start with manageable questions, and mark and skip when stuck.")),
    "work_email": ("work", r"怎么写工作邮件|怎麼寫工作郵件|催进度怎么说|催進度怎麼說|如何礼貌催|如何禮貌催|邮件.*催.*进度|郵件.*催.*進度|write a work email|follow-up email|polite reminder email",
        ("邮件按“主题明确—背景一句—需要对方做什么—期望时间—感谢”组织；只写必要事实，发送前检查收件人、附件和日期。",
         "郵件按「主旨明確—背景一句—需要對方做什麼—期望時間—感謝」組織；只寫必要事實，寄出前檢查收件人、附件和日期。",
         "Use a clear subject, one-line context, requested action, desired date, and thanks. Include only necessary facts and check recipients, attachments, and dates before sending.")),
    "colleague_conflict": ("work", r"和同事闹矛盾|和同事鬧矛盾|同事.*(?:沟通不顺|溝通不順|冲突|衝突)|团队冲突|團隊衝突|conflict with.*coworker|colleague conflict|team disagreement",
        ("先分开事实、影响和推测，约一个私下且冷静的时间；用“当发生X，我受到Y影响，希望以后Z”的方式表达，并确认下一步。",
         "先分開事實、影響和推測，約一個私下且冷靜的時間；用「當發生X，我受到Y影響，希望以後Z」的方式表達，並確認下一步。",
         "Separate facts, impact, and assumptions and choose a calm private time. Say, 'When X happened, it affected Y; I would like Z next time,' then confirm the next step.")),
    "job_interview": ("work", r"明天面试|明天面試|面试.*(?:怎么准备|怎麼準備|该准备|該準備|很紧张|很緊張)|job interview.*prepare|prepare.*interview|interview tomorrow|nervous.*interview",
        ("核对岗位要求并准备三到五个具体经历，用情境—任务—行动—结果练习；准备自我介绍、两个反问问题、路线设备和材料。",
         "核對職缺要求並準備三到五個具體經歷，用情境—任務—行動—結果練習；準備自我介紹、兩個反問問題、路線設備和資料。",
         "Match the role requirements to three to five concrete examples and practise situation-task-action-result answers. Prepare an introduction, two questions, route or equipment, and documents.")),
    "lost_child": ("care_urgent", r"孩子走丢|孩子走丟|小孩不见了|小孩不見了|找不到孩子|lost child|child is missing|cannot find my child",
        ("立即通知现场工作人员和当地警察，说明最近位置、时间、衣着和照片；一人留在最后看见地点，其他人检查近处危险区域，不要隐瞒失踪时间。",
         "立即通知現場工作人員和當地警察，說明最近位置、時間、衣著和照片；一人留在最後看見地點，其他人檢查附近危險區域，不要隱瞞失蹤時間。",
         "Immediately alert venue staff and local police with last location, time, clothing, and a photo. Keep one person at the last-seen point while others check nearby hazards; do not delay or conceal the time missing.")),
    "elderly_fall": ("care_urgent", r"老人摔倒|長者跌倒|老人跌倒|家里老人摔了|家裡長者跌倒|older person fell|elderly.*fall|senior.*fallen",
        ("先确认意识和呼吸；有头颈背痛、明显畸形、无法起身、服用抗凝药或状态异常时不要强行扶起，立即联系急救。",
         "先確認意識和呼吸；有頭頸背痛、明顯變形、無法起身、服用抗凝血藥或狀態異常時不要強行扶起，立即聯絡急救。",
         "Check responsiveness and breathing. Do not force movement and call emergency services for head, neck, or back pain, deformity, inability to rise, anticoagulant use, or abnormal condition.")),
    "pet_illness": ("care", r"(?:猫|貓).*(?:不吃|没精神|沒精神)|狗一直吐|宠物生病|寵物生病|宠物没精神|寵物沒精神|pet.*not eating|dog.*vomiting|cat.*lethargic|pet is sick",
        ("记录症状、开始时间、饮水排尿、可能接触的食物或毒物并联系兽医；不要自行喂人用药。呼吸困难、抽搐、腹胀、持续呕吐或疑似中毒要急诊。",
         "記錄症狀、開始時間、飲水排尿、可能接觸的食物或毒物並聯絡獸醫；不要自行餵人用藥。呼吸困難、抽搐、腹脹、持續嘔吐或疑似中毒要急診。",
         "Record symptoms, onset, drinking, urination, and possible food or toxin exposure and contact a vet. Do not give human medicine. Breathing trouble, seizures, bloating, repeated vomiting, or suspected poisoning needs emergency care.")),
    "earthquake": ("disaster", r"地震.*(?:来了|來了|发生|發生|怎么办|怎麼辦|应该|應該)|earthquake.*what|during an earthquake",
        ("正在摇晃时趴下、掩护、抓牢，远离玻璃和坠落物；不要冲向电梯或楼梯。停止后检查伤情和燃气，按官方指示撤离。",
         "正在搖晃時趴下、掩護、抓牢，遠離玻璃和墜落物；不要衝向電梯或樓梯。停止後檢查傷情和燃氣，依官方指示撤離。",
         "During shaking, drop, cover, and hold on, away from glass and falling objects. Do not rush to lifts or stairs. Afterwards check injuries and gas and follow official evacuation advice.")),
    "flood": ("disaster", r"洪水来了|洪水來了|家里进洪水|家裡進洪水|城市内涝|城市內澇|被洪水困住|积水.*(?:上涨|上漲|被困)|flood.*what|flash flood|home.*flooded",
        ("尽快前往更高且安全的地点并关注官方疏散信息；不要步行或驾车穿过流动积水，也不要触碰积水中的电器或电线。",
         "儘快前往較高且安全的地點並關注官方疏散資訊；不要步行或駕車穿過流動積水，也不要觸碰積水中的電器或電線。",
         "Move promptly to higher safe ground and follow official evacuation information. Never walk or drive through moving floodwater or touch electrical equipment or wires in water.")),
    "heatwave": ("disaster", r"高温中暑|高溫中暑|高温.*(?:意识|意識|迷糊|昏)|高溫.*(?:意識|迷糊|昏)|天气太热怎么办|天氣太熱怎麼辦|热浪|熱浪|heatwave|heat exhaustion|too hot outside",
        ("移到阴凉或空调处，补充水分并减少活动，关注老人、儿童和慢性病患者；意识混乱、昏厥或皮肤异常高热时立即联系急救并主动降温。",
         "移到陰涼或有空調處，補充水分並減少活動，關注長者、兒童和慢性病患者；意識混亂、昏厥或皮膚異常高熱時立即聯絡急救並主動降溫。",
         "Move to shade or air conditioning, hydrate, reduce activity, and check older people, children, and those with chronic illness. Confusion, fainting, or very hot skin requires emergency help and active cooling.")),
    "air_pollution": ("disaster", r"空气污染|空氣污染|雾霾.*(?:严重|嚴重|跑步)|霧霾.*(?:嚴重|跑步)|空气质量差|空氣品質差|air pollution|bad air quality|wildfire smoke",
        ("查看官方空气质量指数，污染严重时减少户外剧烈活动、关闭明显污染源进入的门窗并按需使用合适过滤；有呼吸系统疾病者遵循医生方案。",
         "查看官方空氣品質指標，污染嚴重時減少戶外劇烈活動、關閉明顯污染源進入的門窗並按需使用合適過濾；有呼吸系統疾病者遵循醫師方案。",
         "Check the official air-quality index, reduce strenuous outdoor activity, close openings when pollution is entering, and use suitable filtration if available. Follow the clinician's plan for respiratory disease.")),
    "moving_home": ("organisation", r"准备搬家|準備搬家|搬家.*(?:怎么安排|怎麼安排|清单|清單|整理|开始|開始)|moving house|moving checklist|prepare to move",
        ("按日期倒排：确认合同和搬运、先清理、按房间装箱并编号、单独准备证件药物充电器和首夜用品；拍照记录贵重物品与水电表。",
         "按日期倒排：確認契約和搬運、先清理、按房間裝箱並編號、單獨準備證件藥物充電器和首夜用品；拍照記錄貴重物品與水電表。",
         "Work backwards from the date: confirm contracts and transport, declutter, label boxes by room, keep documents, medicine, chargers, and first-night items separate, and photograph valuables and meters.")),
    "declutter": ("organisation", r"(?:房间|房間).*(?:太乱|太亂|乱到|亂到|收拾|整理)|东西太多怎么整理|東西太多怎麼整理|declutter|room is messy|organize my room",
        ("先选一个小区域，分成保留、移位、捐出、回收和丢弃五类；十五分钟结束时先处理垃圾和归位，不要一次把整屋物品全倒出来。",
         "先選一個小區域，分成保留、移位、捐出、回收和丟棄五類；十五分鐘結束時先處理垃圾和歸位，不要一次把整屋物品全倒出來。",
         "Choose one small area and sort into keep, relocate, donate, recycle, and discard. After 15 minutes, remove rubbish and put items away; do not empty the whole home at once.")),
    "lost_package": ("consumer", r"快递丢了|快遞丟了|包裹没收到|包裹沒收到|物流.*签收.*(?:没|沒).*(?:收到|拿到)|物流顯示簽收.*沒收到|package missing|parcel.*not arrived|marked delivered.*not received",
        ("先核对地址、门口、代收点和家人邻居，保存物流轨迹和通知；通过承运方及商家官方渠道提交查询，不要把验证码交给陌生来电者。",
         "先核對地址、門口、代收點和家人鄰居，保存物流軌跡和通知；透過承運方及商家官方管道提交查詢，不要把驗證碼交給陌生來電者。",
         "Check the address, entrance, collection point, household, and neighbours and save tracking notices. Open an enquiry through official carrier and seller channels, and never give verification codes to unsolicited callers.")),
}


GROUP_TEXT = {
    "home_safety": (
        ("先保证人员安全，再处理物品；让同住者知道危险和集合地点。", "先確保人員安全，再處理物品；讓同住者知道危險和集合地點。", "Protect people before property and tell others about the hazard and meeting point."),
        ("不要在不确定安全时自行抢修，也不要因取物返回危险区域。", "不要在不確定安全時自行搶修，也不要因取物返回危險區域。", "Do not attempt uncertain repairs or return to danger for belongings."),
        ("存在火焰、爆炸、浓烟、中毒或无法安全撤离时，立即联系当地紧急服务。", "存在火焰、爆炸、濃煙、中毒或無法安全撤離時，立即聯絡當地緊急服務。", "Call local emergency services for flame, explosion, heavy smoke, poisoning, or unsafe evacuation.")),
    "home": (
        ("拍照记录位置和损坏，记录开始时间，并准备物业、维修或公共服务账号信息。", "拍照記錄位置和損壞，記錄開始時間，並準備物業、維修或公共服務帳號資訊。", "Photograph the location and damage, note the start time, and prepare property, repair, or utility account details."),
        ("不要混用化学品、带电涉水或拆卸不熟悉的燃气和配电设备。", "不要混用化學品、帶電涉水或拆卸不熟悉的燃氣和配電設備。", "Do not mix chemicals, enter electrified water, or dismantle unfamiliar gas or electrical equipment."),
        ("危险持续、影响多户、出现焦味漏气或结构损坏时，联系物业、专业维修或紧急服务。", "危險持續、影響多戶、出現焦味漏氣或結構損壞時，聯絡物業、專業維修或緊急服務。", "Escalate to property management, qualified repair, or emergency services for continuing danger, multiple homes, burning smell, gas, or structural damage.")),
    "food": (
        ("记录食材、时间、温度和过敏信息；生熟分开，清洁双手和接触面。", "記錄食材、時間、溫度和過敏資訊；生熟分開，清潔雙手和接觸面。", "Track ingredients, time, temperature, and allergies; separate raw and cooked food and clean hands and surfaces."),
        ("不要靠闻味或尝一口判断高风险食物，也不要给过敏者试吃未知成分。", "不要靠聞味或試吃判斷高風險食物，也不要讓過敏者試吃未知成分。", "Do not rely on smell or tasting to judge high-risk food or give unknown ingredients to someone with an allergy."),
        ("疑似食物中毒、严重脱水、持续呕吐、血便或高风险人群不适时及时就医。", "疑似食物中毒、嚴重脫水、持續嘔吐、血便或高風險族群不適時及時就醫。", "Seek medical care for suspected poisoning, severe dehydration, repeated vomiting, blood in stool, or illness in a high-risk person.")),
    "health_urgent": (
        ("准备症状、开始时间、年龄、疾病、过敏和用药信息，并让他人知道情况。", "準備症狀、開始時間、年齡、疾病、過敏和用藥資訊，並讓他人知道情況。", "Prepare symptoms, onset, age, conditions, allergies, and medicines and alert another person."),
        ("不要等待模型诊断，也不要擅自使用他人的处方药。", "不要等待模型診斷，也不要擅自使用他人的處方藥。", "Do not wait for a model diagnosis or use someone else's prescription medicine."),
        ("呼吸、意识、严重出血或症状快速恶化时立即联系当地急救。", "呼吸、意識、嚴重出血或症狀快速惡化時立即聯絡當地急救。", "Call local emergency services immediately for breathing, consciousness, severe bleeding, or rapid deterioration.")),
    "health": (
        ("记录症状、时间、体温或伤口变化以及已经采取的措施。", "記錄症狀、時間、體溫或傷口變化以及已採取的措施。", "Record symptoms, timing, temperature or wound changes, and steps already taken."),
        ("不要自行加倍用药、混用不明药物或用偏方替代必要诊疗。", "不要自行加倍用藥、混用不明藥物或以偏方取代必要診療。", "Do not double doses, mix unknown medicines, or replace needed care with unverified remedies."),
        ("症状严重、持续或加重，或涉及婴幼儿、孕妇、老人和重大基础病时，联系医生或急救。", "症狀嚴重、持續或加重，或涉及嬰幼兒、孕婦、長者和重大基礎病時，聯絡醫師或急救。", "Contact a clinician or emergency service when symptoms are severe, persistent, worsening, or involve infants, pregnancy, older age, or major illness.")),
    "wellbeing": (
        ("把当前感受、触发因素、睡眠和能获得的支持写下来，只安排一个可执行的小步骤。", "把目前感受、觸發因素、睡眠和可取得的支持寫下來，只安排一個可執行的小步驟。", "Write down current feelings, triggers, sleep, and available support, then choose one manageable next step."),
        ("不要用酒精、危险行为或自行停药来应对，也不要因羞耻而完全隔离自己。", "不要用酒精、危險行為或自行停藥來應對，也不要因羞恥而完全孤立自己。", "Do not cope through alcohol, dangerous behaviour, or stopping medicine, and do not isolate yourself out of shame."),
        ("若出现自伤、伤人想法或无法保证安全，立即联系当地紧急服务、危机热线和可信的人。", "若出現自傷、傷人想法或無法確保安全，立即聯絡當地緊急服務、危機專線和可信任的人。", "For thoughts of harming yourself or others, or inability to stay safe, contact emergency services, a crisis line, and a trusted person now.")),
    "digital": (
        ("先保存资料，记录报错和发生时间，一次只改变一个设置，便于回退和定位。", "先儲存資料，記錄錯誤訊息和發生時間，一次只改變一個設定，便於還原和定位。", "Save data, record errors and timing, and change one setting at a time so changes can be reversed and diagnosed."),
        ("不要安装来路不明的修复工具、关闭安全功能或删除不认识的系统文件。", "不要安裝來源不明的修復工具、關閉安全功能或刪除不認識的系統檔案。", "Do not install unknown repair tools, disable security, or delete unfamiliar system files."),
        ("出现鼓包、焦味、进水、反复崩溃或数据不可替代时，停止操作并找官方支持或专业维修。", "出現膨脹、焦味、進水、反覆當機或資料不可取代時，停止操作並找官方支援或專業維修。", "Stop and seek official support or qualified repair for swelling, burning smell, liquid damage, repeated crashes, or irreplaceable data.")),
    "digital_security": (
        ("使用可信设备，保存安全通知、时间和异常活动，并先保护邮箱、手机号和支付账户。", "使用可信裝置，保存安全通知、時間和異常活動，並先保護信箱、手機號碼和支付帳戶。", "Use a trusted device, preserve alerts, timing, and abnormal activity, and secure email, phone, and payment accounts first."),
        ("不要向来电者提供验证码、屏幕共享、远程控制或恢复密钥。", "不要向來電者提供驗證碼、螢幕共享、遠端控制或復原金鑰。", "Never give unsolicited callers verification codes, screen sharing, remote control, or recovery keys."),
        ("涉及转账、身份盗用、勒索或持续控制时，联系银行、平台、运营商和当地警方。", "涉及轉帳、身分盜用、勒索或持續控制時，聯絡銀行、平台、電信業者和當地警方。", "For transfers, identity theft, extortion, or ongoing control, contact banks, platforms, the carrier, and local police.")),
    "travel": (
        ("保存订单和通知，使用承运方、政府或保险公司的官方应用和联系方式。", "保存訂單和通知，使用承運方、政府或保險公司的官方應用程式和聯絡方式。", "Keep bookings and notices and use official carrier, government, or insurer apps and contacts."),
        ("不要向搜索广告或陌生私信中的所谓客服付款或提供验证码。", "不要向搜尋廣告或陌生私訊中的所謂客服付款或提供驗證碼。", "Do not pay supposed support agents in search adverts or unsolicited messages or share verification codes."),
        ("证件丢失、人身危险、受困或官方要求疏散时，联系当地警察、紧急服务或使领馆。", "證件遺失、人身危險、受困或官方要求疏散時，聯絡當地警察、緊急服務或使領館。", "Contact police, emergency services, or a consulate for lost documents, danger, being stranded, or official evacuation.")),
    "money": (
        ("保留账单、合同和付款记录，先区分必要支出、债务最低还款和可调整支出。", "保留帳單、契約和付款記錄，先區分必要支出、債務最低還款和可調整支出。", "Keep bills, agreements, and payments and separate essentials, minimum debt payments, and adjustable spending."),
        ("不要用高成本新债填补日常缺口，也不要向陌生人支付解冻费或手续费。", "不要用高成本新債填補日常缺口，也不要向陌生人支付解凍費或手續費。", "Do not cover routine gaps with high-cost new debt or pay strangers release or processing fees."),
        ("无法保障住房、食物、医疗或最低还款时，尽早联系正规机构询问困难援助和债务咨询。", "無法保障住房、食物、醫療或最低還款時，儘早聯絡正規機構詢問困難援助和債務諮詢。", "If housing, food, health, or minimum payments are at risk, contact legitimate hardship and debt-advice services early.")),
    "study": (
        ("写明截止日期、可用时间和最小成果，每次学习后记录掌握点与下一步。", "寫明截止日期、可用時間和最小成果，每次學習後記錄掌握點與下一步。", "Write down the deadline, available time, and minimum outcome and record learned points and the next step after each session."),
        ("不要只反复阅读或制定过度精细的计划，应使用回忆、练习和反馈。", "不要只反覆閱讀或制定過度精細的計畫，應使用回憶、練習和回饋。", "Do not rely only on rereading or overplanning; use recall, practice, and feedback."),
        ("长期无法学习且伴随明显焦虑、失眠或功能受损时，联系老师、学校支持或专业人员。", "長期無法學習且伴隨明顯焦慮、失眠或功能受損時，聯絡老師、學校支援或專業人員。", "Seek support from a teacher, school service, or professional if persistent inability to study comes with significant anxiety, insomnia, or impairment.")),
    "work": (
        ("保存必要记录，明确事实、目标、对象和希望完成的时间，再选择合适的书面或当面沟通。", "保存必要記錄，明確事實、目標、對象和希望完成的時間，再選擇合適的書面或當面溝通。", "Keep necessary records, clarify facts, goal, audience, and desired timing, then choose written or face-to-face communication."),
        ("不要在人身攻击、情绪高涨或未核实事实时群发消息。", "不要在人身攻擊、情緒高漲或未核實事實時群發訊息。", "Do not broadcast messages while angry, attack people, or state unverified claims."),
        ("涉及安全、歧视、骚扰、报复或正式纪律程序时，保存记录并使用单位正式支持渠道。", "涉及安全、歧視、騷擾、報復或正式紀律程序時，保存記錄並使用單位正式支援管道。", "For safety, discrimination, harassment, retaliation, or formal discipline, preserve records and use formal workplace support channels.")),
    "care_urgent": (
        ("先确认人员安全、意识、呼吸、位置和事件时间，并请附近可信的人协助。", "先確認人員安全、意識、呼吸、位置和事件時間，並請附近可信任的人協助。", "Check safety, responsiveness, breathing, location, and event time and ask a trusted nearby person to assist."),
        ("不要因担心麻烦而拖延报警或急救，也不要强行移动疑似重伤者。", "不要因擔心麻煩而拖延報警或急救，也不要強行移動疑似重傷者。", "Do not delay police or emergency help out of embarrassment and do not force movement of a possibly seriously injured person."),
        ("人员失踪、意识异常、呼吸困难或疑似重伤时立即联系当地警察或急救。", "人員失蹤、意識異常、呼吸困難或疑似重傷時立即聯絡當地警察或急救。", "Call police or emergency medical services immediately for a missing person, altered consciousness, breathing difficulty, or suspected serious injury.")),
    "care": (
        ("记录对象、症状或需求、开始时间和已经采取的措施，并联系合适的照护或专业服务。", "記錄對象、症狀或需求、開始時間和已採取的措施，並聯絡合適的照護或專業服務。", "Record who is affected, symptoms or needs, onset, and actions taken and contact the appropriate care professional."),
        ("不要使用未经专业确认的人用药物、偏方或强制处理。", "不要使用未經專業確認的人用藥物、偏方或強制處理。", "Do not use human medicines, unverified remedies, or force without professional advice."),
        ("呼吸、意识、抽搐、中毒、持续恶化或无法安全照护时立即联系急救或专业服务。", "呼吸、意識、抽搐、中毒、持續惡化或無法安全照護時立即聯絡急救或專業服務。", "Seek emergency or professional help for breathing, consciousness, seizures, poisoning, continuing deterioration, or unsafe care.")),
    "disaster": (
        ("关注当地政府、气象和应急部门的官方信息，准备水、药、照明、充电和联系人。", "關注當地政府、氣象和應急部門的官方資訊，準備水、藥、照明、充電和聯絡人。", "Follow official local government, weather, and emergency information and prepare water, medicines, lighting, power, and contacts."),
        ("不要穿越封锁区、积水、倒塌物或带电区域，也不要传播未核实消息。", "不要穿越封鎖區、積水、倒塌物或帶電區域，也不要傳播未核實訊息。", "Do not cross closures, floodwater, debris, or electrified areas or spread unverified information."),
        ("收到撤离指令、住所不安全或有人受伤失联时，立即按官方路线撤离并联系紧急服务。", "收到撤離指令、住所不安全或有人受傷失聯時，立即依官方路線撤離並聯絡緊急服務。", "Evacuate by official routes and contact emergency services when ordered, when shelter is unsafe, or when someone is injured or missing.")),
    "organisation": (
        ("确定范围、截止时间和三件最重要的结果，用清单逐项完成并保留必要凭证。", "確定範圍、截止時間和三件最重要的結果，用清單逐項完成並保留必要憑證。", "Define scope, deadline, and the three most important outcomes, then use a checklist and keep necessary proof."),
        ("不要一次摊开所有任务或在未备份、未确认前丢弃重要文件和物品。", "不要一次攤開所有任務或在未備份、未確認前丟棄重要文件和物品。", "Do not expand every task at once or discard important documents and items before backup and confirmation."),
        ("涉及贵重物品、危险废物、重要证件或超出体力能力时，使用正规专业服务并请他人协助。", "涉及貴重物品、危險廢棄物、重要證件或超出體力能力時，使用正規專業服務並請他人協助。", "Use legitimate professional help for valuables, hazardous waste, key documents, or work beyond your physical capacity.")),
    "consumer": (
        ("保存订单、物流、付款和沟通记录，并只通过商家与承运方官方渠道查询。", "保存訂單、物流、付款和溝通記錄，並只透過商家與承運方官方管道查詢。", "Keep order, tracking, payment, and communication records and use only official seller and carrier channels."),
        ("不要提供验证码、远程控制设备或向所谓理赔人员先付款。", "不要提供驗證碼、遠端控制裝置或向所謂理賠人員先付款。", "Do not provide verification codes, remote device access, or advance payment to supposed claims agents."),
        ("确认丢失、出现异常扣款或官方渠道无法解决时，及时使用平台申诉、支付争议或消费者服务。", "確認遺失、出現異常扣款或官方管道無法解決時，及時使用平台申訴、支付爭議或消費者服務。", "Use platform disputes, payment disputes, or consumer services promptly for confirmed loss, abnormal charges, or unresolved official enquiries.")),
}


SOURCES = {
    "home_safety": ("中国消防", "https://www.119.gov.cn/"),
    "home": ("国家应急科普", "https://www.mem.gov.cn/"),
    "food": ("国家食品安全风险评估中心", "https://www.cfsa.net.cn/"),
    "health_urgent": ("国家卫生健康委员会", "https://www.nhc.gov.cn/"),
    "health": ("国家卫生健康委员会", "https://www.nhc.gov.cn/"),
    "wellbeing": ("世界卫生组织", "https://www.who.int/health-topics/mental-health"),
    "digital": ("国家互联网应急中心", "https://www.cert.org.cn/"),
    "digital_security": ("国家反诈中心/公安部", "https://www.mps.gov.cn/"),
    "travel": ("中国领事服务网", "https://cs.mfa.gov.cn/"),
    "money": ("国家金融监督管理总局金融消费者保护", "https://www.nfra.gov.cn/"),
    "study": ("LocalAI 学习流程模板", "local://daily-life/study"),
    "work": ("LocalAI 沟通流程模板", "local://daily-life/work"),
    "care_urgent": ("中国红十字会", "https://www.redcross.org.cn/"),
    "care": ("中国红十字会", "https://www.redcross.org.cn/"),
    "disaster": ("中华人民共和国应急管理部", "https://www.mem.gov.cn/"),
    "organisation": ("LocalAI 日常组织模板", "local://daily-life/organisation"),
    "consumer": ("全国12315平台", "https://www.12315.cn/"),
}


def classify_daily_life(text: str) -> str | None:
    for topic, (_, pattern, _) in GUIDES.items():
        if re.search(pattern, text, re.I):
            return topic
    return None


def looks_like_daily_life(text: str) -> bool:
    return classify_daily_life(text) is not None


def ensure_daily_life_guides(db_path: Path) -> int:
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS daily_life_guides(
      topic TEXT PRIMARY KEY,group_name TEXT NOT NULL,patterns TEXT NOT NULL,
      title_zh TEXT NOT NULL,title_hant TEXT NOT NULL,title_en TEXT NOT NULL,
      action_zh TEXT NOT NULL,action_hant TEXT NOT NULL,action_en TEXT NOT NULL,
      checklist_zh TEXT NOT NULL,checklist_hant TEXT NOT NULL,checklist_en TEXT NOT NULL,
      avoid_zh TEXT NOT NULL,avoid_hant TEXT NOT NULL,avoid_en TEXT NOT NULL,
      escalate_zh TEXT NOT NULL,escalate_hant TEXT NOT NULL,escalate_en TEXT NOT NULL,
      source TEXT NOT NULL,source_url TEXT NOT NULL,revision TEXT NOT NULL,confidence REAL NOT NULL)""")
    for topic, (group, pattern, action) in GUIDES.items():
        title = {
            "gas_smell":("疑似燃气泄漏","疑似燃氣洩漏","Suspected gas leak"), "home_fire":("住宅火情","住宅火情","Fire or smoke at home"),
            "power_outage":("家庭停电","家庭停電","Power outage"), "water_leak":("家庭漏水","家庭漏水","Water leak"),
            "clogged_drain":("排水堵塞","排水堵塞","Blocked drain"), "mold_damp":("潮湿和霉菌","潮濕和黴菌","Damp and mould"),
            "fridge_food":("冰箱故障与食物安全","冰箱故障與食物安全","Refrigerator and food safety"), "grease_fire":("油锅起火","油鍋起火","Grease fire"),
            "meal_planning":("三餐规划","三餐規劃","Meal planning"), "food_allergy":("疑似食物过敏","疑似食物過敏","Possible food allergy"),
            "urgent_symptoms":("紧急身体症状","緊急身體症狀","Emergency symptoms"), "minor_burn":("轻微烫伤","輕微燙傷","Minor burn"),
            "minor_cut":("轻微割伤","輕微割傷","Minor cut"), "fever":("发热处理","發熱處理","Fever"),
            "missed_medicine":("漏服药物","漏服藥物","Missed medicine"), "sleep_problem":("睡眠和作息","睡眠和作息","Sleep routine"),
            "stress_overload":("压力过载","壓力過載","Feeling overwhelmed"), "no_internet":("家庭网络故障","家庭網路故障","Internet outage"),
            "slow_computer":("电脑运行缓慢","電腦運作緩慢","Slow computer"), "lost_phone":("手机丢失","手機遺失","Lost phone"),
            "hacked_account":("账号疑似被盗","帳號疑似被盜","Compromised account"), "storage_full":("存储空间不足","儲存空間不足","Storage full"),
            "phone_not_charging":("手机无法充电","手機無法充電","Phone not charging"), "missed_transport":("错过交通班次","錯過交通班次","Missed transport"),
            "lost_documents":("证件或钱包丢失","證件或錢包遺失","Lost documents or wallet"), "bad_weather_trip":("恶劣天气出行","惡劣天氣出行","Severe-weather travel"),
            "budgeting":("日常预算","日常預算","Household budgeting"), "unexpected_bill":("异常或无力支付的账单","異常或無力支付的帳單","Unexpected or unaffordable bill"),
            "study_plan":("学习计划","學習計畫","Study plan"), "procrastination":("学习拖延","學習拖延","Procrastination"),
            "exam_anxiety":("考试紧张","考試緊張","Exam anxiety"), "work_email":("工作邮件","工作郵件","Work email"),
            "colleague_conflict":("同事沟通冲突","同事溝通衝突","Colleague conflict"), "job_interview":("面试准备","面試準備","Job interview preparation"),
            "lost_child":("儿童走失","兒童走失","Missing child"), "elderly_fall":("老人跌倒","長者跌倒","Older person has fallen"),
            "pet_illness":("宠物不适","寵物不適","Unwell pet"), "earthquake":("地震应对","地震應對","Earthquake safety"),
            "flood":("洪水与内涝","洪水與內澇","Flood safety"), "heatwave":("高温与中暑风险","高溫與中暑風險","Heatwave safety"),
            "air_pollution":("空气污染","空氣污染","Air pollution"), "moving_home":("搬家安排","搬家安排","Moving home"),
            "declutter":("整理房间","整理房間","Decluttering"), "lost_package":("包裹未收到","包裹未收到","Missing package"),
        }[topic]
        checklist, avoid, escalate = GROUP_TEXT[group]
        source, source_url = SOURCES[group]
        con.execute("INSERT OR REPLACE INTO daily_life_guides VALUES(" + ",".join("?" for _ in range(22)) + ")",
                    (topic, group, pattern, *title, *action, *checklist, *avoid, *escalate,
                     source, source_url, "2026-09", .84))
    con.commit()
    count = con.execute("SELECT count(*) FROM daily_life_guides").fetchone()[0]
    con.close()
    return count


def answer_daily_life(db_path: Path, text: str, language: str) -> str | None:
    topic = classify_daily_life(text)
    if topic is None:
        return None
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT * FROM daily_life_guides WHERE topic=?", (topic,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    con.close()
    # 正常发布数据库只读查询；仅在旧数据库缺表或缺记录时执行一次迁移。
    if row is None:
        ensure_daily_life_guides(db_path)
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT * FROM daily_life_guides WHERE topic=?", (topic,)).fetchone()
        con.close()
    offsets = {"zh":(3,6,9,12,15), "zh_hant":(4,7,10,13,16), "en":(5,8,11,14,17)}
    title_i, action_i, checklist_i, avoid_i, escalate_i = offsets[language]
    labels = {
        "zh":("现在先做","随后检查","不要这样做","何时升级处理","本地指南来源"),
        "zh_hant":("現在先做","隨後檢查","不要這樣做","何時升級處理","本機指南來源"),
        "en":("Do this now","Then check","Avoid","Escalate when","Local guide source"),
    }[language]
    return (f"{row[title_i]}\n"
            f"1. {labels[0]}：{row[action_i]}\n"
            f"2. {labels[1]}：{row[checklist_i]}\n"
            f"3. {labels[2]}：{row[avoid_i]}\n"
            f"4. {labels[3]}：{row[escalate_i]}\n"
            f"{labels[4]}：{row[18]} — {row[19]}；revision {row[20]}；confidence {row[21]:.0%}")


def daily_life_eval_cases():
    """与回复正文分离的自然改写题；每个场景至少一题。"""
    return [
        ("屋里闻到一股煤气味该干嘛","gas_smell","不要开关电器"), ("厨房油锅突然冒火了","grease_fire","不要泼水"),
        ("我们家忽然停电了要检查什么","power_outage","高功率电器"), ("天花板一直滴水怎么处理","water_leak","总水阀"),
        ("洗手池堵住完全不下水","clogged_drain","不要混合"), ("卧室墙角反复长霉","mold_damp","水源"),
        ("冰箱坏了一夜，肉还能不能吃","fridge_food","宁可丢弃"), ("这周不知道吃啥，帮我理一下","meal_planning","过敏"),
        ("吃完东西嘴唇突然肿起来","food_allergy","呼吸困难"), ("家人突然胸痛又喘不过气","urgent_symptoms","立即联系"),
        ("手背被开水轻微烫到了","minor_burn","流动凉水"), ("切菜时手指划了个小口","minor_cut","直接按压"),
        ("有点发烧在家先怎么观察","fever","记录体温"), ("今天忘了吃处方药怎么办","missed_medicine","不要自行加倍"),
        ("最近作息乱得完全睡不着","sleep_problem","固定起床"), ("手头事情太多感觉要撑不住","stress_overload","可以推迟"),
        ("全家设备突然都连不上WiFi","no_internet","路由器"), ("笔记本最近卡得特别厉害","slow_computer","一键加速"),
        ("我的手机找不到了该先做啥","lost_phone","丢失模式"), ("邮箱提示有陌生设备登录","hacked_account","多因素"),
        ("电脑硬盘已经一点空间都没有","storage_full","系统目录"), ("手机插线以后完全没反应","phone_not_charging","鼓包"),
        ("我没赶上今天这班高铁","missed_transport","官方"), ("在外地把身份证和钱包弄丢了","lost_documents","冻结银行卡"),
        ("明天暴雨但原计划要出门","bad_weather_trip","官方预警"), ("我每个月工资总是不够用","budgeting","固定支出"),
        ("这月电费账单高得离谱","unexpected_bill","要求明细"), ("一个月后考试怎么排复习","study_plan","主动回忆"),
        ("每次写作业都拖到最后一天","procrastination","两分钟"), ("一进考场我就特别慌","exam_anxiety","模拟题"),
        ("想礼貌地发邮件催一下项目进度","work_email","期望时间"), ("最近跟同组同事沟通总起冲突","colleague_conflict","事实"),
        ("明早有场面试今晚该准备什么","job_interview","具体经历"), ("商场里突然找不到孩子了","lost_child","当地警察"),
        ("家里老人摔倒后说背很疼","elderly_fall","不要强行扶起"), ("猫两天没吃东西也没精神","pet_illness","兽医"),
        ("刚开始地震我现在应该怎么办","earthquake","趴下"), ("路面积水上涨快被困住了","flood","更高"),
        ("高温天家人开始意识迷糊","heatwave","立即联系急救"), ("今天雾霾很严重还能跑步吗","air_pollution","空气质量指数"),
        ("下个月要搬家从哪里开始整理","moving_home","首夜用品"), ("房间乱到不知道从哪儿下手","declutter","小区域"),
        ("物流显示签收但我根本没拿到","lost_package","验证码"),
        ("My account shows a login I do not recognize.","hacked_account","MFA"),
        ("My laptop has become extremely slow.","slow_computer","optimiser"),
        ("I missed my flight this morning.","missed_transport","official"),
        ("How should I prepare for tomorrow's interview?","job_interview","concrete examples"),
        ("There is smoke in my apartment.","home_fire","Evacuate"),
        ("牆角一直反覆發霉怎麼處理","mold_damp","水源"),
        ("我的手機完全充不進電","phone_not_charging","膨脹"),
        ("明天有颱風但我需要出門","bad_weather_trip","官方預警"),
        ("考試時我總是緊張到腦袋空白","exam_anxiety","模擬題"),
    ]
