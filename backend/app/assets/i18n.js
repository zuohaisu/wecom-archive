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
