"""转存前的广告预过滤（纯函数，零网络请求）。

两条硬约束决定了这里的形状，改判据之前先把它们读一遍：

1. **宁可漏删，不可误杀。** 判为广告必须同时满足三个条件——名字命中关键词、
   是附件型后缀、体积小于阈值。少一条就放过。目录永远不判：目录里可能装着
   正片，判错的代价是用户拿到残缺资源，而且他不会回来投诉，只会觉得资源坏了。
   体积读不到（0）也放过。
2. **不占用户从点击到拿到链接的时间。** 这里全是内存计算。作为对照：原来的
   做法是转存完再列目录、再删文件，最坏 24 个请求 / 8 秒，全压在点击链路上。

命中关键词但是视频、压缩包、文档的，一律不判广告——"某某电影[更多资源见
xxx].mp4" 这种是常态，删了就是删正片。这类只在日志里记一笔（见 base.mirror）。
"""

from __future__ import annotations

from .types import File

#: 文本/快捷方式类：只要命中关键词就判（这类文件没有"正片"属性）
#: .md 是 2026-11 加进来的：源分享里出现「加V群.md」「领取方式.md」这类引流
#: 说明，和 .txt 同类——正文就是广告本身，不存在"正文是正片"的情况。
TEXT_SUFFIXES = frozenset({".txt", ".url", ".html", ".htm", ".ini", ".md"})

#: 图片类单独一档，判据更严（见 `_image_is_ad`）。为什么不干脆排除图片：
#: "更多资源.jpg""扫码关注.png" 是最常见的引流载体，全放过等于不做。
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"})

#: 会参与判定的后缀（其余一律不动）。刻意不含 .doc/.docx/.pdf/.zip/.mp4：
#: 学习资料站的正片经常就是一叠 docx 和 pdf，名字里带"公众号"也不稀奇。
ATTACHMENT_SUFFIXES = TEXT_SUFFIXES | IMAGE_SUFFIXES

#: 默认体积上限。正片最低也是几十 MB，2MB 足够把 txt/jpg 那类挡在外面；
#: 同时又不会把"图集"类资源里的大图判成广告——它们还要过关键词那关。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def suffix_of(name: str) -> str:
    """取小写后缀（含点）。没有后缀返回空串。"""
    text = (name or "").strip()
    dot = text.rfind(".")
    # 点在开头（.gitignore 这种）或没有点，都算没有后缀
    if dot <= 0 or dot == len(text) - 1:
        return ""
    return text[dot:].lower()


def stem_of(name: str) -> str:
    """取不含后缀的主干（小写，去首尾空格）。"""
    text = (name or "").strip()
    dot = text.rfind(".")
    if dot <= 0:
        return text.lower()
    return text[:dot].strip().lower()


def hit_keyword(name: str, keywords) -> bool:
    """名字是否命中任一关键词。大小写不敏感，只匹配名字不匹配路径。

    不匹配路径是有意的：路径里带上层目录名，"资源/微信"这种会被误杀。
    """
    low = (name or "").lower()
    if not low:
        return False
    return any(k and k in low for k in keywords)


def is_ad(file: File, keywords, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """这个条目能不能判定为广告。"""

    def image_is_ad(name: str) -> bool:
        """图片比文本更严：**主干要正好等于某个拦截词**。

        为什么单独加这一道：微信保存下来的图片默认叫 `微信图片_20240101.jpg`，
        它命中"微信"这两个字。图集资源里全是这种名字——只用"包含"判断的话，
        一次能把用户的截图全删了。而真正的广告图，名字通常就是一句广告语
        （"更多资源.jpg""扫码关注.png"），主干和关键词能对上。

        代价是漏掉"加群领福利.png"这类（主干不是任何一个词），认了：
        少删一张是脏，错删一张是残。
        """
        stem = stem_of(name)
        return any(stem == k for k in keywords if k)

    if file.is_dir:
        return False
    if not file.name:
        return False
    # 体积未知就不判——读不到大小说明信息不全，宁可放过
    if int(file.size or 0) <= 0:
        return False
    if max_bytes > 0 and int(file.size) > max_bytes:
        return False
    suffix = suffix_of(file.name)
    if suffix not in ATTACHMENT_SUFFIXES:
        return False
    if not hit_keyword(file.name, keywords):
        return False
    if suffix in IMAGE_SUFFIXES:
        return image_is_ad(file.name)
    return True


def split(
    files: list[File],
    keywords,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[list[File], list[File]]:
    """把顶层文件分成 (保留, 判为广告) 两堆。顺序不变。"""
    kept: list[File] = []
    dropped: list[File] = []
    for file in files:
        (dropped if is_ad(file, keywords, max_bytes) else kept).append(file)
    return kept, dropped


def explain(
    name: str,
    size: int,
    keywords,
    max_bytes: int = DEFAULT_MAX_BYTES,
    *,
    is_dir: bool = False,
) -> dict:
    """把判定过程摊开给后台看：这个文件名会不会被拦，卡在哪一步。

    后台配关键词最怕的是"配完不知道会不会误杀"，只能等线上出事。这个函数
    和 `is_ad` 走同一套判断，运营在管理端拿真实文件名试一下就知道结果。
    """
    file = File(fid="probe", name=name or "", size=int(size or 0), is_dir=is_dir)

    if is_dir:
        reason = "目录不判断——目录里可能装着正片"
    elif not file.name:
        reason = "没有文件名"
    elif int(file.size or 0) <= 0:
        reason = "读不到体积，放过（宁可漏删）"
    elif max_bytes > 0 and int(file.size) > max_bytes:
        reason = (
            f"{file.human_size} 超过体积上限 {max_bytes / 1024 / 1024:.0f}MB，放过"
        )
    elif suffix_of(file.name) not in ATTACHMENT_SUFFIXES:
        suffix = suffix_of(file.name) or "无后缀"
        reason = f"{suffix} 不是附件型后缀，正片类文件一律不动"
    elif not hit_keyword(file.name, keywords):
        reason = "名字里没有命中任何拦截词"
    elif suffix_of(file.name) in IMAGE_SUFFIXES and not any(
        stem_of(file.name) == k for k in keywords if k
    ):
        reason = (
            f"图片类要求主干正好等于拦截词；主干是「{stem_of(file.name)}」，"
            "放过（微信默认图名这类不能误伤）"
        )
    else:
        word = next((k for k in keywords if k and k in file.name.lower()), "")
        # 不再带"会被拦下"前缀：面板上已经有一枚 verdict，重复读着别扭
        reason = f"命中拦截词「{word}」"

    return {
        "hit": is_ad(file, keywords, max_bytes),
        "reason": reason,
        "suffix": suffix_of(file.name),
        "attachment": suffix_of(file.name) in ATTACHMENT_SUFFIXES,
        "keyword_hit": hit_keyword(file.name, keywords),
    }
