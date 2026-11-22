# 每日热点简报（全自动手机版）

零第三方依赖，每天北京时间 **07:45** 自动在云端抓取国内、国际、科技、AI、财经新闻，调用 DeepSeek 提炼去重并发布到手机网页。

## 手机端 3 分钟部署指南

1. **上传文件**：在 GitHub 新建一个公开仓库（如 `daily-brief`），把压缩包解压后的全部文件（包含 `.github/workflows/daily-brief.yml`）上传进去。
2. **配置 API Key**：进入仓库 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`：
   - Name 填：`DEEPSEEK_API_KEY`
   - Secret 填：你的 DeepSeek API Key
3. **开启网页托管**：进入仓库 `Settings` -> `Pages`，将 `Source` 改为 **`GitHub Actions`**。
4. **立即测试**：进入顶部 `Actions` -> 选 `Daily Hotspot Brief` -> 点 `Run workflow`。等待约 40 秒变绿后，点击生成的网址即可在手机查看。
5. **变成手机 App**：用手机浏览器打开网址，选择「添加到主屏幕」，以后每天直接点图标看最新简报。
