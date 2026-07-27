<Slide style={{
    padding: '20px 72px',
    fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif",
    background: '#ffffff',
    flexDirection: 'column',
}}>
    {/* A 标题块 */}
    <Box style={{ height: 100, justifyContent: 'center' }}>
        <Text style={{ fontSize: 34, fontWeight: 'bold', color: '#0F172A' }}>技术架构与部署</Text>
    </Box>

    {/* B 内容区 */}
    <Box style={{ flex: 1, flexDirection: 'row', gap: 28, height: 520 }}>
        {/* 左侧架构 SVG */}
        <Box style={{
            width: 560,
            height: 520,
            borderRadius: 16,
            background: 'rgba(59,130,246,0.06)',
            justifyContent: 'center',
            alignItems: 'center',
        }}>
            <svg width={520} height={480} viewBox='0 0 520 480' style={{ fontFamily: "'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif" }}>
                <text x='260' y='28' textAnchor='middle' fontSize='18' fontWeight='bold' fill='#0F172A'>数据流与部署架构</text>

                {/* 节点 1 */}
                <rect x='150' y='45' width='220' height='56' rx='12' fill='#3B82F6' />
                <text x='260' y='80' textAnchor='middle' fontSize='16' fill='#ffffff' fontWeight='600'>企业微信</text>

                <line x1='260' y1='101' x2='260' y2='135' stroke='#3B82F6' strokeWidth='3' />
                <polygon points='260,142 255,135 265,135' fill='#3B82F6' />

                {/* 节点 2 */}
                <rect x='150' y='142' width='220' height='56' rx='12' fill='#06B6D4' />
                <text x='260' y='177' textAnchor='middle' fontSize='16' fill='#ffffff' fontWeight='600'>拉取加密消息</text>

                <line x1='260' y1='198' x2='260' y2='232' stroke='#06B6D4' strokeWidth='3' />
                <polygon points='260,239 255,232 265,232' fill='#06B6D4' />

                {/* 节点 3 */}
                <rect x='150' y='239' width='220' height='56' rx='12' fill='#3B82F6' />
                <text x='260' y='274' textAnchor='middle' fontSize='16' fill='#ffffff' fontWeight='600'>RSA + AES 解密</text>

                <line x1='260' y1='295' x2='260' y2='329' stroke='#3B82F6' strokeWidth='3' />
                <polygon points='260,336 255,329 265,329' fill='#3B82F6' />

                {/* 节点 4 */}
                <rect x='150' y='336' width='220' height='56' rx='12' fill='#06B6D4' />
                <text x='260' y='371' textAnchor='middle' fontSize='16' fill='#ffffff' fontWeight='600'>PostgreSQL 存储</text>

                <line x1='260' y1='392' x2='260' y2='426' stroke='#06B6D4' strokeWidth='3' />
                <polygon points='260,433 255,426 265,426' fill='#06B6D4' />

                {/* 节点 5 */}
                <rect x='150' y='433' width='220' height='56' rx='12' fill='#3B82F6' />
                <text x='260' y='468' textAnchor='middle' fontSize='16' fill='#ffffff' fontWeight='600'>审查控制台</text>

                {/* 媒体存储支线 */}
                <line x1='315' y1='364' x2='380' y2='364' stroke='#06B6D4' strokeWidth='2' strokeDasharray='6 4' />
                <rect x='390' y='336' width='100' height='56' rx='10' fill='#ffffff' stroke='#06B6D4' strokeWidth='2' />
                <text x='440' y='371' textAnchor='middle' fontSize='14' fill='#06B6D4' fontWeight='600'>媒体存储</text>
            </svg>
        </Box>

        {/* 右侧洞察 */}
        <Box style={{
            width: 548,
            height: 520,
            borderRadius: 16,
            background: '#ffffff',
            boxShadow: '0 4px 20px rgba(15,23,42,0.08)',
            padding: 32,
            flexDirection: 'column',
            justifyContent: 'space-between',
        }}>
            <Box style={{ flexDirection: 'column', gap: 20 }}>
                <Text style={{ fontSize: 26, fontWeight: 'bold', color: '#0F172A' }}>部署与运维优势</Text>
                <Box style={{ flexDirection: 'column', gap: 18 }}>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='cogs' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>标准组件：</span>Python FastAPI + PostgreSQL + systemd 定时器，可复用、易运维。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='server' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>分钟级部署：</span>阿里云 ECS 自动化脚本一键拉起，服务与定时器版本化管理。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='expand-arrows-alt' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>弹性扩展：</span>媒体存储可插拔（本地 → 对象存储），多租户模型支持规模化。
                        </Text>
                    </Box>
                    <Box style={{ flexDirection: 'row', gap: 12, alignItems: 'flex-start' }}>
                        <FAIcon name='tools' style={{ fill: '#3B82F6', width: 20, height: 20, marginTop: 4 }} />
                        <Text style={{ fontSize: 18, lineHeight: 1.6, color: 'rgba(15,23,42,0.85)', flex: 1 }}>
                            <span style={{ fontWeight: 'bold' }}>低运维：</span>自动同步、自动解密、自动媒体下载，管理员专注审查。
                        </Text>
                    </Box>
                </Box>
            </Box>
            <Box style={{
                borderRadius: 12,
                background: 'linear-gradient(135deg, rgba(59,130,246,0.1) 0%, rgba(6,182,212,0.1) 100%)',
                padding: 16,
                flexDirection: 'row',
                alignItems: 'center',
                gap: 12,
            }}>
                <FAIcon name='check-circle' style={{ fill: '#3B82F6', width: 28, height: 28 }} />
                <Text style={{ fontSize: 18, fontWeight: 600, color: '#0F172A' }}>企业自有云部署，数据不出域</Text>
            </Box>
        </Box>
    </Box>

    {/* C 页脚条 */}
    <Box style={{ height: 60, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>365企微会话存档</Text>
        <Text style={{ fontSize: 14, color: 'rgba(15,23,42,0.5)' }}>12 / 13</Text>
    </Box>
</Slide>
