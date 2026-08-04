/* All names, organisations, messages, IDs, and metrics in this file are fictional demo data. */
window.CrowntimeDemoData = {
  conversations: [
    {
      id: "orbit",
      name: "星河项目协作群",
      type: "群聊 · 6 人",
      time: "10:42",
      snippet: "我把报价方案和交付节点整理好了。",
      participants: [
        ["林舟", "员工", "林"],
        ["苏晴", "员工", "苏"],
        ["程远", "外部联系人", "程"],
        ["项目组", "群聊", "群"]
      ],
      stats: [["归档消息", "248"], ["媒体附件", "12"], ["最近活跃", "10:42"], ["审阅记录", "6"]],
      messages: [
        { sender: "苏晴 · 员工", time: "10:26", text: "程总，今天的项目进展和交付节点我已经汇总，方便的话请您确认一下优先级。", self: true },
        { sender: "程远 · 星河示例科技", time: "10:31", text: "收到。我们更关心第一阶段的验收时间，报价方案也请一并发群里。", self: false },
        { sender: "林舟 · 员工", time: "10:36", text: "已补充到方案中。第一阶段按演示环境的样例数据安排，后续以双方确认的范围为准。", self: true },
        { sender: "苏晴 · 员工", time: "10:42", file: "项目方案_v2.pdf", meta: "PDF · 1.8 MB", self: true }
      ]
    },
    {
      id: "quote",
      name: "林舟 ↔ 周宁",
      type: "单聊",
      time: "昨天",
      snippet: "报价和容量规则已经写清楚了。",
      participants: [["林舟", "员工", "林"], ["周宁", "外部联系人", "周"]],
      stats: [["归档消息", "86"], ["媒体附件", "3"], ["最近活跃", "昨天"], ["审阅记录", "2"]],
      messages: [
        { sender: "周宁 · 云帆演示贸易", time: "昨天 16:08", text: "请问系统是按员工数量收费，还是按存储空间收费？", self: false },
        { sender: "林舟 · 员工", time: "昨天 16:11", text: "示例套餐不按座席计费：99 元/年含 5GB，超出部分按 1 元/GB/月增加。", self: true },
        { sender: "周宁 · 云帆演示贸易", time: "昨天 16:14", text: "明白了，我们先看一下系统里的检索和审计功能。", self: false }
      ]
    },
    {
      id: "support",
      name: "售后问题跟进群",
      type: "群聊 · 4 人",
      time: "周一",
      snippet: "客户已确认收到处理说明。",
      participants: [["苏晴", "员工", "苏"], ["赵可", "员工", "赵"], ["何安", "外部联系人", "何"]],
      stats: [["归档消息", "154"], ["媒体附件", "8"], ["最近活跃", "周一"], ["审阅记录", "4"]],
      messages: [
        { sender: "何安 · 演示客户", time: "周一 14:03", text: "问题已经处理好了，谢谢。请把本次沟通记录留存一下。", self: false },
        { sender: "赵可 · 员工", time: "周一 14:05", text: "好的，本群会话已按授权范围归档，可通过关键词和时间筛选回溯。", self: true },
        { sender: "苏晴 · 员工", time: "周一 14:07", text: "处理说明已同步，后续如需补充请在群内留言。", self: true }
      ]
    }
  ],
  searchResults: [
    { conversation: "星河项目协作群", sender: "林舟 · 员工", time: "2026-08-03 10:36", text: "已补充到方案中。第一阶段按演示环境的样例数据安排，后续以双方确认的范围为准。", id: "msg_demo_0048" },
    { conversation: "林舟 ↔ 周宁", sender: "周宁 · 云帆演示贸易", time: "2026-08-02 16:14", text: "明白了，我们先看一下系统里的检索和审计功能。", id: "msg_demo_0121" },
    { conversation: "售后问题跟进群", sender: "赵可 · 员工", time: "2026-08-01 14:05", text: "本群会话已按授权范围归档，可通过关键词和时间筛选回溯。", id: "msg_demo_0210" },
    { conversation: "星河项目协作群", sender: "苏晴 · 员工", time: "2026-07-30 09:20", text: "归档数据仅供被授权的管理员审阅，查看动作会保留操作记录。", id: "msg_demo_0298" }
  ]
};
