#!/usr/bin/env python3
"""生活化法律办事指南：只提供分流、证据与合法程序，不预测个案结论。"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


TOPICS = {
 "dismissal": {
  "patterns":r"被.*?(?:辞退|辭退|开除|開除|裁员|裁員|解雇)|(?:公司|老板|老闆|雇主|僱主).*(?:辞退|辭退|开除|開除|裁员|裁員|解雇|让我走|讓我走|叫我离职|叫我離職|要求离职|要求離職)|wrongful termination|fired|laid off",
  "title":("被辞退或裁员","被辭退或裁員","Dismissal or layoff"),
  "immediate":("不要急着签署空白文件、倒签文件或写成“自愿离职”；先索取书面解除理由、日期及结算清单。","不要急著簽署空白文件、倒簽文件或寫成「自願離職」；先索取書面解除理由、日期及結算清單。","Do not sign blank, backdated, or inaccurate resignation papers. Ask for the reason, effective date, and final-pay calculation in writing."),
  "evidence":("保存合同、员工手册、绩效记录、通知、聊天、工资与社保记录，并写下事件时间线。","保存契約、員工手冊、績效記錄、通知、聊天、薪資與社保記錄，並寫下事件時間線。","Preserve the contract, handbook, performance records, notices, messages, pay and benefit records, and write a timeline."),"group":"employment"},
 "overtime": {
  "patterns":r"加班(?:费|費)?.*(?:不给|不給|没发|沒發)|加班.*(?:没有|沒有|未付|未支付|没算|沒算).*(?:加班费|加班費)|强制加班|強制加班|overtime.*(?:unpaid|not paid)|(?:did )?not pay.*overtime|unpaid overtime",
  "title":("加班和加班费争议","加班和加班費爭議","Overtime and overtime-pay dispute"),
  "immediate":("按日期记录实际上下班、休息、工作内容及谁安排了加班，不要篡改公司的原始记录。","按日期記錄實際上下班、休息、工作內容及誰安排了加班，不要竄改公司的原始記錄。","Record actual start, end, break times, tasks, and who required the overtime. Do not alter original employer records."),
  "evidence":("保存考勤、排班、打卡、门禁、邮件、工作聊天、任务提交时间、工资条和银行流水。","保存考勤、排班、打卡、門禁、電郵、工作聊天、任務提交時間、薪資單和銀行流水。","Preserve attendance records, schedules, timecards, access records, email, work messages, task timestamps, payslips, and bank records."),"group":"employment"},
 "work_injury": {
  "patterns":r"工伤|工傷|上班.*(?:受伤|受傷|摔伤|摔傷)|工作.*(?:受伤|受傷|摔伤|摔傷)|work(?:place)? injury|injured at work",
  "title":("工伤或工作场所受伤","工傷或工作場所受傷","Workplace injury"),
  "immediate":("先就医并如实说明受伤时间、地点和经过；有持续危险时离开危险区域并联系紧急服务。尽快书面通知单位。","先就醫並如實說明受傷時間、地點和經過；有持續危險時離開危險區域並聯絡緊急服務。儘快書面通知單位。","Seek medical care, accurately state when, where, and how the injury occurred, leave any continuing danger, and promptly notify the employer in writing."),
  "evidence":("保存病历、诊断、票据、事故现场照片、监控线索、证人、排班和单位报告；不要为了索赔夸大病情。","保存病歷、診斷、票據、事故現場照片、監控線索、證人、排班和單位報告；不要為了索賠誇大病情。","Preserve medical records and bills, scene photos, video leads, witness details, schedules, and employer reports. Do not exaggerate symptoms."),"group":"employment"},
 "consumer": {
  "patterns":r"商家.*不(?:退|换|換)|(?:店家|卖家|賣家).*(?:拒绝退钱|拒絕退錢|不退款)|退款.*不给|退款.*不給|商品.*(?:坏|壞|故障|瑕疵|假货|假貨)|(?:网购|網購).*(?:坏|壞|故障|瑕疵|退货|退貨)|defective (?:product|appliance)|merchant.*refund|seller.*refund|consumer complaint",
  "title":("退款、瑕疵商品或消费争议","退款、瑕疵商品或消費爭議","Refund, defective product, or consumer dispute"),
  "immediate":("先停止继续使用可能不安全的商品；在退货期限或平台申诉期限内用书面方式提出明确诉求。","先停止繼續使用可能不安全的商品；在退貨期限或平台申訴期限內用書面方式提出明確訴求。","Stop using any potentially unsafe product and make a clear written request within applicable return or platform-dispute periods."),
  "evidence":("保存订单、付款记录、广告和商品页面截图、条款、发票、开箱及瑕疵照片、客服记录和物流凭证。","保存訂單、付款記錄、廣告和商品頁面截圖、條款、發票、開箱及瑕疵照片、客服記錄和物流憑證。","Preserve the order, payment record, advert and listing screenshots, terms, receipt, unboxing and defect photos, support messages, and shipping proof."),"group":"consumer"},
 "fraud": {
  "patterns":r"被(?:骗|騙)|诈骗|詐騙|刷单|刷單|杀猪盘|殺豬盤|冒充客服|scam|fraud|phishing|stolen money",
  "title":("诈骗或可疑转账","詐騙或可疑轉帳","Fraud or suspicious transfer"),
  "immediate":("立即停止转账和联系骗子；联系银行、支付平台或卡机构尝试止付、冻结或争议交易。若设备或账号可能被控制，先用安全设备改密码并启用多因素验证。","立即停止轉帳和聯絡騙徒；聯絡銀行、支付平台或發卡機構嘗試止付、凍結或爭議交易。若裝置或帳號可能被控制，先用安全裝置更改密碼並啟用多因素驗證。","Stop sending money and contacting the scammer. Contact the bank, payment platform, or card issuer to seek a hold, freeze, or dispute. If an account or device may be compromised, use a safe device to change passwords and enable MFA."),
  "evidence":("保存账号、电话号码、网址、聊天、转账凭证、订单、邮件原文和时间线；不要删除记录，也不要再付所谓“解冻费”或“追回费”。","保存帳號、電話號碼、網址、聊天、轉帳憑證、訂單、電郵原文和時間線；不要刪除記錄，也不要再付所謂「解凍費」或「追回費」。","Preserve account names, phone numbers, URLs, chats, transfer receipts, orders, original emails, and a timeline. Do not pay a supposed release or recovery fee."),"group":"crime"},
 "rental": {
  "patterns":r"房东|房東|房租|租房|租屋|押金|保证金|保證金|驱逐|驅逐|房屋.*维修|房屋.*維修|landlord|tenant|security deposit|eviction|rent dispute",
  "title":("租房、押金、维修或驱逐争议","租屋、押金、修繕或驅逐爭議","Rent, deposit, repair, or eviction dispute"),
  "immediate":("查看租约和当地规则，以可留痕方式列明押金、维修或终止租约的事实与诉求。不要自行换锁、停水停电、扣押财物或以冲突方式处理。","查看租約和當地規則，以可留痕方式列明押金、修繕或終止租約的事實與訴求。不要自行換鎖、停水停電、扣押財物或以衝突方式處理。","Review the lease and local rules, then state the deposit, repair, or termination issue in a traceable notice. Do not use self-help lockouts, utility shutoffs, property seizure, or confrontation."),
  "evidence":("保存租约、付款和押金凭证、入住退租照片、维修通知、检查报告、沟通记录和所有法院或房东通知。","保存租約、付款和押金憑證、入住退租照片、修繕通知、檢查報告、溝通記錄和所有法院或房東通知。","Preserve the lease, rent and deposit proof, move-in/out photos, repair notices, inspection reports, communications, and every landlord or court notice."),"group":"housing"},
 "debt": {
  "patterns":r"借钱.*不还|借錢.*不還|欠钱.*不还|欠錢.*不還|欠款|催收|讨债|討債|债务|債務|debt collector|owe me money|loan.*not repaid|unpaid loan",
  "title":("借款、欠款或催收争议","借款、欠款或催收爭議","Loan, debt, or collection dispute"),
  "immediate":("先核对债权人、债务人、金额、到期日、利息和已还款。用书面方式要求对方确认或提供债务明细；不要威胁、骚扰、公开隐私或擅自拿走财物。","先核對債權人、債務人、金額、到期日、利息和已還款。以書面方式要求對方確認或提供債務明細；不要威脅、騷擾、公開隱私或擅自拿走財物。","Verify the creditor, debtor, amount, due date, interest, and payments. Request or provide an itemised written statement. Do not threaten, harass, expose private information, or seize property yourself."),
  "evidence":("保存借据、合同、转账、还款、聊天、催告及对方承认债务的合法记录；警惕用新借款支付“解冻费”的骗局。","保存借據、契約、轉帳、還款、聊天、催告及對方承認債務的合法記錄；警惕以新借款支付「解凍費」的騙局。","Preserve notes, contracts, transfers, repayments, messages, demands, and lawful acknowledgments of the debt. Beware of demands for a new payment to release funds."),"group":"civil"},
 "traffic": {
  "patterns":r"交通事故|车祸|車禍|撞车|撞車|被车撞|被車撞|肇事|hit by (?:a )?car|car accident|traffic accident|vehicle collision",
  "title":("交通事故处理","交通事故處理","Traffic accident"),
  "immediate":("先确保人身安全；有人受伤、道路仍危险或对方逃逸时立即联系当地警察和医疗急救。不要为逃避责任离开现场，也不要阻碍救援。","先確保人身安全；有人受傷、道路仍危險或對方逃逸時立即聯絡當地警察和醫療急救。不要為逃避責任離開現場，也不要阻礙救援。","Prioritise safety. Contact local police and emergency medical services if anyone is injured, the road remains dangerous, or a driver leaves. Do not leave to evade responsibility or obstruct aid."),
  "evidence":("在安全前提下拍摄车辆位置、损伤、路况、标志和车牌，交换身份、联系方式和保险资料，记录证人并及时通知保险公司。陈述事实，不猜测或伪造。","在安全前提下拍攝車輛位置、損傷、路況、標誌和車牌，交換身分、聯絡方式和保險資料，記錄證人並及時通知保險公司。陳述事實，不猜測或偽造。","When safe, photograph positions, damage, road conditions, signs, and plates; exchange identity, contact, and insurance details; record witnesses; and notify the insurer. State facts without guessing or fabrication."),"group":"safety"},
 "domestic_violence": {
  "patterns":r"家暴|家庭暴力|家人.*(?:暴力|威胁|威脅|打)|伴侣.*(?:打|威胁|威脅|控制)|配偶.*(?:打|威胁|威脅)|被家人打|domestic violence|partner.*abuse|spouse.*abuse|family abuse",
  "title":("家庭暴力或人身威胁","家庭暴力或人身威脅","Domestic violence or personal threat"),
  "immediate":("如果有立即危险，先到安全地点并联系当地紧急服务；不要为了取证返回危险现场。可在安全设备上联系可信的人，准备证件、药物、钥匙和紧急联系方式。","如果有立即危險，先到安全地點並聯絡當地緊急服務；不要為了取證返回危險現場。可在安全裝置上聯絡可信的人，準備證件、藥物、鑰匙和緊急聯絡方式。","If danger is immediate, go to a safe place and contact local emergency services. Do not return to danger just to collect evidence. On a safe device, contact a trusted person and prepare ID, medication, keys, and emergency contacts."),
  "evidence":("在安全前提下保存伤情照片、就医记录、威胁信息、报警或求助记录和证人资料；注意对方可能监控手机或账号。","在安全前提下保存傷情照片、就醫記錄、威脅訊息、報警或求助記錄和證人資料；注意對方可能監控手機或帳號。","When safe, preserve injury photos, medical records, threats, police or help records, and witnesses. Consider whether the other person monitors the device or account."),"group":"safety"},
 "privacy": {
  "patterns":r"个人信息.*泄露|個人(?:資訊|資料).*(?:外洩|泄露)|隐私.*泄露|隱私.*外洩|偷拍|人肉|盗用.*身份|盜用.*身分|data breach|privacy violation|identity theft|personal (?:data|information).*(?:leak|breach)|data (?:was )?leaked",
  "title":("个人信息、隐私或身份盗用","個人資料、隱私或身分盜用","Personal data, privacy, or identity theft"),
  "immediate":("更改受影响账户的密码并启用多因素验证；联系银行或平台冻结异常操作。涉及证件或金融身份盗用时尽快报警并向相关机构挂失。","更改受影響帳戶的密碼並啟用多因素驗證；聯絡銀行或平台凍結異常操作。涉及證件或金融身分盜用時儘快報警並向相關機構掛失。","Change affected passwords, enable MFA, and contact banks or platforms to stop abnormal activity. Promptly report stolen identity or financial credentials to police and relevant issuers."),
  "evidence":("保存泄露通知、网页和账号截图、登录提醒、邮件原文、交易记录、向平台提出删除或更正请求的记录；不要传播泄露内容。","保存外洩通知、網頁和帳號截圖、登入提醒、電郵原文、交易記錄、向平台提出刪除或更正請求的記錄；不要傳播外洩內容。","Preserve breach notices, page and account screenshots, login alerts, original emails, transactions, and deletion or correction requests. Do not further distribute leaked material."),"group":"consumer"},
 "discrimination": {
  "patterns":r"歧视|歧視|性骚扰|性騷擾|职场骚扰|職場騷擾|就业.*不公平|就業.*不公平|(?:因为|因為|因).*(?:性别|性別|年龄|年齡|残疾|殘疾).*(?:拒绝录用|拒絕錄用|不录用|不錄用)|discrimination|sexual harassment|workplace harassment",
  "title":("就业歧视或职场骚扰","就業歧視或職場騷擾","Employment discrimination or workplace harassment"),
  "immediate":("明确记录每次行为的日期、地点、言辞、参与者、证人和对工作造成的影响；如有人身危险，先联系紧急服务。不要以违法录音、侵入账号或公开他人隐私取证。","明確記錄每次行為的日期、地點、言辭、參與者、證人和對工作造成的影響；如有人身危險，先聯絡緊急服務。不要以違法錄音、侵入帳號或公開他人隱私取證。","Record each event's date, place, words or conduct, participants, witnesses, and work impact. If safety is threatened, contact emergency services. Do not use unlawful recording, account intrusion, or public exposure as evidence gathering."),
  "evidence":("保存政策、招聘或人事决定、绩效材料、排班、投诉和回复、邮件及聊天；书面要求公司保存相关监控和记录。","保存政策、招聘或人事決定、績效材料、排班、申訴和回覆、電郵及聊天；書面要求公司保存相關監控和記錄。","Preserve policies, hiring or personnel decisions, performance material, schedules, complaints and responses, emails, and messages; request preservation of relevant video and records in writing."),"group":"employment"},
 "medical": {
  "patterns":r"医疗事故|醫療事故|医疗过错|醫療過錯|医疗处置.*(?:问题|問題)|醫療處置.*問題|看病.*(?:过错|過錯|出问题|出問題)|误诊|誤診|漏诊|漏診|手术.*出问题|手術.*出問題|医院.*投诉|醫院.*申訴|medical malpractice|misdiagnos|hospital complaint",
  "title":("医疗服务或医疗损害争议","醫療服務或醫療損害爭議","Medical-service or medical-harm dispute"),
  "immediate":("健康风险优先：症状紧急或恶化时立即就医或求助急救，不要为了纠纷取证而延误治疗。可寻求独立医生的复诊意见。","健康風險優先：症狀緊急或惡化時立即就醫或求助急救，不要為了爭議取證而延誤治療。可尋求獨立醫師的複診意見。","Health comes first: seek urgent or emergency care if symptoms are severe or worsening. Do not delay treatment to gather evidence. Consider an independent medical review."),
  "evidence":("依法申请并保存完整病历、检查影像和报告、知情同意材料、处方、费用票据、沟通记录及后续诊断；不要修改医疗资料。","依法申請並保存完整病歷、檢查影像和報告、知情同意資料、處方、費用票據、溝通記錄及後續診斷；不要修改醫療資料。","Lawfully request and preserve the complete medical record, imaging and reports, consent material, prescriptions, bills, communications, and later diagnoses. Do not alter medical records."),"group":"health"},
 "copyright": {
  "patterns":r"抄袭|抄襲|盗用.*作品|盜用.*作品|(?:原创|原創|文章|图片|圖片|作品).*(?:盗用|盜用|抄走|复制|複製)|图片.*侵权|圖片.*侵權|版权侵权|版權侵權|著作权侵权|著作權侵權|copyright infringement|stole my (?:photo|work|video)|my work was copied",
  "title":("作品被抄袭或版权争议","作品被抄襲或版權爭議","Copyright or copied-work dispute"),
  "immediate":("先保存原始创作文件、发布时间、版本历史和侵权页面，不要用威胁、攻击网站或公开他人隐私的方式维权。发送平台通知前确认自己确实拥有相关权利。","先保存原始創作檔案、發布時間、版本歷史和侵權頁面，不要以威脅、攻擊網站或公開他人隱私的方式維權。發送平台通知前確認自己確實擁有相關權利。","Preserve source files, publication dates, version history, and the allegedly infringing page. Do not threaten, attack systems, or expose private data. Confirm ownership before sending a platform notice."),
  "evidence":("保存带时间信息的源文件、草稿、项目记录、登记或合同、网页存档、链接和传播范围；区分事实、表达、授权和合理使用等问题。","保存帶時間資訊的原始檔、草稿、專案記錄、登記或契約、網頁存檔、連結和傳播範圍；區分事實、表達、授權和合理使用等問題。","Preserve timestamped source files, drafts, project history, registrations or contracts, archived pages, links, and distribution evidence; distinguish facts, expression, licences, and possible exceptions."),"group":"civil"},
 "contract": {
  "patterns":r"合同.*违约|合約.*違約|契約.*違約|(?:合同|合約|契约|契約).*(?:不履行|拒绝履行|拒絕履行|到期.*不)|对方.*不履约|對方.*不履約|breach of contract|contract.*(?:not perform|breach)",
  "title":("合同违约或不履行","契約違約或不履行","Breach or non-performance of contract"),
  "immediate":("先核对合同主体、义务、履行期限、违约条款、通知方式、管辖和仲裁条款。书面说明违约事实、希望的补救及合理期限。不要采取扣押财物、威胁或破坏等私力报复。","先核對契約主體、義務、履行期限、違約條款、通知方式、管轄和仲裁條款。書面說明違約事實、希望的補救及合理期限。不要採取扣押財物、威脅或破壞等私力報復。","Check the parties, duties, due dates, breach terms, notice method, forum, and arbitration clause. Give written notice of the breach, requested remedy, and reasonable deadline. Do not use threats, damage, or self-help seizure."),
  "evidence":("保存签署版本、附件、订单、交付和验收、发票付款、变更、催告、损失及减损措施的记录。","保存簽署版本、附件、訂單、交付和驗收、發票付款、變更、催告、損失及減損措施的記錄。","Preserve signed versions, attachments, orders, delivery and acceptance, invoices and payments, amendments, demands, loss evidence, and mitigation steps."),"group":"civil"},
 "family": {
  "patterns":r"离婚|離婚|抚养权|撫養權|监护权|監護權|赡养费|贍養費|child custody|divorce|child support",
  "title":("离婚、子女或家庭关系争议","離婚、子女或家庭關係爭議","Divorce, children, or family dispute"),
  "immediate":("如有暴力、威胁或儿童安全风险，先采用安全求助路径。不要隐匿或毁坏共同财产、伪造债务、教唆孩子作证或违反现有法院命令。","如有暴力、威脅或兒童安全風險，先採用安全求助途徑。不要隱匿或毀壞共同財產、偽造債務、教唆孩子作證或違反現有法院命令。","If violence, threats, or child-safety risks exist, use the safety route first. Do not conceal or destroy shared assets, fabricate debt, coach a child as a witness, or violate an existing court order."),
  "evidence":("整理身份和婚姻文件、子女照护安排、财产债务、收入支出、重要沟通和现有协议或裁定；以儿童利益和安全为优先。","整理身分和婚姻文件、子女照護安排、財產債務、收入支出、重要溝通和現有協議或裁定；以兒童利益和安全為優先。","Organise identity and marriage documents, childcare arrangements, assets and debts, income and expenses, key communications, and existing agreements or orders, prioritising child welfare and safety."),"group":"family"},
}


REGIONS = {
 "CN": {
  "name":("中国大陆","中國大陸","mainland China"),
  "routes":{
   "employment":("向工作地人力资源社会保障部门、劳动监察或劳动争议调解仲裁机构咨询；地方实施和期限需要核对。","向工作地人力資源社會保障部門、勞動監察或勞動爭議調解仲裁機構諮詢；地方實施和期限需要核對。","Contact the labour administration, labour inspection, or labour-dispute mediation/arbitration body where the work occurred; verify local rules and deadlines."),
   "consumer":("先与经营者和平台书面协商；不成可通过全国12315平台或当地市场监管部门投诉，也可请求消费者组织调解。","先與經營者和平台書面協商；不成可透過全國12315平台或當地市場監管部門投訴，也可請求消費者組織調解。","First complain in writing to the seller and platform; if unresolved, use the national 12315 platform or local market regulator, or request consumer mediation."),
   "crime":("尽快联系银行或平台并向公安机关报案；紧急危险拨110。不要相信收费“追回资金”的陌生人。","儘快聯絡銀行或平台並向公安機關報案；緊急危險撥110。不要相信收費「追回資金」的陌生人。","Contact the bank or platform and report to public security promptly; call 110 for immediate danger. Do not trust strangers charging to recover funds."),
   "housing":("租赁规则和受理机关有明显城市差异；向房屋所在地住房城乡建设（住建）/住房主管部门、人民调解或12348公共法律服务确认，再按合同选择调解、仲裁或诉讼。","租賃規則和受理機關有明顯城市差異；向房屋所在地住房城鄉建設（住建）／住房主管部門、人民調解或12348公共法律服務確認，再按契約選擇調解、仲裁或訴訟。","Rental rules vary by city. Check with the local housing authority, people's mediation, or 12348 public legal service, then use mediation, agreed arbitration, or litigation as applicable."),
   "safety":("有伤者联系120；需要现场处置或存在人身危险联系110，交通事故也可按当地规则联系交警。后续向保险、调解或有管辖权机关处理。","有傷者聯絡120；需要現場處置或存在人身危險聯絡110，交通事故亦可按當地規則聯絡交警。後續向保險、調解或有管轄權機關處理。","Call 120 for medical emergency and 110 for police response or immediate danger; follow local traffic-police procedures. Then use insurance, mediation, or the competent authority."),
   "health":("先向医疗机构投诉管理部门反映并依法复制病历；可向当地卫生健康主管部门咨询医疗纠纷调解、行政投诉及鉴定渠道。","先向醫療機構申訴管理部門反映並依法複製病歷；可向當地衛生健康主管部門諮詢醫療糾紛調解、行政申訴及鑑定途徑。","Complain to the provider and lawfully obtain records, then ask the local health authority about medical-dispute mediation, administrative complaints, and expert review."),
   "civil":("先书面催告并查看合同中的仲裁和管辖条款；可使用人民调解、12348公共法律服务、约定仲裁或有管辖权法院。","先書面催告並查看契約中的仲裁和管轄條款；可使用人民調解、12348公共法律服務、約定仲裁或有管轄權法院。","Give written notice and check arbitration and forum clauses; consider people's mediation, 12348 public legal service, agreed arbitration, or the competent court."),
   "family":("向当地婚姻登记、妇联、民政、人民调解、12348或有管辖权法院确认程序；涉及家庭暴力时优先报警并询问人身安全保护令。","向當地婚姻登記、婦聯、民政、人民調解、12348或有管轄權法院確認程序；涉及家庭暴力時優先報警並詢問人身安全保護令。","Check procedure with local civil-affairs, women's services, mediation, 12348, or the competent court; for domestic violence, prioritise police and ask about a personal-safety protection order.")},
  "sources":[("中国法律服务网/12348","https://www.12348.gov.cn/"),("全国12315平台","https://www.12315.cn/")]},
 "US-FED": {
  "name":("美国（联邦基线）","美國（聯邦基線）","United States (federal baseline)"),
  "routes":{
   "employment":("联邦机关只处理其权限内事项；根据问题联系DOL、OSHA或EEOC，并同时核对工作所在州和城市的劳动机关与更有利规则。","聯邦機關只處理其權限內事項；依問題聯絡DOL、OSHA或EEOC，並同時核對工作所在州和城市的勞動機關與更有利規則。","Federal agencies cover only matters within their authority. Depending on the issue, contact DOL, OSHA, or EEOC and also check the state and city labour agency and more protective rules."),
   "consumer":("先向商家和支付机构书面投诉；通过USA.gov查找对应投诉机关和州消费者保护办公室，诈骗或欺骗性商业行为可向FTC报告。","先向商家和支付機構書面申訴；透過USA.gov查找對應申訴機關和州消費者保護辦公室，詐騙或欺騙性商業行為可向FTC舉報。","Complain in writing to the business and payment provider; use USA.gov to identify the correct regulator and state consumer office, and report scams or deceptive conduct to the FTC."),
   "crime":("立即联系金融机构；向当地警察报案。诈骗可向ReportFraud.ftc.gov报告，网络犯罪还可向FBI IC3报告。","立即聯絡金融機構；向當地警察報案。詐騙可向ReportFraud.ftc.gov舉報，網路犯罪亦可向FBI IC3舉報。","Immediately contact financial institutions and local police. Report fraud at ReportFraud.ftc.gov and internet crime to FBI IC3."),
   "housing":("租客权利高度依州、市而异；通过USA.gov查找当地住房投诉和法律援助。住房歧视可向HUD/FHEO投诉。","租客權利高度依州、市而異；透過USA.gov查找當地住房申訴和法律扶助。住房歧視可向HUD/FHEO申訴。","Tenant rights vary by state and city. Use USA.gov to locate local housing complaints and legal aid. Housing discrimination may be reported to HUD/FHEO."),
   "safety":("紧急危险或严重受伤拨911；按州法联系警察、医疗和保险。家庭暴力可联系当地服务或美国全国反家暴热线。","緊急危險或嚴重受傷撥911；按州法聯絡警察、醫療和保險。家庭暴力可聯絡當地服務或美國全國反家暴熱線。","Call 911 for immediate danger or serious injury and follow state police, medical, and insurance rules. For domestic violence, contact local services or the National Domestic Violence Hotline."),
   "health":("先取得病历并使用医院投诉程序；执业、过失和诉讼规则主要由州法决定，应联系州医疗委员会、保险机构或当地律师。","先取得病歷並使用醫院申訴程序；執業、過失和訴訟規則主要由州法決定，應聯絡州醫療委員會、保險機構或當地律師。","Obtain records and use the provider complaint process. Licensing, malpractice, and litigation are mainly state-law matters; contact the state medical board, insurer, or local counsel."),
   "civil":("合同、债务和多数私人纠纷主要适用州法；先书面通知，再通过州消费者机关、CFPB（适用金融/催收时）、调解、小额法庭或当地法律援助选择路径。","契約、債務和多數私人爭議主要適用州法；先書面通知，再透過州消費者機關、CFPB（適用金融／催收時）、調解、小額法庭或當地法律扶助選擇途徑。","Contract, debt, and most private disputes are mainly state-law matters. Give written notice, then consider the state consumer office, CFPB for covered financial collection, mediation, small-claims court, or local legal aid."),
   "family":("离婚、监护和抚养主要由州法院处理；查工作或居住州的家庭法院规则及法律援助。涉及暴力时先使用911和当地保护服务。","離婚、監護和扶養主要由州法院處理；查工作或居住州的家事法院規則及法律扶助。涉及暴力時先使用911和當地保護服務。","Divorce, custody, and support are primarily state-court matters. Check the relevant family-court rules and legal aid. For violence, prioritise 911 and local protective services.")},
  "sources":[("USAGov complaints","https://www.usa.gov/complaints"),("USAGov legal aid","https://www.usa.gov/legal-aid"),("FTC ReportFraud","https://reportfraud.ftc.gov/")]},
 "TW": {
  "name":("中国台湾地区","中國臺灣地區","Taiwan"),
  "routes":{
   "employment":("向劳务提供地的地方劳动主管机关咨询、申诉/劳检或申请免费劳资争议调解；也可评估仲裁、诉讼及劳动法律扶助。","向勞務提供地的地方勞動主管機關諮詢、申訴／勞檢或申請免費勞資爭議調解；亦可評估仲裁、訴訟及勞動法律扶助。","Contact the local labour authority where work was performed for advice, inspection/complaint, or free mediation, and assess arbitration, litigation, and labour legal aid."),
   "consumer":("先向业者和平台申诉；可拨1950消费者服务专线咨询，或通过行政院消费者保护会在线申诉系统提出申诉。","先向業者和平台申訴；可撥1950消費者服務專線諮詢，或透過行政院消費者保護會線上申訴系統提出申訴。","Complain first to the business and platform, call the 1950 consumer line, or use the Executive Yuan consumer online complaint system."),
   "crime":("立即联系银行或平台并拨110报案；诈骗咨询和通报可使用165反诈骗专线/平台。不要再付任何追回费。","立即聯絡銀行或平台並撥110報案；詐騙諮詢和通報可使用165反詐騙專線／平台。不要再付任何追回費。","Immediately contact the bank or platform and call 110; use the 165 anti-fraud service for scam advice and reporting. Do not pay a recovery fee."),
   "housing":("查看租约和住宅租赁定型化契约规则；可向所在地地方政府租赁/地政或消费者机关咨询，并使用调解、法律扶助或法院程序。","查看租約和住宅租賃定型化契約規則；可向所在地地方政府租賃／地政或消費者機關諮詢，並使用調解、法律扶助或法院程序。","Review the lease and mandatory residential-lease terms; contact local rental/land administration or consumer services, then use mediation, legal aid, or court as appropriate."),
   "safety":("紧急危险拨110，医疗急救拨119；家庭暴力、性侵害或性骚扰可拨24小时113保护专线。交通事故按警方和保险程序处理。","緊急危險撥110，醫療急救撥119；家庭暴力、性侵害或性騷擾可撥24小時113保護專線。交通事故按警方和保險程序處理。","Call 110 for immediate danger and 119 for medical emergency. The 24-hour 113 service covers domestic violence, sexual assault, and harassment. Follow police and insurance procedure for traffic accidents."),
   "health":("先取得病历并向医疗机构反映；可向所在地卫生局咨询医疗争议调解和行政申诉，必要时寻求法律扶助或专业评估。","先取得病歷並向醫療機構反映；可向所在地衛生局諮詢醫療爭議調解和行政申訴，必要時尋求法律扶助或專業評估。","Obtain records and complain to the provider, then ask the local health bureau about medical-dispute mediation and administrative complaints; seek legal aid or expert review if needed."),
   "civil":("先以书面催告并核对契约约定；可使用乡镇市区调解、消费者申诉（适用时）、法律扶助、约定仲裁或法院程序。","先以書面催告並核對契約約定；可使用鄉鎮市區調解、消費者申訴（適用時）、法律扶助、約定仲裁或法院程序。","Give written notice and check the agreement; consider local mediation, consumer complaint where applicable, legal aid, agreed arbitration, or court."),
   "family":("向户政、社会局/处、家庭法院或法律扶助确认离婚、子女和扶养程序；涉及暴力先拨110或113。","向戶政、社會局／處、家事法院或法律扶助確認離婚、子女和扶養程序；涉及暴力先撥110或113。","Check divorce, child, and support procedure with household registration, social services, family court, or legal aid; for violence call 110 or 113 first.")},
  "sources":[("行政院消费者保护会","https://cpc.ey.gov.tw/"),("内政部165全民防骗","https://165.npa.gov.tw/"),("卫生福利部113保护专线","https://dep.mohw.gov.tw/DOPs/fp-1183-6499-105.html")]},
}


ACTION_RE = re.compile(r"怎么办|怎麼辦|怎么处理|怎麼處理|如何|维权|維權|投诉|投訴|举报|舉報|起诉|起訴|报警|報警|what (?:can|should) i do|how (?:can|do|should)|where (?:can|do) i",re.I)
URGENT_TOPICS={"fraud","traffic","domestic_violence","work_injury","privacy"}


def classify_life_legal(text: str) -> str|None:
    for topic,item in TOPICS.items():
        # 每个 topic 的模式本身已是具体纠纷场景；陈述式求助（例如“房东扣着押金”）
        # 同样需要分流，不能强迫用户额外补一句“怎么办”。
        if re.search(item["patterns"],text,re.I):return topic
    return None


def looks_like_life_legal(text: str) -> bool:
    return classify_life_legal(text) is not None


def _sources_for(jurisdiction: str, topic: str):
    group=TOPICS[topic]["group"]
    if jurisdiction=="CN":
        sources=[("中国法律服务网/12348","https://www.12348.gov.cn/"),
                 ("国家法律法规数据库","https://flk.npc.gov.cn/")]
        if group=="employment":sources.append(("人力资源和社会保障部","https://www.mohrss.gov.cn/"))
        if group=="consumer":sources.append(("全国12315平台","https://www.12315.cn/"))
        if group in {"crime","safety"}:sources.append(("中华人民共和国公安部","https://www.mps.gov.cn/"))
        if group=="health":sources.append(("国家卫生健康委员会","https://www.nhc.gov.cn/"))
        if topic=="copyright":sources.append(("国家版权局","https://www.ncac.gov.cn/"))
    elif jurisdiction=="US-FED":
        sources=[("USAGov legal aid","https://www.usa.gov/legal-aid")]
        if group=="employment":sources.append(("U.S. Department of Labor","https://www.dol.gov/"))
        if topic=="discrimination":sources.append(("U.S. EEOC charge portal","https://www.eeoc.gov/filing-charge-discrimination"))
        if topic=="work_injury":sources.append(("OSHA workers' rights","https://www.osha.gov/workers"))
        if group=="consumer":sources.extend([("USAGov complaints","https://www.usa.gov/complaints"),("FTC ReportFraud","https://reportfraud.ftc.gov/")])
        if group=="crime":sources.extend([("FTC ReportFraud","https://reportfraud.ftc.gov/"),("FBI Internet Crime Complaint Center","https://www.ic3.gov/")])
        if group=="housing":sources.extend([("USAGov housing complaints","https://www.usa.gov/housing-complaints"),("HUD housing discrimination complaints","https://www.hud.gov/contactus/file-complaint")])
        if topic=="debt":sources.append(("Consumer Financial Protection Bureau","https://www.consumerfinance.gov/consumer-tools/debt-collection/"))
        if topic=="domestic_violence":sources.append(("USAGov domestic violence help","https://www.usa.gov/domestic-violence"))
        if topic=="copyright":sources.append(("U.S. Copyright Office","https://www.copyright.gov/"))
    else:
        sources=[("法律扶助基金会","https://www.laf.org.tw/")]
        if group=="employment":sources.append(("臺灣勞動部勞資爭議處理","https://www.mol.gov.tw/1607/28690/89680/"))
        if group in {"consumer","housing"}:sources.append(("行政院消费者保护会","https://cpc.ey.gov.tw/"))
        if group=="crime":sources.append(("内政部警政署165全民防骗","https://165.npa.gov.tw/"))
        if group=="safety":sources.append(("内政部警政署","https://www.npa.gov.tw/"))
        if topic=="domestic_violence":sources.append(("卫生福利部113保护专线","https://dep.mohw.gov.tw/DOPs/fp-1183-6499-105.html"))
        if group=="health":sources.append(("卫生福利部","https://www.mohw.gov.tw/"))
        if topic=="copyright":sources.append(("经济部智慧财产局","https://www.tipo.gov.tw/"))
    return sources


def ensure_life_legal_guides(db_path: Path) -> int:
    con=sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS life_legal_guides(
      guide_id TEXT PRIMARY KEY,jurisdiction TEXT NOT NULL,topic TEXT NOT NULL,
      title_zh TEXT NOT NULL,title_hant TEXT NOT NULL,title_en TEXT NOT NULL,
      immediate_zh TEXT NOT NULL,immediate_hant TEXT NOT NULL,immediate_en TEXT NOT NULL,
      evidence_zh TEXT NOT NULL,evidence_hant TEXT NOT NULL,evidence_en TEXT NOT NULL,
      route_zh TEXT NOT NULL,route_hant TEXT NOT NULL,route_en TEXT NOT NULL,
      revision TEXT NOT NULL,confidence REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS life_legal_sources(
      guide_id TEXT NOT NULL,source_name TEXT NOT NULL,source_url TEXT NOT NULL,
      revision TEXT NOT NULL,confidence REAL NOT NULL,PRIMARY KEY(guide_id,source_url))""")
    for jurisdiction,region in REGIONS.items():
        for topic,item in TOPICS.items():
            guide_id=f"{jurisdiction}_{topic}";route=region["routes"][item["group"]]
            con.execute("INSERT OR REPLACE INTO life_legal_guides VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (guide_id,jurisdiction,topic,*item["title"],*item["immediate"],*item["evidence"],*route,"2026-09",.86))
            con.execute("DELETE FROM life_legal_sources WHERE guide_id=?",(guide_id,))
            con.executemany("INSERT INTO life_legal_sources VALUES(?,?,?,?,?)",
              ((guide_id,name,url,"checked 2026-09",.92) for name,url in _sources_for(jurisdiction,topic)))
    con.commit();count=con.execute("SELECT count(*) FROM life_legal_guides").fetchone()[0];con.close();return count


def answer_life_legal(db_path: Path,text: str,language: str,jurisdiction: str|None,region: str="") -> str|None:
    topic=classify_life_legal(text)
    if not topic:return None
    item=TOPICS[topic];li={"zh":0,"zh_hant":1,"en":2}[language]
    if jurisdiction in {"HK","MO"}:
        return {"zh":f"这涉及{region}的独立法律制度。本地生活指南不能用其他地区法律代替。请先按下列安全步骤处理，并查询当地官方法律服务：{item['immediate'][li]} {item['evidence'][li]}",
                "zh_hant":f"這涉及{region}的獨立法律制度。本機生活指南不能以其他地區法律代替。請先按下列安全步驟處理，並查詢當地官方法律服務：{item['immediate'][li]} {item['evidence'][li]}",
                "en":f"This concerns the separate legal system of {region}; another jurisdiction's law cannot be substituted. Take these safety steps and consult official local legal services: {item['immediate'][li]} {item['evidence'][li]}"}[language]
    if jurisdiction is None:
        place={"zh":"请补充事情发生地：中国大陆城市、美国州和城市，或中国台湾地区。",
               "zh_hant":"請補充事情發生地：中國大陸城市、美國州和城市，或中國臺灣地區。",
               "en":"Please give where this occurred: the mainland-China city, the U.S. state and city, or Taiwan."}[language]
        disclaimer={"zh":"先不要等待：","zh_hant":"先不要等待：","en":"Do not wait to take safe preliminary steps: "}[language]
        return f"{item['title'][li]}\n{place}\n{disclaimer}{item['immediate'][li]}\n证据/记录：{item['evidence'][li]}"
    requested=jurisdiction
    if jurisdiction=="CN-LOCAL":jurisdiction="CN"
    if jurisdiction in {"US-STATE","US-UNSPECIFIED"}:jurisdiction="US-FED"
    if jurisdiction not in REGIONS:return None
    ensure_life_legal_guides(db_path)
    con=sqlite3.connect(db_path)
    row=con.execute("SELECT * FROM life_legal_guides WHERE jurisdiction=? AND topic=?",(jurisdiction,topic)).fetchone()
    sources=con.execute("SELECT source_name,source_url,revision FROM life_legal_sources WHERE guide_id=? ORDER BY source_name",(row[0],)).fetchall()
    con.close()
    title_i,immediate_i,evidence_i,route_i={"zh":(3,6,9,12),"zh_hant":(4,7,10,13),"en":(5,8,11,14)}[language]
    labels={"zh":("先做","保存证据","合法处理路径","地域与期限提醒","官方入口"),
            "zh_hant":("先做","保存證據","合法處理途徑","地域與期限提醒","官方入口"),
            "en":("First steps","Preserve evidence","Lawful route","Location and deadline note","Official entry points")}[language]
    if requested=="US-STATE":location=region
    elif requested=="US-UNSPECIFIED":location={"zh":"必须继续确认工作/交易/住所所在州和城市","zh_hant":"必須繼續確認工作／交易／住所所在州和城市","en":"you must still identify the relevant state and city"}[language]
    elif requested=="CN-LOCAL":location=f"{region}（并核对当地受理机关）"
    else:location=REGIONS[jurisdiction]["name"][li]
    deadline={"zh":"程序、管辖和期限因请求类型及地点而异；不要只等待口头协商，尽快向官方入口确认。",
              "zh_hant":"程序、管轄和期限因請求類型及地點而異；不要只等待口頭協商，儘快向官方入口確認。",
              "en":"Procedure, forum, and deadlines vary by claim and location. Do not rely only on oral negotiation; promptly confirm with an official entry point."}[language]
    sep=":" if language!="en" else ":"
    lines=[row[title_i],f"1. {labels[0]}{sep}{row[immediate_i]}",f"2. {labels[1]}{sep}{row[evidence_i]}",
           f"3. 书面沟通：用可留痕方式写明事实、日期、金额或具体诉求及合理回复期限；只陈述可证明的事实。" if language=="zh" else
           (f"3. 書面溝通：以可留痕方式寫明事實、日期、金額或具體訴求及合理回覆期限；只陳述可證明的事實。" if language=="zh_hant" else
            "3. Written communication: state provable facts, dates, amount or requested remedy, and a reasonable response deadline in a traceable format."),
           f"4. {labels[2]}{sep}{row[route_i]}",f"5. {labels[3]}{sep}{location}{'; ' if language=='en' else '；'}{deadline}",f"{labels[4]}{sep}"]
    if topic == "privacy":
        lines.insert(5, {"zh":"补充：还应根据所在地核对个人信息保护、数据泄露通知与身份盗用规则。",
                         "zh_hant":"補充：還應依所在地核對個人資料保護、資料外洩通知與身分盜用規則。",
                         "en":"Additional note: check applicable personal-data protection, breach-notification, and identity-theft rules."}[language])
    lines.extend(f"- {name} — {url}（{revision}）" for name,url,revision in sources)
    lines.append({"zh":"本指南用于合法分流，不判断谁一定胜诉，也不替代律师的个案意见。若有即时人身危险、严重伤病、资产正在转移或期限临近，应立即联系当地紧急服务、主管机关或律师。",
                  "zh_hant":"本指南用於合法分流，不判斷誰一定勝訴，也不替代律師的個案意見。若有即時人身危險、嚴重傷病、資產正在轉移或期限臨近，應立即聯絡當地緊急服務、主管機關或律師。",
                  "en":"This guide provides lawful triage; it does not predict who will win or replace case-specific legal advice. For immediate danger, serious injury, asset flight, or a near deadline, contact local emergency services, the competent authority, or a lawyer now."}[language])
    return "\n".join(lines)


def life_eval_cases():
    focused=[
      ("中国大陆网购商品坏了商家不给退款怎么办","全国12315"),("美國加州房東不退押金怎麼辦","California"),
      ("臺灣遇到網路詐騙怎麼辦","165"),("中国大陆被公司违法辞退怎么办","人力资源社会保障"),
      ("Under U.S. federal law, what should I do about workplace discrimination?","EEOC"),
      ("臺灣上班受傷怎麼辦","免費勞資爭議調解"),("中国大陆遇到家庭暴力怎么办","110"),
      ("I face domestic violence in Taiwan. What should I do?","113"),("美国发生车祸怎么办","911"),
      ("中国大陆借钱不还怎么处理","12348"),("臺灣房東不修房子怎麼辦","地方政府"),
      ("My personal data was leaked in the United States. What should I do?","state"),
      ("中国大陆发生医疗事故怎么办","病历"),("美国合同违约怎么处理","州法"),
      ("臺灣作品被抄襲怎麼辦","原始創作"),("离婚孩子抚养权怎么办","事情发生地")]
    prompts={
      "dismissal":("中国大陆被公司辞退怎么办","臺灣被公司辭退怎麼辦","I was fired in the United States. What should I do?"),
      "overtime":("中国大陆加班费不给怎么办","臺灣加班費沒發怎麼辦","My U.S. employer did not pay overtime. What should I do?"),
      "work_injury":("中国大陆上班受伤怎么办","臺灣工作受傷怎麼辦","I was injured at work in the United States. What should I do?"),
      "consumer":("中国大陆商品坏了商家不退款怎么办","臺灣網購商品有瑕疵怎麼辦","How do I handle a defective product refund in the United States?"),
      "fraud":("中国大陆被网络诈骗了怎么办","臺灣被網路詐騙怎麼辦","I was the victim of a scam in the United States. What should I do?"),
      "rental":("中国大陆房东不退押金怎么办","臺灣房東不退押金怎麼辦","My U.S. landlord kept my security deposit. What should I do?"),
      "debt":("中国大陆借钱不还怎么办","臺灣欠款不還怎麼辦","A loan was not repaid in the United States. What should I do?"),
      "traffic":("中国大陆发生车祸怎么办","臺灣發生車禍怎麼辦","What should I do after a car accident in the United States?"),
      "domestic_violence":("中国大陆遭遇家暴怎么办","臺灣遇到家暴怎麼辦","I face domestic violence in the United States. What should I do?"),
      "privacy":("中国大陆个人信息泄露怎么办","臺灣個人資料外洩怎麼辦","My personal data was leaked in the United States. What should I do?"),
      "discrimination":("中国大陆遇到职场歧视怎么办","臺灣遇到職場性騷擾怎麼辦","How do I report workplace discrimination in the United States?"),
      "medical":("中国大陆发生医疗事故怎么办","臺灣發生醫療事故怎麼辦","How do I handle possible medical malpractice in the United States?"),
      "copyright":("中国大陆作品被抄袭怎么办","臺灣作品被抄襲怎麼辦","My work was copied in the United States. How do I handle copyright infringement?"),
      "contract":("中国大陆合同违约怎么办","臺灣契約違約怎麼辦","How do I handle a breach of contract in the United States?"),
      "family":("中国大陆离婚孩子抚养权怎么办","臺灣離婚孩子監護權怎麼辦","How do I handle divorce and child custody in the United States?")}
    for topic,questions in prompts.items():
        focused.extend((questions[index],TOPICS[topic]["title"][index]) for index in range(3))
    return focused
