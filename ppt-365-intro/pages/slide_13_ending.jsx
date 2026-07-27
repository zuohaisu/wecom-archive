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
            src="resources/images/hero_cover.png"
            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
        />
        <Box style={{
            position: 'absolute',
            top: 0, left: 0, right: 0, bottom: 0,
            background: 'linear-gradient(135deg, rgba(15,23,42,0.9) 0%, rgba(15,23,42,0.55) 50%, rgba(15,23,42,0.2) 100%)',
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
        alignItems: 'center',
        gap: 32,
    }}>
        <Text style={{
            fontSize: 44,
            fontWeight: 'bold',
            lineHeight: 1.5,
            color: '#ffffff',
            textAlign: 'center',
            maxWidth: 960,
            textShadow: '0 4px 20px rgba(0,0,0,0.3)',
        }}>
            让每一次企业微信对话<br />都成为可信赖的合规资产
        </Text>
        <Box style={{
            flexDirection: 'row',
            alignItems: 'center',
            gap: 16,
            marginTop: 8,
        }}>
            <Box style={{
                padding: '16px 40px',
                borderRadius: 30,
                background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
                boxShadow: '0 8px 24px rgba(59,130,246,0.35)',
            }}>
                <Text style={{ fontSize: 22, fontWeight: 600, color: '#ffffff' }}>立即预约演示</Text>
            </Box>
        </Box>
        <Text style={{
            fontSize: 20,
            color: 'rgba(255,255,255,0.7)',
            marginTop: 16,
            textAlign: 'center',
        }}>
            365企微会话存档 · 开启会话合规之旅
        </Text>
    </Box>
</Slide>
