<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>应用场景</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 28, height: 520 }}>
        {/* 左大图 */}
        <Box style={{ width: 680, height: 520, borderRadius: 16, overflow: 'hidden' }}>
            <Image
                src="resources/images/scenario.png"
                style={{ width: '100%', height: '100%', objectFit: 'cover' }}
            />
        </Box>

        {/* 右文字卡 */}
        <Box style={{
            width: 428,
            height: 520,
            borderRadius: 16,
            padding: 32,
            background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
            flexDirection: 'column',
            justifyContent: 'space-between',
        }}>
            <Box style={{ flexDirection: 'column', gap: 20 }}>
                <Text style={{ fontSize: 26, fontWeight: 'bold', color: '#ffffff' }}>4 大典型场景</Text>
                <Box style={{ flexDirection: 'column', gap: 18 }}>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='building' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.95 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>金融保险销售合规：</span>理财销售留痕、双录补充、适当性管理。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='headset' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.95 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>客户成功客诉举证：</span>服务承诺还原、客诉处理、服务质检。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='users' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.95 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>私域运营防飞单：</span>防止员工私加客户、保护渠道价格与客户资产。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='user-check' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.95 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>离职继承与交接：</span>客户关系不随人走，会话记录可完整继承。
                        </Text>
                    </Box>
                </Box>
            </Box>
            <Box style={{
                borderRadius: 12,
                background: 'rgba(255,255,255,0.2)',
                padding: 16,
                flexDirection: 'column',
                gap: 4,
            }}>
                <Text style={{ fontSize: 36, fontWeight: 'bold', color: '#ffffff', lineHeight: 1 }}>4 大场景</Text>
                <Text style={{ fontSize: 16, color: 'rgba(255,255,255,0.9)' }}>覆盖销售、客服、运营、HR 等高频需求</Text>
            </Box>
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>11 / 13</Text>
    </Box>
</Slide>
