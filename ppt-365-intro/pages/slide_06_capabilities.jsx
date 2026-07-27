<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>核心能力总览</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 32, height: 520 }}>
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
            <Box style={{ flexDirection: 'column', gap: 18 }}>
                <Text style={{ fontSize: 48, fontWeight: 'bold', color: '#ffffff', lineHeight: 1.2 }}>核心能力</Text>
                <Text style={{ fontSize: 20, color: 'rgba(255,255,255,0.9)', lineHeight: 1.6 }}>
                    覆盖存档、解密、检索、审查、审计、体验六大环节，<br />形成完整会话合规闭环。
                </Text>
            </Box>
            <Text style={{ fontSize: 14, color: 'rgba(255,255,255,0.7)' }}>6 CAPABILITIES</Text>
        </Box>

        {/* 右侧 3 行 × 2 列能力卡 */}
        <Box style={{ flex: 1, height: 520, flexDirection: 'column', gap: 20 }}>
            {/* 第 1 行 */}
            <Box style={{ flexDirection: 'row', gap: 16, height: 160 }}>
                <Box style={{
                    width: 374, height: 160, borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                    padding: 22, flexDirection: 'column', gap: 12,
                }}>
                    <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                        <Box style={{ width: 40, height: 40, borderRadius: 10, background: 'rgba(59,130,246,0.15)', justifyContent: 'center', alignItems: 'center' }}>
                            <FAIcon name='database' style={{ fill: '#3B82F6', width: 20, height: 20 }} />
                        </Box>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>全量合规存档</Text>
                    </Box>
                    <Text style={{ fontSize: 18, lineHeight: 1.55, color: 'rgba(15,23,42,0.7)' }}>文本、图片、语音、视频、文件全消息类型，私聊+群聊全量留存。</Text>
                </Box>
                <Box style={{
                    width: 374, height: 160, borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                    padding: 22, flexDirection: 'column', gap: 12,
                }}>
                    <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                        <Box style={{ width: 40, height: 40, borderRadius: 10, background: 'rgba(59,130,246,0.15)', justifyContent: 'center', alignItems: 'center' }}>
                            <FAIcon name='lock' style={{ fill: '#3B82F6', width: 20, height: 20 }} />
                        </Box>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>端到端安全解密</Text>
                    </Box>
                    <Text style={{ fontSize: 18, lineHeight: 1.55, color: 'rgba(15,23,42,0.7)' }}>RSA + AES 解密流水线，支持密钥版本化轮换。</Text>
                </Box>
            </Box>

            {/* 第 2 行 */}
            <Box style={{ flexDirection: 'row', gap: 16, height: 160 }}>
                <Box style={{
                    width: 374, height: 160, borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                    padding: 22, flexDirection: 'column', gap: 12,
                }}>
                    <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                        <Box style={{ width: 40, height: 40, borderRadius: 10, background: 'rgba(59,130,246,0.15)', justifyContent: 'center', alignItems: 'center' }}>
                            <FAIcon name='search' style={{ fill: '#3B82F6', width: 20, height: 20 }} />
                        </Box>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>毫秒级会话检索</Text>
                    </Box>
                    <Text style={{ fontSize: 18, lineHeight: 1.55, color: 'rgba(15,23,42,0.7)' }}>关键词、联系人、时间范围多维检索，中英双语界面。</Text>
                </Box>
                <Box style={{
                    width: 374, height: 160, borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                    padding: 22, flexDirection: 'column', gap: 12,
                }}>
                    <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                        <Box style={{ width: 40, height: 40, borderRadius: 10, background: 'rgba(59,130,246,0.15)', justifyContent: 'center', alignItems: 'center' }}>
                            <FAIcon name='desktop' style={{ fill: '#3B82F6', width: 20, height: 20 }} />
                        </Box>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>三栏审查控制台</Text>
                    </Box>
                    <Text style={{ fontSize: 18, lineHeight: 1.55, color: 'rgba(15,23,42,0.7)' }}>员工/联系人 → 会话列表 → 消息时间线，还原企业微信气泡。</Text>
                </Box>
            </Box>

            {/* 第 3 行 */}
            <Box style={{ flexDirection: 'row', gap: 16, height: 160 }}>
                <Box style={{
                    width: 374, height: 160, borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                    padding: 22, flexDirection: 'column', gap: 12,
                }}>
                    <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                        <Box style={{ width: 40, height: 40, borderRadius: 10, background: 'rgba(59,130,246,0.15)', justifyContent: 'center', alignItems: 'center' }}>
                            <FAIcon name='chart-line' style={{ fill: '#3B82F6', width: 20, height: 20 }} />
                        </Box>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>消息可达性审计</Text>
                    </Box>
                    <Text style={{ fontSize: 18, lineHeight: 1.55, color: 'rgba(15,23,42,0.7)' }}>系统诊断页面，聚合统计消息可达性，不触碰内容。</Text>
                </Box>
                <Box style={{
                    width: 374, height: 160, borderRadius: 12,
                    background: 'rgba(59,130,246,0.08)',
                    padding: 22, flexDirection: 'column', gap: 12,
                }}>
                    <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                        <Box style={{ width: 40, height: 40, borderRadius: 10, background: 'rgba(59,130,246,0.15)', justifyContent: 'center', alignItems: 'center' }}>
                            <FAIcon name='comment' style={{ fill: '#3B82F6', width: 20, height: 20 }} />
                        </Box>
                        <Text style={{ fontSize: 21, fontWeight: 'bold', color: '#0F172A' }}>企业微信原生体验</Text>
                    </Box>
                    <Text style={{ fontSize: 18, lineHeight: 1.55, color: 'rgba(15,23,42,0.7)' }}>WeCom OAuth 登录，操作留痕，界面语言一键切换。</Text>
                </Box>
            </Box>
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>06 / 13</Text>
    </Box>
</Slide>
