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

# 永久分享。各来源对这个常量的说法互相冲突（有说 1 有说 4），
# 所以建完必须回读 expired_at 自检，不能只信这个数字。
EXPIRE_FOREVER = 4

PROFILES = {"quark": QUARK, "uc": UC}
