"""夸克 / UC 网盘常量。

两家共用一套接口，只差 pr、api 基址、referer、UA。UC 走同一份代码，
所以 UID 前缀、字段名都不用分支——这一点竞品也是这么做的。
"""

from __future__ import annotations

QUARK = {
    "type": "quark",
    "display": "夸克网盘",
    "pr": "ucpro",
    "api": "https://drive-pc.quark.cn/1/clouddrive",
    "referer": "https://pan.quark.cn",
    "ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
        "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
    ),
    "share_host": "https://pan.quark.cn/s/",
}

UC = {
    "type": "uc",
    "display": "UC网盘",
    "pr": "UCBrowser",
    "api": "https://pc-api.uc.cn/1/clouddrive",
    "referer": "https://drive.uc.cn",
    "ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
        "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
    ),
    "share_host": "https://drive.uc.cn/s/",
}

# 分享有效期。取值没有官方文档，社区多处实现（QuarkPanTool、各类转存脚本）
# 的实测结论一致：1=永久、2=1天、3=7天、4=30天。
#
# 我们用 **1 天（2）**，不建永久分享。三条理由：
#   - 给用户的链接只活 30 分钟（到点删源文件），1 天已经绰绰有余；
#   - 回收任务万一没跑（进程挂了、升级忘了起），分享最多 1 天后自己失效，
#     不会把受限资源永久挂在我们账号上——永久分享是安全事故不是保险；
#   - 竞品也是 1 天。
#
# 2026-11 的事故仍要记住：这里最初填的是 4，本意"永久"，实际是 30 天，
# 部分客户端直接显示"今天"，分享会静默到期。别再凭印象改——改完跑一次
# 后台「手工验一条链接」，看回读的 expired_at 是不是 1 天后。
EXPIRE_ONE_DAY = 2

# 兜底候选：2 万一在改版后失效，退到 7 天 / 30 天（更长的有效期只影响
# 漏删时的暴露窗口，不影响正常的 30 分钟窗口）。不给永久留位置。
# 建完都会回读 expired_at 验证，见 client.create_share。
EXPIRE_CANDIDATES = (EXPIRE_ONE_DAY, 3, 4)

PROFILES = {"quark": QUARK, "uc": UC}
