<Slide style={{
    position: 'relative',
    padding: 0,
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    color: '#ffffff',
    overflow: 'hidden',
}}>
    {/* 全幅背景 */}
    <Box style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 0 }}>
        <Image
            src="resources/images/risk_concept.png"
            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
        />
        <Box style={{
            position: 'absolute',
            top: 0, left: 0, right: 0, bottom: 0,
            background: 'linear-gradient(90deg, rgba(15,23,42,0.88) 0%, rgba(15,23,42,0.5) 50%, rgba(15,23,42,0.18) 100%)',
        }} />
    </Box>

    {/* 内容区 */}
    <Box style={{
        position: 'relative',
        zIndex: 1,
        padding: '60px 72px',
        height: '100%',
        flexDirection: 'column',
        justifyContent: 'center',
    }}>
        <Box style={{ width: 640, flexDirection: 'column', gap: 24 }}>
            <Text style={{
                fontSize: 120,
                fontWeight: 'bold',
                lineHeight: 1,
                color: 'transparent',
                backgroundImage: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
                backgroundClip: 'text',
                marginBottom: -8,
            }}>01</Text>
            <Text style={{
                fontSize: 48,
                fontWeight: 'bold',
                lineHeight: 1.2,
                textShadow: '0 4px 20px rgba(0,0,0,0.3)',
            }}>
                为什么需要<br />会话存档
            </Text>
            <Text style={{
                fontSize: 22,
                lineHeight: 1.7,
                color: 'rgba(255,255,255,0.85)',
                maxWidth: 540,
            }}>
                在监管趋严与私域运营并行的今天，<br />"看不见的对话"正成为企业最大的风险敞口。
            </Text>
        </Box>
    </Box>
</Slide>
