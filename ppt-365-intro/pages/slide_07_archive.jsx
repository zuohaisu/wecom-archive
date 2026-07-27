<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>全量合规存档</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 28, height: 520 }}>
        {/* 左大图 */}
        <Box style={{ width: 680, height: 520, borderRadius: 16, overflow: 'hidden' }}>
            <Image
                src="resources/images/secure_decrypt.png"
                style={{ width: '100%', height: '100%', objectFit: 'cover' }}
            />
        </Box>

        {/* 右文字卡 */}
        <Box style={{
            width: 428,
            height: 520,
            borderRadius: 16,
            background: '#ffffff',
            boxShadow: '0 4px 20px rgba(15,23,42,0.08)',
            padding: 32,
            flexDirection: 'column',
            justifyContent: 'space-between',
        }}>
            <Box style={{ flexDirection: 'column', gap: 20 }}>
                <Text style={{ fontSize: 26, fontWeight: 'bold', color: '#0F172A' }}>从企业微信到安全仓库</Text>
                <Box style={{ flexDirection: 'column', gap: 18 }}>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='check-circle' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 3 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            通过企业微信会话存档 API（C SDK）定时拉取加密消息，确保不遗漏。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='check-circle' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 3 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            覆盖文本、图片、语音、视频、文件、表情等全消息类型。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='check-circle' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 3 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            1 对 1 私聊与群聊统一留存，消息顺序按 WeCom seq 有序归档。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='check-circle' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 3 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            不是备份，而是面向合规与审计的完整数字资产。
                        </Text>
                    </Box>
                </Box>
            </Box>
            <Box style={{
                borderRadius: 12,
                background: 'linear-gradient(135deg, rgba(59,130,246,0.1) 0%, rgba(6,182,212,0.1) 100%)',
                padding: 18,
                flexDirection: 'column',
                gap: 4,
            }}>
                <Text style={{
                    fontSize: 56,
                    fontWeight: 'bold',
                    color: 'transparent',
                    backgroundImage: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
                    backgroundClip: 'text',
                    lineHeight: 1,
                }}>100%</Text>
                <Text style={{ fontSize: 18, color: 'rgba(15,23,42,0.7)' }}>全消息类型覆盖</Text>
            </Box>
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>07 / 13</Text>
    </Box>
</Slide>
