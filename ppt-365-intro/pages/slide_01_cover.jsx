<Slide style={{
    position: 'relative',
    padding: 0,
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    color: '#ffffff',
    overflow: 'hidden',
}}>
    {/* 全幅背景图 */}
    <Box style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 0 }}>
        <Image
            src="resources/images/hero_cover.png"
            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
        />
        <Box style={{
            position: 'absolute',
            top: 0, left: 0, right: 0, bottom: 0,
            background: 'linear-gradient(135deg, rgba(15,23,42,0.92) 0%, rgba(15,23,42,0.55) 55%, rgba(15,23,42,0.15) 100%)',
        }} />
    </Box>

    {/* 内容区 */}
    <Box style={{
        position: 'relative',
        zIndex: 1,
        padding: '48px 72px 40px',
        height: '100%',
        flexDirection: 'column',
        justifyContent: 'center',
    }}>
        <Box style={{
            width: 720,
            flexDirection: 'column',
            gap: 28,
        }}>
            <Box style={{
                width: 80,
                height: 6,
                borderRadius: 3,
                background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
                marginBottom: 8,
            }} />
            <Text style={{
                fontSize: 64,
                fontWeight: 'bold',
                lineHeight: 1.15,
                letterSpacing: 2,
                textShadow: '0 4px 20px rgba(0,0,0,0.35)',
            }}>
                365企微会话存档
            </Text>
            <Text style={{
                fontSize: 28,
                fontWeight: 500,
                lineHeight: 1.4,
                color: 'rgba(255,255,255,0.92)',
                textShadow: '0 2px 12px rgba(0,0,0,0.3)',
            }}>
                企业微信会话合规存档 · 智能审查 · 安全可追溯
            </Text>
            <Text style={{
                fontSize: 22,
                lineHeight: 1.6,
                color: 'rgba(255,255,255,0.75)',
                marginTop: 12,
                maxWidth: 620,
            }}>
                让每一句企业微信对话，都成为可信赖的合规资产。一体化平台，实现全量存档、秒级检索、授权审查。
            </Text>
            <Text style={{
                fontSize: 14,
                color: 'rgba(255,255,255,0.55)',
                marginTop: 48,
            }}>
                产品介绍 · 2026
            </Text>
        </Box>
    </Box>
</Slide>
