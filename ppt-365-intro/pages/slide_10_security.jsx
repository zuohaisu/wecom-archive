<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>安全与合规能力</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 28, height: 520 }}>
        {/* 左栏 60% 数据安全 */}
        <Box style={{
            width: 680,
            height: 520,
            borderRadius: 16,
            padding: 36,
            background: 'linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)',
            flexDirection: 'column',
            gap: 28,
        }}>
            <Box style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
                <Box style={{
                    width: 48, height: 48, borderRadius: 14,
                    background: 'rgba(255,255,255,0.2)',
                    justifyContent: 'center', alignItems: 'center',
                }}>
                    <FAIcon name='lock' style={{ fill: '#ffffff', width: 26, height: 26 }} />
                </Box>
                <Text style={{ fontSize: 28, fontWeight: 'bold', color: '#ffffff' }}>数据安全</Text>
            </Box>
            <Box style={{ flexDirection: 'column', gap: 20, flex: 1, justifyContent: 'center' }}>
                <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                    <FAIcon name='key' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.9 }} />
                    <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                        RSA + AES 端到端解密，支持 publickey_ver 密钥版本化轮换，历史消息仍可解密。
                    </Text>
                </Box>
                <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                    <FAIcon name='shield-alt' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.9 }} />
                    <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                        多租户数据隔离：tenant_id 级权限边界，所有查询按会话租户严格过滤。
                    </Text>
                </Box>
                <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                    <FAIcon name='database' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.9 }} />
                    <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                        可插拔媒体存储：本地磁盘或对象存储，媒体文件按 magic-byte 校验。
                    </Text>
                </Box>
                <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                    <FAIcon name='cloud' style={{ fill: '#ffffff', width: 20, height: 20, marginTop: 4, opacity: 0.9 }} />
                    <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(255,255,255,0.95)', flex: 1 }}>
                        消息与媒体资源分离存储，加密链路可控，降低数据泄露风险。
                    </Text>
                </Box>
            </Box>
        </Box>

        {/* 右栏 40% 合规审计 */}
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
                        <FAIcon name='user-shield' style={{ fill: '#06B6D4', width: 24, height: 24 }} />
                    </Box>
                    <Text style={{ fontSize: 26, fontWeight: 'bold', color: '#0F172A' }}>合规与审计</Text>
                </Box>
                <Box style={{ flexDirection: 'column', gap: 20, flex: 1, justifyContent: 'center' }}>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='id-card' style={{ fill: '#06B6D4', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            管理员通过 WeCom OAuth 登录，身份即企业身份，离职自动失效。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='chart-pie' style={{ fill: '#06B6D4', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            消息可达性审计：聚合统计消息到达与丢失情况，不触碰消息内容。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='history' style={{ fill: '#06B6D4', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            查看、检索、导出行为全程留痕，满足合规审计要求。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
                        <FAIcon name='server' style={{ fill: '#06B6D4', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 19, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            部署于企业自有云，数据不出域，边界可控。
                        </Text>
                    </Box>
                </Box>
            </Box>
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>10 / 13</Text>
    </Box>
</Slide>
