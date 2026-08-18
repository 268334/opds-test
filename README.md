# 南方周末 → OPDS → KOReader（v1）

这个项目会在 GitHub Actions 云端运行，不需要家里的电脑开机。

## v1 做什么

- 定时检查南方周末官网的公开栏目；
- 读取前一天文章的公开标题、公开摘要和原文链接；
- 如果抓取失败，不发布；
- 同一日期的文章清单必须连续两次一致，才生成 EPUB；
- 自动生成 OPDS `catalog.xml`；
- 自动部署到 GitHub Pages；
- KOReader 继续使用你已经配置好的 OPDS 地址。

> v1 不尝试绕过登录、会员、付费或其他访问限制，也不把隐藏的会员正文抓进 EPUB。

## 第一次安装

### 1. 上传项目

把这个 ZIP 解压，把里面的所有文件和文件夹上传到你现有的 GitHub 仓库根目录。

目录应该类似：

```text
.github/
  workflows/
    update.yml
scripts/
  build.py
state/
site/
  books/
config.json
requirements.txt
README.md
```

你原来的 `test.epub` / `catalog.xml` / `index.html` 可以保留，也可以删除。
切换到 GitHub Actions Pages 后，实际发布的是 `site/` 目录。

### 2. 把 Pages 发布方式改成 GitHub Actions

GitHub 仓库：

Settings → Pages → Build and deployment → Source → **GitHub Actions**

这一步很重要。不要继续用 “Deploy from a branch”。

### 3. 手动运行一次

进入：

Actions → **Build South Weekend OPDS** → Run workflow

第一次正常情况通常只会记录“文章清单快照”，日志中会出现：

```text
[wait] first/changed snapshot
```

这是完整性保护，不是错误。

### 4. 再运行第二次

过几分钟再点一次 **Run workflow**。

如果文章清单没有变化，而且所有目标页面都成功读取，就会看到：

```text
[published] infzm-public-digest-YYYY-MM-DD.epub
```

随后 GitHub Pages 会部署新书架。

## KOReader

如果你仍然使用同一个 GitHub 仓库，Pages 地址不会改变，所以 KOReader 原来添加的：

```text
https://你的用户名.github.io/仓库名/catalog.xml
```

通常不用修改。

刷新 OPDS 后会看到：

```text
我的南方周末阅读书架
└── 南方周末公开内容摘要 · YYYY-MM-DD
```

## 自动时间

当前设置为北京时间每天：

- 07:15
- 09:15
- 12:15

GitHub 云端自动检查。第一轮发现清单，后续轮次确认稳定后发布。

## 自定义栏目

编辑 `config.json` 的 `topics` 即可。

默认栏目：

- 新闻
- 深度
- 特稿
- 观点
- 人文
- 对话

## 文件不完整时怎么办

脚本采取保守策略：

1. 任意候选文章页面请求失败 → 本轮不发布；
2. 目标日期文章数低于 `min_articles` → 不发布；
3. 本次文章 ID 清单与上一次不同 → 只保存新快照，不发布；
4. 连续两次清单相同 → 才生成 EPUB。

## 为什么 EPUB 里不是整篇会员正文？

一些南方周末文章页面会显示“登录后获取更多权限”。v1 只处理网页公开提供的标题、简短摘要和原文链接，不绕过访问控制。

后续如果你希望加入自己正常有权访问的内容，建议使用官方允许的方式或你自己的合法导出内容，再放入私人 OPDS，而不是尝试绕过限制。
