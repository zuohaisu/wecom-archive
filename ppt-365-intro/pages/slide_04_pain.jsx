<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>合规压力与管理盲区</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 28, height: 520 }}>
        {/* 左栏 60% 监管合规 */}
        <Box style={{
            width: 680,
            height: 520,
            borderRadius: 16,
            background: '#ffffff',
            boxShadow: '0 4px 20px rgba(15,23,42,0.08)',
            overflow: 'hidden',
            flexDirection: 'column',
        }}>
            <Box style={{
                height: 6,
                background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
            }} />
            <Box style={{ padding: '30px 32px', flexDirection: 'column', gap: 24, flex: 1 }}>
                <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
                    <Box style={{
                        width: 44, height: 44, borderRadius: 12,
                        background: 'rgba(59,130,246,0.12)',
                        justifyContent: 'center', alignItems: 'center',
                    }}>
                        <FAIcon name='shield-alt' style={{ fill: '#3B82F6', width: 24, height: 24 }} />
                    </Box>
                    <Text style={{ fontSize: 26, fontWeight: 'bold', color: '#0F172A' }}>监管合规压力</Text>
                </Box>
                <Box style={{ flexDirection: 'column', gap: 22, flex: 1, justifyContent: 'center' }}>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='file-alt' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            金融、保险、证券等行业监管要求企业微信会话内容必须留存备查，确保沟通记录可举证、可审计。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='balance-scale' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            《网络安全法》《数据安全法》《个人信息保护法》要求个人信息处理全程留痕，合规检查必须有据可依。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='gavel' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            客诉、劳资纠纷、反洗钱审计等场景下，需要快速调取完整对话，避免举证不能或证据缺失。
                        </Text>
                    </Box>
                </Box>
            </Box>
        </Box>

        {/* 右栏 40% 管理盲区 */}
        <Box style={{
            width: 428,
            height: 520,
            borderRadius: 16,
            background: '#ffffff',
            boxShadow: '0 4px 20px rgba(15,23,42,0.08)',
            overflow: 'hidden',
            flexDirection: 'column',
        }}>
            <Box style={{
                height: 6,
                background: 'linear-gradient(135deg, #06B6D4 0%, #3B82F6 100%)',
            }} />
            <Box style={{ padding: '30px 32px', flexDirection: 'column', gap: 24, flex: 1 }}>
                <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
                    <Box style={{
                        width: 44, height: 44, borderRadius: 12,
                        background: 'rgba(6,182,212,0.12)',
                        justifyContent: 'center', alignItems: 'center',
                    }}>
                        <FAIcon name='eye-slash' style={{ fill: '#06B6D4', width: 24, height: 24 }} />
                    </Box>
                    <Text style={{ fontSize: 26, fontWeight: 'bold', color: '#0F172A' }}>管理盲区</Text>
                </Box>
                <Box style={{ flexDirection: 'column', gap: 22, flex: 1, justifyContent: 'center' }}>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='exclamation-circle' style={{ fill: '#06B6D4', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            飞单、私单、带走客户：员工私加客户微信交易，企业难追溯、难取证。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='comment' style={{ fill: '#06B6D4', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            服务态度与口头承诺不一致，客诉纠纷无据可依，品牌口碑受损。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='user-times' style={{ fill: '#06B6D4', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            离职交接断层：客户关系和会话记录随人走，企业资产流失。
                        </Text>
                    </Box>
                </Box>
            </Box>
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>04 / 13</Text>
    </Box>
</Slide>
