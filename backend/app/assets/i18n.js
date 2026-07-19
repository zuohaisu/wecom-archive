/* I18N_CORE_START
 * RND-157 — Admin UI Internationalization foundation.
 *
 * LocaleRegistry is the single source of truth for every supported
 * language. Adding a future locale means adding one entry here — no
 * other file should need to change (selector, persistence, and lookup
 * logic all read from this registry).
 *
 * Business/UI code must go through the I18N helpers below (t, getLocale,
 * setLocale, availableLocales, getLocaleInfo, hasLocale) rather than
 * reaching into LocaleRegistry[...].translations directly.
 */
(function (global) {
  "use strict";

  var DEFAULT_LOCALE = "zh-CN";
  var STORAGE_KEY = "wecom_admin_locale";

  var LocaleRegistry = {
    "zh-CN": {
      code: "zh-CN",
      name: "Simplified Chinese",
      nativeName: "简体中文",
      direction: "ltr",
      translations: {
        "nav.staff": "员工",
        "nav.contact": "联系人",
        "nav.conversations": "会话",
        "nav.settings": "设置",
        "nav.language": "语言",
        "nav.messages": "消息记录",
        "nav.logout": "退出登录",

        "app.subtitle": "对话审阅控制台",

        "console.tzNote": "时间均为北京时间 (UTC+8)",
        "console.monitoredAccounts": "监控账号",
        "console.contactsHeader": "联系人",
        "console.timelineHeader": "消息时间线",
        "console.selectAccountOrContact": "请选择账号或联系人",
        "console.selectConversation": "请选择会话",
        "console.loading": "加载中…",
        "console.noneFound": "未找到",
        "console.noConversations": "未找到会话",
        "console.noMessages": "暂无消息",
        "console.failedToLoadEntities": "加载失败",
        "console.failedToLoadConversations": "会话加载失败",
        "console.failedToLoadPrefix": "加载失败：",
        "console.seatActive": "在用",
        "console.seatHistory": "历史",

        "history.loadingOlder": "正在加载更多历史消息…",
        "history.noMore": "没有更多历史消息",
        "history.failedToLoad": "历史消息加载失败",
        "history.retry": "重试",

        "refresh.manual": "刷新",
        "refresh.lastUpdated": "最近更新：",
        "refresh.nextIn": "下次刷新：",
        "refresh.secondsSuffix": " 秒后",
        "refresh.paused": "已暂停（页面不可见）",
        "refresh.failedPrefix": "刷新失败：",
        "refresh.newMessages": "有新消息",

        "timeline.groupChat": "群聊",
        "timeline.participant": "人",
        "timeline.participants": "人",
        "timeline.groupBadge": "群",
        "timeline.emptyText": "空文本消息",
        "timeline.revokedBadge": "已撤回",
        "revoke.pending": "撤回关联中",
        "revoke.originalMissing": "原始消息不可用",
        "revoke.malformed": "撤回事件异常",

        "convList.groupBadge": "群聊",
        "convList.directBadge": "单聊",
        "convList.messagesSuffix": "条消息",

        "media.image": "图片消息",
        "media.video": "视频消息",
        "media.voice": "语音消息",
        "media.file": "文件消息",
        "media.generic": "媒体消息",
        "media.status.notDownloaded": "未下载",
        "media.status.unsupported": "不支持",
        "media.status.unknown": "状态未知",
        "media.status.failed": "下载失败",
        "media.unsupportedType": "不支持的消息类型",
        "media.unknownType": "不支持未知消息类型",
        "media.loadFailed": "图片加载失败",

        "video.loading": "视频加载中…",
        "video.playbackError": "视频播放失败",
        "video.unsupportedBrowser": "当前浏览器不支持播放该视频",
        "voice.loading": "语音加载中…",
        "voice.playbackError": "语音播放失败",
        "file.download": "下载",
        "file.unavailable": "文件不可用",
        "file.sizeUnknown": "大小未知",
        "emotion.alt": "表情消息",

        "viewer.close": "关闭",
        "viewer.prev": "上一项",
        "viewer.next": "下一项",
        "viewer.loading": "加载中…",
        "viewer.error": "加载失败",
        "viewer.unauthorized": "未授权访问",
        "viewer.missingMedia": "媒体不存在",
        "viewer.dialogLabel": "媒体查看器",
        "viewer.expand": "在查看器中打开",

        "media.error.forbidden": "无权限访问或链接已过期",
        "media.error.network": "网络连接失败",
        "file.fallbackName": "文件",
        "render.messageFailed": "该消息渲染失败",
        "revoke.time": "撤回时间：",
        "link.openLink": "打开链接",

        "mixed.itemsSuffix": "项内容",
        "chatrecord.itemsSuffix": "条记录",
        "chatrecord.viewNested": "查看聊天记录",
        "chatrecord.title": "聊天记录",
        "chatrecord.empty": "无聊天记录内容",
        "composite.unknownChild": "不支持的子消息类型",
        "composite.depthLimitReached": "内容层级过深，已省略",

        "placeholder.video": "不支持视频消息",
        "placeholder.voice": "不支持语音消息",
        "placeholder.file": "不支持文件消息",
        "placeholder.location": "不支持位置消息",
        "placeholder.link": "不支持链接消息",
        "placeholder.card": "不支持名片消息",
        "placeholder.emotion": "不支持表情消息",
        "placeholder.miniprogram": "不支持小程序消息",
        "placeholder.todo": "不支持待办消息",
        "placeholder.unsupported": "不支持未知消息类型",
        "placeholder.markdown": "不支持 Markdown 消息",
        "placeholder.news": "不支持图文消息",
        "placeholder.docmsg": "不支持文档消息",
        "placeholder.audio_doc": "不支持语音文档消息",

        "messageType.text": "文本消息",
        "messageType.image": "图片消息",
        "messageType.video": "视频消息",
        "messageType.voice": "语音消息",
        "messageType.file": "文件消息",
        "messageType.audioArchive": "语音存档消息",
        "messageType.location": "位置消息",
        "messageType.link": "链接消息",
        "messageType.card": "名片消息",
        "messageType.emotion": "表情消息",
        "messageType.miniprogram": "小程序消息",
        "messageType.todo": "待办消息",
        "messageType.revoke": "撤回消息",
        "messageType.mixed": "混合消息",
        "messageType.chatrecord": "聊天记录消息",
        "messageType.system": "系统消息",
        "messageType.unknown": "未知消息类型",
        "messageType.markdown": "Markdown 消息",
        "messageType.news": "图文消息",
        "messageType.docmsg": "文档消息",
        "messageType.audioDoc": "语音文档消息",
        "messageType.vote": "投票消息",
        "messageType.collect": "收集表消息",
        "messageType.meeting": "会议消息",
        "messageType.schedule": "日程消息",
        "messageType.redpacket": "红包消息",
        "messageType.switchCorp": "切换企业消息",

        "card.link.unavailable": "链接不可用",
        "card.location.unknown": "位置未知",
        "card.markdown.empty": "空白 Markdown 消息",
        "card.news.empty": "无图文内容",
        "card.news.noTitle": "无标题文章",
        "card.audioDoc.playbackUnavailable": "暂不支持播放",
        "card.generic.unavailable": "暂不支持展示完整内容",
        "card.vote.type": "投票类型：",
        "card.vote.unnamed": "未命名选项",
        "card.vote.noItems": "无投票选项",
        "card.todo.label": "待办事项",
        "card.collect.entries": "条收集项",
        "card.meeting.time": "会议时间：",
        "card.meeting.place": "会议地点：",
        "card.schedule.start": "开始时间：",
        "card.schedule.end": "结束时间：",
        "card.schedule.place": "地点：",
        "card.redpacket.label": "红包",
        "card.redpacket.nPackets": "个红包",
        "card.switchCorp.switchedTo": "已切换至企业 ",
        "system.event.switch_corp": "已切换企业",
        "system.event.create_room": "群聊成员已更新",
        "system.event.update_room": "群聊成员已更新",
        "system.event.conv_archive_auth": "企业已授权会话内容存档",
        "system.event.unknown": "系统消息",

        "login.username": "用户名",
        "login.password": "密码",
        "login.submit": "登录",
        "login.submitting": "登录中…",
        "login.invalidCredentials": "密码错误，请重试。",
        "login.genericFailure": "登录失败，请重试。",
        "login.footerPassword": "临时管理员登录 — 企业微信登录即将上线",
        "login.wecomButton": "使用企业微信登录",
        "login.footerWecom": "仅限企业内部员工访问",
        "login.error.invalidState": "登录会话已过期或请求被篡改，请重试。",
        "login.error.authFailed": "认证失败，请重试。",
        "login.error.userInactive": "您的企业微信账号已停用，请联系管理员。",
        "login.error.configError": "服务器配置错误，请联系管理员。",

        "nav.diagnostics": "系统诊断",

        "diagnostics.pageTitle": "消息可达性诊断",
        "diagnostics.pageDescription": "本页仅展示消息可达性审计的聚合统计数据，不包含消息内容或原始标识符。",
        "diagnostics.reachabilityTitle": "消息可达性",
        "diagnostics.refresh": "刷新",
        "diagnostics.loading": "正在加载诊断数据…",
        "diagnostics.noData": "暂无诊断数据",
        "diagnostics.failedToLoad": "诊断数据加载失败",
        "diagnostics.retry": "重试",
        "diagnostics.backToConsole": "返回审阅控制台",

        "diagnostics.successfulDecrypted": "成功解密消息数",
        "diagnostics.reachableMessages": "可达消息数",
        "diagnostics.unreachableMessages": "不可达消息数",
        "diagnostics.reachabilityRate": "可达率",

        "diagnostics.scanMetadataTitle": "扫描范围",
        "diagnostics.scannedMessages": "已扫描消息数",
        "diagnostics.matchingTotal": "匹配消息总数",
        "diagnostics.limitLabel": "扫描上限",
        "diagnostics.offsetLabel": "偏移量",
        "diagnostics.hasMoreLabel": "是否有更多",
        "diagnostics.hasMoreYes": "是",
        "diagnostics.hasMoreNo": "否",
        "diagnostics.moreMessagesExist": "本报告仅基于已扫描的分页数据，仍有更多匹配消息未扫描。",

        "diagnostics.unreachableReasons": "不可达原因分布",
        "diagnostics.byMessageType": "按消息类型统计",
        "diagnostics.reasonColumn": "原因",
        "diagnostics.countColumn": "数量",
        "diagnostics.percentColumn": "占比",
        "diagnostics.typeColumn": "消息类型",
        "diagnostics.totalColumn": "总数",

        "reachability.status.reachable_direct": "可达（单聊）",
        "reachability.status.reachable_group": "可达（群聊）",
        "reachability.status.unreachable_missing_recipient": "不可达：缺少接收方记录",
        "reachability.status.unreachable_missing_room": "不可达：缺少群聊房间标识",
        "reachability.status.unreachable_missing_sender": "不可达：缺少发送方",
        "reachability.status.unreachable_membership": "不可达：会话成员归属未命中",
        "reachability.status.unreachable_other": "不可达：其他原因",
        "reachability.status.unknown": "未知状态"
      }
    },
    "zh-TW": {
      code: "zh-TW",
      name: "Traditional Chinese",
      nativeName: "繁體中文",
      direction: "ltr",
      translations: {
        "nav.staff": "員工",
        "nav.contact": "聯絡人",
        "nav.conversations": "會話",
        "nav.settings": "設定",
        "nav.language": "語言",
        "nav.messages": "訊息記錄",
        "nav.logout": "登出",

        "app.subtitle": "對話審閱控制台",

        "console.tzNote": "時間均為北京時間 (UTC+8)",
        "console.monitoredAccounts": "監控帳號",
        "console.contactsHeader": "聯絡人",
        "console.timelineHeader": "訊息時間軸",
        "console.selectAccountOrContact": "請選擇帳號或聯絡人",
        "console.selectConversation": "請選擇會話",
        "console.loading": "載入中…",
        "console.noneFound": "未找到",
        "console.noConversations": "未找到會話",
        "console.noMessages": "暫無訊息",
        "console.failedToLoadEntities": "載入失敗",
        "console.failedToLoadConversations": "會話載入失敗",
        "console.failedToLoadPrefix": "載入失敗：",
        "console.seatActive": "使用中",
        "console.seatHistory": "歷史",

        "history.loadingOlder": "正在載入更多歷史訊息…",
        "history.noMore": "沒有更多歷史訊息",
        "history.failedToLoad": "歷史訊息載入失敗",
        "history.retry": "重試",

        "refresh.manual": "重新整理",
        "refresh.lastUpdated": "最近更新：",
        "refresh.nextIn": "下次重新整理：",
        "refresh.secondsSuffix": " 秒後",
        "refresh.paused": "已暫停（頁面不可見）",
        "refresh.failedPrefix": "重新整理失敗：",
        "refresh.newMessages": "有新訊息",

        "timeline.groupChat": "群組聊天",
        "timeline.participant": "人",
        "timeline.participants": "人",
        "timeline.groupBadge": "群",
        "timeline.emptyText": "空白文字訊息",
        "timeline.revokedBadge": "已撤回",
        "revoke.pending": "撤回關聯中",
        "revoke.originalMissing": "原始訊息不可用",
        "revoke.malformed": "撤回事件異常",

        "convList.groupBadge": "群組",
        "convList.directBadge": "單聊",
        "convList.messagesSuffix": "則訊息",

        "media.image": "圖片訊息",
        "media.video": "影片訊息",
        "media.voice": "語音訊息",
        "media.file": "檔案訊息",
        "media.generic": "媒體訊息",
        "media.status.notDownloaded": "未下載",
        "media.status.unsupported": "不支援",
        "media.status.unknown": "狀態未知",
        "media.status.failed": "下載失敗",
        "media.unsupportedType": "不支援的訊息類型",
        "media.unknownType": "不支援未知訊息類型",
        "media.loadFailed": "圖片載入失敗",

        "video.loading": "影片載入中…",
        "video.playbackError": "影片播放失敗",
        "video.unsupportedBrowser": "目前瀏覽器不支援播放此影片",
        "voice.loading": "語音載入中…",
        "voice.playbackError": "語音播放失敗",
        "file.download": "下載",
        "file.unavailable": "檔案不可用",
        "file.sizeUnknown": "大小未知",
        "emotion.alt": "表情訊息",

        "viewer.close": "關閉",
        "viewer.prev": "上一項",
        "viewer.next": "下一項",
        "viewer.loading": "載入中…",
        "viewer.error": "載入失敗",
        "viewer.unauthorized": "未授權存取",
        "viewer.missingMedia": "媒體不存在",
        "viewer.dialogLabel": "媒體檢視器",
        "viewer.expand": "在檢視器中開啟",

        "media.error.forbidden": "無權限存取或連結已過期",
        "media.error.network": "網路連線失敗",
        "file.fallbackName": "檔案",
        "render.messageFailed": "此訊息渲染失敗",
        "revoke.time": "撤回時間：",
        "link.openLink": "開啟連結",

        "mixed.itemsSuffix": "項內容",
        "chatrecord.itemsSuffix": "則紀錄",
        "chatrecord.viewNested": "查看聊天記錄",
        "chatrecord.title": "聊天記錄",
        "chatrecord.empty": "無聊天記錄內容",
        "composite.unknownChild": "不支援的子訊息類型",
        "composite.depthLimitReached": "內容層級過深，已省略",

        "placeholder.video": "不支援影片訊息",
        "placeholder.voice": "不支援語音訊息",
        "placeholder.file": "不支援檔案訊息",
        "placeholder.location": "不支援位置訊息",
        "placeholder.link": "不支援連結訊息",
        "placeholder.card": "不支援名片訊息",
        "placeholder.emotion": "不支援表情訊息",
        "placeholder.miniprogram": "不支援小程式訊息",
        "placeholder.todo": "不支援待辦訊息",
        "placeholder.unsupported": "不支援未知訊息類型",
        "placeholder.markdown": "不支援 Markdown 訊息",
        "placeholder.news": "不支援圖文訊息",
        "placeholder.docmsg": "不支援文件訊息",
        "placeholder.audio_doc": "不支援語音文件訊息",

        "messageType.text": "文字訊息",
        "messageType.image": "圖片訊息",
        "messageType.video": "影片訊息",
        "messageType.voice": "語音訊息",
        "messageType.file": "檔案訊息",
        "messageType.audioArchive": "語音存檔訊息",
        "messageType.location": "位置訊息",
        "messageType.link": "連結訊息",
        "messageType.card": "名片訊息",
        "messageType.emotion": "表情訊息",
        "messageType.miniprogram": "小程式訊息",
        "messageType.todo": "待辦訊息",
        "messageType.revoke": "撤回訊息",
        "messageType.mixed": "混合訊息",
        "messageType.chatrecord": "聊天記錄訊息",
        "messageType.system": "系統訊息",
        "messageType.unknown": "未知訊息類型",
        "messageType.markdown": "Markdown 訊息",
        "messageType.news": "圖文訊息",
        "messageType.docmsg": "文件訊息",
        "messageType.audioDoc": "語音文件訊息",
        "messageType.vote": "投票訊息",
        "messageType.collect": "收集表訊息",
        "messageType.meeting": "會議訊息",
        "messageType.schedule": "日程訊息",
        "messageType.redpacket": "紅包訊息",
        "messageType.switchCorp": "切換企業訊息",

        "card.link.unavailable": "連結不可用",
        "card.location.unknown": "位置未知",
        "card.markdown.empty": "空白 Markdown 訊息",
        "card.news.empty": "無圖文內容",
        "card.news.noTitle": "無標題文章",
        "card.audioDoc.playbackUnavailable": "暫不支援播放",
        "card.generic.unavailable": "暫不支援顯示完整內容",
        "card.vote.type": "投票類型：",
        "card.vote.unnamed": "未命名選項",
        "card.vote.noItems": "無投票選項",
        "card.todo.label": "待辦事項",
        "card.collect.entries": "條收集項",
        "card.meeting.time": "會議時間：",
        "card.meeting.place": "會議地點：",
        "card.schedule.start": "開始時間：",
        "card.schedule.end": "結束時間：",
        "card.schedule.place": "地點：",
        "card.redpacket.label": "紅包",
        "card.redpacket.nPackets": "個紅包",
        "card.switchCorp.switchedTo": "已切換至企業 ",
        "system.event.switch_corp": "已切換企業",
        "system.event.create_room": "群組成員已更新",
        "system.event.update_room": "群組成員已更新",
        "system.event.conv_archive_auth": "企業已授權會話內容存檔",
        "system.event.unknown": "系統訊息",

        "login.username": "使用者名稱",
        "login.password": "密碼",
        "login.submit": "登入",
        "login.submitting": "登入中…",
        "login.invalidCredentials": "密碼錯誤，請重試。",
        "login.genericFailure": "登入失敗，請重試。",
        "login.footerPassword": "臨時管理員登入 — 企業微信登入即將上線",
        "login.wecomButton": "使用企業微信登入",
        "login.footerWecom": "僅限企業內部員工存取",
        "login.error.invalidState": "登入工作階段已過期或請求遭竄改，請重試。",
        "login.error.authFailed": "驗證失敗，請重試。",
        "login.error.userInactive": "您的企業微信帳號已停用，請聯絡管理員。",
        "login.error.configError": "伺服器設定錯誤，請聯絡管理員。",

        "nav.diagnostics": "系統診斷",

        "diagnostics.pageTitle": "訊息可達性診斷",
        "diagnostics.pageDescription": "本頁僅顯示訊息可達性稽核的彙總統計數據，不包含訊息內容或原始識別碼。",
        "diagnostics.reachabilityTitle": "訊息可達性",
        "diagnostics.refresh": "重新整理",
        "diagnostics.loading": "正在載入診斷資料…",
        "diagnostics.noData": "暫無診斷資料",
        "diagnostics.failedToLoad": "診斷資料載入失敗",
        "diagnostics.retry": "重試",
        "diagnostics.backToConsole": "返回審閱控制台",

        "diagnostics.successfulDecrypted": "成功解密訊息數",
        "diagnostics.reachableMessages": "可達訊息數",
        "diagnostics.unreachableMessages": "不可達訊息數",
        "diagnostics.reachabilityRate": "可達率",

        "diagnostics.scanMetadataTitle": "掃描範圍",
        "diagnostics.scannedMessages": "已掃描訊息數",
        "diagnostics.matchingTotal": "符合訊息總數",
        "diagnostics.limitLabel": "掃描上限",
        "diagnostics.offsetLabel": "偏移量",
        "diagnostics.hasMoreLabel": "是否有更多",
        "diagnostics.hasMoreYes": "是",
        "diagnostics.hasMoreNo": "否",
        "diagnostics.moreMessagesExist": "本報告僅基於已掃描的分頁資料，仍有更多符合訊息尚未掃描。",

        "diagnostics.unreachableReasons": "不可達原因分佈",
        "diagnostics.byMessageType": "依訊息類型統計",
        "diagnostics.reasonColumn": "原因",
        "diagnostics.countColumn": "數量",
        "diagnostics.percentColumn": "佔比",
        "diagnostics.typeColumn": "訊息類型",
        "diagnostics.totalColumn": "總數",

        "reachability.status.reachable_direct": "可達（單聊）",
        "reachability.status.reachable_group": "可達（群聊）",
        "reachability.status.unreachable_missing_recipient": "不可達：缺少接收方紀錄",
        "reachability.status.unreachable_missing_room": "不可達：缺少群聊房間識別碼",
        "reachability.status.unreachable_missing_sender": "不可達：缺少發送方",
        "reachability.status.unreachable_membership": "不可達：會話成員歸屬未命中",
        "reachability.status.unreachable_other": "不可達：其他原因",
        "reachability.status.unknown": "未知狀態"
      }
    },
    en: {
      code: "en",
      name: "English",
      nativeName: "English",
      direction: "ltr",
      translations: {
        "nav.staff": "Staff",
        "nav.contact": "Contact",
        "nav.conversations": "Conversations",
        "nav.settings": "Settings",
        "nav.language": "Language",
        "nav.messages": "Messages",
        "nav.logout": "Logout",

        "app.subtitle": "Conversation Review Console",

        "console.tzNote": "Times shown in Beijing time (UTC+8)",
        "console.monitoredAccounts": "Monitored Accounts",
        "console.contactsHeader": "Contacts",
        "console.timelineHeader": "Message Timeline",
        "console.selectAccountOrContact": "Select an account or contact",
        "console.selectConversation": "Select a conversation",
        "console.loading": "Loading…",
        "console.noneFound": "None found",
        "console.noConversations": "No conversations found",
        "console.noMessages": "No messages",
        "console.failedToLoadEntities": "Failed to load entities",
        "console.failedToLoadConversations": "Failed to load conversations",
        "console.failedToLoadPrefix": "Failed to load: ",
        "console.seatActive": "Active",
        "console.seatHistory": "History",

        "history.loadingOlder": "Loading older messages…",
        "history.noMore": "No more history",
        "history.failedToLoad": "Failed to load history",
        "history.retry": "Retry",

        "refresh.manual": "Refresh",
        "refresh.lastUpdated": "Last updated: ",
        "refresh.nextIn": "Next refresh: ",
        "refresh.secondsSuffix": "s",
        "refresh.paused": "Paused (tab hidden)",
        "refresh.failedPrefix": "Refresh failed: ",
        "refresh.newMessages": "New messages",

        "timeline.groupChat": "Group chat",
        "timeline.participant": "participant",
        "timeline.participants": "participants",
        "timeline.groupBadge": "group",
        "timeline.emptyText": "Empty text message",
        "timeline.revokedBadge": "Revoked",
        "revoke.pending": "Revoke pending",
        "revoke.originalMissing": "Original message unavailable",
        "revoke.malformed": "Malformed revoke event",

        "convList.groupBadge": "group",
        "convList.directBadge": "direct",
        "convList.messagesSuffix": "msgs",

        "media.image": "Image message",
        "media.video": "Video message",
        "media.voice": "Voice message",
        "media.file": "File message",
        "media.generic": "Media message",
        "media.status.notDownloaded": "not downloaded",
        "media.status.unsupported": "unsupported",
        "media.status.unknown": "status unknown",
        "media.status.failed": "download failed",
        "media.unsupportedType": "Unsupported message type",
        "media.unknownType": "Unknown message type",
        "media.loadFailed": "Image failed to load",

        "video.loading": "Loading video…",
        "video.playbackError": "Video playback failed",
        "video.unsupportedBrowser": "Your browser can't play this video",
        "voice.loading": "Loading audio…",
        "voice.playbackError": "Audio playback failed",
        "file.download": "Download",
        "file.unavailable": "File unavailable",
        "file.sizeUnknown": "Unknown size",
        "emotion.alt": "Sticker",

        "viewer.close": "Close",
        "viewer.prev": "Previous",
        "viewer.next": "Next",
        "viewer.loading": "Loading…",
        "viewer.error": "Failed to load",
        "viewer.unauthorized": "Unauthorized",
        "viewer.missingMedia": "Media not found",
        "viewer.dialogLabel": "Media viewer",
        "viewer.expand": "Open in viewer",

        "media.error.forbidden": "Access denied or link expired",
        "media.error.network": "Network connection failed",
        "file.fallbackName": "File",
        "render.messageFailed": "This message failed to render",
        "revoke.time": "Revoked at: ",
        "link.openLink": "Open link",

        "mixed.itemsSuffix": "items",
        "chatrecord.itemsSuffix": "messages",
        "chatrecord.viewNested": "View chat record",
        "chatrecord.title": "Chat record",
        "chatrecord.empty": "No chat record content",
        "composite.unknownChild": "Unsupported nested message",
        "composite.depthLimitReached": "Content too deeply nested — truncated",

        "placeholder.video": "Unsupported video message",
        "placeholder.voice": "Unsupported voice message",
        "placeholder.file": "Unsupported file message",
        "placeholder.location": "Unsupported location message",
        "placeholder.link": "Unsupported link message",
        "placeholder.card": "Unsupported contact card message",
        "placeholder.emotion": "Unsupported sticker message",
        "placeholder.miniprogram": "Unsupported mini program message",
        "placeholder.todo": "Unsupported to-do message",
        "placeholder.unsupported": "Unknown message type",
        "placeholder.markdown": "Unsupported Markdown message",
        "placeholder.news": "Unsupported news message",
        "placeholder.docmsg": "Unsupported document message",
        "placeholder.audio_doc": "Unsupported audio document message",

        "messageType.text": "Text message",
        "messageType.image": "Image message",
        "messageType.video": "Video message",
        "messageType.voice": "Voice message",
        "messageType.file": "File message",
        "messageType.audioArchive": "Audio archive message",
        "messageType.location": "Location message",
        "messageType.link": "Link message",
        "messageType.card": "Contact card message",
        "messageType.emotion": "Sticker message",
        "messageType.miniprogram": "Mini program message",
        "messageType.todo": "To-do message",
        "messageType.revoke": "Message revocation",
        "messageType.mixed": "Mixed message",
        "messageType.chatrecord": "Forwarded chat record",
        "messageType.system": "System message",
        "messageType.unknown": "Unknown message type",
        "messageType.markdown": "Markdown message",
        "messageType.news": "News message",
        "messageType.docmsg": "Document message",
        "messageType.audioDoc": "Audio document message",
        "messageType.vote": "Vote message",
        "messageType.collect": "Collection form message",
        "messageType.meeting": "Meeting message",
        "messageType.schedule": "Schedule message",
        "messageType.redpacket": "Red packet message",
        "messageType.switchCorp": "Switch corp message",

        "card.link.unavailable": "Link unavailable",
        "card.location.unknown": "Unknown location",
        "card.markdown.empty": "Empty Markdown message",
        "card.news.empty": "No articles",
        "card.news.noTitle": "Untitled article",
        "card.audioDoc.playbackUnavailable": "Playback not supported",
        "card.generic.unavailable": "Full content unavailable",
        "card.vote.type": "Type: ",
        "card.vote.unnamed": "Unnamed option",
        "card.vote.noItems": "No vote items",
        "card.todo.label": "To-do",
        "card.collect.entries": "entries",
        "card.meeting.time": "Time: ",
        "card.meeting.place": "Place: ",
        "card.schedule.start": "Start: ",
        "card.schedule.end": "End: ",
        "card.schedule.place": "Place: ",
        "card.redpacket.label": "Red Packet",
        "card.redpacket.nPackets": "packets",
        "card.switchCorp.switchedTo": "Switched to ",
        "system.event.switch_corp": "Switched corp",
        "system.event.create_room": "Group members updated",
        "system.event.update_room": "Group members updated",
        "system.event.conv_archive_auth": "Conversation archive authorized",
        "system.event.unknown": "System event",

        "login.username": "Username",
        "login.password": "Password",
        "login.submit": "Login",
        "login.submitting": "Logging in…",
        "login.invalidCredentials": "Invalid credentials. Please try again.",
        "login.genericFailure": "Login failed. Please try again.",
        "login.footerPassword": "Temporary admin access — WeCom login coming soon",
        "login.wecomButton": "Log in with WeCom",
        "login.footerWecom": "Internal employees only",
        "login.error.invalidState": "Login session expired or request was tampered with. Please try again.",
        "login.error.authFailed": "Authentication failed. Please try again.",
        "login.error.userInactive": "Your WeCom account is inactive. Contact your administrator.",
        "login.error.configError": "Server configuration error. Please contact your administrator.",

        "nav.diagnostics": "System Diagnostics",

        "diagnostics.pageTitle": "Message Reachability Diagnostics",
        "diagnostics.pageDescription": "This page shows aggregate statistics from the message reachability audit only — no message content or raw identifiers are displayed.",
        "diagnostics.reachabilityTitle": "Message Reachability",
        "diagnostics.refresh": "Refresh",
        "diagnostics.loading": "Loading diagnostics…",
        "diagnostics.noData": "No diagnostic data available",
        "diagnostics.failedToLoad": "Failed to load diagnostics",
        "diagnostics.retry": "Retry",
        "diagnostics.backToConsole": "Back to review console",

        "diagnostics.successfulDecrypted": "Successful decrypted messages",
        "diagnostics.reachableMessages": "Reachable messages",
        "diagnostics.unreachableMessages": "Unreachable messages",
        "diagnostics.reachabilityRate": "Reachability rate",

        "diagnostics.scanMetadataTitle": "Scan metadata",
        "diagnostics.scannedMessages": "Scanned messages",
        "diagnostics.matchingTotal": "Matching total",
        "diagnostics.limitLabel": "Scan limit",
        "diagnostics.offsetLabel": "Offset",
        "diagnostics.hasMoreLabel": "More matching messages",
        "diagnostics.hasMoreYes": "Yes",
        "diagnostics.hasMoreNo": "No",
        "diagnostics.moreMessagesExist": "This report is based on the scanned page. More matching messages exist.",

        "diagnostics.unreachableReasons": "Unreachable reason distribution",
        "diagnostics.byMessageType": "By message type",
        "diagnostics.reasonColumn": "Reason",
        "diagnostics.countColumn": "Count",
        "diagnostics.percentColumn": "Percentage",
        "diagnostics.typeColumn": "Message type",
        "diagnostics.totalColumn": "Total",

        "reachability.status.reachable_direct": "Reachable (direct)",
        "reachability.status.reachable_group": "Reachable (group)",
        "reachability.status.unreachable_missing_recipient": "Unreachable: missing recipient record",
        "reachability.status.unreachable_missing_room": "Unreachable: missing room id",
        "reachability.status.unreachable_missing_sender": "Unreachable: missing sender",
        "reachability.status.unreachable_membership": "Unreachable: conversation membership miss",
        "reachability.status.unreachable_other": "Unreachable: other reason",
        "reachability.status.unknown": "Unknown status"
      }
    }
  };

  function hasLocale(code) {
    return Object.prototype.hasOwnProperty.call(LocaleRegistry, code);
  }

  function availableLocales() {
    return Object.keys(LocaleRegistry).map(function (code) {
      return LocaleRegistry[code];
    });
  }

  function getLocaleInfo(code) {
    return hasLocale(code) ? LocaleRegistry[code] : null;
  }

  function readStoredLocale() {
    try {
      if (typeof localStorage === "undefined") return null;
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function writeStoredLocale(code) {
    try {
      if (typeof localStorage === "undefined") return;
      localStorage.setItem(STORAGE_KEY, code);
    } catch (e) {
      // Private browsing / quota / disabled storage — locale still works
      // in-memory for the current page load, it just won't persist.
    }
  }

  var currentLocale = null;
  var changeListeners = [];

  function getLocale() {
    if (currentLocale === null) {
      var stored = readStoredLocale();
      currentLocale = hasLocale(stored) ? stored : DEFAULT_LOCALE;
    }
    return currentLocale;
  }

  function setLocale(code) {
    var next = hasLocale(code) ? code : DEFAULT_LOCALE;
    currentLocale = next;
    writeStoredLocale(next);
    for (var i = 0; i < changeListeners.length; i++) {
      try {
        changeListeners[i](next);
      } catch (e) {
        /* a listener error must not break locale switching for others */
      }
    }
    return next;
  }

  function onChange(fn) {
    if (typeof fn === "function") changeListeners.push(fn);
  }

  function t(key) {
    var locale = getLocale();
    var entry = LocaleRegistry[locale];
    if (entry && Object.prototype.hasOwnProperty.call(entry.translations, key)) {
      return entry.translations[key];
    }
    var fallback = LocaleRegistry[DEFAULT_LOCALE];
    if (fallback && Object.prototype.hasOwnProperty.call(fallback.translations, key)) {
      return fallback.translations[key];
    }
    return key;
  }

  var I18N = {
    t: t,
    getLocale: getLocale,
    setLocale: setLocale,
    availableLocales: availableLocales,
    getLocaleInfo: getLocaleInfo,
    hasLocale: hasLocale,
    onChange: onChange,
    defaultLocale: DEFAULT_LOCALE
  };

  global.LocaleRegistry = LocaleRegistry;
  global.I18N = I18N;
})(typeof globalThis !== "undefined" ? globalThis : this);
/* I18N_CORE_END */
