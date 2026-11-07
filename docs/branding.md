# 品牌资源

[文档目录](index.md)

<img src="../logo.svg" width="560" alt="recoding... — A Recursive Coding Agent">

<img src="../logomark.svg" width="80" height="80" alt="recoding 的递归循环标记">

[完整 Logo](../logo.svg) 用于 README、文档和界面帮助面板；[Logomark](../logomark.svg) 用于侧栏、运行标记和浏览器标签图标。原始 SVG 保存在项目根目录，Web 打包副本位于 `apps/web/recoding_web/static/`，更新图形时同步复制这两个文件。图形保持原比例，由服务端提供，不依赖 CDN 或浏览器本机文件。

## 界面截图

`images/web.png` 是 Web 对合成离线 Session 的只读展示；`images/cli.png` 来自真实 `prompt_toolkit` 屏幕缓冲区，使用同一组示例消息。两者不表示真实 API 响应或模型任务结果。

截图由 `tests/browser/readme.cjs` 复现。测试 Session、原始屏幕缓冲和临时报告位于 `sessions/tests/`，测试结束后自动清理；发布用 PNG 保存到 docs/images，文档只收录不含密钥、真实用户数据或部署方路径的展示图片；路径与时间使用固定合成值，生成器同时检查截图前的展示内容。执行环境见 [开发与测试](development.md)。
