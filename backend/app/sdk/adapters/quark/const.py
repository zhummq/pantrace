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
# 我们**不建永久分享**。三条理由：
#   - 给用户的链接只活 30 分钟（到点删源文件），1 天已经绰绰有余；
#   - 回收任务万一没跑（进程挂了、升级忘了起），分享最多 1 天后自己失效，
#     不会把受限资源永久挂在我们账号上——永久分享是安全事故不是保险；
#   - 竞品也是 1 天。
#
# 2026-11 的事故仍要记住：这里最初填的是 4，本意"永久"，实际是 30 天，
# 部分客户端直接显示"今天"，分享会静默到期。别再凭印象改——改完跑一次
# 后台「手工验一条链接」，看回读的 expired_at 是不是 1 天后。
#
# 档位按"请求的小时数"就近取：(小时, expired_type)。默认 24 小时 → 1 天，
# 和以前完全一致；配置调大才会退到 7 天 / 30 天，且永远从最接近的那档开始试。
EXPIRE_TIERS = ((24, 2), (168, 3), (720, 4))
EXPIRE_ONE_DAY = EXPIRE_TIERS[0][1]

# 兼容旧引用：按 1 天算的完整候选链（2 → 7 天 → 30 天）。
# 不给永久留位置。
EXPIRE_CANDIDATES = tuple(code for _, code in EXPIRE_TIERS)

PROFILES = {"quark": QUARK, "uc": UC}
