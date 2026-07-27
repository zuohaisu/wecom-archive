<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>毫秒级会话检索</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 40, height: 520, alignItems: 'center' }}>
        {/* 左侧巨型数字锚点 */}
        <Box style={{
            width: 480,
            height: 520,
            flexDirection: 'column',
            justifyContent: 'center',
            alignItems: 'center',
            gap: 16,
        }}>
            <Box style={{
                width: 280,
                height: 280,
                borderRadius: 140,
                background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
                justifyContent: 'center',
                alignItems: 'center',
                boxShadow: '0 20px 50px rgba(59,130,246,0.25)',
                flexDirection: 'column',
                gap: 8,
            }}>
                <Text style={{
                    fontSize: 96,
                    fontWeight: 'bold',
                    color: '#ffffff',
                    lineHeight: 1,
                }}>100%</Text>
                <Text style={{ fontSize: 22, color: 'rgba(255,255,255,0.9)' }}>全量可检索</Text>
            </Box>
            <Text style={{ fontSize: 18, color: 'rgba(15,23,42,0.55)', marginTop: 8 }}>毫秒级响应 · 多维定位</Text>
        </Box>

        {/* 右侧洞察列表 */}
        <Box style={{
            flex: 1,
            height: 520,
            flexDirection: 'column',
            justifyContent: 'center',
            gap: 22,
        }}>
            {[
                ['search', '多维检索', '关键词、联系人、会话类型、时间范围交叉筛选，快速定位目标对话'],
                ['clock', '时间线浏览', '按时间顺序展示完整会话，滚动自动加载更早记录，上下文一目了然'],
                ['language', '中英双语', '管理后台支持中英文切换，满足跨国团队与外资合规要求'],
                ['download', '结果可追溯', '检索条件与查看记录均留痕，满足审计与合规调阅要求'],
            ].map(([icon, title, desc], idx) => (
                <Box key={idx} style={{
                    flexDirection: 'row',
                    alignItems: 'flex-start',
                    gap: 18,
                    padding: '20px 22px',
                    borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                }}>
                    <Box style={{
                        width: 44, height: 44, borderRadius: 12,
                        background: 'rgba(59,130,246,0.15)',
                        justifyContent: 'center', alignItems: 'center',
                    }}>
                        <FAIcon name={icon} style={{ fill: '#3B82F6', width: 22, height: 22 }} />
                    </Box>
                    <Box style={{ flexDirection: 'column', gap: 6, flex: 1 }}>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>{title}</Text>
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.7)' }}>{desc}</Text>
                    </Box>
                </Box>
            ))}
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>08 / 13</Text>
    </Box>
</Slide>
