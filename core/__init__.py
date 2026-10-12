"""astrbot_plugin_oil_price 的业务子包。

本目录存放插件的核心实现模块，按职责拆分：

- config          常量与配置辅助（省份、标号映射、价格解析）
- oil_db          SQLite 存储层
- oil_fetch       ShowAPI 138-46 / 138-49 客户端
- oil_east        东方财富 datacenter 客户端（历史初始化主路径 + 每日降级来源）
- oil_history     历史初始化流水线（直连东方财富 -> 过滤 -> 入库）
- oil_analysis    涨跌对比、最低价分析与加油建议
- oil_args        指令参数解析（省份 / 油号 / 数量，顺序自由 + 偏好补齐）
- oil_calc        加油计算器（单位解析 + Decimal 精确换算）
- oil_render      出图（Jinja2 模板 + html_render）
- oil_push        主动推送
- oil_whitelist   推送白名单解析与会话判定
- oil_http        本地 HTTP 取图服务（aiohttp，独立端口）

注意：AstrBot 以 ``data.plugins.<插件目录>.main`` 的形式导入插件，
因此插件目录自身即是一个包，这里统一使用相对导入。
"""
