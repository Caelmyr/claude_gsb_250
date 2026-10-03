"""结果缓存与结果文件管理。

难点之一「结果缓存」：同一个（图像 + 流水线）组合不重复计算。

- 缓存键 = sha256(图像内容哈希 + 流水线规范化 JSON)，命中直接复用结果文件。
- 结果图落盘到 data/results/<result_id>.png（原子写），cache.json 记录键->结果映射。
- LRU 淘汰：超过条目数或字节数上限时，按最后访问时间踢掉最久未用的结果，
  同步删除其文件，保持 JSON 与文件一致。
- 引用保护：被处理历史引用的结果是「活」结果——缓存命中会让多条历史共用同一张
  结果图。删除单条历史的联动删除、LRU 腾空间都必须先经 pinned_checker 确认该
  结果已无任何历史引用，引用计数 > 0 一律保留，避免删掉一处就弄坏别处的历史。
- 特征/检测/分割/风格等单图接口也统一走这里，天然获得缓存能力。
"""
import hashlib
import json
import os
import threading
import time
import uuid

from PIL import Image

from . import config
from .storage import JsonStore, atomic_write_bytes, now_iso
from .algorithms import util


def make_key(*parts):
    """由若干字符串片段生成确定性缓存键。"""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class ResultCache:
    def __init__(self, pinned_checker=None):
        """pinned_checker(result_id, exclude_history_id=None) -> bool：
        判断结果是否仍被历史等引用。

        返回 True 表示结果「被钉住」：删除单条历史联动、LRU 自动腾空间时
        都不能动它的文件，否则会把别处仍在查看的结果图弄丢。
        """
        self.store = JsonStore(config.CACHE_JSON, {})
        self._pinned_checker = pinned_checker
        self._local = threading.local()

    def set_pinned_checker(self, checker):
        self._pinned_checker = checker

    def _is_pinned(self, result_id):
        if not result_id or self._pinned_checker is None:
            return False
        exclude = getattr(self._local, "exclude_history_id", None)
        try:
            return bool(self._pinned_checker(result_id, exclude))
        except TypeError:
            try:
                return bool(self._pinned_checker(result_id))
            except Exception:
                # 引用关系查询失败时保守处理：当作仍被引用，绝不误删
                return True
        except Exception:
            # 引用关系查询失败时保守处理：当作仍被引用，绝不误删
            return True

    def delete_result(self, result_id, force=False):
        """按 result_id 删除结果（供历史删除联动）。

        默认先做引用检查：该结果仍被其他历史引用时跳过，保留共享结果图，
        返回 False；force=True 时才无条件删除。
        调用方应先删除历史本身，使引用计数自然反映「删完之后」的状态；
        也可通过 context 传入正在删除的历史 id（不依赖删除顺序）。
        """
        if not force and self._is_pinned(result_id):
            return False
        entry = self.get_entry(result_id)
        if not entry:
            return False

    # ------------------------------------------------------------------ 读
    def get(self, key):
        entry = self.store.read().get(key)
        if not entry:
            return None
        path = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
        if not os.path.exists(path):
            return None
        self._touch(key)
        return entry.get("result_id")

    def _touch(self, key):
        def _upd(doc):
            doc = dict(doc)
            if key in doc:
                e = dict(doc[key])
                e["last_access"] = time.time()
                doc[key] = e
            return doc
        self.store.update(_upd)

    def touch_result(self, result_id):
        """按 result_id 更新最后访问时间（历史里查看结果也算一次使用）。"""
        def _upd(doc):
            doc = dict(doc)
            for k, e in doc.items():
                if e.get("result_id") == result_id:
                    e = dict(e)
                    e["last_access"] = time.time()
                    doc[k] = e
                    break
            return doc
        self.store.update(_upd)

    def get_entry(self, result_id):
        for entry in self.store.read().values():
            if entry.get("result_id") == result_id:
                return entry
        return None

    def result_path(self, result_id):
        entry = self.get_entry(result_id)
        if not entry:
            return None
        p = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
        return p if os.path.exists(p) else None

    def result_image(self, result_id):
        p = self.result_path(result_id)
        if not p:
            return None
        try:
            return Image.open(p)
        except Exception:
            return None

    def list_results(self):
        """按创建时间倒序返回结果条目列表。"""
        entries = list(self.store.read().values())
        entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        return entries

    # ------------------------------------------------------------------ 写
    def put(self, key, image, meta=None):
        """保存结果图并登记缓存，返回 result_id。"""
        result_id = uuid.uuid4().hex
        file_name = result_id + ".png"
        dest = os.path.join(config.RESULTS_DIR, file_name)

        rgb = util.ensure_rgb(image)
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        # 原子写：先写临时文件再 rename
        tmp = dest + ".tmp"
        rgb.save(tmp, "PNG", optimize=True)
        os.replace(tmp, dest)

        entry = {
            "result_id": result_id,
            "key": key,
            "file": file_name,
            "size_bytes": os.path.getsize(dest),
            "width": rgb.size[0],
            "height": rgb.size[1],
            "meta": meta or {},
            "created_at": now_iso(),
            "last_access": time.time(),
        }

        def _upd(doc):
            doc = dict(doc)
            doc[key] = entry
            return doc

        self.store.update(_upd)
        self.evict_if_needed()
        return result_id

    # ------------------------------------------------------------------ 淘汰
    def evict_if_needed(self):
        """超限时按 LRU 腾空间，但被历史引用的结果一律保留。

        引用计数 > 0 的结果是「活的」结果图（历史详情还要打开它），
        即使很久没访问也不能删；只在未被引用的结果里按最久未用顺序淘汰。
        返回实际淘汰条目数。
        """
        entries = self.store.read()
        if not entries:
            return 0
        total_bytes = sum(e.get("size_bytes", 0) for e in entries.values())
        count = len(entries)
        if count <= config.CACHE_MAX_ENTRIES and total_bytes <= config.CACHE_MAX_BYTES:
            return 0

        # 只从未被引用的结果中挑选淘汰对象，按最后访问时间升序
        candidates = [
            (k, e) for k, e in entries.items()
            if not self._is_pinned(e.get("result_id"))
        ]
        candidates.sort(key=lambda kv: kv[1].get("last_access", 0))
        removed = 0
        for key, entry in candidates:
            if count <= config.CACHE_MAX_ENTRIES and total_bytes <= config.CACHE_MAX_BYTES:
                break
            path = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
            entries.pop(key, None)
            total_bytes -= entry.get("size_bytes", 0)
            count -= 1
            removed += 1

        if removed:
            self.store.write(entries)
        return removed

    def delete_result(self, result_id, force=False, exclude_history_id=None):
        """按 result_id 删除结果（供历史删除联动）。

        默认先做引用检查：该结果仍被其他历史引用时跳过，保留共享结果图，
        返回 False；force=True 时才无条件删除。
        exclude_history_id 为正在删除的历史 id，计数时排除它，
        使保护不依赖「先删历史还是先删结果」的调用顺序。
        """
        self._local.exclude_history_id = exclude_history_id
        try:
            pinned = self._is_pinned(result_id)
        finally:
            self._local.exclude_history_id = None
        if not force and pinned:
            return False
        entry = self.get_entry(result_id)
        if not entry:
            return False
        path = os.path.join(config.RESULTS_DIR, entry.get("file", ""))
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass

        def _upd(doc):
            doc = dict(doc)
            for k, e in list(doc.items()):
                if e.get("result_id") == result_id:
                    doc.pop(k)
            return doc
        self.store.update(_upd)
        return True
