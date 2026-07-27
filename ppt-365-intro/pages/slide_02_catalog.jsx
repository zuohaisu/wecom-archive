<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>目录</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 36, height: 520 }}>
        {/* 左标题栏 */}
        <Box style={{
            width: 340,
            height: 520,
            borderRadius: 16,
            padding: 32,
            background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
            flexDirection: 'column',
            justifyContent: 'space-between',
        }}>
            <Box style={{ flexDirection: 'column', gap: 20 }}>
                <Text style={{ fontSize: 48, fontWeight: 'bold', color: '#ffffff', lineHeight: 1.2 }}>目录</Text>
                <Text style={{ fontSize: 20, color: 'rgba(255,255,255,0.9)', lineHeight: 1.5 }}>
                    5 大章节<br />一览核心卖点
                </Text>
            </Box>
            <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                <Box style={{ width: 48, height: 4, borderRadius: 2, background: '#ffffff' }} />
                <Text style={{ fontSize: 14, color: 'rgba(255,255,255,0.8)' }}>CONTENTS</Text>
            </Box>
        </Box>

        {/* 右内容列表 */}
        <Box style={{ flex: 1, flexDirection: 'column', gap: 16, height: 520, justifyContent: 'center' }}>
            {[
                ['01', '为什么需要会话存档', '监管合规压力与企业管理盲区的双重驱动'],
                ['02', '产品定位与核心能力', '一个平台解决存档、解密、检索、审查四大环节'],
                ['03', '关键能力详解', '全量合规存档 · 毫秒级检索 · 三栏审查控制台'],
                ['04', '安全与合规', '端到端加密、租户隔离、审计留痕'],
                ['05', '应用场景与部署', '金融、客服、私域、离职继承等典型场景'],
            ].map(([num, title, desc]) => (
                <Box key={num} style={{
                    flexDirection: 'row',
                    alignItems: 'center',
                    gap: 20,
                    padding: '22px 24px',
                    borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                }}>
                    <Box style={{
                        width: 44,
                        height: 44,
                        borderRadius: 22,
                        background: '#3B82F6',
                        justifyContent: 'center',
                        alignItems: 'center',
                    }}>
                        <Text style={{ fontSize: 18, fontWeight: 'bold', color: '#ffffff' }}>{num}</Text>
                    </Box>
                    <Box style={{ flexDirection: 'column', gap: 6 }}>
                        <Text style={{ fontSize: 22, fontWeight: 'bold', color: '#0F172A' }}>{title}</Text>
                        <Text style={{ fontSize: 18, color: 'rgba(15,23,42,0.65)', lineHeight: 1.5 }}>{desc}</Text>
                    </Box>
                </Box>
            ))}
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>02 / 13</Text>
    </Box>
</Slide>
