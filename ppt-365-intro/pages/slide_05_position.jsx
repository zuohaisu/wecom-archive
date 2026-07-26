<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
    position: 'relative',
}}>
    {/* 装饰圆 */}
    <Box style={{
        position: 'absolute',
        top: 80, right: 120,
        width: 240, height: 240, borderRadius: 120,
        background: 'rgba(59,130,246,0.06)',
        zIndex: 0,
    }} />
    <Box style={{
        position: 'absolute',
        bottom: 120, left: 80,
        width: 180, height: 180, borderRadius: 90,
        background: 'rgba(6,182,212,0.06)',
        zIndex: 0,
    }} />

    {/* A 标题块（可省略标题，用居中内容替代） */}
    <Box style={{ height: 100, zIndex: 1 }} />

    {/* B 内容区 */}
    <Box style={{
        flex: 1,
        zIndex: 1,
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
        gap: 28,
        height: 520,
    }}>
        <Box style={{
            width: 180, height: 180, borderRadius: 90,
            background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
            justifyContent: 'center', alignItems: 'center',
            boxShadow: '0 16px 40px rgba(59,130,246,0.25)',
        }}>
            <FAIcon name='comments' style={{ fill: '#ffffff', width: 80, height: 80 }} />
        </Box>
        <Text style={{
            fontSize: 40,
            fontWeight: 'bold',
            color: '#0F172A',
            textAlign: 'center',
            marginTop: 8,
        }}>
            365企微会话存档
        </Text>
        <Text style={{
            fontSize: 30,
            fontWeight: 600,
            lineHeight: 1.4,
            color: 'transparent',
            backgroundImage: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
            backgroundClip: 'text',
            textAlign: 'center',
            maxWidth: 900,
        }}>
            企业微信会话合规存档 + 智能审查一体化平台
        </Text>
        <Text style={{
            fontSize: 22,
            lineHeight: 1.7,
            color: 'rgba(15,23,42,0.7)',
            textAlign: 'center',
            maxWidth: 780,
            marginTop: 8,
        }}>
            让对话从"阅后即焚"变为"全程留痕、随时可查、安全可控"，<br />把散落在企业微信里的沟通资产变成可审计的合规资产。
        </Text>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', zIndex: 1 }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>05 / 13</Text>
    </Box>
</Slide>
