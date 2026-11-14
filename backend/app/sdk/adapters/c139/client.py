"""中国移动云盘（139）客户端。

这是五个盘里唯一**请求体要加密**的：分享侧接口全部走 AES-128-CBC，
密钥硬编码在客户端里（`PVGDmv...`），IV 随机生成后拼在密文前面，
响应也是密文、还可能 gzip。明文发过去只会得到一句 body 解析失败。

两套通道：
- 分享侧（share-kd-njs.yun.139.com）：AES 加密，匿名可用，带 x-deviceinfo 头
- 个人侧（personal-kd-njs.yun.139.com）：明文 JSON + Authorization: Basic <base64>

登录态从 cookie 里来：`authorization` 字段本身是 base64，解出来是
`pc:<手机号>:<...>`，手机号既当 msisdn 又当 account。
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ... import cookie as cookie_util
from ...base import PanClient
from ...errors import (
    AuthError,
    ConfigError,
    NetdiskError,
    NotFoundError,
    ShareInvalidError,
    TaskTimeoutError,
    UpstreamError,
    ViolationError,
)
from ...http import Requester
from ...registry import register
from ...types import Account, File, ShareInfo, ShareLink, TaskResult

log = logging.getLogger("pantrace.sdk.c139")

SHARE_BASE = "https://share-kd-njs.yun.139.com"
CLOUD_BASE = "https://personal-kd-njs.yun.139.com"
SHARE_LIST_URL = f"{SHARE_BASE}/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6"
TRANSFER_CREATE_URL = f"{SHARE_BASE}/yun-share/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTask"
TRANSFER_QUERY_URL = f"{SHARE_BASE}/yun-share/richlifeApp/devapp/IBatchOprTask/queryBatchOprTaskDetail"
CREATE_SHARE_URL = "https://yun.139.com/orchestration/personalCloud-rebuild/outlink/v1.0/getOutLink"
FILE_LIST_URL = f"{CLOUD_BASE}/hcy/file/list"
BATCH_TRASH_URL = f"{CLOUD_BASE}/hcy/recyclebin/batchTrash"

AES_KEY = b"PVGDwmcvfs1uV3d1"
SHARE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
)
PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.6839.99 Safari/537.36"
)
SHARE_DEVICEINFO = "||3|12.27.0|||||chrome 150.0.0.0|360X444|zh-cn|||"
CLIENT_INFO = (
    "||9|7.18.0|chrome|132.0.0.0|084a669f0b1b0fc62fef1aca1147a412||"
    "windows 10||zh-CN|||dW5kZWZpbmVk||"
)


def aes_encrypt(plaintext: str) -> str:
    raw = plaintext.encode("utf-8")
    pad = 16 - len(raw) % 16
    padded = raw + bytes([pad]) * pad
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()


def aes_decrypt(text: str) -> str:
    raw = base64.b64decode(text)
    iv, payload = raw[:16], raw[16:]
    decryptor = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).decryptor()
    data = decryptor.update(payload) + decryptor.finalize()
    if data:
        pad = data[-1]
        if 1 <= pad <= 16:
            data = data[:-pad]
    if len(data) > 2 and data[0] == 0x1F and data[1] == 0x8B:
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


@register
class C139Client(PanClient):
    type = "c139"
    display = "中国移动云盘"

    def __init__(self, account: Account, *, timeout: float = 15.0, on_cookie=None):
        self._authorization = ""
        self._msisdn = ""
        super().__init__(account, timeout=timeout, on_cookie=on_cookie)

    def _build_requester(self) -> Requester:
        cookies = self.account.cookie or ""
        self._authorization = cookie_util.parse(cookies).get("authorization", "")
        if not self._authorization:
            raise ConfigError("139 账号 cookie 里缺 authorization 字段")
        self._msisdn = _account_of(self._authorization)
        return Requester(
            SHARE_BASE,
            headers={"Accept": "application/json, text/plain, */*", "Origin": "https://yun.139.com", "Referer": "https://yun.139.com/"},
            timeout=self.timeout,
        )

    # ---- 两套请求通道 ----

    def _share_post(self, url: str, payload: dict, action: str) -> dict:
        body = aes_encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        raw = self.http.post(
            url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": SHARE_UA,
                "hcy-cool-flag": "1",
                "x-deviceinfo": SHARE_DEVICEINFO,
                "x-yun-api-version": "v1",
                "x-yun-app-channel": "10213406",
                "x-yun-channel-source": "10213406",
                "x-yun-client-info": CLIENT_INFO,
                "x-yun-module-type": "100",
                "x-yun-svc-type": "1",
                **({"Authorization": self._authorization} if self._authorization else {}),
            },
            action=action,
        )
        return _unwrap(raw, action)

    def _cloud_post(self, url: str, payload: dict, action: str) -> dict:
        raw = self.http.post(
            url,
            json_body=payload,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": PC_UA,
                "x-svctype": "1",
                "x-yun-api-version": "v1",
                "x-yun-app-channel": "10000034",
                "x-yun-channel-source": "10000034",
                "x-yun-client-info": CLIENT_INFO,
                "x-yun-module-type": "100",
                "x-yun-svc-type": "1",
                "Authorization": self._authorization,
            },
            action=action,
        )
        return _check(raw, action)

    # ---- 分享侧 ----

    def share_info(self, url: str, pwd: str = "") -> ShareInfo:
        from ...urls import parse

        parsed = parse(url)
        if not parsed.ok:
            raise ShareInvalidError("链接不是 139 分享")
        link_id = parsed.pwd_id
        passcode = pwd or parsed.passcode

        resp = self._share_post(
            SHARE_LIST_URL,
            {
                "getOutLinkInfoReq": {
                    "account": self._msisdn,
                    "linkID": link_id,
                    "passwd": passcode,
                    "caSrt": 0,
                    "coSrt": 0,
                    "srtDr": 1,
                    "bNum": 1,
                    "pCaID": "root",
                    "eNum": 200,
                },
                "commonAccountInfo": {"account": self._msisdn, "accountType": 1},
            },
            "读取 139 分享",
        )
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        catalogs = data.get("caLst") or []
        contents = data.get("coLst") or []
        if not catalogs and not contents:
            raise ShareInvalidError("分享里没有文件", raw=resp)

        files: list[File] = []
        total = 0
        for item in catalogs:
            files.append(
                File(
                    fid=str(item.get("caId") or item.get("catalogID") or ""),
                    name=str(item.get("caName") or item.get("name") or ""),
                    is_dir=True,
                )
            )
        for item in contents:
            size = int(item.get("coSize") or 0)
            total += size
            files.append(
                File(
                    fid=str(item.get("coId") or item.get("contentID") or ""),
                    name=str(item.get("coName") or item.get("name") or ""),
                    size=size,
                    is_dir=bool(item.get("isdir")),
                )
            )
        files = [f for f in files if f.fid]
        if not files:
            raise ShareInvalidError("分享里没有文件", raw=resp)
        return ShareInfo(
            platform="c139",
            pwd_id=link_id,
            title=str(data.get("linkName") or data.get("caName") or files[0].name)[:60],
            passcode=passcode,
            files=files,
            total_size=total,
        )

    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        if not to_dir:
            raise ConfigError("139 必须配置临时目录（账号 temp_dir）")
        co_ids = [f.fid for f in info.files if not f.is_dir]
        ca_ids = [f.fid for f in info.files if f.is_dir]
        resp = self._share_post(
            TRANSFER_CREATE_URL,
            {
                "createOuterLinkBatchOprTaskReq": {
                    "msisdn": self._msisdn,
                    "ownerAccount": "",
                    "taskType": 1,
                    "taskInfo": {
                        "contentInfoList": co_ids,
                        "catalogInfoList": ca_ids,
                        "newCatalogID": to_dir,
                        "linkID": info.pwd_id,
                        "newCatalogName": "",
                        "needPassword": True,
                    },
                    "linkID": info.pwd_id,
                    "needPassword": True,
                },
                "commonAccountInfo": {"account": self._msisdn, "accountType": 1},
            },
            "转存",
        )
        task_id = str((resp.get("data") or {}).get("taskID") or "")
        if not task_id:
            raise UpstreamError("139 转存没有返回 taskID", raw=resp)
        return self.wait_task(task_id, timeout=max(45.0, self.timeout * 3)).fids

    def task_status(self, task_id: str) -> TaskResult:
        resp = self._share_post(
            TRANSFER_QUERY_URL,
            {
                "queryBatchOprTaskDetailReq": {
                    "taskID": task_id,
                    "msisdn": self._msisdn,
                    "commonAccountInfo": {"account": self._msisdn, "accountType": 1},
                }
            },
            "查询转存任务",
        )
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        task = data.get("batchOprTask") or {}
        done = int(task.get("progress") or 0) >= 100 and int(task.get("taskStatus") or 0) == 2
        fids: list[str] = []
        for key in ("contentList", "catalogList"):
            for item in ((data.get(key) or {}).get("idRspInfo") or []):
                rid = str(item.get("rstId") or "")
                if rid:
                    fids.append(rid)
        return TaskResult(task_id=task_id, done=done, fids=fids, extra={"raw": resp})

    def create_share(self, fids: list[str], title: str, expire_hours: int = 0) -> ShareLink:
        period = max(1, int(expire_hours // 24) or 1)
        resp = self._cloud_post(
            CREATE_SHARE_URL,
            {
                "getOutLinkReq": {
                    "subLinkType": 0,
                    "encrypt": 0,
                    "coIDLst": fids,
                    "caIDLst": [],
                    "pubType": 1,
                    "dedicatedName": (title or "资源分享")[:60],
                    "period": period,
                    "periodUnit": 1,
                    "viewerLst": [],
                    "extInfo": {"isWatermark": 0, "shareChannel": "3001"},
                    "commonAccountInfo": {"account": self._msisdn, "accountType": 1},
                }
            },
            "创建分享",
        )
        data = (resp.get("data") or {}).get("getOutLinkRes") or {}
        items = data.get("getOutLinkResSet") or []
        if not items:
            raise UpstreamError("139 创建分享没有返回链接", raw=resp)
        item = items[0]
        url = str(item.get("linkUrl") or "")
        if not url:
            raise UpstreamError("139 创建分享链接为空", raw=resp)
        return ShareLink(url=url, passcode=str(item.get("passwd") or ""), share_id=str(item.get("linkID") or ""))

    # ---- 文件侧 ----

    def list_files(self, parent_id: str = "", page: int = 1, size: int = 100) -> list[File]:
        resp = self._cloud_post(
            FILE_LIST_URL,
            {
                "pageInfo": {"pageSize": min(int(size), 200), "pageCursor": None},
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "parentFileId": parent_id or "/",
                "imageThumbnailStyleList": ["Small", "Large"],
            },
            "列目录",
        )
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        out: list[File] = []
        for item in data.get("items") or []:
            fid = str(item.get("coID") or item.get("caID") or item.get("id") or "")
            if not fid:
                continue
            out.append(
                File(
                    fid=fid,
                    name=str(item.get("name") or item.get("coName") or ""),
                    size=int(item.get("size") or item.get("coSize") or 0),
                    is_dir=str(item.get("type")) in ("folder", "dir") or bool(item.get("isdir")),
                )
            )
        return out

    def delete(self, fids: list[str]) -> None:
        if not fids:
            return
        self._cloud_post(BATCH_TRASH_URL, {"fileIds": fids}, "删除文件")

    def probe(self) -> bool:
        try:
            self.list_files("", 1, 1)
            return True
        except NetdiskError:
            return False


def _account_of(authorization: str) -> str:
    """authorization 是 base64，解开是 'pc:<手机号>:...'。取中间那段当账号。"""
    text = authorization.strip()
    if text.lower().startswith("basic"):
        text = text[5:].strip()
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    parts = decoded.split(":")
    return parts[1] if len(parts) > 1 else ""


def _unwrap(raw: Any, action: str) -> dict:
    """分享侧响应是 base64 密文；拿不到明文时按明文兜底。"""
    if isinstance(raw, dict) and "_text" in raw:
        text = str(raw.get("_text") or "").strip()
        if not text:
            raise UpstreamError(f"{action}失败：空响应")
        try:
            return _check(json.loads(aes_decrypt(text)), action)
        except (ValueError, UnicodeDecodeError):
            try:
                return _check(json.loads(text), action)
            except ValueError as exc:
                raise UpstreamError(f"{action}失败：响应无法解析") from exc
    return _check(raw, action)


def _check(payload: Any, action: str) -> dict:
    if not isinstance(payload, dict):
        raise UpstreamError(f"{action}失败：响应不是对象")
    if payload.get("success") is False:
        message = str(payload.get("message") or payload.get("msg") or action)
        raise ShareInvalidError(f"{action}失败：{message}", raw=payload)
    code = payload.get("resultCode", payload.get("code"))
    if code in (None, "", 0, "0", "0000"):
        return payload
    message = str(payload.get("message") or payload.get("msg") or f"code={code}")
    raise UpstreamError(f"{action}失败：{message}", code=code, raw=payload)
