<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>会话审查控制台</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'column', gap: 24, height: 520 }}>
        {/* 上方大图 */}
        <Box style={{ width: 1136, height: 286, borderRadius: 16, overflow: 'hidden' }}>
            <Image
                src="resources/images/console_review.png"
                style={{ width: '100%', height: '100%', objectFit: 'cover' }}
            />
        </Box>

        {/* 下方 3 卡片 */}
        <Box style={{ flexDirection: 'row', gap: 24, height: 210, alignItems: 'stretch' }}>
            {[
                ['columns', '三栏审查', '员工 / 联系人选择 → 会话列表 → 消息时间线，信息层级清晰'],
                ['comment-dots', '气泡还原', '还原企业微信聊天样式，阅读体验与原生对话一致'],
                ['user-shield', '权限管控', '授权管理员可调阅，企业微信 OAuth 登录，操作全程留痕'],
            ].map(([icon, title, desc], idx) => (
                <Box key={idx} style={{
                    flex: 1,
                    borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                    padding: 22,
                    flexDirection: 'column',
                    gap: 12,
                }}>
                    <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                        <Box style={{
                            width: 40, height: 40, borderRadius: 10,
                            background: 'rgba(59,130,246,0.15)',
                            justifyContent: 'center', alignItems: 'center',
                        }}>
                            <FAIcon name={icon} style={{ fill: '#3B82F6', width: 20, height: 20 }} />
                        </Box>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>{title}</Text>
                    </Box>
                    <Text style={{ fontSize: 18, lineHeight: 1.55, color: 'rgba(15,23,42,0.7)' }}>{desc}</Text>
                </Box>
            ))}
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>09 / 13</Text>
    </Box>
</Slide>
