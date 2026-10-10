# 维护 HTML 图文档案

安装依赖：`python -m pip install -r tools/requirements.txt`。

```powershell
python -X utf8 tools/archive_html.py --source-root '<已发布公众号下载目录>' --work-dir '<D盘临时工作目录>'
```

只更新指定文章时增加 `--ids <12位ID> <12位ID>`。脚本以已保存 HTML 的 `#js_content` 和原样式生成阅读页；不是将 Markdown 转成 HTML。先复用有 URL 对应关系的 MHTML 图片，再从原来的公开微信图片地址补齐；不读取浏览器账号、Cookie 或登录态。

生成结果：仓库 `html/<年份>/<ID>/article.html` 与配套图片、正文中的本地图片引用、首页及主题下载入口、JSON/CSV 元数据。缓存及 ZIP 图文包放在 `--work-dir`，不提交缓存。图片下载失败会保留缓存，重跑仅补未成功的原地址；不会替换为示意图。

沿用现有同步脚本时，可传 `-HtmlPython <已安装依赖的Python路径>` 和 `-HtmlWorkDir <D盘工作目录>`；脚本在文章/索引更新后调用此生成器。

推送后，将单篇 `<ID>.zip` 上传到本仓库 `html-archive` Release。单篇下载入口固定使用该 Release；更新已有文章时替换同名包。合集入口使用 GitHub 自带的仓库 ZIP，根目录 `index.html` 提供带图阅读索引；工作目录中的 `wangge-articles-html.zip` 仅供本地交付备用。完成公开下载读回与解压图片检查后，再宣布更新完成。ZIP 不进 Git，避免图片重复占据仓库历史。

视频、小程序及微信互动依赖原平台，本包保留公众号原文入口。正文与配图遵循仓库 CC BY-NC-ND 4.0；本目录的辅助脚本采用 MIT License。
