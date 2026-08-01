# PotatoFlow v1.6.1

- 修复 Windows 安装版首次启动时，托盘图标的 ICO 资源无法被 Pillow 完整解码，导致 `buffer is not large enough` 崩溃的问题。
- 托盘运行时改用可验证的 PNG 图标；Windows EXE 和安装器文件图标保持不变。
- Windows 发布流程新增打包后桌面资源自检，防止仅测试服务模式时遗漏桌面启动问题。
